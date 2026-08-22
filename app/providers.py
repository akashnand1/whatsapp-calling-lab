"""Swappable STT / LLM / TTS providers.

Every provider here implements the same small interface, so you can move any
stage between a cloud API and your own hardware without touching the pipeline.

    STT_PROVIDER = deepgram | whisper_local
    LLM_PROVIDER = anthropic | bedrock | openai_compatible
    TTS_PROVIDER = elevenlabs | piper_local

Why this matters: with the cloud options, raw caller audio and full transcripts
leave your infrastructure. The `*_local` and `openai_compatible` options keep
everything on hardware you control. See DATA-RESIDENCY.md for exactly what
crosses which boundary.

Latency reality check. Self-hosting costs you time-to-first-word:

    Deepgram + Claude + ElevenLabs     ~700-900 ms   (target)
    Whisper + local LLM + Piper        ~1.2-2.5 s    (noticeably laggy)

That gap is mostly STT endpointing. Deepgram decides "the human stopped talking"
far better than a local VAD does. Budget for it, or put a GPU behind Whisper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx
import numpy as np

from .config import get_settings


def _s():
    """Settings, which DO include .env values (os.getenv does not)."""
    return get_settings()

log = logging.getLogger("providers")

# Clause boundaries. Devanagari ends sentences with U+0964 (danda) rather than a
# full stop, so without it a Hindi reply never chunks and the agent waits for the
# whole response before speaking a word.
STT_RATE_LOCAL = 16000

# One Whisper model per (size, device, compute), shared across every call.
#
# Each connection used to load its own. Reopening /selftest a few times left
# several ~500 MB models resident, each spawning its own CTranslate2 thread pool.
# The machine went to swap and inference collapsed to ~60x slower than real time
# -- 62 seconds to transcribe 1.1 seconds of audio. The model is stateless across
# calls to transcribe(), so sharing one is safe and the load cost is paid once.
_WHISPER_CACHE: dict[tuple[str, str, str], object] = {}
_WHISPER_CACHE_LOCK = asyncio.Lock()

_CLAUSE_END = re.compile(r"[.!?,;:।॥]\s|[।॥]|\n")


def _spoken_turn_tokens() -> int:
    """Output cap for one spoken turn.

    Scripts differ enormously in tokens-per-word. Devanagari costs roughly 3-4x
    what Latin script does, so a 180-token cap that comfortably fits two English
    sentences truncates Hindi mid-word -- which then reaches the TTS as a broken
    fragment.

    Raised well above a normal turn because of the final summary: reading nine
    milestones back in Devanagari is long, and when the cap was 420 the model ran
    out of budget mid-tool-call and the turn produced NO speech at all -- the
    caller simply never heard the summary. A cap only needs to be low enough to
    stop rambling, and the prompt already enforces brevity.
    """
    code = _s().agent_language.lower()[:2]
    if code in ("hi", "ar"):
        # 1400 was still not enough. On a real call a round that had to emit
        # three record_milestone calls in Devanagari came back
        # stop_reason=max_tokens with the last call truncated, and another came
        # back with no usable content at all. A cap only has to be low enough to
        # stop rambling; the prompt already enforces brevity, and unused budget
        # costs nothing.
        return 2400
    # Registered languages carry their own budget, because tokens-per-word varies
    # by script: Cyrillic costs more than Latin, Devanagari more again.
    from .languages import spec
    s = spec(code)
    return s.turn_tokens if s else 800


def _processing_line() -> str:
    """What to say when the failure is OURS and we still have the answer.

    Not an apology, and above all not "say that again". The driver's answer was
    perfectly clear and we are still holding it; asking him to repeat it makes the
    agent look broken and burns the one thing a four-minute call cannot spare. He
    had already said "you keep asking me the same thing" by the second time.
    """
    from .languages import spec
    sp = spec(_s().agent_language)
    return (sp.processing if sp else None) or "Bear with me, noting that down."


# ===========================================================================
# Interfaces
# ===========================================================================

class STTProvider(ABC):
    """Streaming speech-to-text. Consumes 16 kHz mono int16 PCM."""

    # Set by the pipeline so the provider can guard against the agent's own voice
    # returning through a speakerphone.
    agent_speaking: bool = False
    # Emitted-frame counter from the outbound track, set by whoever pumps audio.
    # None means "no information", and the gate falls back to timing alone.
    playback_frames: int | None = None
    # True when the last transcript was low-confidence. Engines that give no
    # confidence signal leave this False, which correctly means "no reason to
    # doubt it" rather than falsely claiming certainty.
    last_unclear: bool = False

    # True while the provider judges the CALLER to be speaking. Barge-in must be
    # driven from this, not a second VAD in the caller's pump -- a duplicate VAD
    # has its own threshold and no echo awareness, so it will happily cut the
    # agent off on its own echo while the real gate is correctly ignoring it.
    caller_speaking: bool = False

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abstractmethod
    async def events(self) -> AsyncIterator[tuple[str, str]]:
        """Yields ('partial'|'final'|'utterance_end'|'speech_started', text)."""
        ...

    @abstractmethod
    async def close(self) -> None: ...

    async def finalize(self) -> None:
        """Flush and return whatever is pending. No-op for engines that decode
        on end-of-speech anyway; Deepgram needs it because otherwise a caller
        that stops sending audio waits for an idle timeout instead of a result."""
        return None


# Verified against platform.claude.com/docs/en/about-claude/pricing on
# 20 Aug 2026. USD per MILLION tokens: (input, output, cache_read, cache_write_5m).
# Kept here so a call reports real money rather than a token count nobody
# converts. Re-check before quoting these to anyone -- prices change.
MODEL_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5":            (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5":          (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-4-6":        (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5-20251001": (1.00, 5.00, 0.10, 1.25),
}


def estimate_cost_usd(model: str, tin: int, tout: int, cread: int, cwrite: int) -> float | None:
    """USD for one call's token usage. None when the model is not in the table."""
    key = next((k for k in MODEL_PRICES if model.startswith(k)), None)
    if key is None:
        return None
    pin, pout, pread, pwrite = MODEL_PRICES[key]
    return (
        tin * pin + tout * pout + cread * pread + cwrite * pwrite
    ) / 1_000_000


class TTSProvider(ABC):
    """Streaming text-to-speech. Emits mono int16 PCM at `rate`."""

    rate: int = 24000

    @abstractmethod
    async def stream(self, text: str) -> AsyncIterator[bytes]: ...

    async def close(self) -> None:
        return None


class LLMProvider(ABC):
    """Streaming chat, chunked into speakable clauses."""

    def __init__(self, system_prompt: str) -> None:
        self.system = system_prompt
        self.history: list[dict] = []
        self._tools: list[dict] = []
        self._handlers: dict = {}
        # Real token usage for this call, filled in from the API's own reporting.
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cache_read = 0
        self.tokens_cache_write = 0
        self.api_calls = 0

    #: Tools after which the turn is OVER. Without this the model said its
    #: goodbye, called end_call in the same reply exactly as instructed, and then
    #: the tool loop went round again and it said goodbye a second time. The
    #: driver heard "dhanyawad, aapka din shubh rahe" twice.
    terminal_tools: frozenset[str] = frozenset({"end_call"})

    def register_tools(self, tools: list[dict], handlers: dict) -> None:
        """Attach tool definitions and their Python implementations."""
        self._tools = tools
        self._handlers = handlers

    @abstractmethod
    async def respond(self, user_text: str) -> AsyncIterator[str]: ...

    def cancel_last_turn(self) -> None:
        """After a barge-in the assistant did not finish speaking its turn, so
        leaving it in history makes the model believe it did."""
        if self.history and self.history[-1]["role"] == "assistant":
            self.history.pop()

    @staticmethod
    def _chunk(buf: str) -> tuple[list[str], str]:
        """Split off complete clauses; return (chunks, remainder)."""
        out: list[str] = []
        while True:
            m = _CLAUSE_END.search(buf)
            if not m:
                break
            cut = m.end()
            chunk, buf = buf[:cut].strip(), buf[cut:]
            if chunk:
                out.append(chunk)
        return out, buf


