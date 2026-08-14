# Runbook: ring +971554225948 with 7-day temporary permission

Your specific test.

| | |
|---|---|
| Business number | **+971526328601** (UAE → business-initiated calling allowed) |
| Target | **+971554225948** |
| Permission | **temporary, 7 days** |
| Cost | permission request + ~$0.0127/min, 6-second pulses |

> **On credentials:** everything below goes into `.env` on **your** machine. Never
> paste the access token into a chat, a ticket, or a shared doc — it can send
> messages and place billable calls as your business. I don't need it and won't
> ask for it.

---

## Part 1 — The five values for `.env`

```bash
WA_ACCESS_TOKEN=EAAJB7ZB...
WA_PHONE_NUMBER_ID=123456789012345
WA_BUSINESS_ACCOUNT_ID=987654321098765
WA_WEBHOOK_VERIFY_TOKEN=trukker-lab-8f2a9c
WA_APP_SECRET=a1b2c3d4e5f6...

MEDIA_ONLY=1
```

### Where each one lives

**1. `WA_ACCESS_TOKEN`**

`developers.facebook.com` → **My Apps** → your app → left sidebar **WhatsApp** →
**API Setup** → the **Temporary access token** box at the top → **Copy**.

⚠️ **This expires in 24 hours.** Fine for today. If tomorrow's run fails on the
messaging-limit step, this is why. For anything ongoing, make a System User token:

> `business.facebook.com` → **Business Settings** → **Users** → **System Users** →
> **Add** → name it, role *Admin* → **Add Assets** → your app, with full control →
> **Generate New Token** → select your app → tick **`whatsapp_business_messaging`**
> and **`whatsapp_business_management`** → expiry **Never** → Generate → **copy it
> now**, it is shown once.

**2. `WA_PHONE_NUMBER_ID`**

Same **API Setup** page. Under **Send and receive messages**, the **From** dropdown
shows `+971 52 632 8601`. Directly beneath it is **Phone number ID** — a ~15-digit
number. That, not the phone number itself.

**3. `WA_BUSINESS_ACCOUNT_ID`**

Same page, just below: **WhatsApp Business Account ID**. Only needed if you later
create templates, but grab it now.

**4. `WA_WEBHOOK_VERIFY_TOKEN`**

You invent this. Any random string — `trukker-lab-8f2a9c` is fine. It is not a
secret Meta issues; it is a shared value proving the verification callback is
really yours. You'll paste the same string into Meta in Part 2.

**5. `WA_APP_SECRET`**

App Dashboard → **App Settings** → **Basic** → **App Secret** → **Show** (asks for
your password) → copy. This lets us verify `X-Hub-Signature-256`, so nobody who
learns your webhook URL can post fake call events.

---

## Part 2 — Webhook, so Meta can call you back

```bash
# shell 1
source .venv/bin/activate
uvicorn app.main:app --port 8000

# shell 2
ngrok http 8000        # → https://xxxx.ngrok-free.app
```

App Dashboard → **WhatsApp** → **Configuration**:

1. **Callback URL:** `https://xxxx.ngrok-free.app/webhook`
2. **Verify token:** exactly your `WA_WEBHOOK_VERIFY_TOKEN`
3. **Verify and save** — server must be running. Look for `webhook verified` in its log.
4. **Webhook fields** → subscribe **`calls`** and **`messages`**

`messages` is not optional here — **the permission reply arrives on it.**

Sanity check:

```bash
python cli.py doctor       # local media diagnosis
python cli.py preflight    # account readiness
```

`doctor` will report media as not viable on a laptop. **Expected.** The call will
still ring; you just won't hear audio. That is Test A.

---

## Part 3 — Get 7-day temporary permission

**The critical thing: temporary vs permanent is the user's choice, not ours.**
There is no API flag. Both buttons appear on the same prompt, so you must tap the
right one.

Because you want *temporary*, do **not** use the business-profile toggle I
suggested earlier — that grants permanent. Use the permission-request flow.

### Step 3a — open the 24-hour window

On **+971554225948**, send any WhatsApp message to **+971526328601**. "hi" is fine.

This opens a customer service window, which lets us send a **free-form** permission
request instead of a paid template. Free-form is the cheap path.

### Step 3b — send the request

```bash
python cli.py request-temporary 971554225948
```

No `+`. The command will:

- refuse if you already hold permission, and tell you which kind
- check you're within the 1-per-24h / 2-per-7-days request limit
- send the request
- wait, polling until you tap

### Step 3c — tap the right button

On the handset a prompt appears in the chat. Two options:

| Button | Result |
|---|---|
| `Allow calls` | **permanent** — not what you want |
| **`Temporarily allow calls`** | **7 days — tap this** |

The command then confirms:

```
╭─ 7-day permission granted ─────────────────╮
│ status: temporary                          │
│ expires: 2026-08-20 14:32:05 (6d 23h from now) │
╰────────────────────────────────────────────╯
```

If you tap the wrong one it says so and tells you how to revoke and retry.

> The 168-hour clock runs from **your approval** and does **not** extend when you
> chat. No webhook fires on expiry — track `expiration_time` yourself.

If step 3b fails complaining about the window, your "hi" was more than 24 hours
ago. Send another and re-run immediately.

---

## Part 4 — Ring it

```bash
python ringtest.py 971554225948
```

Expected on your phone:

- **WhatsApp** rings — its own call screen and ringtone, not your phone dialler
- Shows your business name with the verified badge
- Free for you as recipient; needs data or wifi
- You answer → **silence**

**Silence is the pass.** It proves token, messaging limit, calling enabled,
permission, webhook delivery and SDP exchange all work. Only the media path is
missing, and that needs a public IP.

Expected terminal output:

```
── 5. Do we have permission to call 971554225948? ──
  ✓ permission = temporary, expires 1755689525
  ✓ start_call quota 0/100 per PT24H

── 6. Placing the call ──
  ✓ call_id = wacid.HBgLOTcxNTU0MjI1OTQ4...

── 7. Waiting for webhooks ──
  ice=checking  pc=connecting  accepted=False
  ✓ ANSWERED

── 8. Result ──
  PARTIAL — call rang and you answered, but media never connected.
```

`PARTIAL` is the correct result from a laptop. `PASS` only happens on a public-IP
host.

---

## If it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Fails at step 3 (messaging limit) | Token expired, or TIER_250 | New token; or verify your business |
| `SIP is ENABLED` | SIP disables the Graph API calling endpoints | WhatsApp Manager → Phone numbers → gear → Calls → SIP off |
| `request-temporary` says window closed | No message in last 24h | Send another "hi", re-run at once |
| Prompt never arrives on the phone | `messages` field not subscribed | Part 2 step 4 |
| Rings, but zero webhooks | Webhook URL wrong / ngrok restarted | Re-verify the Callback URL |
| Doesn't ring at all | Permission, or wrong number format | `python cli.py permission 971554225948` |
| Rings, answer, silence | **Expected on a laptop** | See DEPLOY.md for audio |

**Careful:** four consecutive unanswered calls **auto-revokes** your permission.
Answer the test calls.

---

## What to send me

Paste the terminal output of:

```bash
python cli.py doctor
python cli.py preflight
python cli.py permission 971554225948
python ringtest.py 971554225948
```

Redact nothing except the token if it appears (it shouldn't — none of these print
it). I can diagnose from there.
