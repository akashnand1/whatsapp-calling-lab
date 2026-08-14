"""Deciding when the caller is actually speaking.

Two field conditions break a naive energy threshold, and both are unavoidable
for drivers:

1. SPEAKERPHONE. Drivers hold the phone on speaker, so the agent's own voice
   comes back through the mic. The handset's echo canceller helps but never
   fully succeeds at speaker volume in a cab. Residual echo trips an energy VAD,
   the agent barges in on itself, cuts its own sentence, then transcribes its own
   words as if the caller had said them.

2. ROAD NOISE. Horns, engines, crowds. A horn is far louder than speech, so any
   fixed threshold either ignores it (and misses quiet speech) or fires on it
   (and transcribes noise).

The answer is three layers, cheapest first:

    energy      instant, rejects silence, adapts to the ambient floor
    echo guard  raises the bar while the agent is talking
    Silero VAD  decides speech vs. noise on the buffered utterance

Silero is what actually separates a horn from a voice: it is a small neural VAD
trained on speech, so loudness alone does not fool it. It is too slow to run per
20 ms frame on a laptop alongside Whisper, so it runs once per utterance as a
verification gate. Energy handles the per-frame work.
"""

from __future__ import annotations

import logging
import re

import numpy as np

log = logging.getLogger("vad")

STT_RATE = 16000


