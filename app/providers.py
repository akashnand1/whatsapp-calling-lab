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
    return 1400 if _s().agent_language.lower()[:2] in ("hi", "ar") else 800


# ===========================================================================
# Interfaces
# ===========================================================================

class STTProvider(ABC):
    """Streaming speech-to-text. Consumes 16 kHz mono int16 PCM."""

    # Set by the pipeline so the provider can guard against the agent's own voice
    # returning through a speakerphone.
    agent_speaking: bool = False

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

    URL = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-2-general&encoding=linear16&sample_rate=16000&channels=1"
        "&punctuate=true&interim_results=true&endpointing=250"
        "&utterance_end_ms=1000&vad_events=true&language=multi"
    )

    def __init__(self) -> None:
        self._key = get_settings().deepgram_api_key
        self._ws = None
        self._closed = False

    async def connect(self) -> None:
        import websockets
        self._ws = await websockets.connect(
            self.URL, additional_headers={"Authorization": f"Token {self._key}"}
        )
        log.info("STT: deepgram connected")

    async def send_audio(self, pcm16: bytes) -> None:
        if self._ws and not self._closed:
            try:
                await self._ws.send(pcm16)
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
        silence_ms: int = 1100,
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
        self._language = None if code.startswith("auto") else (code[:2] or None)

        from .config import STT_HINT
        self._hint = STT_HINT
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
        speaking = self._gate.update(pcm16, agent_speaking=self.agent_speaking)
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
                    initial_prompt=(self._hint if secs >= 1.5 else None),
                    # DO NOT pin temperature to 0. The default is a fallback
                    # ladder [0.0 .. 1.0]: Whisper decodes greedily first, and
                    # when compression_ratio_threshold detects degenerate output
                    # it RE-DECODES at a higher temperature to escape. Pinning 0
                    # removes that escape and the decoder gets stuck emitting one
                    # token forever -- "बजे बजे बजे बजे ..." for an entire turn.
                    temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
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
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception:
            log.exception("whisper transcribe failed")
            return

        took = time.monotonic() - t0
        log.info(
            "utterance #%d: %.1fs audio transcribed in %.1fs (%.1fx real time)",
            seq, secs, took, secs / took if took else 0,
        )
        if text:
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
        for _ in range(6):                       # bound the tool loop
            buf, said = "", []
            tool_calls: list[dict] = []

            async with self._client.messages.stream(
                model=self._model,
                max_tokens=_spoken_turn_tokens(),
                system=self.system,
                messages=self.history,
                tools=self._tools or [],
            ) as stream:
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

            if buf.strip():
                said.append(buf.strip())
                yield buf.strip()

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
                log.warning("model returned no usable content — ending turn")
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

            results = []
            for tc in tool_calls:
                out = self._run_tool(tc.name, tc.input or {})
                results.append(
                    {"type": "tool_result", "tool_use_id": tc.id, "content": out}
                )
            self.history.append({"role": "user", "content": results})

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

        # Loaded lazily on first use, then reused for the life of the process.
        self._loaded = None
        self._lock = asyncio.Lock()

        log.info(
            "TTS: piper (local) model=%s rate=%d",
            os.path.basename(self._model or "?"), self.rate,
        )

    async def _voice(self):
        """Load the ONNX model once and keep it.

        The obvious implementation shells out to the `piper` binary per call. Do
        not: the model is re-loaded on every invocation, and because the LLM emits
        clause-sized chunks a single reply pays that cost several times. Measured
        at ~2.5s for the first chunk. Holding the model in-process drops it to
        tens of milliseconds.
        """
        if self._loaded is not None:
            return self._loaded
        async with self._lock:
            if self._loaded is None:                 # re-check inside the lock
                from piper import PiperVoice
                t0 = time.monotonic()
                self._loaded = await asyncio.to_thread(PiperVoice.load, self._model)
                try:
                    self.rate = int(self._loaded.config.sample_rate)
                except Exception:
                    pass
                log.info(
                    "piper model loaded in %.0f ms (rate=%d) — cached for the process",
                    (time.monotonic() - t0) * 1000, self.rate,
                )
        return self._loaded

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
    name = _s().stt_provider.lower()
    if name == "whisper_local":
        return WhisperLocalSTT()
    if name == "deepgram":
        return DeepgramSTT()
    raise ValueError(f"unknown STT_PROVIDER={name}")


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
    stt = _s().stt_provider.lower()
    llm = _s().llm_provider.lower()
    tts = _s().tts_provider.lower()
    external = {
        "deepgram": "caller audio -> Deepgram (US)",
        "whisper_local": "stays on your hardware",
        "anthropic": "transcript -> Anthropic API",
        "bedrock": f"transcript -> AWS Bedrock ({_s().aws_region})",
        "openai_compatible": f"stays on your network ({_s().llm_base_url})",
        "elevenlabs": "agent script -> ElevenLabs (US)",
        "piper_local": "stays on your hardware",
    }
    return {
        "stt": f"{stt} — {external.get(stt, '?')}",
        "llm": f"{llm} — {external.get(llm, '?')}",
        "tts": f"{tts} — {external.get(tts, '?')}",
    }
