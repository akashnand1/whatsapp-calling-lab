# Running the media leg outside the UAE

## Why

Every layer of this lab worked from the laptop except audio. Three causes were
stacked behind one symptom ("rings, answers, silence"), and the last one cannot
be fixed in code:

**The UAE filters VoIP at the ISP.** The laptop's public address was
`217.165.96.224` (Etisalat). Signalling is HTTPS to `graph.facebook.com` and
passes fine, which is why every step up to `ACCEPTED` succeeded. Media is UDP to
Meta's media servers, and that is blocked:

```
Check CandidatePair(('192.168.10.121', 64783) -> ('31.13.80.130', 3480)) FAILED
138021 — WhatsApp client terminated the call due to not receiving any media
```

The same filtering explains why `staticauth.openrelay.metered.ca` was unreachable
on UDP/3478 *and* TCP/80 *and* TCP/443 — a known TURN host, blocked by
destination on every port — while STUN to Cloudflare answered in 41 ms.

The laptop VPN did not help because it split-tunnels: `route get 31.13.80.130`
returned `interface: en0`, so Meta's traffic never entered the tunnel.

A container outside the UAE removes the problem instead of working around it.

> The handset's VPN was never relevant. Media does not flow phone-to-laptop —
> the laptop talks to Meta's servers, and Meta talks to the handset separately.
> Two independent hops. Only the laptop's hop was broken.

## One honest unknown

GitHub does not document whether Codespaces permits arbitrary outbound UDP.
It almost certainly does, but **verify it before anything else** — step 6 is a
five-second check that answers it. If STUN gets no reply there, the platform
blocks UDP and we move to a plain VM rather than debugging further.

---

## Steps

### 1. Commit locally

```bash
cd "/Users/akash/Documents/WhatsApp Calling Feature"
rm -f .git/index.lock          # stale lock, safe to delete
git add -A
git status --short             # ← LOOK at this before continuing
```

`.env` **must not** appear in that list. If it does, stop — `.gitignore` is not
being applied and you would publish your access token and Claude key.

Use the email attached to the GitHub account you are pushing to. GitHub only
attributes a commit if the author email is a verified address on that account —
committing as `@trukker.com` to a personal account leaves the history showing an
unrecognised author.

```bash
git config user.email "your-personal@email.com"
git config user.name  "Akash"
git commit -m "WhatsApp calling lab"
```

`git config` without `--global` sets this for **this repository only**, so your
other repos keep whatever identity they already use.

### 2. Create a PRIVATE repo and push

Via the web: <https://github.com/new> → name it `whatsapp-calling-lab` → **Private**
→ do **not** add a README or .gitignore → Create.

```bash
git branch -M main
git remote add origin https://github.com/<your-username>/whatsapp-calling-lab.git
git push -u origin main
```

**If the push is rejected or prompts for the wrong account**, macOS has cached
credentials for a different GitHub login in Keychain. Easiest fix is the GitHub
CLI, which manages its own token:

```bash
brew install gh          # if you do not have it
gh auth login            # choose GitHub.com → HTTPS → browser
git push -u origin main
```

### What ends up in the repo

`.env` is excluded, so no tokens or keys. The code that *is* pushed still carries
TruKKer identifiers in comments and defaults — the WABA ID `1292846775792529`,
phone number ID `919208021266743`, the business number, and notes about the
HappyRobot integration on the same WABA. None of it is a credential, and none of
it grants access to anything without the token. It is still company
configuration living in a personal account, so keep the repo **private**, and if
this work continues past the experiment, move it to a TruKKer-owned repo.

### 3. Launch the Codespace

On the repo page: **Code** → **Codespaces** tab → **Create codespace on main**.

`.devcontainer/setup.sh` runs automatically: installs the packages, downloads the
Hindi Piper voice, and writes container paths into `.env`. Takes 3–5 minutes.
Wait for `Container ready.` in the terminal.

### 4. Add your secrets

The repo has no `.env` (correctly). Edit the one the container generated:

```bash
code .env
```

Fill in `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID` (919208021266743) and
`WA_WEBHOOK_VERIFY_TOKEN` (trukker-lab-8f2a9c).

The Claude key goes in the shell, never in a file:

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

To avoid re-typing it on every rebuild, add it once as a Codespaces secret:
<https://github.com/settings/codespaces> → **New secret** → `ANTHROPIC_API_KEY`
→ grant it to this repo. It then appears as an environment variable
automatically.

> The API Setup token expires every 24 hours. `STEP-BY-STEP.md` step 6 covers a
> System User token that does not.

### 5. Verify the config loaded

```bash
python cli.py doctor
```

Expect `STUN configured — reflexive candidate`.

### 6. THE GATE — check the media path before anything else

```bash
python cli.py stun-test
```

