# Connecting your WhatsApp number and calling your own phone

Exact click-paths. Roughly 30 minutes if your account is already verified.

> **I never need your credentials.** Every value below goes into `.env` on your
> own machine. Do not paste tokens into chat, tickets, or shared docs — the
> access token can send messages and place billable calls as your business.

---

## Step 0 — What you need

- A **WhatsApp Business Account (WABA)** on the WhatsApp Business Platform (Cloud API), *not* the consumer WhatsApp Business app
- A **Meta app** with the WhatsApp product added
- **Business verification** completed (this is what lifts your messaging limit to 2,000 — the hard gate for calling)
- A business number **not** registered in the US, Canada, Egypt, Vietnam or Nigeria. Business-initiated calling is blocked from those. UAE (+971) and Saudi (+966) are fine.
- A second phone with WhatsApp installed, to be the "customer". **Your own mobile is ideal.**

---

## Step 1 — Collect four values

**App Dashboard** → your app → **WhatsApp** → **API Setup**

| What you see | Goes into `.env` as |
|---|---|
| Temporary access token | `WA_ACCESS_TOKEN` |
| Phone number ID (under "From") | `WA_PHONE_NUMBER_ID` |
| WhatsApp Business Account ID | `WA_BUSINESS_ACCOUNT_ID` |

⚠️ The token on that page **expires in 24 hours.** Fine for today. For anything
ongoing, create a **System User token** instead: Business Settings → Users →
System Users → Add → assign the app with `whatsapp_business_messaging` and
`whatsapp_business_management` → Generate token → set no expiry.

**App Dashboard** → **App Settings** → **Basic** → **App Secret** → Show →
copy into `WA_APP_SECRET`.

Then invent any random string for `WA_WEBHOOK_VERIFY_TOKEN` — you'll paste the
same value into Meta in step 3.

---

