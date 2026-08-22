# Terminal commands — copy/paste in order

> **If anything returns `code=190`, check the token first:**
> ```bash
> python cli.py token
> ```
> It prints the type, the exact expiry and how long is left. A temporary token's
> expiry is a fixed clock time stamped when Meta issued it — **not** 24 hours from
> when you clicked Generate — so one made a few hours ago can already be dead.
> `STEP-BY-STEP.md` step 6 makes a System User token that never expires.

Target: ring **+971554225948** from **+971526328601** with **7-day temporary**
permission.

---

## Do we need a template? No — if you message us first.

The rule is not "permission requests need a template". It is:

> **Templates are required only to _initiate_ contact** — that is, when no
> customer service window is open.

Inside an open 24-hour window, we may send **any** message type free-form,
including the permission request. It goes as `type: "interactive"` with
`interactive.type: "call_permission_request"` — a non-template message.

**So the order of operations is what avoids the template:**

```
You message +971526328601  ──►  24h window opens
                                      │
                                      ▼
                     we reply with a FREE-FORM permission request
                              (no template, no approval wait)
```

If the window were closed, we would instead need a `call_permission_request`
**template** — created via `POST /<WABA_ID>/message_templates`, submitted to Meta,
waiting on approval, and **billed on every send whether you accept or not**
(~$0.05 marketing / ~$0.016 utility for UAE).

Your single "hi" from the handset is what skips all of that.

> **Small caveat, worth checking once:** Meta's calling docs state flatly that
> "call permission request messages are subject to messaging charges," while the
> pricing docs say non-template messages inside an open window are free. Ambiguous
> for the free-form variant. Expect it to be free or near-free, but glance at the
> billing webhook after your first run rather than trusting either doc.

---

## Step 0 — Install (once)

```bash
cd whatsapse-calling-lab            # your path
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

macOS, if `aiortc` fails to build:

```bash
brew install ffmpeg opus libvpx srtp pkg-config
```

Debian/Ubuntu:

```bash
sudo apt install -y ffmpeg libopus-dev libvpx-dev libsrtp2-dev pkg-config libavdevice-dev
```

Then create your `.env`:

```bash
cp .env.example .env
$EDITOR .env      # fill in the 5 values, and set MEDIA_ONLY=1
```

---

## Step 1 — Local checks (no network, no cost)

```bash
python cli.py doctor
```

Expect **FAIL on "No route for media"** if you're on a laptop. That is correct and
expected — the call will still ring, you just won't hear audio.

Must show **OK on "Credentials in .env"**. If not, stop and fix `.env`.

---

## Step 2 — Start the server + tunnel

Three terminals from here on.

**Terminal 1:**
```bash
source .venv/bin/activate
uvicorn app.main:app --port 8000
```

**Terminal 2:**
```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok-free.app` URL.

**Terminal 3** — everything below runs here:
```bash
source .venv/bin/activate
curl -s localhost:8000/api/health | python3 -m json.tool
```

---

## Step 3 — Register the webhook (browser, one time)

App Dashboard → **WhatsApp** → **Configuration**:

- Callback URL: `https://xxxx.ngrok-free.app/webhook`
- Verify token: your `WA_WEBHOOK_VERIFY_TOKEN`
- **Verify and save** → Terminal 1 should log `webhook verified`
- **Webhook fields** → subscribe **`calls`** and **`messages`**

`messages` is required — the permission reply and your inbound "hi" both arrive on it.

---

## Step 4 — Account readiness

```bash
python cli.py preflight
```

Needs all of:

- messaging limit **not** `TIER_250`
- `calling_status: ENABLED` — if not, run `python cli.py enable-calling`
- `sip: DISABLED` — SIP disables the endpoints this uses
- no `RESTRICTIONS`

---

## Step 5 — Open the 24h window  ← the step that avoids a template

**On the +971554225948 handset:** open WhatsApp, find the chat with
**+971526328601**, send `hi`.

Terminal 1 will log the inbound message. Confirm:

```bash
python cli.py window 971554225948
```

Want: **`OPEN — 23h 5xm left`**. If it says `UNKNOWN`, the webhook wasn't
subscribed to `messages` when you sent it — subscribe, send another message,
re-check.

---

## Step 6 — Request 7-day temporary permission

```bash
python cli.py request-temporary 971554225948
```

Then **on the handset**, in the chat, tap:

| Button | Result |
|---|---|
| `Allow calls` | permanent — **not this** |
| **`Temporarily allow calls`** | **7 days — tap this** |

The command polls and confirms:

```
╭─ 7-day permission granted ──────────────────────────╮
│ status: temporary                                   │
│ expires: 2026-08-20 14:32:05 (6d 23h from now)      │
╰─────────────────────────────────────────────────────╯
```

Verify independently:

```bash
python cli.py permission 971554225948
```

---

## Step 7 — Ring the phone

```bash
python ringtest.py 971554225948
```

**On +971554225948:** WhatsApp rings — its own call screen, your verified business
name. **Answer it.** You will hear silence.

Expected ending:

```
── 8. Result ──
  PARTIAL — call rang and you answered, but media never connected.
```

**`PARTIAL` is the pass from a laptop.** Signalling works end to end; only media is
missing, and that needs a public IP (see DEPLOY.md).

⚠️ Answer the call. Four consecutive unanswered calls **auto-revokes** permission.

---

## Step 8 — Test the AI separately (no WhatsApp, no cost)

```bash
# .env: MEDIA_ONLY=0
# restart Terminal 1, then:
open http://localhost:8000/selftest
```

Click **Connect & talk**. Use **headphones** — otherwise the agent hears itself
through your speakers and interrupts its own sentence. Try talking over it to check
barge-in. On hangup it prints the transcript and time-to-first-word.

---

## The whole thing, condensed

```bash
# terminal 1
uvicorn app.main:app --port 8000

# terminal 2
ngrok http 8000

# terminal 3
python cli.py doctor
python cli.py preflight
#   → register webhook in browser, subscribe calls + messages
#   → send "hi" from +971554225948 to +971526328601
python cli.py window 971554225948            # want OPEN
python cli.py request-temporary 971554225948  # tap "Temporarily allow calls"
python cli.py permission 971554225948         # want status=temporary
python ringtest.py 971554225948               # phone rings
```

## If something breaks

```bash
python cli.py events        # raw webhooks Meta sent
python cli.py active        # live calls with ICE/DTLS state
python cli.py doctor        # local media diagnosis
curl -s localhost:8000/api/health | python3 -m json.tool
```

Paste the output of the failing command. None of these print your access token.
