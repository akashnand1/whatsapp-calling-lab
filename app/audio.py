"""Audio plumbing between aiortc and the AI pipeline.

Two directions:

  outbound  TTS PCM (24 kHz) -> resample to 48 kHz -> 20 ms frames -> aiortc
  inbound   aiortc frames (48 kHz) -> resample to 16 kHz -> STT websocket

The outbound track emits silence whenever its queue is empty. That is deliberate
and important: Meta warns that WhatsApp "might not always send the first RTP
media packet", so if we waited for their audio before sending ours we would
deadlock with both sides listening.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time

import av
import numpy as np
from aiortc import MediaStreamTrack
from av import AudioFrame

log = logging.getLogger("audio")

WIRE_RATE = 48000          # Opus on the wire. Mandated by Meta.
FRAME_MS = 20              # Mandated ptime.
SAMPLES_PER_FRAME = WIRE_RATE * FRAME_MS // 1000   # 960
STT_RATE = 16000           # What Deepgram wants
TTS_RATE = 24000           # What ElevenLabs gives us


class OutboundAudioTrack(MediaStreamTrack):
    """A single-SSRC audio track fed from an asyncio queue.

    Meta requires exactly one audio SSRC, so there must be exactly one instance
    of this per call. Do not add a second audio track for hold music or DTMF --
    mix into this one instead.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buf = np.zeros(0, dtype=np.int16)
        self._pts = 0
        self._start: float | None = None
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=WIRE_RATE
        )
        self.speaking = False

    def push_pcm(self, pcm: bytes, rate: int = TTS_RATE) -> None:
        """Queue mono int16 PCM for playback, resampling to the wire rate."""
        if rate != WIRE_RATE:
            pcm = resample_pcm(pcm, rate, WIRE_RATE)
        self._queue.put_nowait(pcm)

    @property
    def is_playing(self) -> bool:
        """True while audio is being emitted OR still queued.

        `speaking` alone is not enough: it reflects the frame just sent. Queued
        audio that has not yet been emitted is still going to come out of the
        caller's speaker, and is therefore still going to echo back. Anything
        guarding against echo must consult this, not the generation state.
        """
        return self.speaking or not self._queue.empty() or len(self._buf) > 0

    def interrupt(self) -> None:
        """Barge-in: drop everything queued and stop mid-sentence.

        This is what makes an agent feel human rather than infuriating. Without
        it, the agent keeps monologuing over someone trying to answer.
        """
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        self._buf = np.zeros(0, dtype=np.int16)
        self.speaking = False
        if dropped:
            log.info("barge-in: dropped %d queued chunks", dropped)

    async def recv(self) -> AudioFrame:
        # Pace output to real time. aiortc calls recv() as fast as we allow, so
        # without this we would fire audio far faster than 50 packets/sec.
        if self._start is None:
            self._start = time.monotonic()
        target = self._start + (self._pts / WIRE_RATE)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        # Top up the buffer from the queue without ever blocking. Silence is a
        # perfectly good thing to send.
        while len(self._buf) < SAMPLES_PER_FRAME and not self._queue.empty():
            chunk = self._queue.get_nowait()
            self._buf = np.concatenate(
                [self._buf, np.frombuffer(chunk, dtype=np.int16)]
            )

        if len(self._buf) >= SAMPLES_PER_FRAME:
            samples = self._buf[:SAMPLES_PER_FRAME]
            self._buf = self._buf[SAMPLES_PER_FRAME:]
            self.speaking = True
        else:
            # Pad the tail, then go quiet.
            samples = np.zeros(SAMPLES_PER_FRAME, dtype=np.int16)
            if len(self._buf):
                samples[: len(self._buf)] = self._buf
                self._buf = np.zeros(0, dtype=np.int16)
            self.speaking = False

        frame = AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = WIRE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, WIRE_RATE)
        self._pts += SAMPLES_PER_FRAME
        return frame


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resample of mono int16 PCM.

    Good enough for speech. If you care about quality, swap in soxr or
    scipy.signal.resample_poly -- but note that a better resampler costs latency,
    and on a voice call latency is the scarcer resource.
    """
    if src_rate == dst_rate:
        return pcm
    src = np.frombuffer(pcm, dtype=np.int16)
    if src.size == 0:
        return pcm
    n_dst = int(round(src.size * dst_rate / src_rate))
    x_src = np.linspace(0, 1, src.size, endpoint=False)
    x_dst = np.linspace(0, 1, n_dst, endpoint=False)
    return np.interp(x_dst, x_src, src).astype(np.int16).tobytes()


class InboundResampler:
    """Converts inbound WebRTC frames to mono 16 kHz int16 PCM for the STT stage.

    This must use av's own resampler rather than hand-rolled numpy, because of a
    subtlety that silently destroys the audio:

    av returns PACKED (interleaved) audio as shape (1, nb_samples * channels).
    So a stereo frame looks one-dimensional, and a naive `shape[0] > 1` channel
    check never fires. The L,R,L,R samples then get treated as consecutive mono
    samples -- doubling the apparent duration and mangling the waveform. Whisper
    responds by emitting phonetically-plausible gibberish, which is maddening to
    debug because transcription *appears* to be working.

    AudioResampler handles channel layout, sample format and rate correctly in
    one step. It is stateful, so keep one instance per inbound track.
    """

    def __init__(self, dst_rate: int = STT_RATE) -> None:
        self._dst_rate = dst_rate
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=dst_rate
        )
        self._logged = False

    def to_pcm16(self, frame: AudioFrame) -> bytes:
        if not self._logged:
            log.info(
                "inbound audio: %d Hz, layout=%s, format=%s -> mono %d Hz",
                frame.sample_rate,
                getattr(frame.layout, "name", "?"),
                getattr(frame.format, "name", "?"),
                self._dst_rate,
            )
            self._logged = True

        out = bytearray()
        for resampled in self._resampler.resample(frame):
            arr = resampled.to_ndarray()
            out.extend(arr.astype(np.int16).tobytes())
        return bytes(out)


def frame_to_pcm16(frame: AudioFrame, dst_rate: int = STT_RATE) -> bytes:
    """Deprecated single-frame helper.

    Kept only so older call sites do not break. Prefer InboundResampler: a fresh
    resampler per frame loses the internal state needed for correct rate
    conversion across frame boundaries.
    """
    return InboundResampler(dst_rate).to_pcm16(frame)


class EnergyVAD:
    """Dead-simple energy-based voice activity detector, used only for barge-in.

    This is not good enough to decide when a *turn* has ended -- for that, rely
    on the STT provider's endpointing, which is far better. All this needs to do
    is answer "is the human making noise right now", fast and cheaply, so we can
    stop talking over them.

    If you get false barge-ins on a noisy truck cab, raise `threshold` or
    increase `frames_required`.
    """

    def __init__(self, threshold: int = 900, frames_required: int = 3) -> None:
        self.threshold = threshold
        self.frames_required = frames_required
        self._hits = 0

    def is_speech(self, pcm: bytes) -> bool:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        if rms > self.threshold:
            self._hits += 1
        else:
            self._hits = 0
        return self._hits >= self.frames_required

    def reset(self) -> None:
        self._hits = 0