# ===========================================================================
# STT
# ===========================================================================

class DeepgramSTT(STTProvider):
    """Cloud STT. Best endpointing available; caller audio leaves your network."""

    def __init__(self) -> None:
        s = get_settings()
        self._key = s.deepgram_api_key
        self._ws = None
        self._closed = False

        from .languages import spec
        lang = spec(s.agent_language)
        # Language comes from the registry, per language. The previous version
        # hardcoded `model=nova-2-general&language=multi`, and nova-2's "multi"
        # is SPANISH + ENGLISH ONLY -- so Hindi audio would have been decoded as
        # Spanish and returned confident nonsense. nova-3's "multi" is the
        # ten-language code-switching model that does include Hindi.
        self._lang = lang.dg_lang if lang else "en"
        self._model = s.deepgram_model
        self.URL = (
            "wss://api.deepgram.com/v1/listen"
            f"?model={self._model}"
            "&encoding=linear16&sample_rate=16000&channels=1"
            "&punctuate=true&interim_results=true"
            f"&endpointing={s.deepgram_endpointing_ms}"
            f"&utterance_end_ms={s.deepgram_utterance_end_ms}"
            f"&vad_events=true&language={self._lang}"
        )

        # Echo suppression. Deepgram does its own endpointing, so we do not need
        # the SpeechGate for turn detection -- but we DO need to stop the agent's
        # own voice being transcribed off a speakerphone. This class had no
        # gating at all, so the agent would have answered itself. Suppressing
        # also stops us paying Deepgram per-minute for our own audio.
        self._tail = 0
        self._tail_frames = 20            # 400 ms at 20 ms/frame

        # Barge-in while suppressed.
        #
        # Deepgram never sets `caller_speaking` -- that flag belongs to the local
        # SpeechGate -- so with this provider barge-in was dead: anything the
        # driver said over the agent was thrown away silently and never reached
        # the recogniser at all. That is why a "shukriya" spoken over the closing
        # line vanished without trace. Suppression is still right (we must not
        # transcribe our own voice off a speakerphone), but it has to be
        # interruptible, so run a cheap energy detector on the frames we drop.
        #
        # Deliberately conservative: it takes sustained, clearly-above-echo audio
        # to trip, and the transcript-level `looks_like_echo` check in ai.py is
        # the backstop if our own voice ever gets through anyway.
        self._loud_frames = 0
        self._barge_pulse = False         # see send_audio: a ONE-frame signal
        self._barged = False              # already interrupted this utterance
        self._barge_frames = 8            # 160 ms of speech, not a door slam
        # A FIXED threshold of 900 was too deaf -- the driver said "saare doc de
        # diye hai" over the document question and it never registered -- so the
        # bar is CALIBRATED PER UTTERANCE instead.
        #
        # The first 300 ms of each thing the agent says is treated as a sample of
        # how loudly our own voice is coming back, and the bar is set well above
        # that. On a handset there is almost no echo, so the bar sits at the floor
        # and an ordinary speaking voice interrupts. On a speakerphone the echo is
        # loud, the bar rises with it, and only someone genuinely talking over us
        # gets through. Learning the level from "quiet frames" instead does not
        # work: on a speakerphone there are none, so the bar never rises and the
        # agent interrupts itself.
        self._barge_floor_rms = 400.0     # never trip below this, whatever
        self._barge_echo_mult = 2.5       # ... nor below this multiple of echo
        self._calib_frames = 15           # 300 ms of "this is our own voice"
        self._utt_frames = 0
        self._echo_peak = 0.0
        self._was_speaking = False
        self._held: list[bytes] = []      # last few suppressed frames
        # Deepgram hangs up after ~10s with nothing received. Send a KeepAlive
        # every few seconds while suppressed, comfortably inside that window.
        self._last_sent = time.monotonic()
        self._keepalive_s = 3.0

    async def connect(self) -> None:
        import websockets
        self._ws = await websockets.connect(
            self.URL, additional_headers={"Authorization": f"Token {self._key}"}
        )
        log.info(
            "STT: deepgram connected — model=%s language=%s", self._model, self._lang
        )

    async def send_audio(self, pcm16: bytes) -> None:
        if not self._ws or self._closed:
            return
        # `caller_speaking` is a ONE-FRAME pulse. The session reads it the moment
        # this call returns, and it has to be gone by the next frame: the 400 ms
        # echo tail is still suppressed audio, so a latched flag would re-fire
        # barge-in twenty times over for a single interruption.
        if self._barge_pulse:
            self.caller_speaking = False
            self._barge_pulse = False

        # A new agent utterance restarts the echo calibration, because the level
        # depends on what the handset is doing right now, not on the last turn.
        if self.agent_speaking and not self._was_speaking:
            self._utt_frames = 0
            self._echo_peak = 0.0
            self._loud_frames = 0
        self._was_speaking = self.agent_speaking

        # Do not forward our own playback. Costs money and gets transcribed.
        suppress = self.agent_speaking or self._tail > 0
        if self.agent_speaking:
            self._tail = self._tail_frames
        elif self._tail > 0:
            self._tail -= 1

        if suppress and not self._barged and self._is_barge_in(pcm16):
            # The driver is talking over us. Stop suppressing, and forward the
            # frames we were about to discard so his first syllable survives --
            # dropping it turns "shukriya, bas ho gaya" into "bas ho gaya", or
            # into nothing at all.
            log.info("barge-in detected during playback — resuming transcription")
            self.caller_speaking = True
            self._barge_pulse = True
            # One announcement per interruption. Normally the session flushes the
            # track and `agent_speaking` goes False within a frame or two, but if
            # playback ever wedged we would otherwise re-fire barge-in every
            # 160 ms for the rest of the call.
            self._barged = True
            self._tail = 0
            suppress = False
            # Require a fresh run of loud frames before announcing another
            # barge-in, so a caller who keeps talking does not fill the log.
            self._loud_frames = 0
            held, self._held = self._held, []
            for frame in held:
                try:
                    await self._ws.send(frame)
                except Exception:
                    self._closed = True
                    return
            self._last_sent = time.monotonic()

        if suppress:
            # CRITICAL: Deepgram closes the socket with 1011 after ~10s of
            # receiving nothing. Suppressing the agent's own audio therefore
            # kills the connection during any long agent turn -- and the
            # milestone read-back is ~30 SECONDS of continuous speech. A
            # KeepAlive costs nothing and is not billed as audio.
            await self._keepalive()
            return
        self._loud_frames = 0
        self._held.clear()
        if not self.agent_speaking:
            # Re-arm only once WE have stopped talking. The barge-in branch above
            # deliberately falls through to here, so re-arming unconditionally let
            # one interruption announce itself every 160 ms for as long as the
            # agent kept speaking.
            self._barged = False
        try:
            await self._ws.send(pcm16)
            self._last_sent = time.monotonic()
        except Exception:
            self._closed = True

    def _is_barge_in(self, pcm16: bytes) -> bool:
        """Sustained speech-level energy while we are the one talking.

        Energy alone cannot tell the driver's voice from our own echo, which is
        why turn-*ending* is left to Deepgram. It is good enough to decide "someone
        is definitely talking over us", and being wrong costs one interrupted
        sentence rather than a lost answer.
        """
        # Keep a short rolling window so the start of the interruption is not lost
        # to the detector's own warm-up period.
        self._held.append(pcm16)
        if len(self._held) > self._barge_frames + 4:
            self._held.pop(0)

        try:
            import numpy as np
            samples = np.frombuffer(pcm16, dtype=np.int16)
            if samples.size == 0:
                return False
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        except Exception:
            return False

        # Calibration window: this is our own voice, by definition. Measure it
        # and refuse to call it an interruption.
        if self._utt_frames < self._calib_frames:
            self._utt_frames += 1
            self._echo_peak = max(self._echo_peak, rms)
            return False

        thresh = max(self._barge_floor_rms, self._barge_echo_mult * self._echo_peak)
        if rms >= thresh:
            self._loud_frames += 1
        else:
            self._loud_frames = 0
        return self._loud_frames >= self._barge_frames

    async def _keepalive(self) -> None:
        now = time.monotonic()
        if now - self._last_sent < self._keepalive_s:
            return
        try:
            await self._ws.send(json.dumps({"type": "KeepAlive"}))
            self._last_sent = now
        except Exception:
            self._closed = True

    async def finalize(self) -> None:
        """Flush pending audio and ask for the final transcript now.

        Without this, a caller that stops sending audio waits for Deepgram's
        endpointing and then for its idle timeout -- which is how `test-ai` hit
        1011 while sitting in wait_for() after the last frame.
        """
        if self._ws and not self._closed:
            try:
                await self._ws.send(json.dumps({"type": "Finalize"}))
            except Exception:
                self._closed = True

    async def close(self) -> None:
        self._closed = True
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            await self._ws.close()

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        if not self._ws:
            raise RuntimeError("connect() first")
        # A closed socket is END OF STREAM, not a failure. Letting
        # ConnectionClosed propagate pushed a websockets exception up through the
        # pipeline and out of `test-ai` as a traceback -- and on a live call it
        # would kill the turn handler when the call simply ended. Deepgram also
        # closes with 1011 after ~10s of idle, which is expected whenever the
        # caller has stopped talking and we are waiting.
        import websockets.exceptions as _wse
        try:
            async for raw in self._iter_ws():
                yield raw
        except _wse.ConnectionClosedOK:
            log.info("deepgram stream closed")
        except _wse.ConnectionClosedError as e:
            # 1011 with "did not receive audio" is an idle timeout, not a fault.
            if "timeout window" in str(e):
                log.info("deepgram closed on idle timeout (no audio to send)")
            else:
                log.warning("deepgram closed unexpectedly: %s", e)
        finally:
            self._closed = True

    async def _iter_ws(self) -> AsyncIterator[tuple[str, str]]:
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "SpeechStarted":
                yield "speech_started", ""
            elif kind == "UtteranceEnd":
                yield "utterance_end", ""
            elif kind == "Results":
                alts = msg.get("channel", {}).get("alternatives", [])
                if not alts:
                    continue
                text = (alts[0].get("transcript") or "").strip()
                if text:
                    yield ("final" if msg.get("is_final") else "partial"), text


