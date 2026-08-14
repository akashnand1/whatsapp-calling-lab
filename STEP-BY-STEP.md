# Step by step — TruKKer, filled in with your real values

**Confirmed from your account so far**

| Item | Value | Status |
|---|---|---|
| Meta App | TruKKer Partner | App ID `3405368092946688` |
| App mode | In development / Unpublished | fine for testing |
| WABA | TruKKer (the real one, not Test) | `1292846775792529` |
| Calling number | +971 52 632 8601 | Connected, High quality, verified badge |
| Phone number ID | `919208021266743` | ✓ |
| Messaging limit | **100,000 / 24h** | ✓ far above the 2,000 gate |
| Target | +971 55 422 5948 | your handset |

Two values still to collect, and one warning to clear.

---

## Step 1 — Clear the "Messaging may be unavailable" warning ⚠️

On the API Setup page, next to **+971 52 632 8601**, there is an amber warning:

> Messaging may be unavailable. **Review issue**

**Do this:** click **Review issue** and tell me what it says.

**Why first:** this warning sits on the exact number we want to call *from*. It is
usually one of:

| Likely cause | Effect on our test |
|---|---|
| No payment method / billing issue on the WABA | **Blocker** — Meta requires a valid payment method to place calls |
| Number is managed by a BSP (Wati/Gupshup/Infobip) | **Blocker** — a number can only be controlled by one app at a time |
| Business verification or policy alert | Usually not blocking for calls |
| Display-name review pending | Not blocking |

There is no point generating tokens if this turns out to be a BSP conflict, so
let's read it before going further.

---

## Step 2 — Generate the access token

Same page, **Access Token** box at the top (currently empty).

1. Click **Generate access token**
2. A dialog appears asking which WhatsApp Business account — choose **TruKKer**
   (`1292846775792529`), **not** the Test account
3. Click **Copy**
4. Paste it straight into `.env`

> ⏰ **This token expires in 24 hours.** Fine for today's test. Step 6 covers the
> permanent option.
>
> 🔒 Paste it into `.env` only. Don't put it in chat, Slack, or a ticket — it can
> send messages and place billable calls as TruKKer.

---

## Step 3 — Get the App Secret

1. Left sidebar → **App settings** → **Basic**
2. Find **App secret** → click **Show** (it will ask for your Facebook password)
3. Copy → paste into `.env` as `WA_APP_SECRET`

**Why:** lets us verify the `X-Hub-Signature-256` header, so we know call webhooks
genuinely came from Meta and not from anyone who guessed your webhook URL.

---

## Step 4 — Write your `.env`

```bash
cd whatsapp-calling-lab
cp .env.example .env
```

Then edit it so these lines read:

```bash
# --- from Step 2 ---
WA_ACCESS_TOKEN=<paste the generated token>

# --- already known, copy exactly ---
WA_PHONE_NUMBER_ID=919208021266743
WA_BUSINESS_ACCOUNT_ID=1292846775792529
WA_GRAPH_VERSION=v26.0

# --- you invent this one, any random string ---
WA_WEBHOOK_VERIFY_TOKEN=trukker-lab-8f2a9c

# --- from Step 3 ---
WA_APP_SECRET=<paste the app secret>

# --- start with no AI, no audio ---
MEDIA_ONLY=1
```

Verify locally — no network calls, nothing charged:

```bash
source .venv/bin/activate
python cli.py doctor
```

Expect **OK** on "Credentials in .env" and **FAIL** on "No route for media".
That FAIL is correct on a laptop: the call will still ring, you just won't hear
audio.

---

## Step 5 — Enable calling on +971 52 632 8601

Do this in the UI rather than by API — it avoids needing Advanced Access on
`whatsapp_business_management`, which an unpublished app may not have.

1. Go to **WhatsApp Manager** → **Account tools** → **Phone numbers**
2. Find **+971 52 632 8601**
3. Click the **gear icon** on its row
4. Open the **Calls** tab
5. Turn **on**:
   - **Allow voice calls**
   - **Display call buttons** (optional, but it lets your customers call you too)
6. If there is a SIP / Developer settings section, leave SIP **OFF** — SIP disables
   the Graph API calling endpoints this lab uses

Then confirm from the terminal:

```bash
uvicorn app.main:app --port 8000    # terminal 1
python cli.py preflight             # terminal 2
```

Want to see: `calling_status: ENABLED`, `sip: DISABLED`, no `RESTRICTIONS`.

---

## Step 6 — Optional: a token that doesn't expire

Only if you want to keep testing past 24 hours.

1. `business.facebook.com` → **Business Settings**
2. **Users** → **System Users** → **Add**, name it e.g. `whatsapp-calling-lab`, role **Admin**
3. **Add Assets** → **Apps** → **TruKKer Partner** → toggle **Manage app**
4. **Add Assets** → **WhatsApp Accounts** → **TruKKer** → full control
5. **Generate New Token** → app **TruKKer Partner**
6. Tick **`whatsapp_business_messaging`** and **`whatsapp_business_management`**
7. Expiration **Never** → **Generate**
8. **Copy it immediately** — it is shown once only

Replace `WA_ACCESS_TOKEN` in `.env`.

---

## Step 7 — Webhook, so Meta can call you back

```bash
# terminal 1
uvicorn app.main:app --port 8000

# terminal 2
ngrok http 8000        # copy the https URL
```

Back in the App Dashboard: WhatsApp use case → **Configuration**

1. **Callback URL:** `https://xxxx.ngrok-free.app/webhook`
2. **Verify token:** exactly your `WA_WEBHOOK_VERIFY_TOKEN`
3. **Verify and save** → terminal 1 must log `webhook verified`
4. **Webhook fields** → subscribe **`calls`** AND **`messages`**

Both are required. `calls` carries the call lifecycle; `messages` carries your
inbound "hi" and the permission reply.

---

## Step 8 — Open the 24h window (this is what avoids a paid template)

**On +971 55 422 5948:** open WhatsApp, find the chat with **+971 52 632 8601**,
send `hi`.

```bash
python cli.py window 971554225948
```

Want: **OPEN — 23h 5xm left**.

If it says UNKNOWN, `messages` wasn't subscribed when you sent it. Subscribe, send
another message, re-check.

---

## Step 9 — Request 7-day temporary permission

```bash
python cli.py request-temporary 971554225948
```

**On the handset**, a prompt appears in the chat. Tap:

| Button | Result |
|---|---|
| `Allow calls` | permanent — **not this** |
| **`Temporarily allow calls`** | **7 days — tap this one** |

Confirm:

```bash
python cli.py permission 971554225948     # want status=temporary
```

---

## Step 10 — Ring your phone

```bash
python ringtest.py 971554225948
```

WhatsApp rings on +971554225948, showing the TruKKer verified name. **Answer it.**
You will hear silence.

Expected ending:

```
── 8. Result ──
  PARTIAL — call rang and you answered, but media never connected.
```

**`PARTIAL` is the pass from a laptop.** Cost ≈ 3.8 cents.

⚠️ Answer it — four consecutive unanswered calls auto-revokes your permission.

---

## Order of operations, condensed

```
1.  Review issue      ← tell me what it says before anything else
2.  Generate token    → .env
3.  App Secret        → .env
4.  python cli.py doctor
5.  WhatsApp Manager → gear on +971526328601 → Calls → on
6.  (optional) System User token
7.  ngrok + webhook, subscribe calls + messages
8.  send "hi" from your handset → python cli.py window 971554225948
9.  python cli.py request-temporary 971554225948 → tap "Temporarily allow"
10. python ringtest.py 971554225948
```
