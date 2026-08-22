"""One CallSession per call: owns the WebRTC peer connection and the AI pipeline.

Lifecycle for an outbound (business-initiated) call:

    1  create_offer()          build SDP offer, gather ICE candidates
    2  <POST /calls>           send offer to Meta via Graph API
    3  accept_answer(sdp)      apply Meta's SDP answer -> ICE -> DTLS -> SRTP
    4  on_accepted()           user picked up. ONLY NOW start the agent.
    5  hangup()                terminate, tear down, report

Step 4 matters more than it looks. If you start the agent on the API's success
response, or on RINGING, the agent delivers its greeting to a ringing phone and
the human answers midway through a sentence. Wait for ACCEPTED.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from . import sdp as sdp_utils
from .audio import InboundResampler, OutboundAudioTrack
from .config import GREETING, INBOUND_GREETING, SYSTEM_PROMPT, get_settings
from .ai import Pipeline

log = logging.getLogger("session")


def _turn_rest_credentials(secret: str, ttl_seconds: int = 8 * 3600) -> tuple[str, str]:
    """Derive time-limited TURN credentials from a shared secret.

    This is the TURN REST API scheme (RFC 5766 / draft-uberti-behave-turn-rest):

        username   = "<unix-expiry>:<any-name>"
        credential = base64( HMAC-SHA1( secret, username ) )

    It matters here because it means no signup: services that publish a static
    auth secret can be used by computing credentials locally, rather than
    registering for an API key and fetching them over HTTP.
    """
    import base64
    import hashlib
    import hmac
    import time

    expiry = int(time.time()) + ttl_seconds
    username = f"{expiry}:trukker-lab"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def _ice_config() -> RTCConfiguration:
    s = get_settings()
    servers: list[RTCIceServer] = []

    if s.stun_server:
        servers.append(RTCIceServer(urls=[s.stun_server]))

    # Static-auth TURN: TURN_STATIC_AUTH=host,secret
    # Credentials are computed locally, so no account is required.
    if s.turn_static_auth:
        parts = [p.strip() for p in s.turn_static_auth.split(",")]
        if len(parts) == 2:
            host, secret = parts
            user, cred = _turn_rest_credentials(secret)

            # ORDER MATTERS, and not for the reason you would expect: aiortc
            # supports exactly ONE turn server. Its connection_kwargs() takes the
            # first URL and `continue`s past every other. So a list is not a
            # fallback chain -- only entry [0] is ever used.
            #
            # Listing UDP first therefore meant aiortc tried TURN over UDP to
            # port 80, which home routers and ISPs commonly filter. Allocation
            # failed silently, no relay candidate was gathered, and the call rang
            # then sat mute. TCP/443 is the transport that survives hostile
            # networks, so it goes first.
            transport = s.turn_transport.lower() or "tcp443"
            first = {
                "tcp443": f"turn:{host}:443?transport=tcp",
                "tcp80": f"turn:{host}:80?transport=tcp",
                "udp443": f"turn:{host}:443",
                "udp80": f"turn:{host}:80",
            }.get(transport, f"turn:{host}:443?transport=tcp")

            servers.append(RTCIceServer(urls=[first], username=user, credential=cred))
            log.info(
                "TURN (static auth) %s  [aiortc uses only one URL; "
                "set TURN_TRANSPORT to try udp80/udp443/tcp80]", first,
            )
        else:
            log.warning("TURN_STATIC_AUTH malformed; expected 'host,secret'")

    # Explicit TURN: TURN_SERVER=urls,user,credential   (';' separates entries)
    if s.turn_server:
        for entry in s.turn_server.split(";"):
            parts = [p.strip() for p in entry.split(",")]
            if len(parts) == 3 and parts[0]:
                servers.append(
                    RTCIceServer(urls=[parts[0]], username=parts[1], credential=parts[2])
                )
                log.info("TURN via %s", parts[0])
            elif entry.strip():
                log.warning("TURN_SERVER entry malformed: %r", entry)

    if not servers:
        log.info("no STUN/TURN configured — relying on host candidates only")
    return RTCConfiguration(iceServers=servers)


def _force_ice_controlling(pc: RTCPeerConnection) -> None:
    """Make us the ICE CONTROLLING agent even when answering.

    Why this is necessary: Meta's VoIP stack is ICE-LITE, and per RFC 5245 a lite
    implementation always takes the CONTROLLED role. Standard WebRTC makes the
    *answerer* controlled too -- so on an inbound call both sides would be
    CONTROLLED, no one would nominate a candidate pair, and ICE would hang.

    This reaches into aiortc internals, so it is written defensively: if the
    attribute layout changes, we log and carry on rather than crash. If you see
    the warning and inbound calls connect but stay silent, this is the first
    thing to look at.
    """
    try:
        for transceiver in pc.getTransceivers():
            ice = transceiver.sender.transport.transport   # RTCIceTransport
            conn = ice._connection                        # aioice Connection
            if conn.ice_controlling:
                log.info("ICE role already CONTROLLING")
            else:
                conn.ice_controlling = True
                # aiortc sets the role once and guards it with _role_set; pin it
                # so setRemoteDescription/start does not flip us back.
                ice._role_set = True
                log.info("forced ICE role to CONTROLLING (Meta is ICE-LITE)")
            return
    except Exception as e:
        log.warning(
            "could not force ICE CONTROLLING role (%s: %s). Inbound ICE may stall "
            "because Meta is ICE-LITE and also takes CONTROLLED.",
            type(e).__name__,
            e,
        )


@dataclass
class CallSession:
    to: str
    call_id: str | None = None
    direction: str = "BUSINESS_INITIATED"
    correlation_id: str = ""

    pc: RTCPeerConnection | None = None
    track: OutboundAudioTrack | None = None
    pipeline: Pipeline | None = None

    accepted: bool = False
    started_at: float | None = None
    ended_at: float | None = None
    _tasks: list[asyncio.Task] = field(default_factory=list)

    # -- setup --------------------------------------------------------------

    async def create_offer(self) -> str:
        """Build the SDP offer. We are the offerer, which makes us ICE
        CONTROLLING -- exactly the role Meta requires, since their stack only
        ever takes CONTROLLED."""
        s = get_settings()
        self.pc = RTCPeerConnection(configuration=_ice_config())

        # Exactly ONE audio track. Meta's relay rewrites all business audio to a
        # single fixed SSRC, and the WhatsApp client handles one source only.
        self.track = OutboundAudioTrack()
        self.pc.addTrack(self.track)

        @self.pc.on("track")
        def _on_track(track):  # noqa: ANN001
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._consume_inbound(track)))

        @self.pc.on("connectionstatechange")
        async def _on_state():
            log.info("pc state=%s", self.pc.connectionState if self.pc else "?")

        @self.pc.on("iceconnectionstatechange")
        async def _on_ice():
            log.info("ice state=%s", self.pc.iceConnectionState if self.pc else "?")

        # aiortc offers G722 by default, which Meta does NOT support. Strip it
        # before creating the offer or the call can fail on codec negotiation.
        sdp_utils.restrict_codecs(self.pc, allow_g711=False)

        offer = await self.pc.createOffer()

        # setLocalDescription performs ICE gathering, which contacts the TURN
        # server. A bad secret or an unreachable relay raises here and the whole
        # call dies. Falling back to host candidates at least lets the phone ring
        # -- and a ringing-but-silent call is a far more informative failure than
        # a 500, because it isolates the problem to the relay.
        try:
            await self.pc.setLocalDescription(offer)
        except Exception as e:
            log.error(
                "ICE gathering failed (%s: %s). Retrying WITHOUT TURN — the call "
                "will ring but audio will not flow unless you have a public IP.",
                type(e).__name__, e,
            )
            await self.pc.close()
            self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
            self.track = OutboundAudioTrack()
            self.pc.addTrack(self.track)
            sdp_utils.restrict_codecs(self.pc, allow_g711=False)

            @self.pc.on("track")
            def _on_track_retry(track):  # noqa: ANN001
                if track.kind == "audio":
                    self._tasks.append(
                        asyncio.create_task(self._consume_inbound(track))
                    )

            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)

        raw = self.pc.localDescription.sdp
        # Order matters: reduce fingerprints first, then ptime, then candidates.
        # aiortc offers sha-256/384/512; Meta rejects multi-fingerprint offers
        # with error 138008 "SDP Validation error".
        raw = sdp_utils.single_fingerprint(raw)
        raw = sdp_utils.enforce_ptime(raw)
        raw = sdp_utils.rewrite_host_candidates(raw, s.public_ip)
        sdp_utils.describe(raw, "local-offer")
        # Saved so a rejected offer can actually be inspected. Contains no secrets.
        sdp_utils.dump(raw, "last-offer.sdp")
        return raw

    async def accept_inbound(self, offer_sdp: str) -> str:
        """A WhatsApp user called US. Build the SDP answer.

        Differs from the outbound path in three ways that matter:

        1. Meta is the offerer, so by default WebRTC would make *us* ICE
           CONTROLLED. But Meta's stack is ICE-LITE, and RFC 5245 says a lite
           implementation is always CONTROLLED -- both sides controlled means
           nobody nominates a candidate pair and ICE stalls. We force ourselves
           CONTROLLING below.
        2. We answer `setup:active`, making us the DTLS client, which is what
           Meta asks for. aiortc does this naturally as the answerer.
        3. The human is ALREADY on the line, so the agent starts when media
           connects -- not on an ACCEPTED webhook, which only exists outbound.
        """
        s = get_settings()
        self.direction = "USER_INITIATED"
        self.pc = RTCPeerConnection(configuration=_ice_config())

        self.track = OutboundAudioTrack()          # exactly one audio track
        self.pc.addTrack(self.track)
        sdp_utils.restrict_codecs(self.pc, allow_g711=False)

        @self.pc.on("track")
        def _on_track(track):  # noqa: ANN001
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._consume_inbound(track)))

        @self.pc.on("connectionstatechange")
        async def _on_state():
            state = self.pc.connectionState if self.pc else "?"
            log.info("pc state=%s (inbound)", state)
            # Inbound: media up == caller is listening. Greet now.
            if state == "connected" and not self.accepted:
                await self.on_accepted(inbound=True)
            elif state in ("failed", "closed"):
                await self.hangup()

        @self.pc.on("iceconnectionstatechange")
        async def _on_ice():
            log.info("ice state=%s (inbound)", self.pc.iceConnectionState if self.pc else "?")

        sdp_utils.describe(offer_sdp, "remote-offer")
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )
        _force_ice_controlling(self.pc)

        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        raw = self.pc.localDescription.sdp
        raw = sdp_utils.single_fingerprint(raw)
        raw = sdp_utils.enforce_ptime(raw)
        raw = sdp_utils.rewrite_host_candidates(raw, s.public_ip)
        sdp_utils.describe(raw, "local-answer")
        sdp_utils.dump(raw, "last-answer.sdp")
        return raw

    async def accept_answer(self, answer_sdp: str) -> None:
        """Apply Meta's SDP answer. This triggers ICE checks, then DTLS."""
        if not self.pc:
            raise RuntimeError("create_offer() first")
        sdp_utils.describe(answer_sdp, "remote-answer")
        patched = sdp_utils.force_dtls_client(answer_sdp)
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=patched, type="answer")
        )
        log.info("remote description applied; ICE + DTLS in progress")

    # -- the agent ----------------------------------------------------------

    async def on_accepted(self, inbound: bool = False) -> None:
        """The human is on the line. Start the AI pipeline and greet them.

        Outbound: triggered by the ACCEPTED status webhook. Do NOT use pc
        connected -- DTLS completes while the phone is still ringing, so greeting
        then means talking to a ringtone and the human answers mid-sentence.

        Inbound: triggered by pc connected, because the caller is already there
        and no ACCEPTED webhook is sent for calls they initiated.
        """
        if self.accepted:
            return
        self.accepted = True
        self.started_at = time.monotonic()

        if get_settings().media_only:
            log.info("MEDIA_ONLY=1: playing a tone-free silence, no AI pipeline")
            return

        assert self.track is not None
        self.pipeline = Pipeline(
            system_prompt=SYSTEM_PROMPT,
            on_audio=lambda pcm, rate: self.track.push_pcm(pcm, rate),  # type: ignore[union-attr]
            interrupt_playback=self.track.interrupt,
            on_finish=self._agent_finished,
            still_playing=lambda: bool(self.track and self.track.is_playing),
        )

        # Greet and connect the recogniser AT THE SAME TIME.
        #
        # These used to be sequential, and connecting Deepgram took the better
        # part of a second on a real call -- a second of silence after the driver
        # answered, for nothing, because there is nothing worth hearing from him
        # until we have spoken. Greeting first also means the recogniser comes up
        # while our own audio is playing, which it would suppress anyway.
        greeting = asyncio.create_task(
            self.pipeline.say(INBOUND_GREETING if inbound else GREETING)
        )
        try:
            await self.pipeline.start()
            self._tasks.append(asyncio.create_task(self.pipeline.run_stt_loop()))
        finally:
            await greeting

    # -- the agent decides the call is over ---------------------------------

    def _agent_finished(self) -> None:
        """Called once the agent has said goodbye and heard nothing further."""
        # Deliberately NOT added to self._tasks: hangup() cancels everything in
        # that list, and this task is the one calling hangup() -- it would cancel
        # itself part-way through teardown.
        self._teardown = asyncio.create_task(self._terminate())

    async def _terminate(self) -> None:
        """Hang up from OUR side, at Meta and locally.

        Closing only the peer connection leaves the call up as far as WhatsApp is
        concerned, so the driver keeps staring at a live call screen. Terminating
        at the Graph API is what actually ends it.
        """
        log.info("agent finished the conversation — terminating the call")
        if self.call_id:
            from .graph import GraphClient
            g = GraphClient()
            try:
                await g.terminate_call(self.call_id)
            except Exception:
                log.warning("Meta rejected the terminate; closing locally anyway",
                            exc_info=True)
            finally:
                await g.close()
        await self.hangup()

    async def _consume_inbound(self, track) -> None:  # noqa: ANN001
        """Pump inbound audio into STT, and watch for barge-in."""
        # One resampler for the whole track -- it carries state between frames.
        resampler = InboundResampler()
        while True:
            try:
                frame = await track.recv()
            except Exception:
                log.info("inbound track ended")
                return

            pcm16 = resampler.to_pcm16(frame)

            if self.pipeline:
                # Cheap energy VAD purely for barge-in. Turn *ending* is decided
                # by the STT provider's endpointing, which is far more reliable.
                #
                # Drive this from ACTUAL PLAYBACK, not generation. Piper renders a
                # whole sentence in one call, so generation finishes seconds before
                # the audio leaves the caller's speaker -- and it is the speaker
                # that produces echo. Getting this wrong makes the agent transcribe
                # its own greeting and answer itself.
                audible = self.pipeline.is_speaking or bool(
                    self.track and self.track.is_playing
                )
                self.pipeline.stt.agent_speaking = audible
                # Also hand over the emitted-frame counter, so the gate's
                # stuck-detector can tell a long read-back from a wedged track.
                self.pipeline.stt.playback_frames = (
                    self.track.frames_emitted if self.track else None
                )

                # Feed the provider first, then trust ITS gate. A second VAD here
                # has no echo awareness and would cut the agent off on its own voice.
                await self.pipeline.stt.send_audio(pcm16)

                if audible and self.pipeline.stt.caller_speaking:
                    self.pipeline.barge_in()

    # -- teardown -----------------------------------------------------------

    async def hangup(self) -> None:
        self.ended_at = time.monotonic()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self.pipeline:
            await self.pipeline.close()
        if self.pc:
            await self.pc.close()
        log.info("session closed call_id=%s duration=%.1fs", self.call_id, self.duration)

    @property
    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.ended_at or time.monotonic()) - self.started_at

    def transcript_text(self) -> str:
        return self.pipeline.transcript_text() if self.pipeline else ""


class SessionRegistry:
    """In-memory map of call_id -> CallSession.

    Fine for a lab. In production this needs to be shared state, because Meta's
    webhooks may land on a different instance than the one that placed the call
    -- and the WebRTC peer connection lives in the process that created it. The
    usual fix is to pin each call to an instance and route webhooks by call_id.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, CallSession] = {}
        self._pending: list[CallSession] = []

    def add_pending(self, s: CallSession) -> None:
        self._pending.append(s)

    def bind(self, call_id: str, s: CallSession) -> None:
        s.call_id = call_id
        self._by_id[call_id] = s
        if s in self._pending:
            self._pending.remove(s)

    def get(self, call_id: str) -> CallSession | None:
        return self._by_id.get(call_id)

    def pop(self, call_id: str) -> CallSession | None:
        return self._by_id.pop(call_id, None)

    def all(self) -> list[CallSession]:
        return list(self._by_id.values())


registry = SessionRegistry()