class WhisperLocalSTT(STTProvider):
    """Self-hosted STT via faster-whisper. No audio leaves your network.

        pip install faster-whisper

    Honest limitation: Whisper is not a streaming model. We buffer speech, detect
    the end of an utterance with an energy VAD, then transcribe the whole chunk.
    That means:

      * No useful partial results.
      * Endpointing is worse than Deepgram's. A driver who pauses mid-sentence
        will get cut off; tune `silence_ms` upward if that happens.
      * Latency = utterance length is irrelevant, but transcription time is not.
        On CPU expect 0.5-1.5 s for a short turn; on a GPU, ~150 ms.

    Use `MODEL_SIZE=small` on CPU, `medium`/`large-v3` with a GPU.
    """

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        # How long a pause ends the caller's turn. 700 ms split people
        # mid-sentence: drivers pause to think, to check a document, or because
        # they are driving. The cost of waiting longer is a little latency; the
        # cost of cutting them off is a fragment that transcribes as nonsense and
        # an agent that answers half a question.
        # None -> take STT_SILENCE_MS from config, so this and the Nemotron path
        # cannot drift apart. An explicit value still overrides, for tests.
        silence_ms: int | None = None,
        threshold: int = 700,
    ) -> None:
        self._model_size = model_size or _s().whisper_model
        self._device = device or _s().whisper_device
        # Decoding language.
        #
        #   "hi" / "en"  pinned. Best accuracy IN that language, but audio in any
        #                other language is forced through the wrong phoneme set
        #                and comes out as nonsense.
        #   "auto"       Whisper detects per utterance. Handles a caller switching
        #                language, at the cost of occasional wrong guesses on very
        #                short replies like "haan" or "ok".
        code = get_settings().agent_language.lower()
        # Decode as the language the registry says, not necessarily the one we
        # SPEAK. Urdu is the case: it decodes as Hindi, because they are the same
        # spoken language and Hindi is far better resourced. Reading
        # agent_language directly would decode Urdu audio as Urdu and lose that.
        from .languages import spec as _lspec
        _sp = _lspec(code)
        if _sp is not None:
            self._language = _sp.decode_lang
        else:
            self._language = None if code.startswith("auto") else (code[:2] or None)

        from .config import STT_HINT
        self._hint = STT_HINT
        if silence_ms is None:
            silence_ms = get_settings().stt_silence_ms
        self._silence_frames = silence_ms // 20  # inbound frames are 20 ms
        self._threshold = threshold

        # Adaptive, echo-aware per-frame gate, plus a Silero pass that rejects
        # utterances containing no actual speech (horns, engine noise, crowds).
        from .vad import SileroGate, SpeechGate
        self._gate = SpeechGate()
        self._silero = SileroGate()
        # Set by the pipeline so the gate knows when to guard against our own
        # voice returning through the caller's speakerphone.
        self.agent_speaking = False

        self._model = None
        self._buf = bytearray()
        self._silent = 0
        self._in_speech = False
        self._peak = 0.0
        self._idle = 0
        self._out: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # Only ONE inference at a time. Each transcription asks CTranslate2 for
        # every core, so two or three overlapping runs thrash the CPU and a
        # 5-second utterance can take 45 seconds. Utterances are also numbered so
        # a queued one can be dropped if the caller has since spoken again --
        # there is no value in transcribing stale audio the conversation has
        # already moved past.
        self._infer_lock = asyncio.Lock()
        self._utterance = 0

    async def connect(self) -> None:
        from faster_whisper import WhisperModel
        compute = "int8" if self._device == "cpu" else "float16"

        # Thread count is a trap on Apple Silicon. os.cpu_count() counts
        # efficiency cores as well as performance ones, so asking for cores-1
        # oversubscribes: CTranslate2 spawns threads that land on slow cores and
        # fight the asyncio loop and Opus codec for the fast ones. Observed
        # result was 62 SECONDS to transcribe 1.1 seconds of audio. Capping at 4
        # keeps the work on performance cores and leaves headroom for media I/O.
        threads = min(4, max(1, (os.cpu_count() or 4) - 1))

        key = (self._model_size, self._device, compute)
        async with _WHISPER_CACHE_LOCK:
            cached = _WHISPER_CACHE.get(key)
            if cached is not None:
                self._model = cached
                log.info("whisper %s reused from cache", self._model_size)
            else:
                t0 = time.monotonic()
                # Loading blocks for seconds; keep it off the event loop.
                self._model = await asyncio.to_thread(
                    lambda: WhisperModel(
                        self._model_size,
                        device=self._device,
                        compute_type=compute,
                        cpu_threads=threads,
                        num_workers=1,
                    )
                )
                _WHISPER_CACHE[key] = self._model
                log.info(
                    "whisper %s loaded in %.1fs using %d cpu threads (cached)",
                    self._model_size, time.monotonic() - t0, threads,
                )
        log.info(
            "STT: whisper %s on %s, language=%s (local)",
            self._model_size, self._device, self._language,
        )
        if self._language == "hi" and self._model_size in ("tiny", "base"):
            log.warning(
                "whisper '%s' is weak on Hindi -- expect poor accuracy. "
                "Use WHISPER_MODEL=small or medium.",
                self._model_size,
            )

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return

        # Adaptive gate: threshold follows the ambient noise floor, and is raised
        # while the agent is speaking so its own voice returning through a
        # speakerphone does not register as the caller talking.
        speaking = self._gate.update(
            pcm16,
            agent_speaking=self.agent_speaking,
            playback_frames=getattr(self, "playback_frames", None),
        )
        self._peak = self._gate.peak
        # Published so barge-in uses THIS decision rather than a second VAD.
        self.caller_speaking = speaking

        if speaking:
            if not self._in_speech:
                self._in_speech = True
                # Start from clean. If a previous utterance was abandoned (a
                # barge-in, or a transcription that produced nothing), stale audio
                # would otherwise be prepended and inflate the next buffer.
                self._buf = bytearray()
                log.info("speech started (%s)", self._gate.describe())
                await self._out.put(("speech_started", ""))
            self._buf.extend(pcm16)
            self._silent = 0
        elif self._in_speech:
            self._buf.extend(pcm16)          # keep trailing silence for context
            self._silent += 1
            if self._silent >= self._silence_frames:
                audio, self._buf = bytes(self._buf), bytearray()
                secs = len(audio) / 2 / 16000
                self._in_speech = False
                self._silent = 0
                self._utterance += 1
                log.info(
                    "speech ended after %.1fs — queued utterance #%d",
                    secs, self._utterance,
                )
                asyncio.create_task(self._transcribe(audio, self._utterance))
        else:
            # Idle. Report periodically so a mis-set threshold is visible rather
            # than presenting as "the agent cannot hear me".
            self._idle += 1
            if self._idle % 250 == 0:
                log.info("listening… no speech yet (%s)", self._gate.describe())

    async def _transcribe(self, pcm: bytes, seq: int = 0) -> None:
        if len(pcm) < 16000 // 2:   # under ~0.5 s: almost certainly a cough
            return

        # Wait for any in-flight inference to finish, then check we are still
        # relevant. Overlapping runs are what caused 45-second transcriptions.
        async with self._infer_lock:
            if seq and seq < self._utterance:
                log.info(
                    "dropping stale utterance #%d (caller has spoken since, now #%d)",
                    seq, self._utterance,
                )
                return
            await self._run_inference(pcm, seq)

    async def _run_inference(self, pcm: bytes, seq: int) -> None:
        t0 = time.monotonic()
        secs = len(pcm) / 2 / 16000
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        # Normalise the level. Browser and phone mics often deliver speech at
        # 3-10% of full scale, which Whisper transcribes poorly -- and which its
        # internal VAD discards outright as silence. Peak-normalising to ~0.7
        # costs nothing and is the difference between a transcript and nothing.
        peak = float(np.abs(audio).max())
        if 0 < peak < 0.7:
            audio = audio * (0.7 / peak)

        # Does this buffer contain speech at all, or just road noise? Energy said
        # "loud"; Silero decides "speech". A horn is far louder than a voice, so
        # this is the only layer that reliably rejects it. It also trims leading
        # and trailing noise, which both improves accuracy and shortens inference.
        #
        # Runs in a thread: it is an ONNX forward pass, and on the event loop it
        # stalls the WebRTC media pump, which corrupts the very audio we are about
        # to transcribe.
        has_speech, audio, why = await asyncio.to_thread(self._silero.verify, audio)
        if not has_speech:
            log.info("utterance #%d discarded: %s", seq, why)
            return
        log.info("utterance #%d: %s", seq, why)
        secs = len(audio) / STT_RATE_LOCAL
        s_cfg = get_settings()

        try:
            segments, _ = await asyncio.to_thread(
                lambda: self._model.transcribe(
                    audio,
                    language=self._language,
                    # vad_filter MUST stay off. Our energy VAD already decided
                    # this buffer is speech; running Whisper's Silero VAD on top
                    # is a second, stricter gate that was deleting 100% of the
                    # audio on quiet input and yielding an empty transcript.
                    vad_filter=False,
                    # Greedy decoding. The default beam_size=5 is roughly 3-4x
                    # slower for a marginal accuracy gain that does not survive
                    # telephone-quality audio anyway.
                    beam_size=1,
                    # Suppress Whisper's habit of inventing text during near
                    # silence (it will happily emit "Thank you for watching").
                    condition_on_previous_text=False,
                    no_speech_threshold=0.6,
                    # Prime the decoder with freight vocabulary and the short
                    # replies drivers actually give. Short utterances ("हाँ") have
                    # too little acoustic evidence to decode on their own, so the
                    # language-model prior decides -- and this is how we steer it.
                    # No hint on very short clips. A one-word answer has so
                    # little acoustic evidence that the conditioning text decides
                    # the output -- which is how "haan" became "बजे". Above ~1.5s
                    # there is enough signal for the vocabulary hint to help
                    # rather than dominate.
                    # Default is NO prompt. The vocabulary hint was meant to
                    # steer short answers, but on a real call it bled its own
                    # FORMAT into the output -- a comma-separated word list in,
                    # comma-separated fragments out ("ना, क्याड़ी, बिल्टी,
                    # करनेगा"). Prompt-shaped text also scores worse on
                    # avg_logprob, which trips log_prob_threshold, which fires
                    # the temperature ladder below and re-decodes the clip up to
                    # six times. That is why one 7.0s utterance took 18.1s.
                    # Set WHISPER_USE_HINT=1 to put it back and compare.
                    initial_prompt=(
                        self._hint if (s_cfg.whisper_use_hint and secs >= 1.5) else None
                    ),
                    # DO NOT pin temperature to 0. The default is a fallback
                    # ladder [0.0 .. 1.0]: Whisper decodes greedily first, and
                    # when compression_ratio_threshold detects degenerate output
                    # it RE-DECODES at a higher temperature to escape. Pinning 0
                    # removes that escape and the decoder gets stuck emitting one
                    # token forever -- "बजे बजे बजे बजे ..." for an entire turn.
                    # Shortened from [0.0 .. 1.0]. The ladder must exist -- pinning
                    # 0.0 makes the decoder stick emitting one token forever --
                    # but six rungs means a bad clip costs six full decodes, and
                    # rungs above 0.4 produce increasingly invented text anyway.
                    # Two rungs caps the worst case at 2x instead of 6x.
                    temperature=s_cfg.whisper_temperature_ladder,
                    # Repetition is THE characteristic Whisper failure on short,
                    # noisy, accented speech. These two attack it directly rather
                    # than relying on the fallback to notice after the fact.
                    repetition_penalty=1.15,
                    no_repeat_ngram_size=3,
                    # Explicit, so the fallback actually triggers: a high
                    # compression ratio means the text is repetitive.
                    compression_ratio_threshold=2.4,
                    log_prob_threshold=-1.0,
                )
            )
            segs = list(segments)
            text = " ".join(s.text.strip() for s in segs).strip()
        except Exception:
            log.exception("whisper transcribe failed")
            return

        took = time.monotonic() - t0
        # Report the decoder's own confidence, not just the clock. Without this
        # a slow, garbled turn is indistinguishable from a slow, correct one,
        # and there is no way to tell whether the fallback ladder fired.
        #   avg_logprob  < -1.0  -> below log_prob_threshold, ladder re-decodes
        #   compression_ratio > 2.4 -> degenerate/repetitive, ladder re-decodes
        #   no_speech_prob > 0.6 -> Whisper thinks this is not speech at all
        if segs:
            lp = sum(s.avg_logprob for s in segs) / len(segs)
            cr = max(s.compression_ratio for s in segs)
            ns = max(s.no_speech_prob for s in segs)
            flags = []
            if lp < -1.0:
                flags.append("LOW-CONFIDENCE (ladder fired)")
            if cr > 2.4:
                flags.append("REPETITIVE")
            if ns > 0.6:
                flags.append("MAYBE-NOT-SPEECH")
            log.info(
                "utterance #%d: %.1fs audio transcribed in %.1fs (%.1fx real time) "
                "logprob=%.2f compression=%.2f no_speech=%.2f%s",
                seq, secs, took, secs / took if took else 0, lp, cr, ns,
                ("  <- " + ", ".join(flags)) if flags else "",
            )
        else:
            log.info(
                "utterance #%d: %.1fs audio transcribed in %.1fs — NO SEGMENTS",
                seq, secs, took,
            )
        if text:
            # Publish how much to trust this transcript. Whisper's own
            # avg_logprob is the only honest signal available, and without it
            # downstream code cannot tell a confident transcript from a guess --
            # so it must either confirm everything (slow) or confirm nothing
            # (silently records wrong times). Neither is acceptable.
            self.last_unclear = bool(segs) and (
                (sum(s.avg_logprob for s in segs) / len(segs)) < -0.75
            )
            await self._out.put(("final", text))
            await self._out.put(("utterance_end", ""))

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        while True:
            yield await self._out.get()

    async def close(self) -> None:
        # Drop only our reference. The model stays in _WHISPER_CACHE for the next
        # call -- unloading and reloading a 500 MB model per call is what made
        # repeated sessions pathologically slow.
        self._model = None


