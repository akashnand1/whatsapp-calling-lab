"""The conversation loop: STT -> LLM -> TTS, streaming end to end, with barge-in.

Providers are pluggable -- see providers.py. This module only orchestrates.

The single hardest constraint is that a human expects a first word within roughly
800 ms of finishing their sentence:

    STT endpointing         ~250 ms
    LLM time-to-first-token ~300 ms
    TTS time-to-first-byte  ~150 ms
    network + jitter        ~100 ms

So nothing here buffers a complete response. The LLM emits clause-sized chunks
and we start speaking chunk one while the model is still writing chunk two. If
you swap a provider, preserve that property or the agent will feel sluggish.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable

from .config import get_settings
from .providers import make_llm, make_stt, make_tts

log = logging.getLogger("ai")


def _fallback_line() -> str:
    """Said when a turn fails, so the caller never hears dead air."""
    if get_settings().agent_language.lower().startswith("hi"):
        return "एक सेकंड रुकिए, ज़रा दिक़्क़त आ गई थी। आप फिर से बताइए।"
    return "One moment, I had a small problem there. Please say that again."


FALLBACK_LINE = _fallback_line()


class Pipeline:
    """Owns the three stages and the turn-taking state machine.

    `on_audio(pcm, rate)` receives synthesised speech ready for the wire.
    `interrupt_playback()` flushes whatever is queued, for barge-in.
    """

    def __init__(
        self,
        system_prompt: str,
        on_audio: Callable[[bytes, int], None],
        interrupt_playback: Callable[[], None],
    ) -> None:
        self.stt = make_stt()
        self.llm = make_llm(system_prompt)
        self.tts = make_tts()

        # One trip state per call, with the tools bound to it. Milestone facts
        # are captured as they are confirmed rather than parsed out of prose
        # afterwards, so `trip.to_dict()` is a TMS-ready payload at any moment.
        from .milestones import TripState
        from .trip_tools import TOOLS, make_handlers
        self.trip = TripState()
        self.llm.register_tools(TOOLS, make_handlers(self.trip))
        self._on_audio = on_audio
        self._interrupt = interrupt_playback

        self._speaking = False
        self._speak_task: asyncio.Task | None = None
        # Bumped on every barge-in. A playback loop whose generation no longer
        # matches must stop pushing audio immediately -- see _speak().
        self._generation = 0
        self.transcript: list[tuple[str, str]] = []

        # Time-to-first-word per turn. This is the number that predicts whether
        # people find the agent tolerable, so measure it from day one.
        self.latencies_ms: list[int] = []
        self._turn_started: float | None = None

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    async def start(self) -> None:
        await self.stt.connect()

    async def close(self) -> None:
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        await self.stt.close()
        await self.tts.close()

    # -- speaking -----------------------------------------------------------

    async def say(self, text: str) -> None:
        """Speak a fixed line (greeting, transfer notice, goodbye).

        Goes through the same task machinery as a generated turn, because a
        caller talking over the *greeting* is one of the most common barge-ins
        there is -- and an unregistered task cannot be cancelled.
        """
        await self._launch(_once(text))

    async def handle_turn(self, user_text: str) -> None:
        """The human finished a turn. Think, then speak."""
        self.transcript.append(("user", user_text))
        log.info("user: %s", user_text)
        self._turn_started = time.monotonic()
        spoke = len(self.transcript)
        try:
            await self._launch(self.llm.respond(user_text))
            # A turn that completes without SAYING anything is the worst
            # outcome: the driver hears dead air and concludes the line dropped.
            # It happened on a real call -- one Anthropic request returned 200,
            # then nothing at all, no text, no tool call, no error. Silence is
            # never an acceptable answer, so say something.
            if len(self.transcript) == spoke:
                log.error(
                    "turn produced NO speech (no text, no tool call, no error) "
                    "— speaking the fallback so the caller is not left in silence"
                )
                await self._launch(_once(FALLBACK_LINE))
        except asyncio.CancelledError:
            # Log BEFORE re-raising. This path left no trace at all, so a
            # cancelled turn was indistinguishable from a turn that never
            # started -- which is exactly what made the dead-air call
            # impossible to diagnose.
            log.warning(
                "turn CANCELLED after %.1fs — superseded by a new turn, or the "
                "call ended", time.monotonic() - self._turn_started,
            )
            raise
        except Exception:
            # A failed LLM call must never leave the caller in silence. Before,
            # an API error killed the task and the driver just heard nothing --
            # on a real call that reads as a dropped line, and they hang up.
            log.exception("turn failed; speaking a fallback")
            try:
                await self._launch(_once(FALLBACK_LINE))
            except Exception:
                log.exception("fallback also failed")

    async def _launch(self, chunks: AsyncIterator[str]) -> None:
        """Run one playback stream as a cancellable task, superseding any prior."""
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        self._speak_task = asyncio.create_task(self._speak(chunks))
        try:
            await self._speak_task
        except asyncio.CancelledError:
            pass

    def barge_in(self) -> None:
        """The human started talking over us. Stop mid-sentence, immediately.

        This is the difference between an agent that feels human and one that is
        infuriating. Without it, the agent monologues over someone answering.

        Deliberately unconditional. `is_speaking` only tells you whether the LLM
        and TTS are still *generating* -- it goes False the moment the last chunk
        is synthesised, while seconds of audio may still be draining out of the
        track's queue. Guarding on it meant a caller interrupting the tail of a
        sentence got talked over. Callers gate on
        `pipeline.is_speaking or track.speaking`; flushing an already-empty queue
        is harmless and cheap.
        """
        was_generating = self._speaking
        log.info("barge-in (generating=%s)", was_generating)
        # Bump the generation FIRST. Cancellation only takes effect at the next
        # await point, so without this the in-flight TTS loop can push more audio
        # into the track after we have already flushed it -- and the agent starts
        # talking again a moment later.
        self._generation += 1
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        self._interrupt()
        self._set_speaking(False)
        if was_generating:
            # We never finished saying that turn, so drop it from history --
            # otherwise the model believes it said something the caller never heard.
            self.llm.cancel_last_turn()

    def _set_speaking(self, value: bool) -> None:
        """Keep the STT gate informed, so it can guard against our own echo.

        Without this the gate cannot distinguish the caller from the agent's own
        voice returning through a speakerphone -- the single most common failure
        mode when drivers do not use a headset.
        """
        self._speaking = value
        gate_owner = getattr(self.stt, "agent_speaking", None)
        if gate_owner is not None or hasattr(self.stt, "agent_speaking"):
            self.stt.agent_speaking = value  # type: ignore[attr-defined]

    async def _speak(self, chunks: AsyncIterator[str]) -> None:
        self._generation += 1
        gen = self._generation
        self._set_speaking(True)
        said: list[str] = []
        first = True
        try:
            async for chunk in chunks:
                if gen != self._generation:
                    return                      # superseded
                said.append(chunk)
                log.info("agent: %s", chunk)
                async for pcm in self.tts.stream(chunk):
                    if gen != self._generation:
                        # Stop generating too, not just playing. Otherwise we pay
                        # the TTS vendor for audio nobody will ever hear.
                        return
                    if first and self._turn_started is not None:
                        ms = int((time.monotonic() - self._turn_started) * 1000)
                        self.latencies_ms.append(ms)
                        log.info("time-to-first-word: %d ms", ms)
                        first = False
                    self._on_audio(pcm, self.tts.rate)
        except asyncio.CancelledError:
            log.info("playback cancelled after: %s", " ".join(said)[:120])
            raise
        finally:
            if gen == self._generation:
                self._set_speaking(False)
                self._turn_started = None
            if said:
                self.transcript.append(("agent", " ".join(said)))

    # -- driving from STT ---------------------------------------------------

    async def run_stt_loop(self) -> None:
        """Turn STT events into agent turns. Run as a background task."""
        from .vad import looks_degenerate, looks_like_echo

        pending = ""
        async for kind, text in self.stt.events():
            if kind == "final":
                # Drop repetition loops before they can accumulate into `pending`.
                if looks_degenerate(text):
                    continue
                pending = f"{pending} {text}".strip()
            elif kind == "utterance_end" and pending:
                turn, pending = pending, ""

                # Last line of defence against a speakerphone. If acoustic gating
                # let our own voice through, the transcript still gives it away by
                # matching what we just said. Acoustic methods cannot make this
                # check; only the text can.
                recent = [t for who, t in self.transcript[-4:] if who == "agent"]
                if looks_like_echo(turn, recent):
                    continue

                # Tell the model how far to trust what it just "heard".
                #
                # Confirming every answer wasted a fifth of the first real call;
                # confirming none of them would silently record a mis-heard time
                # as fact, which is worse -- a wrong timestamp in the TMS looks
                # exactly as authoritative as a right one. The decision needs
                # evidence, and the STT engine is the only thing that has any.
                if getattr(self.stt, "last_unclear", False):
                    turn = f"{turn}\n[transcript confidence: LOW — confirm before recording]"

                asyncio.create_task(self.handle_turn(turn))

    def stats(self) -> dict[str, object]:
        lat = self.latencies_ms
        return {
            "turns": len([1 for who, _ in self.transcript if who == "user"]),
            "ttfw_ms": {
                "count": len(lat),
                "avg": round(sum(lat) / len(lat)) if lat else None,
                "min": min(lat) if lat else None,
                "max": max(lat) if lat else None,
            },
            "trip": self.trip.to_dict(),
            "cost": self._cost(),
        }

    def _cost(self) -> dict[str, object]:
        """Actual LLM spend for this call, from the API's own token counts.

        Reported per call because that is the unit the business cares about, and
        because an estimate built from character counts was out by an unknown
        factor -- Devanagari tokenises far worse than Latin and the ratio is not
        something to guess at.
        """
        from .config import get_settings
        from .providers import estimate_cost_usd

        llm = self.llm
        tin = getattr(llm, "tokens_in", 0)
        tout = getattr(llm, "tokens_out", 0)
        cr = getattr(llm, "tokens_cache_read", 0)
        cw = getattr(llm, "tokens_cache_write", 0)
        model = get_settings().anthropic_model
        usd = estimate_cost_usd(model, tin, tout, cr, cw)
        out: dict[str, object] = {
            "model": model,
            "api_calls": getattr(llm, "api_calls", 0),
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_read": cr,
            "cache_write": cw,
            "llm_usd": round(usd, 4) if usd is not None else None,
        }
        if cr == 0 and tin > 20000:
            # Worth flagging: the system prompt and tool schemas are resent in
            # full on every turn, and they are the bulk of the input. Caching
            # them costs 1.25x once and 0.1x thereafter.
            out["hint"] = (
                "no cache reads — enabling prompt caching on the system prompt "
                "and tools would cut input cost by roughly half"
            )
        return out

    def transcript_text(self) -> str:
        return "\n".join(f"{who}: {what}" for who, what in self.transcript)


async def _once(text: str) -> AsyncIterator[str]:
    yield text
