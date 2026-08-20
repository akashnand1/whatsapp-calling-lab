"""Streaming speech-to-text via nvidia/nemotron-3.5-asr-streaming-0.6b.

Why this exists
---------------
The first production-shaped call spent 76% of its latency in speech recognition:
~5 500 ms per utterance, warm, on 4 CPU cores. The cause was architectural, not
model size. Whisper is an offline encoder-decoder: we wait for end-of-speech and
*then* transcribe the whole buffer, so nothing at all happens while the driver is
talking, and the entire cost lands after they stop.

A cache-aware streaming model inverts that. It consumes fixed audio chunks and
carries encoder state forward between them, so by the time the driver stops
speaking almost all the work is already done and only the final chunk remains.
The published WER is also far better in the languages we need -- 6.81% on Hindi
(FLEURS) against roughly 30-40% for Whisper `small` on accented phone audio.

Verified facts (19 Aug 2026), not assumed:
  * The streaming API is `asr_model.conformer_stream_step(...)`, threading
    `cache_last_channel`, `cache_last_time`, `cache_last_channel_len` and
    `previous_hypotheses` from one call to the next. Taken from NeMo's own
    examples/asr/asr_cache_aware_streaming reference script.
  * Language is selected with `set_inference_prompt(<key>)`, where the key comes
    from the model's `prompt_dictionary` (e.g. "en-US", or "auto").
  * Cache-aware models are **float32 only**. NeMo raises NotImplementedError for
    any other compute dtype, so there is no fp16 speed-up to be had.

Honest caveats, because they affect whether this is worth deploying:
  * `nemo_toolkit[asr]` is a heavy dependency (torch, lightning, hydra) --
    gigabytes, against faster-whisper's hundreds of megabytes.
  * float32 on CPU means ~2.5 GB resident for a 0.6B model. On an 8 GB Codespace
    that coexists with Whisper only if Whisper is not also loaded.
  * Licence is OpenMDW-1.1, which is permissive but is NOT Apache/MIT. Have it
    confirmed before production.
  * The measured RTF on 4 shared vCPUs is unknown. `cli.py test-ai` will tell
    you. If it is worse than Whisper, say so and stop -- do not assume.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

import numpy as np

from .config import get_settings
from .providers import STT_RATE_LOCAL, STTProvider
from .vad import SpeechGate, looks_degenerate

log = logging.getLogger("providers")

# Loaded models are process-wide. Loading is slow and the weights are large;
# a per-session load previously caused 62-second transcriptions when several
# sessions each held their own copy and the box started swapping.
_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = asyncio.Lock()


def _pick_prompt_key(model, want: str) -> str:
    """Choose a language prompt key from the model's OWN dictionary.

    Hardcoding "hi-IN" would be a guess. The key set is a property of the
    checkpoint, so ask the checkpoint: prefer an exact language-region match,
    fall back to any key starting with the language code, and finally to "auto"
    (which makes the model detect the language itself -- measurably worse than
    telling it, per NVIDIA's own LangID-vs-auto comparison, but never wrong).
    """
    keys: list[str] = []
    try:
        pd = getattr(model, "prompt_dictionary", None)
        if pd:
            keys = list(pd.keys() if hasattr(pd, "keys") else pd)
    except Exception:
        keys = []

    if not keys:
        log.warning("nemotron: no prompt_dictionary found; using 'auto' language detection")
        return "auto"

    want = want.lower()
    for k in keys:                                  # exact language-region, e.g. hi-IN
        if k.lower().startswith(f"{want}-"):
            return k
    for k in keys:                                  # bare code, e.g. hi
        if k.lower() == want:
            return k
    log.warning(
        "nemotron: language %r is not in this checkpoint's prompt keys (%s...); "
        "falling back to 'auto'. Accuracy will be lower than an explicit language.",
        want, ", ".join(sorted(keys)[:8]),
    )
    return "auto"


class NemotronStreamingSTT(STTProvider):
    """Cache-aware streaming ASR.

    Uses the same `SpeechGate` as the Whisper path for turn detection. That is
    deliberate: the gate carries the echo suppression, the adaptive noise floor
    and the playback-progress logic that took seven speakerphone scenarios to get
    right. Swapping the recogniser should not throw that away.
    """

    def __init__(self) -> None:
        s = get_settings()
        from .languages import spec
        self._spec = spec(s.agent_language)
        self._decode_lang = self._spec.decode_lang if self._spec else "en"
        self._model_name = s.nemotron_model
        self._model = None
        self._prompt_key = "auto"

        self._gate = SpeechGate()
        self._out: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._buf = bytearray()
        self._speaking = False
        self._utterance = 0
        self._task: asyncio.Task | None = None

        # Streaming state, threaded through conformer_stream_step.
        self._cache = None
        self._prev_hyp = None
        self._prev_pred = None
        self._step = 0

    # Rough resident cost measured on an 8 GB Codespace: NeMo + torch import is
    # ~1.5-2 GB, the 0.6B model in float32 is ~2.4 GB, and the .nemo archive is
    # extracted to disk first (2.37 GB). With Piper's onnxruntime and the VS Code
    # server already resident, 8 GB is not enough -- the kernel OOM-killer ends
    # the process with a bare "Terminated" and no traceback, which is the most
    # confusing failure in this whole stack. So check first and say so.
    _NEEDED_MB = 5000

    @staticmethod
    def _available_mb() -> int | None:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
        return None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        avail = self._available_mb()
        if avail is not None and avail < self._NEEDED_MB:
            raise MemoryError(
                f"nemotron needs roughly {self._NEEDED_MB} MB but only {avail} MB "
                f"is available. Loading it would get this process OOM-killed with "
                f"a bare 'Terminated' and no explanation.\n"
                f"Options, cheapest first:\n"
                f"  1. STT_ENGINE=whisper in .env — works now, ~5s slower per turn\n"
                f"  2. Rebuild the Codespace on a 4-core/16GB machine (burns free\n"
                f"     core-hours twice as fast, but fits comfortably)\n"
                f"  3. Stop other processes: the VS Code server and a running\n"
                f"     uvicorn together hold well over a gigabyte"
            )
        if avail is not None:
            log.info("nemotron: %d MB available, need ~%d MB", avail, self._NEEDED_MB)

        async with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(self._model_name)
            if cached is None:
                t0 = time.monotonic()
                cached = await asyncio.to_thread(self._load)
                _MODEL_CACHE[self._model_name] = cached
                log.info(
                    "nemotron %s loaded in %.1fs (float32 — cache-aware models "
                    "do not support fp16)", self._model_name, time.monotonic() - t0,
                )
            self._model = cached

        self._prompt_key = _pick_prompt_key(self._model, self._decode_lang)
        if hasattr(self._model, "set_inference_prompt"):
            await asyncio.to_thread(self._model.set_inference_prompt, self._prompt_key)
            try:
                # Strip the "<hi-IN>" tag the model prepends; the LLM should not
                # see it, and it pollutes the echo-similarity check.
                self._model.decoding.set_strip_lang_tags(True)
            except Exception:
                pass

        spoken = self._spec.code if self._spec else "?"
        log.info(
            "STT: nemotron streaming, speaking=%s decoding=%s prompt=%s",
            spoken, self._decode_lang, self._prompt_key,
        )
        if self._spec and self._spec.stt_lang:
            log.info(
                "nemotron: %s audio is decoded as %s — transcripts arrive in the "
                "%s script, which is fine because only the LLM reads them",
                spoken, self._decode_lang, self._decode_lang,
            )
        self._reset_stream()

    def _load(self):
        import torch
        from nemo.collections.asr.models import ASRModel

        model = ASRModel.from_pretrained(self._model_name, map_location="cpu")
        model.eval()
        # float32 is not a choice: NeMo raises NotImplementedError for any other
        # compute dtype on cache-aware models.
        return model.to(dtype=torch.float32)

    def _reset_stream(self) -> None:
        if self._model is None:
            return
        self._cache = self._model.encoder.get_initial_cache_state(batch_size=1)
        self._prev_hyp = None
        self._prev_pred = None
        self._step = 0

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        # The model stays in _MODEL_CACHE on purpose -- reloading it per call is
        # what made Whisper take 62 seconds.

    # -- audio in ----------------------------------------------------------
    async def send_audio(self, pcm16: bytes) -> None:
        speaking = self._gate.update(
            pcm16,
            agent_speaking=self.agent_speaking,
            playback_frames=getattr(self, "playback_frames", None),
        )
        self.caller_speaking = speaking

        if speaking:
            if not self._speaking:
                self._speaking = True
                self._buf.clear()
                self._reset_stream()
                await self._out.put(("speech_started", ""))
            self._buf.extend(pcm16)
            # Feed the recogniser WHILE they talk. This is the whole point: by
            # end-of-speech nearly all the compute is already spent.
            await self._maybe_step()
        elif self._speaking:
            self._speaking = False
            self._utterance += 1
            await self._flush(self._utterance)

    async def _maybe_step(self) -> None:
        """Run one streaming step once a full chunk has accumulated."""
        need = int(STT_RATE_LOCAL * get_settings().nemotron_chunk_s) * 2  # int16
        if len(self._buf) < need:
            return
        chunk = bytes(self._buf[:need])
        del self._buf[:need]
        text = await asyncio.to_thread(self._step_sync, chunk, False)
        if text:
            await self._out.put(("partial", text))

    async def _flush(self, seq: int) -> None:
        t0 = time.monotonic()
        tail = bytes(self._buf)
        self._buf.clear()
        text = await asyncio.to_thread(self._step_sync, tail, True) if tail else ""
        took = (time.monotonic() - t0) * 1000

        if not text:
            log.info("utterance #%d: nothing recognised", seq)
            return
        if looks_degenerate(text):
            log.info("utterance #%d discarded: degenerate output %r", seq, text[:60])
            return

        # RNNT gives no per-utterance confidence the way Whisper's avg_logprob
        # does, so we cannot honestly claim to know. False means "no reason to
        # doubt it", which is the truthful default -- the prompt's other three
        # confirmation triggers still apply.
        self.last_unclear = False
        log.info("utterance #%d: final chunk decoded in %.0fms — %r", seq, took, text[:80])
        await self._out.put(("final", text))
        await self._out.put(("utterance_end", ""))

    def _step_sync(self, pcm: bytes, last: bool) -> str:
        """One conformer_stream_step. Runs in a thread: it is a torch forward
        pass, and on the event loop it stalls the WebRTC media pump, which
        corrupts the very audio we are trying to transcribe."""
        import torch

        if self._model is None or not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        sig = torch.from_numpy(audio).unsqueeze(0)
        sig_len = torch.tensor([sig.shape[1]], dtype=torch.long)

        cache_last_channel, cache_last_time, cache_last_channel_len = self._cache
        try:
            with torch.inference_mode():
                (
                    self._prev_pred,
                    texts,
                    cache_last_channel,
                    cache_last_time,
                    cache_last_channel_len,
                    self._prev_hyp,
                ) = self._model.conformer_stream_step(
                    processed_signal=sig,
                    processed_signal_length=sig_len,
                    cache_last_channel=cache_last_channel,
                    cache_last_time=cache_last_time,
                    cache_last_channel_len=cache_last_channel_len,
                    keep_all_outputs=last,
                    previous_hypotheses=self._prev_hyp,
                    previous_pred_out=self._prev_pred,
                    drop_extra_pre_encoded=(
                        0 if self._step == 0
                        else self._model.encoder.streaming_cfg.drop_extra_pre_encoded
                    ),
                    return_transcription=True,
                )
        except Exception:
            log.exception("nemotron stream step failed")
            return ""

        self._cache = (cache_last_channel, cache_last_time, cache_last_channel_len)
        self._step += 1

        if not texts:
            return ""
        first = texts[0]
        return (getattr(first, "text", None) or str(first)).strip()

    # -- events out --------------------------------------------------------
    async def events(self) -> AsyncIterator[tuple[str, str]]:
        while True:
            yield await self._out.get()