# ===========================================================================
# LLM
# ===========================================================================

class AnthropicLLM(LLMProvider):
    """Claude via the Anthropic API, or via Bedrock/Vertex for data residency.

    Set LLM_PROVIDER=bedrock to route through your own AWS account, which keeps
    the request inside a region you choose and under your existing AWS data
    agreements. Check which regions currently offer the model you want.
    """

    # Set False for the life of the process if a model rejects the parameter,
    # rather than failing every single turn to learn the same thing again.
    _thinking_param = True

    def __init__(self, system_prompt: str, use_bedrock: bool = False) -> None:
        super().__init__(system_prompt)
        s = get_settings()
        self._model = s.anthropic_model
        if use_bedrock:
            from anthropic import AsyncAnthropicBedrock
            self._client = AsyncAnthropicBedrock(
                aws_region=_s().aws_region
            )
            self._model = _s().bedrock_model_id or self._model
            log.info("LLM: claude via bedrock region=%s", _s().aws_region)
        else:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=s.anthropic_api_key)
            log.info("LLM: claude via anthropic api")

    def _cached_system(self):
        """System prompt as a cacheable block.

        The Hindi prompt plus the milestone ladder is ~4,500 tokens and it is
        IDENTICAL on every turn of every call. Without caching it is re-read and
        re-billed 24 times per call, which is most of the input cost and part of
        the latency. A cache write costs 1.25x once; every read after that is
        0.1x. See MODEL_PRICES above for the rates this is based on.
        """
        if not _s().prompt_caching:
            return self.system
        return [{
            "type": "text",
            "text": self.system,
            "cache_control": {"type": "ephemeral"},
        }]

    def _cached_tools(self):
        """Tool schemas, with the cache breakpoint on the LAST one.

        A breakpoint caches everything BEFORE it, so one marker on the final
        tool covers the whole array. Marking every tool would waste breakpoints
        (there are only four available per request).
        """
        tools = self._tools or []
        if not tools or not _s().prompt_caching:
            return tools
        out = [dict(t) for t in tools]
        out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    async def respond(self, user_text: str) -> AsyncIterator[str]:
        self.history.append({"role": "user", "content": user_text})
        async for chunk in self._turn():
            yield chunk

    async def _turn(self) -> AsyncIterator[str]:
        """One assistant turn, looping while the model wants to call tools.

        Tools are how milestone data becomes structured. Relying on the model to
        merely *mention* what it collected leaves you parsing prose at the end of
        a call; recording each fact as it is confirmed gives a payload the TMS can
        consume, and lets the agent know precisely what is still missing.
        """
        _empty_retries = 0
        # Has this turn produced any audible speech yet? The tool loop used to
        # let the model speak on EVERY round, so one driver answer could produce
        # three separate spoken blocks: it asked "origin border kab pahunche" in
        # round 1, asked it AGAIN in round 2, then bolted the document question on
        # in round 3. From the driver's side that is two or three thoughts
        # fighting over the line. A human says one thing and then waits.
        spoke_this_turn = False
        for _round in range(6):                  # bound the tool loop
            buf, said = "", []
            tool_calls: list[dict] = []

            # Log every request. On a real call one request returned 200 and then
            # produced nothing -- no text, no tool call, no error -- and there was
            # no way to tell whether the model had answered, called a tool, or
            # the stream had simply stalled. A line per round makes the shape of
            # a turn visible.
            log.info(
                "llm round %d: model=%s tools=%d history=%d caching=%s",
                _round + 1, self._model, len(self._tools or []),
                len(self.history), _s().prompt_caching,
            )
            kwargs: dict = dict(
                model=self._model,
                max_tokens=_spoken_turn_tokens(),
                system=self._cached_system(),
                messages=self.history,
                tools=self._cached_tools(),
            )
            # Extended thinking is the enemy of a live phone call. Thinking tokens
            # are billed, they count against max_tokens, and the driver cannot hear
            # one of them. Worse: when the budget runs out INSIDE a thinking block
            # the response arrives with no text and no tool call, which this code
            # could only report as "no usable content". That happened twice on one
            # real call -- 200 OK, sixteen seconds of silence, and then the driver
            # was asked to repeat an answer he had given perfectly clearly.
            #
            # It goes in extra_body, not as a keyword. The installed SDK rejected
            # `thinking=` outright ("unexpected keyword argument"), so the setting
            # never reached the API at all -- it only burned a round discovering
            # that. extra_body is merged into the request JSON by every SDK
            # version, so this works regardless of how old the client is.
            if AnthropicLLM._thinking_param:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if (
                            event.type == "content_block_delta"
                            and getattr(event.delta, "type", "") == "text_delta"
                        ):
                            buf += event.delta.text
                            chunks, buf = self._chunk(buf)
                            for c in chunks:
                                said.append(c)
                                yield c
                    final = await stream.get_final_message()
            except Exception as e:
                if kwargs.get("extra_body") and "thinking" in str(e).lower():
                    log.warning(
                        "%s rejected thinking={'type':'disabled'} (%s) — retrying "
                        "without it and not asking again this process. Latency "
                        "and cost will both be higher; upgrade the anthropic SDK.",
                        self._model, e,
                    )
                    AnthropicLLM._thinking_param = False
                    continue
                raise

            # Accumulate REAL token counts. Per-call cost was previously an
            # estimate built from character counts and an assumed tokens-per-word
            # ratio for Devanagari -- which is exactly the kind of number that is
            # quietly wrong by 2x. The API returns the truth, so record it and
            # let `pipeline.stats()` report actual money.
            try:
                u = final.usage
                self.tokens_in += getattr(u, "input_tokens", 0) or 0
                self.tokens_out += getattr(u, "output_tokens", 0) or 0
                self.tokens_cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
                self.tokens_cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
                self.api_calls += 1
            except Exception:
                pass

            if buf.strip():
                said.append(buf.strip())
                yield buf.strip()
            if said:
                spoke_this_turn = True

            # Rebuild the assistant turn as plain dicts rather than replaying the
            # SDK's response objects. Those do not round-trip: a `thinking` block
            # comes back in a shape the API rejects
            # ("content.0.thinking.text: Extra inputs are not permitted"), which
            # killed every turn after the first tool call. Keeping only the block
            # types we actually need also means new block types cannot break us.
            assistant: list[dict] = []
            tool_calls = []
            for b in final.content:
                btype = getattr(b, "type", "")
                if btype == "text" and getattr(b, "text", "").strip():
                    assistant.append({"type": "text", "text": b.text})
                elif btype == "tool_use":
                    tool_calls.append(b)
                    assistant.append({
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input or {},
                    })
                # thinking / redacted_thinking deliberately dropped.

            if not assistant:
                # Log WHAT came back. This was previously a bare warning, so there
                # was no way to tell a thinking-only response from a truncated tool
                # call from an empty stream -- three different bugs with the same
                # symptom.
                blocks = [getattr(b, "type", "?") for b in final.content] or ["<none>"]
                log.warning(
                    "round %d produced NO usable content: stop_reason=%s blocks=%s "
                    "output_tokens=%s",
                    _round + 1, getattr(final, "stop_reason", "?"), blocks,
                    getattr(getattr(final, "usage", None), "output_tokens", "?"),
                )
                # An empty reply AFTER we have already spoken is not a failure at
                # all -- it is the model saying "I have said my piece". Treating
                # it as an error is what produced a spurious "ji, likh raha hoon"
                # tacked onto the end of perfectly good questions.
                if spoke_this_turn:
                    log.info("empty reply after speaking — the model considers "
                             "the turn finished, which it is")
                    return

                # Nothing was said, so this really is a failure. Retry the SAME
                # turn: the driver said something intelligible and we still have
                # it, so telling him to repeat it is both wrong and infuriating --
                # it is what made him snap "you keep asking me the same thing".
                if _empty_retries == 0:
                    _empty_retries += 1
                    if not said:
                        line = _processing_line()
                        log.info("speaking %r, then retrying the turn", line)
                        yield line
                    continue
                log.error("two empty responses in a row — abandoning this turn")
                return
            self.history.append({"role": "assistant", "content": assistant})

            if not tool_calls:
                if not said:
                    # Ran out of tokens, or produced only thinking. Either way the
                    # caller hears nothing, which is the worst possible outcome --
                    # surface it rather than letting the call go quiet.
                    log.warning(
                        "turn ended with no speech (stop_reason=%s). Raise "
                        "max_tokens if this recurs.",
                        getattr(final, "stop_reason", "?"),
                    )
                return

            log.info(
                "llm round %d: stop_reason=%s spoke=%d clause(s) tools=%s",
                _round + 1, getattr(final, "stop_reason", "?"), len(said),
                [tc.name for tc in tool_calls],
            )

            # NO filler here any more.
            #
            # This used to speak a holding line when a round produced tool calls
            # and no text -- but it could only fire once the round had ALREADY
            # finished, which is after the silence rather than during it. The
            # driver had already sat through the whole gap by then. Pipeline's
            # stall filler (see ai.py) speaks ~900 ms into the turn WHILE the
            # model works, which is the only timing that actually helps, and
            # doing both put two holding lines back to back.
            results = []
            for tc in tool_calls:
                out = self._run_tool(tc.name, tc.input or {})
                results.append(
                    {"type": "tool_result", "tool_use_id": tc.id, "content": out}
                )
            self.history.append({"role": "user", "content": results})

            ended = [tc.name for tc in tool_calls if tc.name in self.terminal_tools]
            if ended:
                log.info("%s ended the turn — not looping for another reply",
                         ", ".join(ended))
                return

            # ONE SPOKEN UTTERANCE PER TURN.
            #
            # If this round has already said something out loud, the tools have
            # now run and their results are in history for the NEXT turn. Going
            # round again only lets the model add a second thought to a question
            # the driver has not answered yet -- which is exactly what happened:
            # it asked about the documents and then, without pausing, read the
            # entire nine-milestone summary in the same breath.
            if said:
                log.info("spoke %d clause(s) this round — tools have run, ending "
                         "the turn instead of talking over the driver", len(said))
                return

    def _run_tool(self, name: str, args: dict) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"unknown tool {name}"
        try:
            return handler(args)
        except Exception as e:                    # never let a tool kill the call
            log.exception("tool %s failed", name)
            return f"error: {e}"

    def register_tools(self, tools: list[dict], handlers: dict) -> None:
        self._tools = tools
        self._handlers = handlers


