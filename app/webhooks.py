"""Webhook receiver -- the inbound half of the signalling plane.

Meta cannot hold a connection open to you, so you give them a URL and they POST
to it. Two things arrive here:

  field="calls"     connect / ringing / accepted / rejected / terminate
  field="messages"  call_permission_reply, inbound messages, voicemail audio

Every payload is logged verbatim. When you are learning this API, the logs are
the documentation -- Meta's examples are abridged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response

from .config import get_settings
from .graph import GraphClient
from .session import registry

log = logging.getLogger("webhook")
router = APIRouter()

# Calls-related events only, newest last. Customer message bodies are never
# retained here -- see the privacy note in receive().
EVENT_LOG: list[dict[str, Any]] = []

# Tally of inbound traffic belonging to another app on the same number. Counted
# so you can see the fan-out is happening, without recording who or what.
_OTHER_TRAFFIC: dict[str, int] = {"count": 0}


@router.get("/webhook")
async def verify(request: Request) -> Response:
    """Meta's one-time verification handshake.

    Meta GETs your URL with hub.challenge and the verify token you configured.
    Echo the challenge back as plain text or the subscription will not save.
    """
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get(
        "hub.verify_token"
    ) == get_settings().wa_webhook_verify_token:
        log.info("webhook verified")
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    log.warning("webhook verification failed: %s", dict(q))
    return Response(status_code=403, content="forbidden")


def _is_permission_reply(payload: dict[str, Any]) -> bool:
    """True only for call_permission_reply events, which we do want to keep."""
    for e in payload.get("entry", []):
        for c in e.get("changes", []):
            for m in c.get("value", {}).get("messages", []):
                if (m.get("interactive") or {}).get("type") == "call_permission_reply":
                    return True
    return False


def _signature_ok(body: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256 so you know the POST really came from Meta."""
    secret = get_settings().wa_app_secret
    if not secret:
        return True  # not configured; skip (fine for a lab, not for production)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@router.post("/webhook")
async def receive(request: Request) -> Response:
    raw = await request.body()

    if not _signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        log.error("bad webhook signature -- rejecting")
        return Response(status_code=401)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.error("non-JSON webhook body: %r", raw[:400])
        return Response(status_code=200)  # 200 anyway, or Meta will retry forever

    # PRIVACY: a WABA fans webhooks out to EVERY subscribed app, so if a vendor
    # bot also runs on this number we receive a copy of real customer traffic.
    # Retain only calls-related payloads; never persist customer message bodies.
    fields = {
        c.get("field")
        for e in payload.get("entry", [])
        for c in e.get("changes", [])
    }
    if fields & {"calls", "account_update", "account_settings_update"}:
        EVENT_LOG.append(payload)
    elif _is_permission_reply(payload):
        EVENT_LOG.append(payload)      # needed to observe permission decisions
    # else: inbound customer message — handled transiently, never stored.

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})
            try:
                if field == "calls":
                    await _handle_calls(value)
                elif field == "messages":
                    await _handle_messages(value)
                elif field in ("account_update", "account_settings_update"):
                    log.warning("%s: %s", field, json.dumps(value)[:800])
            except Exception:
                # Never let a handler error turn into a non-200. Meta retries,
                # and a retry storm during a live call is its own problem.
                log.exception("handler failed for field=%s", field)

    # Always 200. Meta treats anything else as a delivery failure and retries.
    return Response(status_code=200)


