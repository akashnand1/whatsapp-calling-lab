"""FastAPI app: webhook receiver + a small control API for placing calls."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import get_settings
from .graph import GraphClient, GraphError
from .providers import describe_stack
from .selftest import router as selftest_router
from .session import CallSession, registry
from .webhooks import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

app = FastAPI(title="WhatsApp Calling Lab")
app.include_router(webhook_router)
app.include_router(selftest_router)

@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """A landing page, purely so `/` is not a bare 404.

    Opening the forwarded URL is the natural first thing to do after starting
    the server, and a 404 there reads as "the server is broken" when in fact it
    means "there is no route for /". Worth a few lines to avoid sending someone
    debugging a working system.
    """
    s = get_settings()
    return HTMLResponse(f"""<!doctype html>
<meta charset="utf-8"><title>WhatsApp Calling Lab</title>
<style>
 body{{font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:44rem;
       margin:3rem auto;padding:0 1.25rem;color:#1a1a1a}}
 code{{background:#f3f3f3;padding:.15em .4em;border-radius:3px;font-size:.9em}}
 a{{color:#0a58ca}} li{{margin:.3rem 0}}
 .ok{{color:#1a7f37;font-weight:600}} .no{{color:#b42318;font-weight:600}}
</style>
<h1>WhatsApp Calling Lab</h1>
<p>The server is running. This page has no function beyond telling you that.</p>
<p>
 Number <code>{s.wa_phone_number_id}</code> &middot;
 media path {'<span class="ok">STUN</span>' if s.stun_server
             else '<span class="ok">public IP</span>' if s.public_ip
             else '<span class="ok">TURN</span>' if (s.turn_server or s.turn_static_auth)
             else '<span class="no">none configured</span>'} &middot;
 {'<span class="no">MEDIA_ONLY — no AI</span>' if s.media_only else '<span class="ok">AI agent active</span>'}
</p>
<ul>
 <li><a href="/api/health">/api/health</a> — what this process actually loaded</li>
 <li><a href="/api/preflight">/api/preflight</a> — asks Meta whether calling can work</li>
 <li><a href="/selftest">/selftest</a> — talk to the agent in a browser, no phone call</li>
 <li><a href="/calls">/calls</a> — live call state</li>
 <li><code>POST /webhook</code> — Meta posts here; register this URL in the App Dashboard</li>
</ul>
<p>If Meta cannot verify the webhook, check this port is forwarded
   <b>Public</b> and not Private.</p>
""")


control = APIRouter(prefix="/api")


class CallRequest(BaseModel):
    to: str                      # E.164, no '+' -- e.g. 971501234567
    skip_permission_check: bool = False


@control.get("/health")
async def health() -> dict[str, object]:
    s = get_settings()
    return {
        "ok": True,
        "phone_number_id": s.wa_phone_number_id,
        "graph_version": s.wa_graph_version,
        "media_only": s.media_only,
        "public_ip_configured": bool(s.public_ip),
        "stun_configured": bool(s.stun_server),
        "turn_configured": bool(s.turn_server or s.turn_static_auth),
        "ai_stack": describe_stack(),
    }


@control.get("/preflight")
async def preflight() -> dict[str, object]:
    """Everything that must be true before a call can work.

    Run this first. It catches the three things that block people for hours:
    messaging limit below 2,000, calling not enabled, and an active restriction.
    """
    g = GraphClient()
    out: dict[str, object] = {}
    try:
        try:
            out["messaging_limit"] = await g.get_messaging_limit()
        except GraphError as e:
            out["messaging_limit_error"] = e.payload

        try:
            settings = await g.get_settings_for_number()
            calling = settings.get("calling", {})
            out["calling_status"] = calling.get("status")
            out["callback_permission"] = calling.get("callback_permission_status")
            out["call_icon"] = calling.get("call_icon_visibility")
            out["sip"] = calling.get("sip", {}).get("status", "DISABLED")
            if "restrictions" in calling:
                out["RESTRICTIONS"] = calling["restrictions"]
        except GraphError as e:
            out["settings_error"] = e.payload
    finally:
        await g.close()

    warnings: list[str] = []
    limit = str(out.get("messaging_limit", {}))
    if "TIER_250" in limit:
        warnings.append(
            "Messaging limit is TIER_250. Calling needs 2,000+. Verify your "
            "business to lift it -- no sending volume required."
        )
    if out.get("calling_status") != "ENABLED":
        warnings.append("Calling is not ENABLED on this number. POST /api/enable-calling.")
    if out.get("sip") == "ENABLED":
        warnings.append(
            "SIP is ENABLED, which DISABLES the Graph API calling endpoints and "
            "calls webhooks. This lab uses Graph API, so turn SIP off."
        )
    if "RESTRICTIONS" in out:
        warnings.append("Meta has an active restriction on this number -- see RESTRICTIONS.")
    out["warnings"] = warnings
    return out


@control.post("/enable-calling")
async def enable_calling() -> dict[str, object]:
    g = GraphClient()
    try:
        return await g.enable_calling(callback_permission=True)
    finally:
        await g.close()


@control.get("/permission/{user_wa_id}")
async def permission(user_wa_id: str) -> dict[str, object]:
    g = GraphClient()
    try:
        return await g.get_call_permission(user_wa_id)
    finally:
        await g.close()


class PermissionRequest(BaseModel):
    to: str
    body_text: str = "May we call you to confirm your pickup window?"


@control.post("/permission/request")
async def request_permission(req: PermissionRequest) -> dict[str, object]:
    """Free-form permission request. Requires an OPEN 24h customer service window.

    If this fails, the user has not messaged you in the last 24 hours and you
    need the template variant instead -- which costs a message either way.
    """
    g = GraphClient()
    try:
        return await g.request_permission_freeform(req.to, req.body_text)
    finally:
        await g.close()


@control.post("/call")
async def place_call(req: CallRequest) -> dict[str, object]:
    """Place a business-initiated call and run the AI agent on it."""
    g = GraphClient()
    try:
        # 1. Pre-flight the permission. Cheaper than eating a 138006, and it
        #    tells you your remaining quota against the 1/day, 2/week ceiling.
        if not req.skip_permission_check:
            try:
                perm = await g.get_call_permission(req.to)
            except GraphError as e:
                if e.code == 190:
                    # Two different causes, and the distinction matters: either
                    # the token really expired, or this PROCESS is holding a
                    # stale one. uvicorn --reload watches .py files but NOT .env,
                    # so editing the token does not reach a running server --
                    # which is confusing, because the CLI is a fresh process and
                    # works fine against the same file.
                    raise HTTPException(
                        status_code=401,
                        detail={
                            "error": "Meta rejected the access token (code 190)",
                            "likely_cause": (
                                "This server was started before .env was updated. "
                                "uvicorn --reload does not watch .env, so a running "
                                "process keeps the old token even though the CLI "
                                "sees the new one."
                            ),
                            "fix": (
                                "Restart uvicorn. To have it pick up .env "
                                "automatically in future, start it with: "
                                "uvicorn app.main:app --reload --reload-include '.env' --port 8000"
                            ),
                            "also_check": (
                                "The API Setup token expires after 24h. See "
                                "STEP-BY-STEP.md step 6 for a System User token "
                                "that never expires."
                            ),
                        },
                    ) from e
                raise
            status = (perm.get("permission") or {}).get("status")
            if status == "no_permission":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "no call permission for this user",
                        "hint": "Send a permission request, or grant it from the "
                                "handset: chat -> tap business number -> "
                                "Business Calling Permission -> Allow calls",
                        "permission": perm,
                    },
                )
            can_call = next(
                (
                    a.get("can_perform_action")
                    for a in perm.get("actions", [])
                    if a.get("action_name") == "start_call"
                ),
                None,
            )
            if can_call is False:
                raise HTTPException(
                    status_code=429,
                    detail={"error": "start_call rate limit reached", "permission": perm},
                )
            log.info("permission ok: %s", status)

        # 2. Build the SDP offer (ICE gathering completes inside this call).
        session = CallSession(to=req.to, correlation_id=f"lab-{uuid.uuid4().hex[:12]}")
        registry.add_pending(session)
        try:
            offer = await session.create_offer()
        except Exception as e:
            # Surface the real reason instead of a bare 500. The usual cause is
            # ICE gathering failing against a TURN server -- wrong credentials,
            # unreachable relay, or UDP blocked outbound -- and "Internal Server
            # Error" tells you none of that.
            log.exception("failed to build SDP offer")
            await session.hangup()
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "could not build the SDP offer",
                    "exception": f"{type(e).__name__}: {e}",
                    "hint": (
                        "Usually ICE gathering against TURN. Check TURN_STATIC_AUTH "
                        "is reachable, or clear it and set PUBLIC_IP instead. "
                        "Run: python cli.py doctor"
                    ),
                },
            ) from e

        # 3. Signal it to Meta.
        try:
            call_id = await g.initiate_call(req.to, offer, session.correlation_id)
        except GraphError as e:
            await session.hangup()
            if e.code == 138006:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "138006 -- no call permission", "payload": e.payload},
                ) from e
            raise HTTPException(status_code=502, detail=e.payload) from e

        registry.bind(call_id, session)
        return {
            "call_id": call_id,
            "correlation_id": session.correlation_id,
            "next": "waiting for Call Connect webhook (SDP answer), then ACCEPTED",
        }
    finally:
        await g.close()


app.include_router(control)


@app.on_event("shutdown")
async def _shutdown() -> None:
    for s in registry.all():
        await s.hangup()