class OpenAICompatLLM(LLMProvider):
    """Any OpenAI-compatible endpoint: vLLM, Ollama, TGI, LM Studio.

    This is the fully self-hosted path -- set LLM_BASE_URL to your own server and
    no prompt or transcript ever leaves your network.

        LLM_PROVIDER=openai_compatible
        LLM_BASE_URL=http://10.0.1.20:8000/v1
        LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
    """

    def __init__(self, system_prompt: str) -> None:
        super().__init__(system_prompt)
        self._base = _s().llm_base_url.rstrip("/")
        self._model = _s().llm_model
        self._key = _s().llm_api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        log.info("LLM: openai-compatible %s @ %s", self._model, self._base)

    def _cached_system(self):
        """System prompt as a cacheable block.

        The Hindi prompt plus the milestone ladder is ~4,500 tokens and it is
        IDENTICAL on every turn of every call. Without caching it is re-read and
        re-billed 24 times per call, which is most of the input cost and part of
        the latency. A cache write costs 1.25x once; every read after that is
        0.1x. See MODEL_PRICES above for the rates this is based on.
        """
        if not _s().prompt_caching:
            return self.system
        return [{
            "type": "text",
            "text": self.system,
            "cache_control": {"type": "ephemeral"},
        }]

    def _cached_tools(self):
        """Tool schemas, with the cache breakpoint on the LAST one.

        A breakpoint caches everything BEFORE it, so one marker on the final
        tool covers the whole array. Marking every tool would waste breakpoints
        (there are only four available per request).
        """
        tools = self._tools or []
        if not tools or not _s().prompt_caching:
            return tools
        out = [dict(t) for t in tools]
        out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    async def respond(self, user_text: str) -> AsyncIterator[str]:
        self.history.append({"role": "user", "content": user_text})
        messages = [{"role": "system", "content": self.system}, *self.history]
        buf, full = "", ""

        async with self._client.stream(
            "POST",
            f"{self._base}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": self._model,
                "messages": messages,
                "max_tokens": 180,
                "stream": True,
            },
        ) as r:
            if r.status_code >= 400:
                log.error("LLM %s: %s", r.status_code, (await r.aread())[:300])
                return
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                except Exception:
                    continue
                buf += delta
                full += delta
                chunks, buf = self._chunk(buf)
                for c in chunks:
                    yield c
        if buf.strip():
            yield buf.strip()
        self.history.append({"role": "assistant", "content": full})