| Result | Meaning |
|---|---|
| A public address that is **not** `217.165.x` | The media path is clear. Continue. |
| No STUN server answers | Codespaces blocks outbound UDP. Stop — move to a plain VM. |
| Symmetric NAT reported | Expected here, and fine. Meta is publicly reachable and we send the checks. |

### 7. Make port 8000 public

**PORTS** tab → right-click 8000 → **Port Visibility** → **Public**.

Meta's webhook is an unauthenticated POST from Facebook's servers. A private port
redirects them to a GitHub login page, the SDP answer never arrives, and the call
rings then dies — indistinguishable from the media failures you have already
spent a day on.

Copy the forwarded URL. It looks like:

```
https://<name>-8000.app.github.dev
```

### 8. Re-register the webhook with Meta

App Dashboard → **WhatsApp** → **Configuration**:

- Callback URL: `https://<name>-8000.app.github.dev/webhook`
- Verify token: `trukker-lab-8f2a9c`
- **Verify and save** — the server must already be running (step 9), so start it first
- Confirm `calls` and `messages` are still subscribed

> Check `python cli.py subscribed-apps` first. HappyRobot (`1002843411758997`) is
> subscribed to the same WABA. Changing the shared Callback URL affects their
> production chatbot.

### 9. Run it

```bash
uvicorn app.main:app --reload --reload-include '.env' --port 8000
```

### 10. Call

In a second terminal:

```bash
python ringtest.py 971554225948
```

Step 2 now compares the server's config against `.env` and refuses to place the
call if they disagree — the failure mode that cost two rounds of debugging.

---

## After testing: DELETE the codespace

Verified against GitHub's billing docs (20 Aug 2026):

| | Free quota | How it is consumed |
|---|---|---|
| Compute | 120 hrs/month | ×4 multiplier on a 4-core machine, so **30 wall-clock hours** |
| Storage | 15 GB-month | accrues hourly for as long as the codespace **EXISTS** |

**Stopping is not enough.** Stopping ends compute billing; storage keeps
accruing. With `nemo_toolkit`, torch, the 2.37 GB `.nemo`, Whisper and the Piper
voices, this codespace is roughly 15-25 GB — left stopped for a month that alone
exceeds the 15 GB quota. Deleting is the only thing that stops storage.

There is a hard safety net: *"If your account does not have a valid payment
method on file, usage is blocked once you use up your quota."* With no card on
file you cannot be charged — usage simply stops.

### Before deleting

1. **Make the Anthropic key permanent** so you never re-paste it:
   <https://github.com/settings/codespaces> → New secret → `ANTHROPIC_API_KEY`
   → grant to this repo. Account-level, survives deletion.
2. **Push your code.** `.env` is gitignored and will be lost, which is fine —
   everything in it is either in `.env.example`, reproducible
   (`WA_PHONE_NUMBER_ID=919208021266743`, `WA_WEBHOOK_VERIFY_TOKEN=trukker-lab-8f2a9c`),
   or expires anyway (the WhatsApp token lasts 24 h).

### Deleting

<https://github.com/codespaces> → `⋯` → **Delete**. Or from the Mac:

```bash
gh codespace list
gh codespace delete -c <codespace-name>
```

Confirm storage has dropped to zero at <https://github.com/settings/billing>.

Recreating costs ~5 minutes plus the model downloads, so delete after a testing
session rather than between two runs on the same day.

## What a call actually costs

Both figures verified 20 Aug 2026. Every call now logs its real cost beside the
transcript, from the API's own token counts rather than an estimate:

```
cost: LLM $0.384 (24 turns, 168000 in / 4800 out / 0 cached)
      + WhatsApp $0.146 (11.5 min) = $0.530
```

| Setup | LLM | WhatsApp | Total |
|---|---|---|---|
| Sonnet 5, no caching, 11.5 min | $0.384 | $0.146 | **$0.53** |
| Sonnet 5 + prompt caching, 4 min | $0.159 | $0.051 | $0.21 |
| Haiku 4.5 + caching, 4 min | $0.080 | $0.051 | **$0.13** |

Prices: Sonnet 5 $2/$10 per MTok, Haiku 4.5 $1/$5, cache reads 10% of input.
WhatsApp calling to a UAE number is $0.0127/min in 6-second pulses.

Nothing else in the stack costs money: Whisper, Nemotron, Piper and the voices
are self-hosted, Cloudflare's STUN is public, and Codespaces port-forwarding
replaced ngrok.

---

## If it is still silent

Look at the server log for the candidate pair:

```
Check CandidatePair((<local>, <port>) -> ('31.13.x.x', 3480)) ...
```

* `State.SUCCEEDED` → media path works; any remaining silence is the AI
  pipeline, which `/selftest` can debug without spending calls.
* `State.FAILED` → still a network block, now on Azure rather than Etisalat.
* No pair at all → no candidates were gathered; re-run `stun-test`.