class SpeechGate:
    """Per-frame speech detection with an adaptive floor and echo suppression.

    Feed it 20 ms frames of 16 kHz mono PCM along with whether the agent is
    currently speaking.
    """

    def __init__(
        self,
        min_threshold: int = 350,
        floor_ratio: float = 3.0,
        echo_multiplier: float = 4.0,
        frames_required: int = 4,
        echo_frames_required: int = 12,
        # Long enough for the echo estimate to converge before we start judging.
        grace_ms: int = 700,
    ) -> None:
        # Absolute lower bound, so a silent room cannot drive the threshold to 0.
        self._min_threshold = min_threshold
        # Speech must exceed this multiple of the ambient noise floor.
        self._floor_ratio = floor_ratio
        # While the agent talks, demand this much more. Residual echo is
        # attenuated by the handset's canceller, so real speech still gets
        # through -- but our own leaked voice does not.
        self._echo_multiplier = echo_multiplier
        # Sustained frames needed. Longer while the agent speaks, because a
        # single echo blip should never cut it off, but a determined interruption
        # (~240 ms of speech) still will.
        self._frames_required = frames_required
        self._echo_frames_required = echo_frames_required
        # Ignore the first moments after the agent starts: that is when its own
        # onset is loudest in the mic.
        self._grace_frames = grace_ms // 20
        # Keep guarding for a moment AFTER the agent's audio stops. Echo arrives
        # with acoustic and buffering delay, so the tail of a sentence is still
        # arriving at the mic when playback has already finished locally.
        self._tail_frames = 20          # 400 ms
        self._tail = 0
        # Fail-safe: if the guard somehow never lifts, drop it after this long.
        # Being deaf to the caller is a worse failure than clipping our own echo.
        self._guard_run = 0
        # 5 s of guarding with ZERO frames emitted. Now that the counter resets
        # on real progress this measures genuine wedging, so it can be short --
        # the old 30 s was long only because it was racing the read-back.
        self._max_guard_frames = 250     # 5 s
        self._last_playback = -1

        self._floor = 200.0          # running estimate of ambient noise
        # Running estimate of how loud our OWN voice comes back through the
        # caller's speaker. A fixed multiplier cannot work here: echo level
        # depends entirely on their speaker volume, phone model and how close the
        # mic is -- observed anywhere from inaudible (headset) to rms 3000+
        # (speakerphone at full volume). So measure it instead of guessing, and
        # require real speech to exceed the measured echo by a margin.
        self._echo_level = 0.0
        self._echo_margin = 2.0
        self._hits = 0
        self._agent_frames = 0
        self._was_agent_speaking = False
        self.peak = 0.0

    @property
    def threshold(self) -> float:
        return max(self._min_threshold, self._floor * self._floor_ratio)

    def update(
        self,
        pcm16: bytes,
        agent_speaking: bool = False,
        playback_frames: int | None = None,
    ) -> bool:
        """Return True when the caller is judged to be speaking."""
        samples = np.frombuffer(pcm16, dtype=np.int16)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        self.peak = max(self.peak, rms)

        # Reset the grace window each time the agent starts a new utterance.
        if agent_speaking and not self._was_agent_speaking:
            self._agent_frames = 0
        if not agent_speaking and self._was_agent_speaking:
            self._tail = self._tail_frames      # audio just stopped; keep guarding
        self._was_agent_speaking = agent_speaking

        if agent_speaking:
            self._agent_frames += 1
        elif self._tail > 0:
            self._tail -= 1

        guarding = agent_speaking or self._tail > 0

        # FAIL-SAFE. If the playback signal ever sticks on -- a track that stops
        # being drained, a session that half-tore-down -- guarding would never
        # lift and the agent would be permanently deaf. Being unable to hear the
        # caller is a worse failure than occasionally clipping our own echo, so
        # after a sustained period force the guard off.
        if guarding:
            # Reset the stuck-detector whenever playback actually ADVANCES.
            #
            # The previous version timed out purely on elapsed guard time, which
            # cannot distinguish "wedged" from "still talking". The milestone
            # read-back is a legitimate ~30 s monologue, so a 30 s timeout fired
            # in the middle of it and switched off echo protection while the
            # agent was mid-sentence -- the opposite of what the fail-safe is
            # for. Counting emitted frames tells the two apart exactly: a wedged
            # track emits nothing, a long one keeps emitting.
            if playback_frames is not None and playback_frames != self._last_playback:
                self._last_playback = playback_frames
                self._guard_run = 0
            self._guard_run += 1
            if self._guard_run > self._max_guard_frames:
                if self._guard_run == self._max_guard_frames + 1:
                    log.warning(
                        "echo guard on for %ds with NO audio emitted — playback "
                        "really is stuck; releasing the guard so the caller can "
                        "be heard",
                        self._max_guard_frames * 20 // 1000,
                    )
                guarding = False
        else:
            self._guard_run = 0
            # Let the echo estimate decay while nothing of ours is playing.
            # Without this a single loud burst raises the bar permanently: the
            # next utterance is judged against an echo level measured minutes ago.
            self._echo_level *= 0.995

        thresh = self.threshold
        needed = self._frames_required
        if guarding:
            in_grace = agent_speaking and self._agent_frames < self._grace_frames
            # Two independent bars, whichever is higher:
            #   the ambient-derived threshold (handles road noise), and
            #   twice the MEASURED echo (handles speakerphone).
            #
            # Note there is deliberately no fixed multiplier on the ambient term.
            # Multiplying it compounded with the noise adaptation: in a noisy cab
            # the base threshold is already ~1800, and x4 put it at ~7100, which
            # silenced a headset user whose real echo was only 150. Measuring the
            # echo makes that multiplier both redundant and harmful.
            candidate = max(thresh, self._echo_level * self._echo_margin)

            if in_grace:
                # Bootstrap. Learn unconditionally, because until the estimate
                # converges every threshold is a guess -- and a loud echo above
                # that guess would never be learned, deadlocking the estimate at
                # zero. Safe to do here: interruptions in the first 700 ms are
                # ignored anyway.
                self._echo_level = 0.75 * self._echo_level + 0.25 * rms
                self._hits = 0
                return False

            # Converged. Now only learn from frames that look like echo rather
            # than speech, or the estimate chases the caller's own voice upward
            # until genuine interruptions can never beat it.
            if rms <= candidate:
                self._echo_level = 0.9 * self._echo_level + 0.1 * rms

            thresh = candidate
            needed = self._echo_frames_required

        if rms > thresh:
            self._hits += 1
        else:
            self._hits = 0
            # Track the ambient floor only on quiet frames, and only when nothing
            # of ours is playing -- otherwise our own audio inflates the estimate
            # and the threshold drifts upward until real speech is ignored.
            if not guarding:
                self._floor = 0.98 * self._floor + 0.02 * rms

        return self._hits >= needed

    def reset(self) -> None:
        self._hits = 0

    def describe(self) -> str:
        return (
            f"floor={self._floor:.0f} threshold={self.threshold:.0f} "
            f"echo={self._echo_level:.0f} peak={self.peak:.0f}"
        )