# ===========================================================================
# TTS
# ===========================================================================

class ElevenLabsTTS(TTSProvider):
    """Cloud TTS. Best quality; the agent's script leaves your network."""

    rate = 24000

    def __init__(self) -> None:
        s = get_settings()
        self._key = s.elevenlabs_api_key
        self._voice = s.elevenlabs_voice_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice}/stream"
            "?output_format=pcm_24000&optimize_streaming_latency=3"
        )
        body = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        async with self._client.stream(
            "POST", url, json=body,
            headers={"xi-api-key": self._key, "Content-Type": "application/json"},
        ) as r:
            if r.status_code >= 400:
                log.error("TTS %s: %s", r.status_code, (await r.aread())[:300])
                return
            async for chunk in r.aiter_bytes(chunk_size=4096):
                if chunk:
                    yield chunk


# Piper's ONNX model costs ~1.2 s to load and is completely stateless once
# loaded, so it belongs to the PROCESS, not to a provider instance. It used to be
# cached on `self`, which looked right and achieved nothing: main.py pre-warms TTS
# at startup with one instance, discards it, and the instance the first CALL
# builds loaded the model all over again. So the very caller the pre-warm existed
# to protect still waited over a second for the greeting -- which is exactly the
# silence-on-answer that was reported.
_PIPER_CACHE: dict[str, object] = {}
_PIPER_LOCK = asyncio.Lock()


