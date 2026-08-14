"""SDP helpers that enforce Meta's mandatory media requirements.

Meta publishes a short list of things that will make the call fail outright, and
they fail in ways that are miserable to diagnose -- typically "call connects,
then total silence" or "audio is severe garbage". Getting these right up front
saves days.

Mandatory (from Meta's Integration Patterns page):
  * Supported codecs only
  * Opus clock rate 48 kHz
  * Opus ptime 20 ms
  * A SINGLE audio SSRC. Meta's relay rewrites all business audio to one fixed
    SSRC before it reaches the WhatsApp client, and the client handles exactly
    one audio source. More than one causes "severe media corruption ... likely
    total media failure".
  * DTMF clock rate 8 kHz

Recommended:
  * We must be ICE-FULL; Meta is ICE-LITE and will not help traverse NAT
  * We take the ICE CONTROLLING role; Meta only ever takes CONTROLLED
  * We act as the DTLS client
  * ECDH keys on the DTLS certificate, to avoid packet fragmentation
  * Do not switch candidate mid-call
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("sdp")

OPUS_CLOCK = 48000
PTIME_MS = 20
DTMF_CLOCK = 8000


def restrict_codecs(pc, allow_g711: bool = False) -> None:
    """Offer only codecs Meta actually supports.

    Why this is necessary: aiortc's default audio codec list includes **G722**,
    which is NOT on Meta's supported list. Meta's requirement is blunt -- "use
    only the supported codecs" -- and an unsupported codec can fail the call
    either at signalling or when media packets are decoded.

    Opus is what you want on the wire. Only enable G.711 (PCMA/PCMU) if some
    legacy device on your side cannot do Opus; it costs you a transcode, extra
    latency, and the entire frequency range above 3.4 kHz.

    Call this after addTrack() and before createOffer().
    """
    from aiortc import RTCRtpSender

    wanted = ["audio/opus", "audio/PCMA", "audio/PCMU"] if allow_g711 else ["audio/opus"]
    available = RTCRtpSender.getCapabilities("audio").codecs

    prefs = [c for name in wanted for c in available if c.mimeType.lower() == name.lower()]
    # Keep DTMF if the stack offers it -- Meta requires its clock rate be 8 kHz.
    prefs += [c for c in available if "telephone-event" in c.mimeType.lower()
              and c.clockRate == DTMF_CLOCK]

    if not prefs:
        log.error("no supported audio codecs available; leaving defaults alone")
        return

    for t in pc.getTransceivers():
        if t.kind == "audio":
            t.setCodecPreferences(prefs)

    dropped = {c.mimeType for c in available} - {c.mimeType for c in prefs}
    log.info(
        "codecs restricted to %s (dropped %s)",
        [c.mimeType for c in prefs],
        sorted(dropped) or "nothing",
    )


def _lines(sdp: str) -> list[str]:
    return sdp.replace("\r\n", "\n").split("\n")


def _join(lines: list[str]) -> str:
    return "\r\n".join(l for l in lines if l != "") + "\r\n"


def payload_types(sdp: str) -> dict[int, str]:
    """Map payload type number -> encoding name/clockrate, from a=rtpmap lines."""
    out: dict[int, str] = {}
    for line in _lines(sdp):
        m = re.match(r"a=rtpmap:(\d+)\s+(\S+)", line)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def single_fingerprint(sdp: str, prefer: str = "sha-256") -> str:
    """Keep exactly ONE DTLS fingerprint line.

    aiortc offers three (sha-256, sha-384, sha-512). RFC 8122 permits multiple,
    but strict validators commonly accept only one and reject the rest of the
    offer -- Meta returns error 138008 "SDP Validation error".

    Keeping sha-256 is the safe choice: it is universally supported and is what
    every browser sends.
    """
    lines = _lines(sdp)
    fps = [l for l in lines if l.startswith("a=fingerprint:")]
    if len(fps) <= 1:
        return sdp

    keep = next((l for l in fps if l.startswith(f"a=fingerprint:{prefer}")), fps[0])
    out, kept = [], False
    for line in lines:
        if line.startswith("a=fingerprint:"):
            if line == keep and not kept:
                out.append(line)
                kept = True
            continue          # drop the others
        out.append(line)
    log.info(
        "reduced %d fingerprints to 1 (%s)", len(fps), keep.split()[0].split(":", 1)[1]
    )
    return _join(out)


def dump(sdp: str, path: str) -> None:
    """Write an SDP to disk so it can be inspected or shared when a call fails.

    An SDP contains local/public IPs and a certificate fingerprint -- no secrets,
    nothing that authenticates as your business.
    """
    try:
        with open(path, "w") as f:
            f.write(sdp)
        log.info("wrote SDP to %s", path)
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


def enforce_ptime(sdp: str) -> str:
    """Pin ptime/maxptime to 20 ms on every audio m-section.

    aiortc already produces 20 ms Opus frames (960 samples at 48 kHz), but Meta
    checks the signalled value, so state it explicitly.
    """
    lines = _lines(sdp)
    out: list[str] = []
    in_audio = False
    for line in lines:
        if line.startswith("m="):
            # Closing out a previous audio section: make sure ptime was stated.
            if in_audio:
                out.append(f"a=ptime:{PTIME_MS}")
                out.append(f"a=maxptime:{PTIME_MS}")
            in_audio = line.startswith("m=audio")
        if re.match(r"a=(ptime|maxptime):", line):
            continue  # drop any existing value; we re-add ours
        out.append(line)
    if in_audio:
        out.append(f"a=ptime:{PTIME_MS}")
        out.append(f"a=maxptime:{PTIME_MS}")
    return _join(out)


def force_dtls_client(remote_answer: str) -> str:
    """Make *us* the DTLS client, as Meta recommends.

    In DTLS-SRTP the side that sends ClientHello is the client, and that is the
    side whose SDP says `setup:active`. We offer `actpass`, which hands Meta the
    choice. If Meta answers `active` it intends to be the client, leaving us as
    the server -- against their own recommendation.

    Rewriting the *remote* answer to `passive` flips the roles so we go active.
    This is SDP munging, which is normally a bad idea; it is justified here only
    because Meta explicitly documents the role they expect us to take. Log the
    outcome and remove this if Meta's behaviour changes.
    """
    if "a=setup:active" not in remote_answer:
        return remote_answer
    log.warning("remote answered setup:active; rewriting to passive so we are DTLS client")
    return remote_answer.replace("a=setup:active", "a=setup:passive")


def rewrite_host_candidates(sdp: str, public_ip: str) -> str:
    """Replace private addresses with a public one for 1:1 NAT hosts.

    On a cloud VM with an elastic/floating IP, aiortc only sees the private
    address. Meta is ICE-LITE and will not probe for you, so unless a public
    address appears in your candidates the call connects and then sits silent.
    """
    if not public_ip:
        return sdp
    out: list[str] = []
    for line in _lines(sdp):
        if line.startswith("a=candidate:") and " typ host" in line:
            parts = line.split()
            # a=candidate:<foundation> <component> <proto> <pri> <ip> <port> typ host
            if len(parts) > 5 and _is_private(parts[4]):
                parts[4] = public_ip
                line = " ".join(parts)
        elif line.startswith("c=IN IP4 ") and _is_private(line.split()[-1]):
            line = f"c=IN IP4 {public_ip}"
        out.append(line)
    return _join(out)


def _is_private(ip: str) -> bool:
    return (
        ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip.startswith("127.")
        or any(ip.startswith(f"172.{n}.") for n in range(16, 32))
    )


def describe(sdp: str, label: str) -> None:
    """Log the handful of SDP attributes that actually matter for debugging.

    When a call is silent, this tells you in one glance whether the fault is in
    codecs, candidates, or DTLS roles.
    """
    pts = payload_types(sdp)
    setup = re.findall(r"a=setup:(\S+)", sdp)
    fps = re.findall(r"a=fingerprint:(\S+)", sdp)
    cands = re.findall(r"a=candidate:\S+ \d+ \S+ \d+ (\S+) (\d+) typ (\S+)", sdp)
    ssrcs = set(re.findall(r"a=ssrc:(\d+)", sdp))
    ptime = re.findall(r"a=ptime:(\d+)", sdp)

    log.info(
        "[%s] codecs=%s setup=%s ptime=%s ssrcs=%s fingerprint_alg=%s",
        label,
        {k: v for k, v in pts.items()},
        setup,
        ptime,
        sorted(ssrcs),
        fps[:1],
    )
    types = set()
    for ip, port, typ in cands:
        log.info("[%s] candidate %s:%s typ=%s", label, ip, port, typ)
        types.add(typ)

    # A TURN server that is configured but produced no relay candidate is the
    # single most confusing failure in this stack: the call rings, is answered,
    # and then ICE sits in "checking" forever with no error anywhere. Say so
    # loudly at the moment the offer is built, rather than leaving it to be
    # inferred from silence.
    if label.startswith("local"):
        from .config import get_settings
        s = get_settings()
        turn_configured = bool(s.turn_server or s.turn_static_auth)
        if turn_configured and "relay" not in types:
            log.error(
                "[%s] TURN is configured but NO 'typ relay' candidate was "
                "gathered — the relay rejected or ignored the allocation. Meta "
                "is ICE-LITE and cannot reach a private address, so media will "
                "not connect. Check the TURN credentials.",
                label,
            )
        elif "relay" in types:
            log.info("[%s] relay candidate present — TURN allocation succeeded", label)

        # A server-reflexive candidate is the cheap win and usually the only
        # thing needed: it is our real public IP:port, which Meta can reach, and
        # media then flows directly with no relay in the path.
        if "srflx" in types:
            pub = [ip for ip, _, typ in cands if typ == "srflx"]
            log.info(
                "[%s] reflexive candidate %s — Meta has a public address to "
                "reach us on, no relay required", label, pub[0] if pub else "?",
            )
        elif not any(not _is_private(ip) for ip, _, _ in cands) and cands:
            log.error(
                "[%s] EVERY candidate is a private address and there is no "
                "reflexive one. Meta is ICE-LITE and will not traverse NAT, so "
                "this call will ring, be answered, and stay silent. Set "
                "STUN_SERVER in .env (see `python cli.py stun-test`).", label,
            )

    if len(ssrcs) > 1:
        log.error(
            "[%s] MORE THAN ONE SSRC (%s). Meta requires exactly one audio SSRC; "
            "expect severe corruption or total media failure.",
            label,
            sorted(ssrcs),
        )
    for pt, name in pts.items():
        if name.lower().startswith("opus") and not name.endswith(f"/{OPUS_CLOCK}/2"):
            log.error("[%s] Opus must be %s/2, got %s", label, OPUS_CLOCK, name)
        if name.lower().startswith("telephone-event") and f"/{DTMF_CLOCK}" not in name:
            log.error("[%s] DTMF clock rate must be %s, got %s", label, DTMF_CLOCK, name)
