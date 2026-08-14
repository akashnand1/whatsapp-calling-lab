"""Thin client over Meta's Graph API for the WhatsApp Calling endpoints.

Everything here is plain HTTPS + JSON. There is no telecom protocol involved --
this is the entire "signalling plane" when you use the Graph API configuration.

URL shape is always:
    https://graph.facebook.com/<version>/<node-id>/<edge>

where the node is usually your phone number ID and the edge is `calls`,
`messages` or `call_permissions`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings

log = logging.getLogger("graph")


class GraphError(RuntimeError):
    """Raised when Meta returns an error payload."""

    def __init__(self, status: int, payload: dict[str, Any]):
        self.status = status
        self.payload = payload
        err = payload.get("error", {})
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        super().__init__(
            f"Graph API {status}: code={self.code} subcode={self.subcode} "
            f"{err.get('message', payload)}"
        )


class GraphClient:
    def __init__(self) -> None:
        s = get_settings()
        self._s = s
        self._client = httpx.AsyncClient(
            base_url=s.graph_base,
            headers={
                "Authorization": f"Bearer {s.wa_access_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(20.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- plumbing -----------------------------------------------------------

    async def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        r = await self._client.request(method, path, **kw)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text}
        if r.status_code >= 400 or "error" in payload:
            raise GraphError(r.status_code, payload)
        return payload

    # -- settings -----------------------------------------------------------

    async def get_settings_for_number(self) -> dict[str, Any]:
        """GET /<PHONE_NUMBER_ID>/settings

        Also surfaces a `restrictions` block if Meta has paused calling on the
        number -- check this first when calls start failing for no clear reason.
        """
        pid = self._s.wa_phone_number_id
        return await self._request("GET", f"/{pid}/settings")

    async def enable_calling(
        self,
        *,
        callback_permission: bool = True,
        call_icon_visible: bool = True,
    ) -> dict[str, Any]:
        """POST /<PHONE_NUMBER_ID>/settings

        `callback_permission_status: ENABLED` is the cheapest permission source
        you have: when a user calls you, they grant temporary call permission
        automatically and it costs you nothing in messaging fees.
        """
        pid = self._s.wa_phone_number_id
        body = {
            "calling": {
                "status": "ENABLED",
                "call_icon_visibility": "DEFAULT" if call_icon_visible else "DISABLE_ALL",
                "callback_permission_status": "ENABLED" if callback_permission else "DISABLED",
            }
        }
        return await self._request("POST", f"/{pid}/settings", json=body)

    async def get_messaging_limit(self) -> dict[str, Any]:
        """Calling requires a messaging limit of 2,000 or above.

        Note this is a *ceiling*, not a quota you must hit. Verifying your
        business lifts you to TIER_2000 with no sending volume required.
        """
        pid = self._s.wa_phone_number_id
        return await self._request(
            "GET",
            f"/{pid}",
            params={"fields": "whatsapp_business_manager_messaging_limit"},
        )

    # -- permissions --------------------------------------------------------

    async def get_call_permission(self, user_wa_id: str) -> dict[str, Any]:
        """GET /<PHONE_NUMBER_ID>/call_permissions?user_wa_id=...

        Call this before *every* outbound attempt. It returns both the current
        permission status and your live remaining quota, which is far cheaper
        than discovering the limit by eating a 138006 error.

        Response shape:
            permission.status         no_permission | temporary | permanent
            permission.expiration_time  unix ts (absent when permanent)
            actions[].can_perform_action
            actions[].limits[]        max_allowed vs current_usage per window
        """
        pid = self._s.wa_phone_number_id
        return await self._request(
            "GET", f"/{pid}/call_permissions", params={"user_wa_id": user_wa_id}
        )

    async def request_permission_freeform(
        self, to: str, body_text: str
    ) -> dict[str, Any]:
        """Free-form call permission request.

        Only works inside an open 24-hour customer service window, i.e. the user
        has messaged you in the last 24 hours. This is the cheap path -- see the
        note in README about verifying whether it is billed.

        The permission prompt itself is fixed by Meta. You control only the body.
        """
        pid = self._s.wa_phone_number_id
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "call_permission_request",
                "action": {"name": "call_permission_request"},
                "body": {"text": body_text},
            },
        }
        return await self._request("POST", f"/{pid}/messages", json=body)

    async def request_permission_template(
        self, to: str, template_name: str, body_params: list[str], lang: str = "en"
    ) -> dict[str, Any]:
        """Templated call permission request -- works outside the 24h window.

        This is billed at the template's category rate (marketing or utility)
        and is charged whether or not the user accepts.
        """
        pid = self._s.wa_phone_number_id
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": lang},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": p} for p in body_params],
                    }
                ],
            },
        }
        return await self._request("POST", f"/{pid}/messages", json=body)

    async def create_permission_template(
        self, name: str, header: str, body: str, footer: str, category: str = "UTILITY"
    ) -> dict[str, Any]:
        """Create a call_permission_request template.

        A text body is *required* for the template variant. Use UTILITY where the
        contact is genuinely transactional -- it is roughly 3x cheaper than
        MARKETING in the UAE and ~5x in Saudi.
        """
        waba = self._s.wa_business_account_id
        if not waba:
            raise ValueError("WA_BUSINESS_ACCOUNT_ID is not set")
        payload = {
            "name": name,
            "language": "en",
            "category": category,
            "components": [
                {"type": "HEADER", "text": header,
                 "example": {"body_text": [["TRK-84213"]]}},
                {"type": "BODY", "text": body,
                 "example": {"body_text": [["Ahmed", "TRK-84213"]]}},
                {"type": "FOOTER", "text": footer},
                {"type": "call_permission_request"},
            ],
        }
        return await self._request("POST", f"/{waba}/message_templates", json=payload)

    # -- calls --------------------------------------------------------------

    async def initiate_call(
        self, to: str, sdp_offer: str, correlation_id: str | None = None
    ) -> str:
        """POST /<PHONE_NUMBER_ID>/calls  {action: connect}

        Returns the WhatsApp call ID (`wacid....`).

        Error 138006 means you do not hold call permission for this user.
        """
        pid = self._s.wa_phone_number_id
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "action": "connect",
            "session": {"sdp_type": "offer", "sdp": sdp_offer},
        }
        if correlation_id:
            # Comes back on the Call Terminate webhook. Max 512 chars.
            body["biz_opaque_callback_data"] = correlation_id[:512]

        res = await self._request("POST", f"/{pid}/calls", json=body)
        call_id = res["calls"][0]["id"]
        log.info("call initiated id=%s to=%s", call_id, to)
        return call_id

    async def terminate_call(self, call_id: str) -> dict[str, Any]:
        """POST /<PHONE_NUMBER_ID>/calls  {action: terminate}

        Meta is explicit that you must call this even if RTCP BYE already went
        out on the media path, because it is what makes billing accurate.
        Skipping it risks being over-billed.
        """
        pid = self._s.wa_phone_number_id
        body = {
            "messaging_product": "whatsapp",
            "call_id": call_id,
            "action": "terminate",
        }
        return await self._request("POST", f"/{pid}/calls", json=body)

    async def accept_call(self, call_id: str, sdp_answer: str) -> dict[str, Any]:
        """Answer an inbound (user-initiated) call with our SDP answer."""
        pid = self._s.wa_phone_number_id
        body = {
            "messaging_product": "whatsapp",
            "call_id": call_id,
            "action": "accept",
            "session": {"sdp_type": "answer", "sdp": sdp_answer},
        }
        return await self._request("POST", f"/{pid}/calls", json=body)

    async def reject_call(self, call_id: str) -> dict[str, Any]:
        pid = self._s.wa_phone_number_id
        return await self._request(
            "POST",
            f"/{pid}/calls",
            json={
                "messaging_product": "whatsapp",
                "call_id": call_id,
                "action": "reject",
            },
        )

    # -- who else is listening? --------------------------------------------

    async def subscribed_apps(self) -> dict[str, Any]:
        """GET /<WABA_ID>/subscribed_apps

        Lists every Meta app subscribed to webhooks for this WhatsApp Business
        Account. Run this BEFORE changing any Callback URL.

        Why it matters: a WABA can have several apps subscribed at once -- yours
        and a BSP's. Each app has its OWN Callback URL, so adding yours does not
        disturb theirs. But if a vendor's integration runs through the SAME app
        you are editing, overwriting that app's Callback URL redirects their
        production traffic to you and takes their service down.
        """
        waba = self._s.wa_business_account_id
        if not waba:
            raise ValueError("WA_BUSINESS_ACCOUNT_ID is not set")
        return await self._request("GET", f"/{waba}/subscribed_apps")

    # -- analytics ----------------------------------------------------------

    async def call_analytics(self, start: int, end: int) -> dict[str, Any]:
        """Cost, completed-call counts and average duration for the WABA."""
        waba = self._s.wa_business_account_id
        field = (
            f"call_analytics.start({start}).end({end})"
            ".granularity(DAILY).dimensions(['DIRECTION'])"
        )
        return await self._request("GET", f"/{waba}", params={"fields": field})
