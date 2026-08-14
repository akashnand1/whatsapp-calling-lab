"""Probe the TURN relay directly, without placing a WhatsApp call.

Why this exists
---------------
A failed TURN allocation is invisible. aiortc does not raise; it logs nothing at
INFO; it simply gathers no `typ relay` candidate and carries on. The first sign
of trouble is a real call that rings, is answered, and then sits in silence
forever while ICE stays in "checking". That is a slow, expensive way to test a
UDP port.

This module asks the one question that matters -- "does the relay give us an
address?" -- in about two seconds, and it asks it separately for each transport,
because the answer usually differs. Home routers and mobile networks commonly
drop UDP to odd ports while passing TCP/443 without comment.

It also exists because of an aiortc behaviour that is easy to misread. From
aiortc/rtcicetransport.py::connection_kwargs:

    # only a single TURN server is supported
    if "turn_server" in kwargs:
        continue

A list of URLs is therefore NOT a fallback chain. Entry [0] is used and every
other entry is silently discarded. Ordering UDP first means UDP is the only
transport ever attempted. This probe drives aioice directly, one transport per
attempt, so nothing is hidden.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import socket as socket_mod
import time
from dataclasses import dataclass

from .config import get_settings

# host, port, transport, ssl  -- ordered most-likely-to-work first.
TRANSPORTS: list[tuple[str, int, str, bool]] = [
    ("tcp443", 443, "tcp", False),
    ("tcp80", 80, "tcp", False),
    ("udp443", 443, "udp", False),
    ("udp80", 80, "udp", False),
    ("udp3478", 3478, "udp", False),
]


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    relay_addr: str = ""
    ms: int = 0


class _Once(asyncio.DatagramProtocol):
    """Send one STUN/TURN message, keep the first reply."""

    def __init__(self, payload: bytes, addr: tuple[str, int]) -> None:
        self.payload, self.addr = payload, addr
        self.reply: asyncio.Future = asyncio.get_running_loop().create_future()

    def connection_made(self, transport) -> None:  # noqa: ANN001
        transport.sendto(self.payload, self.addr)

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if not self.reply.done():
            self.reply.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(exc)


async def _udp_rpc(host: str, port: int, msg, timeout: float):
    """One request, one reply, over UDP. Returns a parsed Message, or None."""
    from aioice import stun

    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: _Once(bytes(msg), (host, port)), local_addr=("0.0.0.0", 0)
    )
    try:
        data = await asyncio.wait_for(proto.reply, timeout=timeout)
        return stun.parse_message(data)
    except asyncio.TimeoutError:
        return None
    finally:
        transport.close()


async def health_check(host: str, user: str, cred: str, timeout: float = 4.0) -> list[Probe]:
    """Ask the relay three escalating questions to locate the fault exactly.

    "No relay candidate" has several very different causes that all look
    identical from the outside, and they have opposite fixes. Rather than guess,
    ask directly:

      1. STUN Binding, unauthenticated. A TURN server answers this too. If a
         reply comes back, UDP reaches the host and returns -- so the network is
         NOT the problem. If nothing comes back, it is.
      2. TURN Allocate, unauthenticated. RFC 5766 says the server MUST reject
         this with 401 and hand back a realm and nonce. Getting that 401 proves
         the TURN service itself is alive and listening.
      3. TURN Allocate with our derived credentials. Now the numeric error code
         is the answer: 401 means the shared secret is wrong, 486 means the free
         tier has no allocations left, 508 means capacity exhausted.

    Every question goes to the relay itself. Nothing is sent to a third party, so
    this leaks no more than a call would.
    """
    from aioice import stun

    out: list[Probe] = []
    port = 3478

    # -- 1. is the host reachable over UDP at all? --------------------------
    t0 = time.monotonic()
    reply = await _udp_rpc(host, port, stun.Message(stun.Method.BINDING, stun.Class.REQUEST), timeout)
    ms = int((time.monotonic() - t0) * 1000)
    if reply is None:
        out.append(Probe("1 reachable?", False,
                         f"no STUN Binding reply from {host}:{port} — either your "
                         "network drops outbound UDP, or the relay is down", ms=ms))
        return out                      # nothing below can succeed
    mapped = reply.attributes.get("XOR-MAPPED-ADDRESS") or reply.attributes.get("MAPPED-ADDRESS")
    out.append(Probe("1 reachable?", True,
                     f"relay answered — UDP works, and it sees you at "
                     f"{mapped[0]}:{mapped[1]}" if mapped else "relay answered", ms=ms))

    # -- 2. is the TURN service alive? --------------------------------------
    def allocate(**attrs):
        m = stun.Message(stun.Method.ALLOCATE, stun.Class.REQUEST)
        m.attributes["REQUESTED-TRANSPORT"] = 17 << 24      # 17 = UDP, per RFC 5766
        m.attributes.update(attrs)
        return m

    t0 = time.monotonic()
    reply = await _udp_rpc(host, port, allocate(), timeout)
    ms = int((time.monotonic() - t0) * 1000)
    if reply is None:
        out.append(Probe("2 TURN alive?", False, "Allocate got no reply at all", ms=ms))
        return out
    err = reply.attributes.get("ERROR-CODE")
    realm = reply.attributes.get("REALM")
    nonce = reply.attributes.get("NONCE")
    if err and err[0] == 401 and realm and nonce:
        out.append(Probe("2 TURN alive?", True,
                         f"401 with realm={realm!r} — correct, TURN is running", ms=ms))
    else:
        out.append(Probe("2 TURN alive?", False,
                         f"expected a 401 challenge, got {err} — not a TURN server, "
                         "or it is misconfigured", ms=ms))
        return out

    # -- 3. do OUR credentials work? ----------------------------------------
    msg = allocate(USERNAME=user, REALM=realm, NONCE=nonce)
    integrity_key = hashlib.md5(f"{user}:{realm}:{cred}".encode()).digest()
    msg.add_message_integrity(integrity_key)

    t0 = time.monotonic()
    reply = await _udp_rpc(host, port, msg, timeout)
    ms = int((time.monotonic() - t0) * 1000)
    if reply is None:
        out.append(Probe("3 credentials", False, "authenticated Allocate got no reply", ms=ms))
        return out

    relayed = reply.attributes.get("XOR-RELAYED-ADDRESS")
    if relayed:
        out.append(Probe("3 credentials", True,
                         "allocation granted", f"{relayed[0]}:{relayed[1]}", ms))
        return out

    err = reply.attributes.get("ERROR-CODE") or (0, "no error code")
    meaning = {
        401: "the shared secret in TURN_STATIC_AUTH is wrong, or this relay no "
             "longer accepts the TURN REST scheme",
        486: "allocation quota exhausted — this free relay is shared with "
             "everyone and has run dry",
        508: "the relay is out of capacity",
    }.get(err[0], "see RFC 5766 for this code")
    out.append(Probe("3 credentials", False, f"{err[0]} {err[1]} — {meaning}", ms=ms))
    return out


# Public STUN servers, tried in order. A STUN server learns your public IP and
# port and nothing else -- it never carries audio, which is the whole point:
# with a server-reflexive candidate the media goes straight to Meta, so no third
# party is in the audio path at all. That is a better data-residency position
# than TURN, where the relay carries every packet.
STUN_SERVERS: list[tuple[str, int]] = [
    ("stun.cloudflare.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.nextcloud.com", 443),
]


class _Persistent(asyncio.DatagramProtocol):
    """One socket, many requests, replies matched by STUN transaction id.

    The socket must be shared across servers: NAT type is determined by whether
    the *same* local port maps to the *same* public port when talking to two
    different destinations. A fresh socket per request would tell us nothing.
    """

    def __init__(self) -> None:
        self.transport = None
        self.pending: dict[bytes, asyncio.Future] = {}

    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        from aioice import stun
        try:
            msg = stun.parse_message(data)
        except Exception:
            return
        fut = self.pending.pop(msg.transaction_id, None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def binding(self, host: str, port: int, timeout: float):
        from aioice import stun
        msg = stun.Message(stun.Method.BINDING, stun.Class.REQUEST)
        fut = asyncio.get_running_loop().create_future()
        self.pending[msg.transaction_id] = fut
        # Resolve explicitly so a DNS failure is not mistaken for a filtered port.
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket_mod.AF_INET, type=socket_mod.SOCK_DGRAM
        )
        ip = infos[0][4][0]
        self.transport.sendto(bytes(msg), (ip, port))
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(msg.transaction_id, None)
            return None, ip
        a = reply.attributes
        return (a.get("XOR-MAPPED-ADDRESS") or a.get("MAPPED-ADDRESS")), ip


async def nat_check(timeout: float = 4.0) -> list[Probe]:
    """Can Meta reach us without a relay, and will a reflexive candidate hold?

    Two questions, in order.

    1. Does STUN work from here at all? If yes we can put a server-reflexive
       candidate -- our real public IP:port -- into the SDP offer, which is what
       was missing when the first call rang and stayed silent.

    2. Is this NAT symmetric? Ask two different STUN servers from the SAME local
       socket. A cone NAT reuses one public port for every destination, so both
       answers match and the advertised candidate is exactly what Meta will see.
       A symmetric NAT allocates a fresh port per destination, so the advertised
       one is already wrong by the time Meta uses it.

    Symmetric is not automatically fatal here. Meta is ICE-LITE and CONTROLLED,
    so it never initiates -- we send the checks, and an ICE-LITE agent replies to
    wherever the request came from, learning us as a peer-reflexive candidate.
    That can work through a symmetric NAT. It is just no longer guaranteed, and
    that is when TURN or a public IP earns its place.
    """
    out: list[Probe] = []

    # Which interface does UDP to the outside world actually leave by? A
    # connect() on a UDP socket sends nothing -- it just asks the kernel to pick
    # a route -- and getsockname() then reveals the source address chosen.
    #
    # This matters because a VPN can be up and carrying your web traffic while
    # UDP still leaks to the local ISP (split tunnelling). The public address
    # alone does not distinguish "VPN off" from "VPN on but not carrying media";
    # the local source address does.
    try:
        probe_sock = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        probe_sock.connect(("1.1.1.1", 53))
        src = probe_sock.getsockname()[0]
        probe_sock.close()
        private = src.startswith(("192.168.", "10.", "172."))
        out.append(Probe(
            "route for UDP", True,
            f"leaves via local address {src}"
            + ("  — a LAN address, so this is your normal interface"
               if src.startswith("192.168.") else
               "  — could be a VPN interface; compare the public address below"),
        ))
    except Exception as e:
        out.append(Probe("route for UDP", False, f"could not determine: {e}"))

    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        _Persistent, local_addr=("0.0.0.0", 0)
    )
    try:
        local_port = transport.get_extra_info("sockname")[1]
        mappings: list[tuple[str, tuple]] = []

        for host, port in STUN_SERVERS:
            t0 = time.monotonic()
            try:
                mapped, ip = await proto.binding(host, port, timeout)
            except Exception as e:
                out.append(Probe(f"{host}:{port}", False, f"{type(e).__name__}: {e}"))
                continue
            ms = int((time.monotonic() - t0) * 1000)
            if mapped is None:
                out.append(Probe(f"{host}:{port}", False,
                                 f"no reply from {ip} — UDP blocked, or this "
                                 "server is down", ms=ms))
                continue
            note = ""
            # Etisalat/du UAE space. The UAE filters VoIP at the ISP, so seeing
            # this here means media will be blocked no matter what else is right.
            if mapped[0].startswith(("217.165.", "94.200.", "86.98.", "5.32.")):
                note = "  ← UAE ISP: VoIP is filtered here, media WILL fail"
            out.append(Probe(f"{host}:{port}", True,
                             f"public address is {mapped[0]}:{mapped[1]}{note}", ms=ms))
            mappings.append((host, mapped))
            if len(mappings) == 2:
                break                       # two is all we need to classify

        if not mappings:
            out.append(Probe("verdict", False,
                             "No STUN server answered. This network blocks outbound "
                             "UDP, which also explains the TURN result — nothing "
                             "here can reach the internet over UDP. Media cannot "
                             "work from this machine by any method."))
            return out

        if len(mappings) == 1:
            out.append(Probe("verdict", True,
                             "Only one server answered, so NAT type is unconfirmed, "
                             "but STUN works — a reflexive candidate is worth trying."))
            return out

        ports = {m[1] for _, m in mappings}
        ips = {m[0] for _, m in mappings}
        if len(ports) == 1 and len(ips) == 1:
            out.append(Probe("verdict", True,
                             f"CONE NAT — local port {local_port} maps to the same "
                             f"public port {ports.pop()} for every destination. A "
                             "reflexive candidate will be valid for Meta too. No "
                             "relay needed, and audio stays peer-to-peer."))
        else:
            # Deliberately NOT reported as a failure. Symmetric NAT is fatal when
            # both peers are behind one and neither is reachable. That is not the
            # situation here: Meta publishes public candidates, and we are the
            # CONTROLLING, ICE-FULL side, so we send the checks outbound. The NAT
            # maps them on the way out and Meta replies to the source address it
            # actually observes, learning us as a peer-reflexive candidate --
            # which RFC 8445 s7.3 requires of an ICE-lite agent. The stale port in
            # our advertised candidate is then irrelevant.
            out.append(Probe("verdict", True,
                             f"SYMMETRIC NAT — one local port mapped to "
                             f"{sorted(ports)} for different destinations. Usually "
                             "still fine: Meta is publicly reachable and we send "
                             "the checks, so it learns our real address as a "
                             "peer-reflexive candidate. Place the call."))
        return out
    finally:
        transport.close()


def _rest_credentials(secret: str, ttl: int = 24 * 3600) -> tuple[str, str]:
    """The TURN REST API scheme (draft-uberti-behave-turn-rest-00).

    username  = <unix-expiry>:<any name>
    credential= base64( HMAC-SHA1( shared-secret, username ) )

    The relay recomputes the same HMAC and compares. Nothing is registered in
    advance, which is why 'static auth' relays need no signup -- but it also
    means a wrong secret produces a perfectly well-formed credential that is
    rejected with 401, indistinguishable from a network failure unless you look.
    """
    username = f"{int(time.time()) + ttl}:whatsapp-lab"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


async def _probe_one(
    host: str, port: int, transport: str, ssl: bool, user: str, cred: str, timeout: float
) -> Probe:
    name = f"{transport}/{port}"
    started = time.monotonic()
    try:
        from aioice import Connection
    except ImportError:
        return Probe(name, False, "aioice not installed")

    # Resolve first, and separately. aioice swallows a DNS failure and returns
    # from gather_candidates() in about a millisecond having done nothing, which
    # is reported identically to a rejected allocation. Those need very different
    # fixes, so tell them apart here rather than guessing later.
    import socket
    try:
        await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET)
    except Exception as e:
        return Probe(name, False, f"DNS lookup for {host} failed ({e}) — the relay "
                                  f"hostname does not resolve from this machine")

    # For TCP we can also prove the port is open before involving ICE at all.
    if transport == "tcp":
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            w.close()
        except asyncio.TimeoutError:
            return Probe(name, False, f"TCP connect to {host}:{port} timed out — "
                                      "port filtered on the way out",
                         ms=int((time.monotonic() - started) * 1000))
        except Exception as e:
            return Probe(name, False, f"TCP connect to {host}:{port} failed: {e}",
                         ms=int((time.monotonic() - started) * 1000))

    conn = Connection(
        ice_controlling=True,
        turn_server=(host, port),
        turn_username=user,
        turn_password=cred,
        turn_transport=transport,
        turn_ssl=ssl,
        # No STUN: we are testing the relay, and a server-reflexive candidate
        # from elsewhere would muddy the result.
        stun_server=None,
        use_ipv6=False,
    )
    try:
        await asyncio.wait_for(conn.gather_candidates(), timeout=timeout)
        relays = [c for c in conn.local_candidates if c.type == "relay"]
        ms = int((time.monotonic() - started) * 1000)
        if relays:
            r = relays[0]
            return Probe(name, True, "allocation granted", f"{r.host}:{r.port}", ms)
        # Careful with the wording here. For UDP all we have proved is that the
        # hostname resolves; aioice retransmits its Allocate and gives up after
        # roughly 5s whether the relay refused or the packets never arrived.
        # An earlier version of this message asserted "reached the relay", which
        # is an overclaim that would send you debugging credentials when the real
        # problem might be a firewall. health_check() settles it properly.
        return Probe(
            name,
            False,
            "no allocation after ~5s of retries — the relay refused, or the "
            "packets never arrived. See the relay health check below.",
            ms=ms,
        )
    except asyncio.TimeoutError:
        return Probe(name, False, f"no response within {timeout:.0f}s — port filtered",
                     ms=int((time.monotonic() - started) * 1000))
    except Exception as e:
        return Probe(name, False, f"{type(e).__name__}: {e}",
                     ms=int((time.monotonic() - started) * 1000))
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def run(timeout: float = 6.0) -> tuple[list[Probe], str]:
    """Probe every transport. Returns (results, recommended TURN_TRANSPORT)."""
    s = get_settings()

    if s.turn_static_auth:
        host, _, secret = s.turn_static_auth.partition(",")
        host, secret = host.strip(), secret.strip()
        user, cred = _rest_credentials(secret)
        source = f"TURN_STATIC_AUTH  host={host}  (credentials derived locally)"
    elif s.turn_server:
        # "urls,user,credential"
        parts = [p.strip() for p in s.turn_server.split(";")[0].split(",")]
        if len(parts) < 3:
            return ([Probe("config", False, "TURN_SERVER must be 'url,user,credential'")], "")
        url, user, cred = parts[0], parts[1], parts[2]
        host = url.split(":")[1] if url.startswith(("turn:", "turns:")) else url
        host = host.split("?")[0]
        source = f"TURN_SERVER  host={host}  (credentials from .env)"
    else:
        return ([Probe("config", False, "no TURN configured in .env")], "")

    results: list[Probe] = []
    for label, port, tr, ssl in TRANSPORTS:
        p = await _probe_one(host, port, tr, ssl, user, cred, timeout)
        p.name = label
        results.append(p)
        if p.ok:
            break          # first success is the one we will use; stop probing

    winner = next((p.name for p in results if p.ok), "")

    # If nothing worked, ask the relay directly what went wrong. The transport
    # sweep can only report absence; this reports a reason.
    # Returned rather than printed: printing from here put the health check
    # ABOVE the sweep results in the terminal, which read as though the health
    # check ran first and made the causal order impossible to follow.
    health = [] if winner else await health_check(host, user, cred)
    return results, winner, health, source


def render(
    results: list[Probe],
    winner: str,
    health: list[Probe] | None = None,
    source: str = "",
) -> str:
    lines: list[str] = []
    if source:
        lines.append(source)
        lines.append("")
    for p in results:
        mark = "OK  " if p.ok else "FAIL"
        lines.append(f"[{mark}] {p.name:8s} {p.ms:>5d}ms  {p.detail}")
        if p.relay_addr:
            lines.append(f"          relay address allocated to us: {p.relay_addr}")
    lines.append("")

    if health:
        lines.append("--- asking the relay directly ---")
        for p in health:
            mark = "OK  " if p.ok else "FAIL"
            lines.append(f"[{mark}] {p.name:14s} {p.ms:>5d}ms  {p.detail}")
            if p.relay_addr:
                lines.append(f"                          relay address: {p.relay_addr}")
        lines.append("")
        # The most useful single distinction, stated plainly.
        if health and not health[0].ok:
            lines.append(
                "Nothing answered on ANY port, including TCP/443. Open Relay "
                "documents 80 and 443 precisely because they pass through "
                "corporate firewalls, so a live relay would have answered. The "
                "likely cause is this network blocking the destination, or DNS "
                "here returning an address that goes nowhere."
            )
            lines.append(
                "Check what it resolves to:   dig +short staticauth.openrelay.metered.ca"
            )
            lines.append("")

    if winner:
        lines.append(f"Working transport: {winner}")
        lines.append(f"Put this in .env:   TURN_TRANSPORT={winner}")
        lines.append(
            "Then restart uvicorn — --reload does not watch .env, so a running "
            "server keeps the old value."
        )
    else:
        lines.append("No transport produced a relay candidate.")
        # Point at the actual failure mode rather than listing every possibility.
        if all("DNS" in p.detail for p in results):
            lines.append(
                "Every attempt failed at DNS, so this is not TURN at all — the "
                "hostname does not resolve. Check the host in TURN_STATIC_AUTH "
                "for a typo, and check this machine has working DNS."
            )
        elif all("timed out" in p.detail or "filtered" in p.detail for p in results):
            lines.append(
                "Every attempt timed out, including TCP/443. That is a network "
                "blocking outbound connections, not a credential problem."
            )
        elif any("granted no allocation" in p.detail for p in results):
            lines.append(
                "The relay answered and refused. The credentials are wrong, or "
                "this free shared relay is out of allocations — it is used by "
                "everyone and does run dry."
            )
        lines.append(
            "The durable fix is a host with a public IP (set PUBLIC_IP=), which "
            "removes the relay, its added latency, and a third party from the "
            "path your call audio travels."
        )
    return "\n".join(lines)
