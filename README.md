# WhatsApp Calling Lab

An experimental AI voice agent on the **WhatsApp Business Calling API**. Python,
Graph API signalling, `aiortc` for the WebRTC media leg, Deepgram → Claude →
ElevenLabs for the conversation.

Built to be run in **three stages**, because if you switch everything on at once
and hear silence, you will not know which of six layers failed.

---

## What's here

```
whatsapp-calling-lab/
├── app/
│   ├── config.py     settings, system prompt, greeting
│   ├── graph.py       ← Graph API client: permissions, calls, settings
│   ├── sdp.py         ← enforces Meta's mandatory media rules
│   ├── audio.py       ← single-SSRC track, resampling, barge-in VAD
│   ├── ai.py          ← streaming STT → LLM → TTS
│   ├── session.py     ← one CallSession per call; WebRTC lifecycle
│   ├── webhooks.py    ← inbound signalling from Meta
│   └── main.py        ← FastAPI app + control API
├── cli.py             ← command line for everything
├── requirements.txt
└── .env.example
```

Two independent planes, matching how the protocol actually works:

| Plane | Files | Carries |
|---|---|---|
| **Signalling** | `graph.py`, `webhooks.py` | ~6 HTTPS messages per call |
| **Media** | `sdp.py`, `audio.py`, `session.py` | ~18,000 SRTP packets per 3-min call |

When something breaks, the symptom tells you the plane. Phone never rings →
signalling. Rings, connects, silence → media (ICE or DTLS, almost always).

---

## Install

```bash
cd whatsapp-calling-lab
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

`aiortc` needs native libraries. On macOS: `brew install ffmpeg opus libvpx srtp`.
On Debian/Ubuntu: `apt install ffmpeg libopus-dev libvpx-dev libsrtp2-dev pkg-config`.

---

## Stage 1 — signalling only (no audio)

Prove your account, permissions and webhooks before touching media. Set
`MEDIA_ONLY=1` in `.env`.

### 1a. Expose your webhook

Meta must be able to reach you over public HTTPS.

**Local, via tunnel:**

```bash
ngrok http 8000
# → https://a1b2c3.ngrok-free.app
```

**Deployed:** any host with public HTTPS. Put it behind nginx/Caddy on `/webhook`.

Either way, note the URL — Meta needs it, and so does the media leg later.

### 1b. Register the webhook with Meta

App Dashboard → your app → WhatsApp → Configuration:

- **Callback URL:** `https://a1b2c3.ngrok-free.app/webhook`
- **Verify token:** the same string as `WA_WEBHOOK_VERIFY_TOKEN` in `.env`
- Click **Verify and save** (start the server first — `GET /webhook` answers the handshake)
- Subscribe to webhook fields: **`calls`**, **`messages`**, and optionally
  `account_update`, `account_settings_update`

### 1c. Start the server and pre-flight

```bash
uvicorn app.main:app --reload --port 8000
```

In another shell:

```bash
python cli.py preflight
```

This catches the three things that block people for hours:

| Warning | Fix |
|---|---|
| `TIER_250` messaging limit | Calling needs 2,000+. **Verify your business** — no sending volume needed. |
| Calling not `ENABLED` | `python cli.py enable-calling` |
| SIP is `ENABLED` | SIP **disables** the Graph API calling endpoints and `calls` webhooks. Turn it off — this lab uses Graph API. |
| `RESTRICTIONS` present | Meta has paused calling on the number. Read the reason and expiry. |

### 1d. Get permission the free way

Fastest possible path — no messaging cost, no template:

> On your test handset: open the chat with the business number → tap the business
> number at the top → scroll to **Business Calling Permission** → **Allow calls**

Verify it landed:

```bash
python cli.py permission 971501234567
```

You get status (`no_permission` / `temporary` / `permanent`), expiry, and your
live remaining quota against the 1-per-day / 2-per-week ceiling.

### 1e. Place a call

```bash
python cli.py call 971501234567
```

Watch the server log. You should see, in order:

```
call initiated id=wacid.HBgL...
[local-offer] codecs={111: 'opus/48000/2'} setup=['actpass'] ptime=['20'] ssrcs=['...']
[local-offer] candidate 185.23.44.10:51234 typ=host
call status wacid.HBgL... -> RINGING
[remote-answer] codecs={111: 'opus/48000/2'} setup=['passive'] ...
remote description applied; ICE + DTLS in progress
ice state=checking → completed
pc state=connected
call status wacid.HBgL... -> ACCEPTED
```

Then:

```bash
python cli.py active
python cli.py hangup wacid.HBgL...
python cli.py events
```

**Do not go to stage 2 until `pc state=connected` appears.** That single line
means ICE found a path and DTLS completed. Everything after depends on it.

---

## Stage 2 — media

Still `MEDIA_ONLY=1`. Goal: `pc state=connected` reliably, on a real network.

**Meta is ICE-LITE.** They will not help traverse NAT — they publish candidates
and wait. If neither side offers a reachable address, the call connects at the
signalling layer and then sits in silence forever. This is *the* most common
failure.

| Your deployment | What to set |
|---|---|
| Cloud VM with public IP | `PUBLIC_IP=<elastic ip>` — `sdp.py` rewrites private candidates |
| Laptop behind home NAT | `TURN_SERVER=turn:host:3478,user,pass` — a tunnel does **not** help media |
| Cloud VM, no NAT | nothing; host candidates are already public |

A tunnel like ngrok forwards **HTTPS only**. It does nothing for UDP media. If
you are developing locally you need either TURN or a cloud host.

Meta's mandatory rules, all enforced in `sdp.py` — `describe()` logs and flags
violations on every call:

- Opus at **48 kHz**, **ptime 20 ms**
- **Exactly one audio SSRC.** Meta's relay rewrites all business audio to one
  fixed SSRC and the client handles a single source. More than one gives
  "severe media corruption ... likely total media failure". Never add a second
  audio track — mix into `OutboundAudioTrack` instead.
- DTMF clock rate 8 kHz
- We must be **ICE-FULL** and take the **CONTROLLING** role (we are the offerer,
  so this is automatic)
- We should be the **DTLS client** — `force_dtls_client()` handles it
- ECDH keys on the DTLS cert, to avoid fragmentation
- **Never wait for Meta's first RTP packet before sending yours.**
  `OutboundAudioTrack` emits silence when idle precisely to avoid this deadlock.

---

## Stage 3 — the AI agent

Set `MEDIA_ONLY=0` and fill in `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`,
`ELEVENLABS_API_KEY`.

```
inbound SRTP → Opus decode → 16 kHz PCM → Deepgram (streaming)
                                              ↓ endpointing
                                          Claude (streaming)
                                              ↓ clause chunks
                                       ElevenLabs (streaming)
                                              ↓ 24 kHz PCM
                              resample 48 kHz → OutboundAudioTrack → SRTP
```

**Latency is the whole game.** A human expects a first word within ~800 ms:

| Stage | Budget |
|---|---|
| STT endpointing | ~250 ms |
| LLM first token | ~300 ms |
| TTS first byte | ~150 ms |
| Network + jitter | ~100 ms |

Nothing may buffer a complete response. Claude streams into clause-sized chunks
(`_CLAUSE_END` in `ai.py`) so the agent starts speaking sentence one while
sentence two is still being written.

**Turn-taking is split deliberately:**

- *Turn ended?* → **Deepgram's endpointing.** Distinguishing "finished" from
  "paused to think" is genuinely hard; the provider is much better at it.
- *Human talking right now?* → **`EnergyVAD`**, only for barge-in. Cheap and fast
  is what matters here. Noisy truck cab causing false triggers? Raise
  `threshold` or `frames_required` in `audio.py`.

On barge-in, `Pipeline.barge_in()` cancels the TTS task, flushes the audio queue,
and calls `llm.cancel_last_turn()` — because the assistant did not actually
finish saying that turn, and leaving it in history makes Claude think it did.

**The agent starts on `ACCEPTED`, never earlier.** Starting on the API's 200 or
on `RINGING` means greeting a ringing phone, and the human answers mid-sentence.
See `_handle_calls` in `webhooks.py`.

---

## Cost while testing

| Item | Cost |
|---|---|
| 3-min call to UAE/KSA (+971/+966) | **$0.0381** |
| 3-min call to India (+91) | $0.0159 |
| Permission granted from handset | **free** |
| Free-form permission request (open window) | probably free — verify |
| Marketing-template permission request | $0.0499 UAE / $0.0501 KSA |
| Call recording + transcription | free today; Meta plans to charge later |

Billing is in **6-second pulses, rounded up** — a 56-second call bills as 10
pulses, not 9.33. Testing is cheap; the permission message is what costs money,
so grant permission from the handset while iterating.

---

## Gotchas that will cost you an afternoon

**PSTN is forbidden on any leg.** Meta's terms: *"Our terms disallow use of PSTN
on any leg of the WhatsApp call."* You may bridge into SIP, but it must stay VoIP
end to end. Rules out routing these calls into a call centre over a telco trunk.

**Always `terminate` explicitly.** Even if RTCP BYE already went out. Meta says
this is what makes billing accurate — skipping it risks over-billing.

**Always return HTTP 200 from the webhook.** Anything else is a delivery failure
and Meta retries. A retry storm during a live call is its own outage. `webhooks.py`
catches handler exceptions and still returns 200.

**Permission does not extend with messaging.** Temporary permission is 168 hours
from *approval*, full stop. Days of active chat buy you nothing on that clock.
And **no webhook fires when it expires** — track `expiration_timestamp` yourself.

**4 consecutive unanswered calls auto-revokes permission.** You get a
`call_permission_reply` webhook with `response: "reject"` and
`response_source: "automatic"`. Clear your cache when you see it.

**The registry is in-process.** `SessionRegistry` is a dict, and the WebRTC peer
connection lives in the process that created it. One instance only. For
production, pin each call to an instance and route webhooks by `call_id`.

**Business-initiated calling is blocked from US, Canada, Egypt, Vietnam and
Nigeria numbers** — based on *your* number's country code, not the recipient's.
UAE and Saudi are fine.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/webhook` | Meta verification handshake |
| POST | `/webhook` | All inbound events |
| GET | `/events` | Last 50 webhooks — point a browser here while testing |
| GET | `/calls` | Active calls with ICE/DTLS state |
| POST | `/hangup/{call_id}` | Terminate |
| GET | `/api/preflight` | Account readiness check |
| POST | `/api/enable-calling` | Enable calling + callback permission |
| GET | `/api/permission/{wa_id}` | Permission status + remaining quota |
| POST | `/api/permission/request` | Free-form permission request |
| POST | `/api/call` | Place a call and run the agent |

---

## Not implemented

- **Inbound (user-initiated) calls.** `webhooks.py` logs them. To handle: build
  an SDP answer from their offer, `POST /calls` with `action: "accept"`.
- **Voicemail.** Announcement must be `audio/ogg` Opus, under 60 s, uploaded with
  `use_case=call_voicemail_announcement`.
- **Tool calling.** The obvious next step: let Claude query your TMS for load
  details mid-call.
- **Escalation to a human.** Needs a second SIP/WebRTC leg to an agent.
- **Metrics.** Track time-to-first-word per turn — it's the number that predicts
  whether people find the agent tolerable.