async def _handle_calls(value: dict[str, Any]) -> None:
    # --- status webhooks: RINGING / ACCEPTED / REJECTED ---
    for st in value.get("statuses", []):
        call_id = st.get("id")
        status = st.get("status")
        log.info("call status %s -> %s", call_id, status)
        session = registry.get(call_id) if call_id else None
        if not session:
            continue

        if status == "ACCEPTED":
            # The human picked up. Only now does the agent start talking.
            await session.on_accepted()
        elif status in ("REJECTED",):
            await session.hangup()
            registry.pop(call_id)

    # --- call event webhooks: connect / terminate ---
    for call in value.get("calls", []):
        call_id = call.get("id")
        event = call.get("event")
        session = registry.get(call_id) if call_id else None

        if event == "connect":
            direction = call.get("direction")
            sdp = (call.get("session") or {}).get("sdp")

            if direction == "BUSINESS_INITIATED":
                # Meta's SDP answer to the offer we sent. Apply it to bring up
                # ICE -> DTLS -> SRTP.
                if session and sdp:
                    await session.accept_answer(sdp)
                elif not session:
                    log.error("connect for unknown call_id=%s", call_id)
            else:
                # A WhatsApp user is calling US. Answer it with the AI agent.
                if not sdp:
                    log.error("inbound connect with no SDP offer, call_id=%s", call_id)
                    continue
                if session:
                    log.info("already handling inbound %s", call_id)
                    continue
                await _answer_inbound(call_id, call.get("from", ""), sdp)

        elif event == "terminate":
            # Our app is subscribed to `calls` for the WHOLE WhatsApp Business
            # Account, so Meta sends us the termination of every call on that
            # number -- including the vendor's production traffic. Those arrive
            # with no correlation id and no duration, and there were dozens of
            # them interleaved with our own call, which made the log unreadable
            # at exactly the moment we needed to read it. Log them at DEBUG.
            ours = session is not None
            (log.info if ours else log.debug)(
                "call terminated id=%s status=%s duration=%ss correlation=%s%s",
                call_id,
                call.get("status"),
                call.get("duration"),
                call.get("biz_opaque_callback_data"),
                "" if ours else "   [not ours — another app on this WABA]",
            )
            for err in call.get("errors", []) or []:
                # Someone else's failed call is not our error.
                (log.error if ours else log.debug)("call error: %s", err)
            if session:
                if session.pipeline:
                    log.info("transcript:\n%s", session.transcript_text())
                    # Report what the call actually cost, next to the transcript.
                    # Per-call economics decide whether this scales to a fleet,
                    # and a number in the log beats a spreadsheet estimate.
                    try:
                        st = session.pipeline.stats()
                        c = st.get("cost") or {}
                        mins = (call.get("duration") or 0) / 60
                        wa = mins * 0.0127          # UAE rate, 6-second pulses
                        llm = c.get("llm_usd")
                        log.info(
                            "cost: LLM $%s (%s turns, %s in / %s out / %s cached) "
                            "+ WhatsApp $%.3f (%.1f min) = $%s",
                            llm, c.get("api_calls"), c.get("tokens_in"),
                            c.get("tokens_out"), c.get("cache_read"), wa, mins,
                            f"{llm + wa:.3f}" if isinstance(llm, float) else "?",
                        )
                        if c.get("hint"):
                            log.info("cost hint: %s", c["hint"])
                    except Exception:
                        log.debug("could not compute call cost", exc_info=True)
                await session.hangup()
                registry.pop(call_id)


async def _answer_inbound(call_id: str, caller: str, offer_sdp: str) -> None:
    """Answer a user-initiated call with the AI agent.

    Build an SDP answer from their offer, then POST action=accept. The agent
    starts when media connects (see CallSession.accept_inbound) rather than on an
    ACCEPTED webhook -- Meta only sends those for calls we place.

    Note: answering inbound calls is also how you protect the number's standing.
    Meta restricts calling on numbers with low pickup rates, and hides the call
    button. An agent that always answers is the cheapest insurance against that.
    """
    from .session import CallSession   # imported here to avoid a circular import

    log.info("inbound call %s from %s — answering", call_id, caller)
    session = CallSession(to=caller, direction="USER_INITIATED")
    session.call_id = call_id
    registry.bind(call_id, session)

    g = GraphClient()
    try:
        answer = await session.accept_inbound(offer_sdp)
        await g.accept_call(call_id, answer)
        log.info("accepted inbound %s — waiting for media", call_id)
    except Exception:
        log.exception("failed to answer inbound %s; rejecting", call_id)
        try:
            await g.reject_call(call_id)
        except Exception:
            log.warning("reject also failed for %s", call_id)
        await session.hangup()
        registry.pop(call_id)
    finally:
        await g.close()


