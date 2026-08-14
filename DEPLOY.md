# Getting audio to actually work

Your number **+971526328601** is a UAE number, so business-initiated calling is
allowed (US, Canada, Egypt, Vietnam and Nigeria are the blocked origins). Rate is
**$0.0127/min**, billed in 6-second pulses.

There are two separate tests, and it is worth being clear which one you are doing.

| Test | Proves | Runs from |
|---|---|---|
| **A — does it ring?** | account, permission, calling config, webhook, SDP exchange | **your laptop, today** |
| **B — can I hear it?** | ICE, DTLS, SRTP, Opus, the AI | **needs a public IP** |

Do A first. It takes 15 minutes, costs about four cents, and rules out five of the
six things that can be wrong.

---

## Test A — make it ring, from your laptop

You will hear **silence** when you answer. That is the expected pass.

```bash
# .env
MEDIA_ONLY=1
WA_ACCESS_TOKEN=...            # App Dashboard → WhatsApp → API Setup
WA_PHONE_NUMBER_ID=...         # the ID, not +971526328601
WA_WEBHOOK_VERIFY_TOKEN=any-random-string
```

```bash
# shell 1
uvicorn app.main:app --port 8000

# shell 2
ngrok http 8000                # → https://xxxx.ngrok-free.app
```

Register the webhook — **App Dashboard → WhatsApp → Configuration**:

- Callback URL: `https://xxxx.ngrok-free.app/webhook`
- Verify token: same string as `WA_WEBHOOK_VERIFY_TOKEN`
- **Verify and save** (server must be running)
- Subscribe to **`calls`** and **`messages`**

Grant yourself permission on your handset — free, 20 seconds:

> WhatsApp → chat with +971526328601 → tap the number at top →
> **Business Calling Permission** → **Allow calls** → choose **Allow** (permanent)

Then:

```bash
python ringtest.py <your-personal-number>    # E.164, no '+'
```

It checks everything in order and stops at the first real blocker with the exact
fix. Expected outcome: **your phone rings showing your verified business name, you
answer, you hear nothing.** That is a pass for Test A.

### Why silence, and why ngrok can't fix it

ngrok forwards **TCP**. Media is **UDP**. The tunnel carries your webhooks
perfectly and does nothing at all for audio.

Compounding it: Meta's VoIP stack is **ICE-LITE**. It publishes its candidates and
waits — it will not probe, and it will not traverse NAT on your behalf. So the
reachable address has to be yours. Behind home or office NAT, you have none.

---

## Test B — audio. Pick one of three.

### Option 1: cloud VM with a public IP  ← simplest

Any provider. UAE region (AWS `me-central-1`, Azure UAE North) keeps latency low
for GCC callers, which matters when your budget is 800 ms.

```bash
# on the VM
sudo apt update
sudo apt install -y python3.11 python3.11-venv ffmpeg \
     libopus-dev libvpx-dev libsrtp2-dev pkg-config libavdevice-dev

git clone <your repo> && cd whatsapp-calling-lab
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in
```

**Firewall — the step everyone forgets.** Open **UDP**, not just TCP:

| Port | Protocol | Why |
|---|---|---|
| 443 or 8000 | TCP | webhooks from Meta |
| **1024–65535** | **UDP** | RTP/SRTP media. Ephemeral, so it needs a range. |

A security group with only TCP 8000 gives you exactly the silent-call symptom.

If the VM sits behind 1:1 NAT (an AWS elastic IP is the usual case), aiortc only
sees the private address, so tell it:

```bash
PUBLIC_IP=<your elastic ip>
```

`sdp.py` rewrites private host candidates to that address. Verify with:

```bash
python cli.py preflight     # then check the logged candidates on a call
```

You want `candidate <public-ip>:<port> typ host` in the offer. A private address
there means the call will be silent.

### Option 2: coturn, if the box must stay on-prem

```bash
sudo apt install -y coturn
```

```conf
# /etc/turnserver.conf
listening-port=3478
fingerprint
lt-cred-mech
user=trukker:<a-strong-password>
realm=trukker.com
external-ip=<coturn's public ip>
min-port=49152
max-port=65535
```

```bash
TURN_SERVER=turn:turn.trukker.com:3478,trukker,<a-strong-password>
```

Media relays through coturn, so latency and bandwidth both go up — but it works
from anywhere, and it stays on infrastructure you own. Given your data-residency
preference this may suit you better than a public STUN server ever would.

### Option 3: prove the agent works without solving networking at all

This is the shortcut worth knowing. The **`/selftest`** page uses your browser as
the WhatsApp client, over localhost — no NAT, no Meta, no cost:

```bash
# .env: MEDIA_ONLY=0
open http://localhost:8000/selftest
```

Same pipeline, same 48 kHz / 20 ms frames, same barge-in. It reports measured
time-to-first-word. Use **headphones** — otherwise the agent hears itself through
your speakers and interrupts its own sentence.

**Do this before Test B.** Debugging "is my agent any good?" and "can Meta reach
my media port?" at the same time is genuinely miserable, and they are completely
independent problems.

---

## Reading a failure

| Symptom | Plane | Cause |
|---|---|---|
| `409` / `138006` before dialling | — | No permission. Grant on handset. |
| Phone never rings, no webhooks | signalling | Callback URL wrong, or `calls` not subscribed |
| Phone never rings, webhooks arrive | signalling | Calling not enabled, or an active restriction |
| Rings → answer → **silence** | **media** | **No public candidate, or UDP blocked** |
| `ice state=failed` | media | Same |
| Audio one direction only | media | Firewall asymmetric — check UDP both ways |
| Choppy / robotic | media | Loss, jitter, or relaying via TURN |
| Connects, then agent says nothing | AI | Check API keys; test at `/selftest` |

The `pc state=connected` log line is the one that matters. It means ICE found a
path **and** DTLS completed. Nothing above it can work until it appears.

---

## Ordered checklist

- [ ] `.env` filled, `MEDIA_ONLY=1`
- [ ] `python cli.py preflight` → limit ≥ 2,000, calling `ENABLED`, SIP `DISABLED`, no restrictions
- [ ] Webhook verified, `calls` + `messages` subscribed
- [ ] Permission granted from your handset (permanent)
- [ ] **`python ringtest.py <your-number>` → phone rings** ← Test A done
- [ ] `/selftest` in a browser with headphones → agent converses, latency under ~900 ms
- [ ] Deployed to a public-IP host, UDP range open, `PUBLIC_IP` set if behind 1:1 NAT
- [ ] `ringtest.py` again → `pc state=connected`
- [ ] `MEDIA_ONLY=0`, `python cli.py call <your-number>` → talk to it

---

## One note on the token

The token from **API Setup expires in 24 hours.** If `ringtest.py` fails at step 3
tomorrow, that is almost certainly why. For anything ongoing, create a System User
token: Business Settings → Users → System Users → Add → assign the app with
`whatsapp_business_messaging` + `whatsapp_business_management` → Generate token →
no expiry.

Keep it in `.env` on your own machine. It can place billable calls as your
business, so treat it like a production database password — and there is never a
reason to paste it into a chat window, including to me.