## Step 2 — Confirm the messaging limit

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000     # in one shell
python cli.py preflight                        # in another
```

If you see `TIER_250`, calling **cannot** be enabled yet. Fix: complete business
verification (Business Settings → Business Info → Start Verification). This
requires **no sending volume** — verification alone lifts you to 2,000.

You need `TIER_2000` or higher before continuing.

---

## Step 3 — Give Meta a webhook URL

Meta must reach you over public HTTPS.

**Quick, for today:**

```bash
ngrok http 8000
# → https://a1b2c3.ngrok-free.app
```

**Note:** the tunnel handles webhooks only. It does **nothing** for UDP media —
see step 6.

**App Dashboard** → **WhatsApp** → **Configuration**:

1. **Callback URL:** `https://a1b2c3.ngrok-free.app/webhook`
2. **Verify token:** exactly what you put in `WA_WEBHOOK_VERIFY_TOKEN`
3. Make sure the server is running, then click **Verify and save**
   *(our `GET /webhook` answers Meta's challenge; you should see `webhook verified` in the log)*
4. Under **Webhook fields**, subscribe to:
   - **`calls`** ← required
   - **`messages`** ← required (permission replies arrive here)
   - `account_update`, `account_settings_update` ← useful warnings

---

## Step 4 — Enable calling on the number

```bash
python cli.py enable-calling
```

Or in the UI: **WhatsApp Manager** → **Account tools** → **Phone numbers** →
gear icon → **Calls** tab → toggle *Allow voice calls*.

Verify:

```bash
python cli.py preflight
```

You want `calling_status: ENABLED`, no `RESTRICTIONS`, and **`sip: DISABLED`**.

> If SIP is enabled it **disables** the Graph API calling endpoints and the
> `calls` webhook. This lab uses Graph API, so SIP must be off.

WhatsApp clients can take up to 7 days to show the change, though most refresh in
minutes. Force it by opening the business chat and viewing the chat info page.
The server honours the setting regardless of what the app displays.

---

## Step 5 — Give yourself call permission (free)

You cannot cold-call. Skip all messaging cost by granting permission from your
own handset:

1. On your mobile, open WhatsApp
2. Open the chat with your business number *(send it any message first if no chat exists)*
3. **Tap the business number at the top**
4. Scroll to **Business Calling Permission**
5. Tap **Allow calls** — choose **Allow** (permanent) rather than temporary, so it doesn't expire in 7 days while you're iterating

Confirm:

```bash
python cli.py permission 971501234567    # your mobile, E.164, no '+'
```

You want `status: temporary` or `permanent`, and `start_call → allowed now: yes`.
The table also shows your remaining quota against the 1-per-day / 2-per-week
permission-request ceiling.

---

## Step 6 — Make the media path reachable

**This is where most people lose an afternoon.** Meta's stack is **ICE-LITE**: it
publishes its candidates and waits. It will not probe, and it will not help
traverse NAT. If neither side offers a reachable address, the call **connects at
the signalling layer and then sits in total silence.**

| Where you're running | What to set |
|---|---|
| Cloud VM with public/elastic IP | `PUBLIC_IP=185.23.44.10` — we rewrite private candidates |
| Cloud VM, directly public | nothing |
| Laptop behind home/office NAT | `TURN_SERVER=turn:host:3478,user,pass` — **required** |

A laptop behind NAT with only ngrok **will not carry audio.** ngrok forwards TCP;
media is UDP. Either run on a cloud host or stand up coturn.

Also open **UDP** in your security group — a rule for TCP 8000 alone is not enough.

---

## Step 7 — First call, no AI

Keep `MEDIA_ONLY=1`. Prove the network before adding six more moving parts.

```bash
python cli.py call 971501234567
```

Your phone should ring showing your verified business name. Answer it. You'll
hear silence — correct, there's no agent yet.

In the server log, you want this sequence:

```
call initiated id=wacid.HBgL...
[local-offer] codecs={111: 'opus/48000/2'} setup=['actpass'] ptime=['20'] ssrcs=['...']
[local-offer] candidate 185.23.44.10:51234 typ=host      ← must be PUBLIC
call status ... -> RINGING
[remote-answer] codecs={111: 'opus/48000/2'} setup=['passive']
ice state=checking → completed
pc state=connected                                        ← the line that matters
call status ... -> ACCEPTED
```

**`pc state=connected` is the goal.** It means ICE found a path and DTLS
completed. Then:

```bash
python cli.py hangup wacid.HBgL...
```

### If it fails

| Symptom | Cause |
|---|---|
| `409` before dialling | No permission — redo step 5 |
| `138006` | Same, from Meta's side |
| Phone never rings | Calling not enabled, or a restriction — `preflight` |
| Rings, connects, `ice state=failed` | **Step 6.** Private candidate only, or UDP blocked |
| No webhooks at all | Tunnel died, or `calls` field not subscribed |
| `pc state=connected` never appears | ICE or DTLS. Check the logged `setup=` roles |

---

## Step 8 — Test the agent *without* WhatsApp

Before spending calls debugging the AI, talk to it from your browser:

```bash
# .env:  MEDIA_ONLY=0  and your chosen providers configured
open http://localhost:8000/selftest
```

Click **Connect & talk**. Your browser becomes the WhatsApp client — same
pipeline, same 48 kHz / 20 ms frames, same barge-in logic, no Meta involved. Try
interrupting mid-sentence to check barge-in works.

On hangup it prints the transcript and your measured **time-to-first-word**.
Under ~900 ms feels natural; over ~1.5 s feels laggy.

Use headphones. Without them the agent hears itself through your speakers and
barges in on its own voice.

**Get the conversation feeling right here.** Debugging "is my agent any good?"
and "can I reach Meta's media relay?" simultaneously is miserable.

---

## Step 9 — The real thing

Set `MEDIA_ONLY=0`, then:

```bash
python cli.py call 971501234567
```

Answer. The agent greets you **on `ACCEPTED`, not before** — so you won't catch
it mid-sentence. Talk to it. Interrupt it.

```bash
python cli.py active     # live ICE/DTLS state, whether it's speaking
python cli.py events     # raw webhooks Meta sent
```

On hangup the log prints the full transcript and latency stats.

**Cost:** about **3.8 US cents** for a 3-minute call to a UAE or Saudi number.
Billing is 6-second pulses rounded up. Permission granted from the handset is
free, so iterate as much as you like.

---

## Quick reference

```bash
python cli.py preflight                    # account readiness
python cli.py enable-calling               # switch calling on
python cli.py permission <wa_id>           # status + remaining quota
python cli.py request-permission <wa_id>   # ask via message (costs money)
python cli.py call <wa_id>                 # place a call
python cli.py active                       # in-progress calls
python cli.py hangup <call_id>             # terminate
python cli.py events                       # recent webhooks
curl localhost:8000/selftest/stack         # what leaves your network
```