class SileroGate:
    """Verifies a buffered utterance actually contains speech, and trims it.

    This is the layer that rejects horns. Energy cannot tell a horn from a shout;
    Silero can, because it models speech rather than loudness.

    Also trims leading and trailing noise so Whisper transcribes only the speech,
    which improves accuracy and cuts inference time.
    """

    # threshold 0.5 is Silero's default, tuned for clean audio. On telephone-band
    # speech through a phone mic it rejected genuine short answers ("हाँ जी") as
    # noise. Lowered, because the energy gate has already established that
    # something loud happened -- Silero's job here is only to reject clearly
    # non-speech sounds like horns, not to adjudicate marginal speech.
    def __init__(self, threshold: float = 0.35, min_speech_ms: int = 150) -> None:
        self._threshold = threshold
        self._min_speech_ms = min_speech_ms
        self._opts = None

    def _options(self):
        if self._opts is None:
            from faster_whisper.vad import VadOptions
            self._opts = VadOptions(
                threshold=self._threshold,
                min_speech_duration_ms=self._min_speech_ms,
                # Do not split on natural pauses -- a driver pausing to think is
                # still the same turn.
                min_silence_duration_ms=800,
                speech_pad_ms=200,
            )
        return self._opts

    def verify(self, audio: np.ndarray) -> tuple[bool, np.ndarray, str]:
        """Return (contains_speech, trimmed_audio, reason).

        `audio` is float32 mono at 16 kHz, already normalised.
        """
        try:
            from faster_whisper.vad import get_speech_timestamps
            spans = get_speech_timestamps(audio, self._options(), sampling_rate=STT_RATE)
        except Exception as e:
            # Never block the pipeline on a VAD failure; assume speech.
            log.warning("silero VAD unavailable (%s) — passing audio through", e)
            return True, audio, "vad-unavailable"

        if not spans:
            return False, audio, "no speech detected (noise only)"

        # Never trim a short utterance. A one-word answer -- "haan", "bhej diya"
        # -- is under a second to begin with, and Silero was cutting it to 0.4s,
        # which removes the onset and leaves Whisper almost nothing to decode.
        # Trimming exists to drop long stretches of road noise, not to shave
        # syllables off a yes.
        total_s = len(audio) / STT_RATE
        if total_s < 2.5:
            return True, audio, f"kept all {total_s:.1f}s (short utterance, not trimmed)"

        start = spans[0]["start"]
        end = spans[-1]["end"]
        # Keep a generous margin either side even on longer clips.
        pad = int(0.25 * STT_RATE)
        start = max(0, start - pad)
        end = min(len(audio), end + pad)
        trimmed = audio[start:end]
        kept = len(trimmed) / STT_RATE
        total = len(audio) / STT_RATE
        reason = f"kept {kept:.1f}s of {total:.1f}s across {len(spans)} span(s)"
        return True, trimmed, reason


# ---------------------------------------------------------------------------
# Echo detection on the transcript
# ---------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


# Function words carry no signal about echo: a caller answering a question uses
# the same particles the question did. Comparing them produces false positives.
_STOPWORDS = {
    # Hindi
    "मैं", "है", "हैं", "हूँ", "हूं", "का", "की", "के", "को", "से", "में", "पर",
    "और", "या", "यह", "वह", "आप", "आपका", "आपकी", "जी", "था", "थी", "थे", "हो",
    "कर", "करना", "क्या", "नहीं", "तो", "भी", "अभी", "कि", "एक", "बस",
    # English
    "the", "a", "an", "is", "are", "was", "were", "i", "you", "your", "my",
    "it", "to", "of", "and", "or", "in", "on", "at", "for", "yes", "no", "ok",
}


def _content_tokens(s: str) -> list[str]:
    return [w for w in _tokens(s) if w not in _STOPWORDS]


def looks_degenerate(text: str) -> bool:
    """True if a transcript is a repetition loop rather than speech.

    Whisper's signature failure on short, noisy, accented audio is emitting one
    token over and over -- "बजे बजे बजे बजे ..." for an entire turn. The decoder
    settings guard against it, but this is the last check before the text reaches
    the LLM, because a degenerate transcript wastes a turn and confuses the model
    far more than simply saying "I didn't catch that".

    Judged on the ratio of unique words to total, which is scale-free and works
    the same in Hindi and English.
    """
    words = _tokens(text)
    if len(words) < 6:
        return False                       # too short to judge

    unique_ratio = len(set(words)) / len(words)
    if unique_ratio < 0.3:
        log.info(
            "discarding degenerate transcript (%d words, only %d unique)",
            len(words), len(set(words)),
        )
        return True

    # Also catch one token dominating even when others are present.
    most_common = max((words.count(w) for w in set(words)), default=0)
    if most_common / len(words) > 0.5:
        log.info(
            "discarding degenerate transcript (one word is %.0f%% of it)",
            most_common / len(words) * 100,
        )
        return True
    return False


def looks_like_echo(
    transcript: str, recent_agent_text: list[str], threshold: float = 0.78
) -> bool:
    """True if the transcript is probably the agent's own voice coming back.

    Compares word ORDER, not just vocabulary. That distinction matters more than
    it sounds: a driver answering "पिकअप समय" question naturally reuses the words
    "पिकअप" and "समय", so bag-of-words overlap hits 50-60% on entirely legitimate
    replies -- which was silently discarding real answers. Echo, by contrast,
    reproduces the agent's words in the same SEQUENCE.

    Deliberately conservative. Wrongly dropping a caller's answer is far worse
    than letting one echo through: the acoustic gate and Silero already handle
    the common cases, and an echo that slips past merely produces one confused
    turn, whereas a discarded answer makes the agent look broken.
    """
    from difflib import SequenceMatcher

    t = _content_tokens(transcript)
    if len(t) < 5:                      # too short to judge safely
        return False

    for said in recent_agent_text[-3:]:
        s = _content_tokens(said)
        if len(s) < 5:
            continue
        ratio = SequenceMatcher(None, t, s).ratio()
        if ratio >= threshold:
            log.info(
                "discarding likely echo (%.0f%% sequence match with what the agent said)",
                ratio * 100,
            )
            return True
    return False