class PiperLocalTTS(TTSProvider):
    """Self-hosted TTS via Piper. Nothing leaves your network.

    Install the binary and a voice model, then:

        PIPER_BIN=/usr/local/bin/piper
        PIPER_MODEL=/opt/voices/en_US-amy-medium.onnx
        PIPER_RATE=22050          # match your model's sample rate

    Fast enough for real time on modest CPU (roughly 0.1x real time). Quality is
    clearly below ElevenLabs but perfectly intelligible, and it costs nothing per
    call. Arabic voices exist but are weaker -- audition before committing.
    """

    def __init__(self) -> None:
        self._bin = _s().piper_bin
        self._model = _s().piper_model
        self.rate = _s().piper_rate

        # If PIPER_MODEL points at a voice for a DIFFERENT language than
        # AGENT_LANGUAGE, prefer the voice that matches the language and say so
        # loudly. Mismatch is silent and baffling: the agent generates correct
        # Turkish and a Hindi voice reads it as gibberish, which sounds like a
        # broken model rather than a one-line config error.
        from .languages import spec as _lang_spec
        want = _lang_spec(_s().agent_language)
        if want and self._model and want.piper_voice not in os.path.basename(self._model):
            guess = os.path.join(
                os.path.dirname(self._model), f"{want.piper_voice}.onnx"
            )
            if os.path.exists(guess):
                log.warning(
                    "PIPER_MODEL is %s but AGENT_LANGUAGE=%s — switching to %s",
                    os.path.basename(self._model), want.code,
                    os.path.basename(guess),
                )
                self._model = guess
            else:
                log.error(
                    "AGENT_LANGUAGE=%s but PIPER_MODEL is %s, and %s is not "
                    "downloaded. The agent will speak %s through a voice trained "
                    "on another language. Run: bash scripts/fetch-voice.sh %s",
                    want.code, os.path.basename(self._model),
                    f"{want.piper_voice}.onnx", want.english_name, want.code,
                )

        if not self._model:
            log.warning("PIPER_MODEL not set; local TTS will fail")
        elif not os.path.exists(self._model):
            log.error("PIPER_MODEL does not exist: %s", self._model)

        # Read the real sample rate from the model's sidecar config rather than
        # trusting PIPER_RATE. If the two disagree, every resample to the 48 kHz
        # wire rate is wrong and the voice comes out chipmunk-pitched or dragging.
        # Piper voices ship at 16000 / 22050 / 24000 depending on the voice.
        cfg_path = f"{self._model}.json"
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    actual = int(json.load(f)["audio"]["sample_rate"])
                if actual != self.rate:
                    log.warning(
                        "PIPER_RATE=%d disagrees with the model (%d Hz) -- using %d",
                        self.rate, actual, actual,
                    )
                self.rate = actual
            except (OSError, KeyError, ValueError, TypeError) as e:
                log.warning("could not read sample rate from %s: %s", cfg_path, e)
        else:
            log.warning(
                "no %s beside the model; trusting PIPER_RATE=%d",
                os.path.basename(cfg_path), self.rate,
            )

        # The loaded voice lives in _PIPER_CACHE at MODULE scope, not here.
        # A per-instance cache is why the startup pre-warm did nothing.

        log.info(
            "TTS: piper (local) model=%s rate=%d",
            os.path.basename(self._model or "?"), self.rate,
        )

    async def _voice(self):
        """Load the ONNX model once per PROCESS and keep it.

        The obvious implementation shells out to the `piper` binary per call. Do
        not: the model is re-loaded on every invocation, and because the LLM emits
        clause-sized chunks a single reply pays that cost several times. Measured
        at ~2.5s for the first chunk. Holding the model in-process drops it to
        tens of milliseconds.

        Keyed by model path at module scope so the startup pre-warm genuinely
        benefits the first real call, and so switching language mid-process does
        not evict the voice already in use.
        """
        key = self._model or ""
        voice = _PIPER_CACHE.get(key)
        if voice is None:
            async with _PIPER_LOCK:
                voice = _PIPER_CACHE.get(key)        # re-check inside the lock
                if voice is None:
                    from piper import PiperVoice
                    t0 = time.monotonic()
                    voice = await asyncio.to_thread(PiperVoice.load, self._model)
                    _PIPER_CACHE[key] = voice
                    log.info(
                        "piper model loaded in %.0f ms — cached for the PROCESS "
                        "(%d voice(s) resident)",
                        (time.monotonic() - t0) * 1000, len(_PIPER_CACHE),
                    )
        else:
            log.debug("piper voice served from the process cache")
        try:
            self.rate = int(voice.config.sample_rate)
        except Exception:
            pass
        return voice

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        try:
            voice = await self._voice()
        except Exception as e:
            log.error("could not load piper model %s: %s", self._model, e)
            return

        # synthesize() is CPU-bound and blocking, so it must not run on the event
        # loop -- that would stall the WebRTC media pump and produce audio glitches.
        def _render() -> list[bytes]:
            return [c.audio_int16_bytes for c in voice.synthesize(text)]

        try:
            for chunk in await asyncio.to_thread(_render):
                if chunk:
                    yield chunk
        except Exception as e:
            log.error("piper synthesis failed: %s", e)


