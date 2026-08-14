# Third parties and data residency

You asked whether anything here leaves your approved systems. **In the version I
first wrote, yes — four external services, including one you never asked for.**
This document lists all of them and how to remove each.

Defaults in `.env.example` have since been changed to the **self-hosted options**,
so out of the box nothing but Meta is contacted.

---

## What touches what

For one call, the sensitive payloads are: **raw caller audio**, the **transcript**
of what they said, and the **agent's script**.

| Service | Receives | Avoidable? | Default now |
|---|---|---|---|
| **Meta / WhatsApp** | All call audio — they relay the media | **No.** Unavoidable by design. | required |
| **Deepgram** (STT) | Raw caller audio | Yes → `whisper_local` | **off** |
| **ElevenLabs** (TTS) | Agent's script text | Yes → `piper_local` | **off** |
| **Anthropic API** (LLM) | Full transcript | Yes → `bedrock` or self-host | **`bedrock`** |
| **Public STUN** | Your media server's public IP:port | Yes → `PUBLIC_IP` or own coturn | **off** |
| **ngrok / tunnel** | Every webhook, incl. phone numbers | Yes → your own HTTPS endpoint | your choice |
| **PyPI** | Nothing at runtime; build-time only | Mirror internally if required | build only |

### The one you didn't ask for

My first version defaulted `STUN_SERVER` to `stun:stun.l.google.com:19302`. That
means **Google would learn your media server's public IP and port on every
call.** Low sensitivity, but it was an unrequested outbound dependency and it
should never have been a default. It is now empty.

### Meta is unavoidable — be clear-eyed about it

Meta relays the media. There is no configuration in which call audio does not
pass through their infrastructure; that is what the product *is*. Two further
notes:

- Meta offers **call recording and transcription** as features. They are free
  today. Do not enable them unless you intend Meta to hold that data.
- Their terms **forbid PSTN on any leg** of a WhatsApp call, so you cannot route
  audio out to your own telco to keep it in-country.

If call audio transiting Meta is itself unacceptable to your compliance position,
then WhatsApp Calling is the wrong channel — no architecture fixes that.

---

## Fully self-hosted configuration

```bash
STT_PROVIDER=whisper_local
WHISPER_MODEL=small            # 'medium'/'large-v3' if you have a GPU
WHISPER_DEVICE=cpu             # 'cuda' with a GPU

LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://10.0.1.20:8000/v1   # your vLLM / Ollama
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct

TTS_PROVIDER=piper_local
PIPER_BIN=/usr/local/bin/piper
PIPER_MODEL=/opt/voices/en_US-amy-medium.onnx
PIPER_RATE=22050

PUBLIC_IP=185.23.44.10         # no STUN needed
STUN_SERVER=
```

Nothing but Meta is contacted. Verify with:

```bash
curl localhost:8000/selftest/stack
```

which prints each stage and whether it leaves your network.

---

## The middle path — probably what you actually want

Full self-hosting costs real quality and latency. A pragmatic compromise:

```bash
STT_PROVIDER=whisper_local     # audio is the most sensitive; keep it in-house
LLM_PROVIDER=bedrock           # Claude inside YOUR AWS account and region
AWS_REGION=<a region you have approved>
TTS_PROVIDER=piper_local       # or elevenlabs: only the agent's own script leaves
```

Reasoning: **caller audio is the sensitive asset**, so keep STT local. The
agent's outbound script is written by you and contains no customer data, so
sending it to a TTS vendor is a much smaller exposure than sending audio.

`LLM_PROVIDER=bedrock` routes Claude through your own AWS account, under your
existing AWS agreements, in a region you pick. Check which regions currently
offer the model you want before committing — availability moves.

---

## The latency cost of self-hosting

This is the real trade-off, and it is not small.

| Stack | Time-to-first-word | Feel |
|---|---|---|
| Deepgram + Claude + ElevenLabs | ~700–900 ms | natural |
| Whisper (GPU) + Bedrock + Piper | ~900 ms–1.3 s | acceptable |
| Whisper (CPU) + local 7B + Piper | ~1.5–2.5 s | noticeably laggy |

Most of the gap is **STT endpointing**. Whisper is not a streaming model, so
`WhisperLocalSTT` buffers speech, waits for silence, then transcribes the whole
utterance. Deepgram decides "the human has stopped talking" far more accurately.

Two consequences worth planning for:

- A driver who pauses mid-sentence may get cut off. Raise `silence_ms` in
  `WhisperLocalSTT` if you see it.
- **Put a GPU behind Whisper if you self-host.** On CPU, transcription alone is
  0.5–1.5 s per turn and the agent will feel slow no matter what else you tune.

The self-test page reports measured time-to-first-word per turn, so you can
compare stacks on your own hardware rather than trusting these estimates.

---

## Credentials — how to handle them

**I never need, and should never be given, any of your credentials.** Not the
access token, not the App Secret, not an API key.

- Put them in `.env` on your own machine. `.env` is gitignored.
- `WA_ACCESS_TOKEN` can send messages and place billable calls as your business.
  Treat it like a production database password.
- For anything beyond experimentation, use a **System User token** rather than a
  temporary user token — the temporary ones from API Setup expire in 24 hours.
- Set `WA_APP_SECRET` so webhook signatures are verified. Without it, anyone who
  learns your webhook URL can post fake call events at you.
- Rotate the token if it ever lands in a log, a screenshot, or a chat window.

---

## Before you go to production

- [ ] Decide whether Meta relaying call audio is acceptable to compliance. Nothing downstream matters if it is not.
- [ ] Leave Meta's call recording and transcription **off** unless you want them holding it.
- [ ] Replace the tunnel with your own HTTPS endpoint.
- [ ] Set `WA_APP_SECRET` and confirm signature verification rejects a forged POST.
- [ ] Set `PUBLIC_IP` or run your own coturn; keep `STUN_SERVER` empty.
- [ ] Decide your retention policy for transcripts and recordings you generate. Right now `Pipeline.transcript` is in memory and logged at teardown — that is a lab default, not a policy.
- [ ] Mirror PyPI internally if your build process requires it.
- [ ] Confirm consent language in the permission request template satisfies local requirements, and that the caller is told they are speaking to an AI where that is mandated.
