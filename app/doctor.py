"""Network diagnosis for the media path.

The commonest failure with WhatsApp Calling is: the call rings, you answer, and
there is total silence forever. That is almost always the media path, and it is
almost always one of two things -- no publicly reachable address, or UDP blocked.

Meta's VoIP stack is **ICE-LITE**. It publishes its candidates and waits. It will
not probe, it will not help you traverse NAT. So one side has to be reachable,
and that side has to be you.

Everything here runs locally. No external service is contacted, so it works on an
air-gapped host and leaks nothing.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from .config import get_settings


@dataclass
class Finding:
    ok: bool
    title: str
    detail: str
    fix: str = ""


def _local_addresses() -> list[str]:
    """Every IPv4 address on this host, without contacting anything."""
    addrs: set[str] = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            addrs.add(info[4][0])
    except Exception:
        pass
    # Also catch addresses not tied to the hostname.
    try:
        import subprocess
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[3]:
                addrs.add(parts[3].split("/")[0])
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
        # Take ONLY the token straight after `inet`. Scanning every IPv4-shaped
        # token also picks up the netmask and broadcast address on each line --
        # which is how 255.0.0.0 and 10.0.255.255 ended up being reported as
        # network interfaces. Harmless, but it makes the output untrustworthy at
        # exactly the moment you are relying on it to diagnose something.
        for line in out.splitlines():
            parts = line.split()
            for i, tok in enumerate(parts):
                if tok == "inet" and i + 1 < len(parts):
                    try:
                        ipaddress.IPv4Address(parts[i + 1])
                        addrs.add(parts[i + 1])
                    except Exception:
                        pass
    except Exception:
        pass
    return sorted(addrs)


def _classify(ip: str) -> str:
    try:
        a = ipaddress.IPv4Address(ip)
    except Exception:
        return "invalid"
    if a.is_loopback:
        return "loopback"
    if a.is_link_local:
        return "link-local"
    if a.is_private:
        return "private"
    return "public"


def diagnose() -> list[Finding]:
    s = get_settings()
    out: list[Finding] = []

    # -- 1. addresses -------------------------------------------------------
    addrs = _local_addresses()
    kinds = {ip: _classify(ip) for ip in addrs}
    public = [ip for ip, k in kinds.items() if k == "public"]
    private = [ip for ip, k in kinds.items() if k == "private"]
    routable = public or private

    out.append(
        Finding(
            ok=bool(routable),
            title="Network interfaces",
            detail=", ".join(f"{ip} ({k})" for ip, k in kinds.items()) or "none found",
            fix="" if routable
            else "Only loopback found. aiortc cannot gather any ICE candidate, so "
                 "media is impossible here. Run on a host with a real interface.",
        )
    )

    # -- 2. can Meta reach our media? --------------------------------------
    if public:
        out.append(
            Finding(
                True,
                "Publicly reachable address",
                f"{public[0]} is public — Meta can reach you directly.",
                "" if not s.public_ip else "",
            )
        )
    elif s.public_ip:
        out.append(
            Finding(
                True,
                "PUBLIC_IP override set",
                f"PUBLIC_IP={s.public_ip}; private candidates will be rewritten. "
                "Correct for a cloud VM behind 1:1 NAT (elastic/floating IP).",
                "Confirm inbound UDP is actually open to this host.",
            )
        )
    elif s.stun_server:
        # STUN is the preferred route, and it is checked BEFORE TURN on purpose.
        # A reflexive candidate lets media go straight to Meta; a relay carries
        # every packet through someone else's server and adds a leg of latency.
        # Reach for TURN only when this fails.
        out.append(
            Finding(
                True,
                "STUN configured — reflexive candidate",
                f"No public address, but STUN_SERVER={s.stun_server} lets aiortc "
                "discover your public IP:port and offer it to Meta. Media then "
                "flows directly between this host and Meta — no relay, and no "
                "third party carrying your audio.",
                "Confirm it works, and check whether this NAT is symmetric:\n"
                "          python cli.py stun-test\n"
                "    Symmetric NAT is usually still fine here, because Meta is\n"
                "    publicly reachable and we send the connectivity checks.",
            )
        )
    elif s.turn_server or s.turn_static_auth:
        which = "static-auth TURN" if s.turn_static_auth else "TURN"
        out.append(
            Finding(
                True,
                f"{which} relay configured",
                "No public address and no STUN, but TURN is set, so Meta can "
                "reach you via the relay. Media takes a detour: expect extra "
                "latency, and note that all call audio passes through the relay "
                "operator.",
                "CONFIGURED is not the same as WORKING. A refused TURN allocation "
                "is silent — no error, just no relay candidate, and then a call "
                "that rings and stays mute. Prove it first:\n"
                "          python cli.py turn-test\n"
                "    Prefer STUN_SERVER if it works: direct media, lower latency,\n"
                "    and nobody else in the audio path.",
            )
        )
    else:
        out.append(
            Finding(
                False,
                "No route for media  ← THE BLOCKER",
                "This host has only private addresses, and none of STUN_SERVER, "
                "PUBLIC_IP or TURN_SERVER is set. aiortc will gather only `typ "
                "host` candidates — your private address — and Meta is ICE-LITE, "
                "so it will not traverse NAT for you. Calls RING and CONNECT, "
                "then stay silent forever.",
                "In order of preference:\n"
                "      (a) BEST, free, no signup — add to .env:\n"
                "          STUN_SERVER=stun:stun.cloudflare.com:3478\n"
                "          Verify with: python cli.py stun-test\n"
                "          Media goes straight to Meta; nobody relays your audio.\n"
                "      (b) Set PUBLIC_IP if this host has a public address or\n"
                "          sits behind 1:1 NAT (cloud VM with an elastic IP)\n"
                "      (c) Run your own coturn and set TURN_SERVER\n"
                "      (d) A public TURN relay, as a last resort. Note that\n"
                "          staticauth.openrelay.metered.ca was UNREACHABLE from\n"
                "          this network on every port (see FINDINGS.md), so do\n"
                "          not assume a free relay will work.\n"
                "    An HTTPS tunnel (ngrok/cloudflared) does NOT help: it forwards\n"
                "    TCP, and media is UDP.",
            )
        )

    # -- 3. can we bind UDP at all? ----------------------------------------
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        port = sock.getsockname()[1]
        sock.close()
        out.append(Finding(True, "UDP socket bind", f"bound ephemeral port {port} fine"))
    except Exception as e:
        out.append(
            Finding(False, "UDP socket bind", str(e),
                    "Something is preventing UDP sockets. Media cannot work.")
        )

    # -- 4. config sanity ---------------------------------------------------
    missing = [
        n for n, v in (
            ("WA_ACCESS_TOKEN", s.wa_access_token),
            ("WA_PHONE_NUMBER_ID", s.wa_phone_number_id),
            ("WA_WEBHOOK_VERIFY_TOKEN", s.wa_webhook_verify_token),
        ) if not v or v == "change-me"
    ]
    out.append(
        Finding(
            ok=not missing,
            title="Credentials in .env",
            detail="all present" if not missing else f"missing: {', '.join(missing)}",
            fix="" if not missing
            else "Fill these in from App Dashboard → WhatsApp → API Setup. "
                 "See SETUP-YOUR-NUMBER.md step 1.",
        )
    )

    # The LLM key lives in the ENVIRONMENT, not .env, which means a shell that
    # forgot to export it produces a call that rings, greets the driver, and then
    # fails on the very first thing they say. Worth catching before the call.
    if not s.media_only:
        prov = s.llm_provider.lower()
        missing_key = (
            prov == "anthropic" and not s.anthropic_api_key
        ) or (
            prov == "bedrock" and not s.aws_region
        )
        out.append(
            Finding(
                ok=not missing_key,
                title="LLM credentials",
                detail=(
                    f"{prov}: key present" if not missing_key
                    else f"{prov}: NO credentials in this environment"
                ),
                fix="" if not missing_key else (
                    "export ANTHROPIC_API_KEY='sk-ant-...' in the shell that runs\n"
                    "       uvicorn, then restart it. The agent will otherwise greet the\n"
                    "       caller and go silent on their first reply.\n"
                    "       Note this is read from the environment, NOT from .env."
                ),
            )
        )

    if s.wa_app_secret:
        out.append(Finding(True, "Webhook signature check", "WA_APP_SECRET set — verified"))
    else:
        out.append(
            Finding(
                True,
                "Webhook signature check",
                "WA_APP_SECRET not set — signatures NOT verified (acceptable in a lab)",
                "Set it before production, or anyone who learns your webhook URL "
                "can post fake call events.",
            )
        )

    # -- 5. media-only reminder --------------------------------------------
    out.append(
        Finding(
            True,
            "Mode",
            f"MEDIA_ONLY={'1 (no AI — silence expected on answer)' if s.media_only else '0 (AI agent active)'}",
            "" if s.media_only
            else "Correct once ringing is proven — which it is. If you now hear "
                 "silence, read the server log before blaming the AI: a line "
                 "saying every candidate is private means the media path failed, "
                 "not the pipeline.",
        )
    )

    return out


def render(findings: list[Finding]) -> tuple[str, bool]:
    """Format for a terminal. Returns (text, all_ok_for_media)."""
    lines: list[str] = []
    blocking = False
    for f in findings:
        mark = "OK  " if f.ok else "FAIL"
        if not f.ok:
            blocking = True
        lines.append(f"[{mark}] {f.title}")
        lines.append(f"       {f.detail}")
        if f.fix:
            for i, fl in enumerate(f.fix.split("\n")):
                lines.append(f"    →  {fl}" if i == 0 else f"       {fl}")
        lines.append("")
    return "\n".join(lines), not blocking