# ===========================================================================
# Factory
# ===========================================================================

def make_stt() -> STTProvider:
    """Pick a recogniser for the CONFIGURED LANGUAGE.

    Three engines, and no single one covers all seven languages:
      deepgram  cloud, sub-second, verified for en/hi/tr/ru/ar/ur (NOT Kazakh)
      nemotron  self-hosted streaming, accurate, but RTF 2.42 on CPU (~4s lag)
      whisper   self-hosted batch, same RTF, worse accuracy — the Kazakh path

    STT_ENGINE=auto (the intent) lets each language choose from
    app/languages.py. Setting it explicitly forces one engine everywhere, which
    is how you A/B a language against a different recogniser.
    """
    s = _s()
    from .languages import spec
    lang = spec(s.agent_language)

    want = s.stt_engine.lower()
    if want == "auto":
        # STT_PROVIDER is legacy: it predates per-language routing. Honour it
        # only as a global override so old .env files keep working.
        legacy = s.stt_provider.lower()
        if legacy == "deepgram":
            want = "deepgram"
        else:
            want = lang.stt_engine if lang else "whisper"

    if want == "deepgram":
        if not s.deepgram_api_key:
            log.error(
                "AGENT_LANGUAGE=%s routes to Deepgram but DEEPGRAM_API_KEY is "
                "unset. Falling back to self-hosted, which is ~4s slower per "
                "turn. Get a key (free $200 credit, no card) at "
                "console.deepgram.com/signup",
                s.agent_language,
            )
            want = "nemotron" if (lang and lang.code != "kk") else "whisper"
        else:
            return DeepgramSTT()

    if want == "nemotron":
        # Probe for the dependency HERE, not by catching ImportError around the
        # constructor. stt_nemotron imports nemo lazily inside _load(), so the
        # constructor succeeds on a machine without nemo and the failure surfaces
        # mid-call, on the first thing the driver says. Check up front instead.
        import importlib.util
        if importlib.util.find_spec("nemo") is None:
            log.error(
                "AGENT_LANGUAGE=%s wants the nemotron streaming recogniser, but "
                "nemo_toolkit is not installed. Falling back to Whisper, which is "
                "roughly 5s slower per turn. Install it with:\n"
                "    pip install 'nemo_toolkit[asr]'",
                s.agent_language,
            )
        else:
            from .stt_nemotron import NemotronStreamingSTT
            # Check RAM before choosing, not inside connect(). An 8GB Codespace
            # cannot hold NeMo + a 2.4GB float32 model + Piper, and exceeding it
            # gets the whole process OOM-killed with a bare "Terminated" — during
            # a live call, mid-sentence. Degrading to Whisper is much better than
            # dying, so decide here where a fallback is still possible.
            avail = NemotronStreamingSTT._available_mb()
            need = NemotronStreamingSTT._NEEDED_MB
            if avail is not None and avail < need:
                log.error(
                    "nemotron needs ~%d MB but only %d MB is free — falling back "
                    "to Whisper rather than risking an OOM kill mid-call. Either "
                    "set STT_ENGINE=whisper to make this explicit, or rebuild the "
                    "Codespace on a 4-core/16GB machine.",
                    need, avail,
                )
            else:
                return NemotronStreamingSTT()
    elif want != "whisper":
        log.warning("unknown STT_ENGINE=%s; using whisper", want)
    return WhisperLocalSTT()


def make_llm(system_prompt: str) -> LLMProvider:
    name = _s().llm_provider.lower()
    if name == "anthropic":
        return AnthropicLLM(system_prompt, use_bedrock=False)
    if name == "bedrock":
        return AnthropicLLM(system_prompt, use_bedrock=True)
    if name == "openai_compatible":
        return OpenAICompatLLM(system_prompt)
    raise ValueError(f"unknown LLM_PROVIDER={name}")


def make_tts() -> TTSProvider:
    name = _s().tts_provider.lower()
    if name == "piper_local":
        return PiperLocalTTS()
    if name == "elevenlabs":
        return ElevenLabsTTS()
    raise ValueError(f"unknown TTS_PROVIDER={name}")


def describe_stack() -> dict[str, str]:
    """What is configured, and whether each stage leaves your network."""
    s = _s()
    stt = s.stt_provider.lower()
    llm = s.llm_provider.lower()
    tts = s.tts_provider.lower()

    # "whisper_local" alone stopped being the whole truth once local STT became
    # two engines chosen per language. Reporting it unqualified would hide the
    # single most important performance fact about a call -- whether that
    # language is streaming or doing 5.5-second batch decodes.
    from .languages import spec
    lang = spec(s.agent_language)
    want = s.stt_engine.lower()
    if want == "auto":
        want = ("deepgram" if s.stt_provider.lower() == "deepgram"
                else (lang.stt_engine if lang else "whisper"))
    if want == "deepgram" and not s.deepgram_api_key:
        stt = "deepgram wanted but DEEPGRAM_API_KEY unset -> self-hosted fallback"
    elif want == "deepgram":
        stt = f"deepgram {s.deepgram_model} language={lang.dg_lang if lang else 'en'}"
    else:
        import importlib.util
        have_nemo = importlib.util.find_spec("nemo") is not None
        if want == "nemotron" and have_nemo:
            stt = "nemotron_streaming"
        elif want == "nemotron":
            stt = "whisper_local (nemotron wanted, nemo_toolkit MISSING)"
        else:
            stt = "whisper_local"
        if lang and lang.stt_lang:
            stt += f", decoding {lang.code} as {lang.decode_lang}"
    external = {
        "deepgram": "caller audio -> Deepgram (US)",
        "whisper_local": "stays on your hardware",
        "nemotron_streaming": "stays on your hardware, streaming",
        "anthropic": "transcript -> Anthropic API",
        "bedrock": f"transcript -> AWS Bedrock ({_s().aws_region})",
        "openai_compatible": f"stays on your network ({_s().llm_base_url})",
        "elevenlabs": "agent script -> ElevenLabs (US)",
        "piper_local": "stays on your hardware",
    }
    # Look the residency note up from the BASE engine, not the decorated string.
    # Decorating stt with "(nemo_toolkit MISSING)" made the lookup miss and print
    # "?" where it should say the audio stays on your hardware -- and that column
    # is the whole point of this function for the data-residency question.
    stt_base = (
        "deepgram" if stt.startswith("deepgram")
        else "nemotron_streaming" if stt.startswith("nemotron")
        else "whisper_local" if stt.startswith("whisper_local")
        else stt
    )
    return {
        "stt": f"{stt} — {external.get(stt_base, '?')}",
        "llm": f"{llm} — {external.get(llm, '?')}",
        "tts": f"{tts} — {external.get(tts, '?')}",
    }
