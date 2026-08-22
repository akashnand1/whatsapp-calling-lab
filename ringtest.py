#!/usr/bin/env python3
"""One command: make my business number ring my personal WhatsApp.

    python ringtest.py 971501234567        # your personal WhatsApp, no '+'

Runs the whole sequence, stops at the first real blocker, and tells you exactly
what to fix. Nothing is guessed -- every step is a real API call whose result is
printed.

Deliberately defaults to MEDIA_ONLY, so success means "my phone rang and the call
connected". You will hear SILENCE when you answer. That is the correct result for
this test -- it proves account, permission, calling config, webhook and SDP
exchange all work, without the media path or the AI being involved.

Once this passes, run `python cli.py call <number>` with MEDIA_ONLY=0 for audio.
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx

from app.doctor import diagnose, render
from app.graph import GraphClient, GraphError

SERVER = "http://127.0.0.1:8000"
C_OK, C_BAD, C_WARN, C_DIM, C_END, C_BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m", "\033[1m",
)


def head(n: int, text: str) -> None:
    print(f"\n{C_BOLD}── {n}. {text} {'─' * max(0, 58 - len(text))}{C_END}")


def ok(m: str) -> None:
    print(f"  {C_OK}✓{C_END} {m}")


def bad(m: str) -> None:
    print(f"  {C_BAD}✗{C_END} {m}")


def warn(m: str) -> None:
    print(f"  {C_WARN}!{C_END} {m}")


def die(m: str, fix: str = "") -> None:
    bad(m)
    if fix:
        print(f"\n  {C_BOLD}Fix:{C_END} {fix}")
    print(f"\n{C_BAD}Stopped.{C_END} Nothing was charged.\n")
    sys.exit(1)


# Watch window in seconds, overridable with --seconds. This only controls how
# long the script REPORTS state; it no longer bounds the call itself.
WATCH_SECONDS = 45


async def main(to: str) -> None:
    print(f"\n{C_BOLD}WhatsApp Calling — ring test{C_END}")
    print(f"{C_DIM}target: {to}{C_END}")

    # ---------------------------------------------------------------- 1
    head(1, "Local environment")
    findings = diagnose()
    text, media_ok = render(findings)
    print("\n".join("  " + l for l in text.splitlines()))
    if any(not f.ok and "Credentials" in f.title for f in findings):
        die("Credentials missing.", "See SETUP-YOUR-NUMBER.md step 1.")
    if any(not f.ok and "interfaces" in f.title.lower() for f in findings):
        die("No usable network interface.", "Run on a host with a real NIC.")
    if not media_ok:
        warn("Media path is NOT viable on this host.")
        warn("The call will still RING and CONNECT — which is what we are testing.")
        warn("You will hear silence. Fix media later, see DEPLOY.md.")

    # ---------------------------------------------------------------- 2
    head(2, "Is the lab server running?")
    try:
        r = httpx.get(f"{SERVER}/api/health", timeout=10)
        r.raise_for_status()
        health = r.json()
        ok(f"server up — phone_number_id={health.get('phone_number_id')}")
    except Exception:
        die("Server not reachable at " + SERVER,
            "Start it in another shell:\n"
            "         uvicorn app.main:app --port 8000")
        health = {}

    # Step 1 read .env in THIS process. The server is a different process and
    # caches its settings, so a running uvicorn can be holding completely
    # different values -- and `--reload` watches .py files, not .env. That gap
    # has now caused two separate silent failures (an expired token that the CLI
    # could not see, and a media path with no STUN). Compare the two directly
    # rather than assuming they agree.
    from app.config import get_settings as _local_settings
    _s = _local_settings()
    drift = [
        (name, mine, theirs)
        for name, mine, theirs in (
            ("STUN_SERVER", bool(_s.stun_server), health.get("stun_configured")),
            ("PUBLIC_IP", bool(_s.public_ip), health.get("public_ip_configured")),
            ("TURN", bool(_s.turn_server or _s.turn_static_auth),
             health.get("turn_configured")),
            ("MEDIA_ONLY", bool(_s.media_only), health.get("media_only")),
        )
        if theirs is not None and bool(mine) != bool(theirs)
    ]
    if "stun_configured" not in health:
        die("The running server predates the STUN support in this code.",
            "It was started before these files changed, so it is still building\n"
            "         offers with host candidates only — which is exactly why the call\n"
            "         connects and stays silent. RESTART it:\n\n"
            "           uvicorn app.main:app --reload --reload-include '.env' --port 8000")
    if drift:
        detail = "\n".join(
            f"           {n}: this shell sees {m}, the server sees {t}"
            for n, m, t in drift
        )
        die("The server's config does not match .env on disk.",
            "uvicorn --reload watches .py files, NOT .env, so a running server\n"
            "         keeps whatever it started with:\n\n" + detail + "\n\n"
            "         Restart it:\n"
            "           uvicorn app.main:app --reload --reload-include '.env' --port 8000")
    ok("server config matches .env")

    g = GraphClient()
    try:
        # ------------------------------------------------------------ 3
        head(3, "Messaging limit (calling needs 2,000+)")
        try:
            lim = await g.get_messaging_limit()
            tier = lim.get("whatsapp_business_manager_messaging_limit", "?")
            if "250" in str(tier):
                die(f"messaging limit is {tier}",
                    "Calling cannot be enabled below 2,000.\n"
                    "         Complete BUSINESS VERIFICATION — this alone lifts you to\n"
                    "         2,000 with no sending volume required.\n"
                    "         Business Settings → Business Info → Start Verification")
            ok(f"messaging limit = {tier}")
        except GraphError as e:
            die(f"could not read messaging limit: {e}",
                "Token is probably expired (the API Setup token lasts 24h) or\n"
                "         lacks whatsapp_business_management.")

        # ------------------------------------------------------------ 4
        head(4, "Calling enabled on the number?")
        try:
            st = (await g.get_settings_for_number()).get("calling", {})
        except GraphError as e:
            die(f"could not read settings: {e}")

        if st.get("sip", {}).get("status") == "ENABLED":
            die("SIP is ENABLED on this number.",
                "SIP DISABLES the Graph API calling endpoints and the `calls`\n"
                "         webhook, which this lab depends on. Turn SIP off in\n"
                "         WhatsApp Manager → Phone numbers → gear → Calls.")

        if "restrictions" in st:
            die(f"Meta has an active restriction: {st['restrictions']}",
                "Wait for expiry. Restrictions follow negative user feedback or\n"
                "         low pickup rates.")

        if st.get("status") != "ENABLED":
            warn("calling is not enabled — enabling it now…")
            try:
                await g.enable_calling(callback_permission=True)
                ok("calling ENABLED (callback permission on)")
            except GraphError as e:
                die(f"could not enable calling: {e}",
                    "Needs whatsapp_business_management with Advanced Access.")
        else:
            ok(f"calling ENABLED, icon={st.get('call_icon_visibility')}, "
               f"callback_permission={st.get('callback_permission_status')}")

        # ------------------------------------------------------------ 5
        head(5, f"Do we have permission to call {to}?")
        try:
            perm = await g.get_call_permission(to)
        except GraphError as e:
            die(f"permission lookup failed: {e}")

        status = (perm.get("permission") or {}).get("status")
        if status == "no_permission":
            print()
            bad("no call permission — and you cannot cold-call on WhatsApp.")
            print(f"\n  {C_BOLD}Fastest route — 7-day TEMPORARY, free, no template:{C_END}")
            print(f"    On {to}, open the chat with your business number and")
            print(f"    {C_BOLD}place a WhatsApp call TO it{C_END} (tap the call icon).")
            print("    Nobody needs to answer — a missed call is enough.")
            print(f"    {C_DIM}This works because callback_permission_status is ENABLED on")
            print(f"    your number, so calling you auto-grants temporary permission.{C_END}")
            print(f"\n  {C_BOLD}Or ask for it explicitly:{C_END}")
            print(f"    1. From {to}, send any message to your business number")
            print("       (opens the 24h window, so no paid template is needed)")
            print(f"    2. python cli.py request-temporary {to}")
            print(f"    3. On the handset tap {C_BOLD}'Temporarily allow calls'{C_END} (7 days)")
            print(f"       {C_DIM}not 'Allow calls', which is permanent{C_END}")
            print(f"\n  Check either way:  python cli.py permission {to}")
            print(f"  Then re-run this script.\n")
            sys.exit(1)

        exp = (perm.get("permission") or {}).get("expiration_time")
        ok(f"permission = {status}" + (f", expires {exp}" if exp else " (no expiry)"))

        for a in perm.get("actions", []):
            if a.get("action_name") == "start_call":
                if a.get("can_perform_action") is False:
                    die("start_call rate limit reached (100 connected calls / 24h)",
                        "Wait for the window to roll over.")
                for lim in a.get("limits", []) or []:
                    ok(f"start_call quota {lim.get('current_usage')}/"
                       f"{lim.get('max_allowed')} per {lim.get('time_period')}")

        # ------------------------------------------------------------ 6
        head(6, "Placing the call")
        print(f"  {C_DIM}~$0.0127/min to a UAE number, billed in 6-second pulses.{C_END}")
        print(f"  {C_BOLD}Watch your phone.{C_END}\n")

        try:
            r = httpx.post(f"{SERVER}/api/call", json={"to": to}, timeout=90)
        except Exception as e:
            die(f"request to lab server failed: {e}")

        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            die(f"call rejected ({r.status_code}): {body}")

        call_id = r.json()["call_id"]
        ok(f"call_id = {call_id}")

        # ------------------------------------------------------------ 7
        head(7, "Waiting for webhooks")
        print(f"  {C_DIM}Meta must reach your webhook for the SDP answer to arrive.")
        print(f"  If nothing appears in 30s, the webhook URL is wrong or the")
        print(f"  `calls` field is not subscribed.{C_END}\n")

        seen: set[str] = set()
        # How long to WATCH. This is not how long the call may last -- see the
        # terminate logic at the end, which no longer kills a healthy call.
        deadline = time.time() + WATCH_SECONDS
        print(f"  {C_DIM}watching for {WATCH_SECONDS}s "
              f"(--seconds N to change){C_END}")
        while time.time() < deadline:
            try:
                calls = httpx.get(f"{SERVER}/calls", timeout=10).json().get("active", [])
            except Exception:
                await asyncio.sleep(1)
                continue

            me = next((c for c in calls if c["call_id"] == call_id), None)
            if me is None:
                if "gone" not in seen:
                    seen.add("gone")
                    print("\n  call ended (you hung up, rejected, or it timed out)")
                break

            state = (me.get("ice_state"), me.get("pc_state"), me.get("accepted"))
            key = str(state)
            if key not in seen:
                seen.add(key)
                ice, pc, acc = state
                print(f"  ice={ice}  pc={pc}  accepted={acc}")
                if pc == "connected":
                    ok("MEDIA CONNECTED — ICE found a path and DTLS completed")
                if acc:
                    ok("ANSWERED")
            await asyncio.sleep(1)

        # ------------------------------------------------------------ 8
        head(8, "Result")
        connected = any("'connected'" in s for s in seen)
        answered = any("True" in s for s in seen)

        if answered and connected:
            print(f"  {C_OK}{C_BOLD}PASS — call rang, you answered, media connected.{C_END}")
            print(f"\n  Everything below the AI works. Next:")
            print(f"    1. set MEDIA_ONLY=0 in .env")
            print(f"    2. test the agent in a browser first: {C_BOLD}open {SERVER}/selftest{C_END}")
            print(f"    3. then: python cli.py call {to}")
        elif answered and not connected:
            print(f"  {C_WARN}{C_BOLD}PARTIAL — call rang and you answered, but media never connected.{C_END}")
            print(f"\n  Signalling is fully working. The media path is not.")
            print(f"  This is the ICE-LITE problem: see DEPLOY.md. You need a public")
            print(f"  IP or TURN. An HTTPS tunnel will never fix it.")
        elif seen:
            print(f"  {C_WARN}Call was placed but never answered.{C_END}")
            print(f"  If the phone never rang: check the number, and confirm the")
            print(f"  `calls` webhook field is subscribed (python cli.py events).")
            print(f"\n  {C_DIM}Careful: 4 consecutive unanswered calls auto-revokes")
            print(f"  your permission.{C_END}")
        else:
            print(f"  {C_BAD}No webhooks at all.{C_END}")
            print(f"  Meta could not reach your webhook. Check the Callback URL in")
            print(f"  App Dashboard → WhatsApp → Configuration, and that `calls`")
            print(f"  is subscribed. Then: python cli.py events")

        # NEVER hang up a call that is working.
        #
        # This used to terminate unconditionally after a 45s watch window, which
        # was fine when the only question was "does it ring and connect". Once
        # the agent existed it meant every conversation was killed mid-sentence
        # at ~40s -- and it looked exactly like the bot dropping the call, which
        # sent us hunting for a fault that was in this script.
        still_live = False
        try:
            active = httpx.get(f"{SERVER}/calls", timeout=10).json().get("active", [])
            still_live = any(c["call_id"] == call_id for c in active)
        except Exception:
            pass

        if still_live and answered:
            print(f"\n  {C_OK}Call is still live — leaving it alone.{C_END}")
            print(f"  {C_DIM}Keep talking; hang up from the handset when you are done.")
            print(f"  The server log will print the transcript and cost on hangup.{C_END}")
        elif still_live:
            # Never answered but still ringing/stuck: clean it up, that is litter.
            print(f"\n  {C_DIM}Terminating an unanswered call…{C_END}")
            try:
                await g.terminate_call(call_id)
            except GraphError:
                pass
        print()
    finally:
        await g.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if "--seconds" in args:
        i = args.index("--seconds")
        try:
            globals()["WATCH_SECONDS"] = int(args[i + 1])
        except (IndexError, ValueError):
            print("--seconds needs a number, e.g. --seconds 300")
            sys.exit(2)
        del args[i:i + 2]

    if len(args) != 1 or not args[0].isdigit():
        print(__doc__)
        print("Give your personal WhatsApp number in E.164 WITHOUT '+':")
        print("  python ringtest.py 971501234567")
        print("\nFor a full conversation, watch for longer:")
        print("  python ringtest.py 971501234567 --seconds 300")
        print("\nThe call is NOT hung up when the window ends — if it is still")
        print("connected the script just stops watching and leaves you talking.")
        sys.exit(2)
    asyncio.run(main(args[0]))