async def _handle_messages(value: dict[str, Any]) -> None:
    for msg in value.get("messages", []):
        interactive = msg.get("interactive") or {}

        if interactive.get("type") == "call_permission_reply":
            reply = interactive["call_permission_reply"]
            log.info(
                "PERMISSION %s from=%s permanent=%s expires=%s source=%s",
                reply.get("response"),
                msg.get("from"),
                reply.get("is_permanent"),
                reply.get("expiration_timestamp"),
                reply.get("response_source"),
            )
            # response_source="automatic" + response="reject" is the auto-revoke
            # that fires after 4 consecutive unanswered calls. Clear any cached
            # permission when you see it.
            continue

        if msg.get("type") == "audio":
            # A voicemail arrives here too -- same schema as an audio message,
            # except messages[].id is a call ID (wacid...) not a message ID.
            mid = msg.get("id", "")
            if mid.startswith("wacid."):
                log.info("voicemail for call %s", mid)
            continue

        # Any other inbound message is another app's business (e.g. a vendor bot
        # sharing this number). We are a passive observer: do not log the sender
        # or the content, and do not reply. Count it and move on.
        _OTHER_TRAFFIC["count"] += 1


@router.get("/window/{user_wa_id}")
async def window(user_wa_id: str) -> dict[str, Any]:
    """Is the 24-hour customer service window open for this user?

    There is no Meta API for this -- you only discover a closed window by getting
    an error. But we *do* receive a webhook whenever the user messages us, so we
    can infer it from the last inbound message we saw.

    Why it matters: inside an open window we may send a FREE-FORM call permission
    request (no template, not billed as a template). Outside it, we must use an
    approved `call_permission_request` template, which has to be created and
    approved by Meta first and is billed on every send.

    Caveat: this only knows about messages received by *this process*. If the
    server was restarted, or the message arrived before you set the webhook up,
    it will report unknown even though the window may well be open.
    """
    import time as _time

    last: int | None = None
    for payload in EVENT_LOG:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                for msg in change.get("value", {}).get("messages", []):
                    if msg.get("from") != user_wa_id:
                        continue
                    try:
                        ts = int(msg.get("timestamp", 0))
                    except (TypeError, ValueError):
                        continue
                    if last is None or ts > last:
                        last = ts

    if last is None:
        return {
            "user": user_wa_id,
            "state": "unknown",
            "detail": "No inbound message from this number seen by this process.",
            "hint": "Send any WhatsApp message from that handset to your business "
                    "number, then re-check. If you already did, the server may have "
                    "restarted since.",
        }

    age = int(_time.time()) - last
    remaining = 24 * 3600 - age
    return {
        "user": user_wa_id,
        "state": "open" if remaining > 0 else "closed",
        "last_inbound_unix": last,
        "age_seconds": age,
        "remaining_seconds": max(0, remaining),
        "remaining_human": (
            f"{remaining // 3600}h {remaining % 3600 // 60}m" if remaining > 0 else "expired"
        ),
        "free_form_allowed": remaining > 0,
    }


@router.get("/events")
async def events() -> dict[str, Any]:
    """Everything Meta has sent this process. Point a browser at it while testing."""
    return {"count": len(EVENT_LOG), "events": EVENT_LOG[-50:]}


@router.get("/calls")
async def active_calls() -> dict[str, Any]:
    return {
        "active": [
            {
                "call_id": s.call_id,
                "to": s.to,
                "accepted": s.accepted,
                "duration_s": round(s.duration, 1),
                "pc_state": s.pc.connectionState if s.pc else None,
                "ice_state": s.pc.iceConnectionState if s.pc else None,
                "speaking": bool(s.pipeline and s.pipeline.is_speaking),
            }
            for s in registry.all()
        ]
    }


@router.post("/hangup/{call_id}")
async def hangup(call_id: str) -> dict[str, Any]:
    g = GraphClient()
    try:
        await g.terminate_call(call_id)
    finally:
        await g.close()
    s = registry.pop(call_id)
    if s:
        await s.hangup()
    return {"terminated": call_id}
