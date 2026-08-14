# Status and known issues

Evaluation after the Hindi self-test session of 14 Aug 2026 (10 turns, ~2 min).

---

## What is working

**The conversation itself.** This is the part that was uncertain, and it is now
demonstrated. Claude reconstructed usable meaning from badly degraded transcripts:

| Whisper heard | Claude understood |
|---|---|
| "आप में कल रात को पहुझ गया था, बारा भजे। और अबी विछ्टे चे आर नो गन्ड़े से लेट कर रहूँ" | "आप कल रात बारह बजे पहुँच गए थे और अभी लोडिंग में थोड़ा समय और लगेगा" |
| "दिस्पैज़ को, डस्तावेच के लिए पुछना है, खलायंच से की सब फॉझ रही है ना" | "डिस्पैच क्लाइंट से कन्फ़र्म करे कि सारे दस्तावेज़ तैयार हैं, ताकि लोडिंग पूरी होते ही आप फ़ौरन निकल सकें" |

It sustained a multi-turn negotiation about documentation and client confirmation,
and offered escalation when it kept failing. That is the hard part of a voice
agent and it is sound.

**Gender consistency** — fixed. Throughout: "समझ गया", "करता हूँ", "पहुँचा देता हूँ".
No feminine drift.

**Repetition loops** — gone. No degenerate output in the session.

**Silero trimming** — working, and earning its place: "kept 0.6s of 1.4s",
"kept 3.6s of 4.3s". It is discarding silence and noise before Whisper sees it.

**Turn-taking** — 10 turns, no self-interruption on speakerphone, no echo
misclassified as speech.

---

## Open issue 1 — English is unusable  ⚠️ DEFERRED BY DECISION

**Symptom.** English speech transcribes as Hindi-phoneme nonsense:

```
spoken:  "Do you speak English?"
heard:   "तुछ विस्पीक इंगलेश?"

spoken:  "Can you understand English?"
heard:   "वहांडर अन्टेस्तान इंगलीश, रही।"
```

**Cause.** `AGENT_LANGUAGE=hi` pins Whisper's decoder to Hindi. Every sound is
forced through the Hindi phoneme set, so English cannot come out right — this is
by construction, not a bug.

**Why it is pinned.** Auto-detect re-runs per utterance and flips on short
replies, which was worse for a Hindi-first conversation.

**Options when this becomes a priority:**

| Option | Cost | Effect |
|---|---|---|
| `AGENT_LANGUAGE=auto` | free, one env var | Whisper detects per utterance. Handles switching; occasionally misfires on one-word replies. |
| Cloud STT with multilingual model | per-minute fee | Deepgram's `language=multi` handles code-switching properly. Removes the problem. |
| Two Piper voices + output language detection | development work | Needed for the agent to *reply* in English. STT alone is not enough — a Hindi voice cannot speak English. |

**Note the asymmetry:** fixing STT only lets the agent *understand* English. To
*reply* in English it also needs an English voice and a language decision per
turn. A genuinely bilingual agent is a feature, not a config change.

---

## Open issue 2 — transcription speed degrades through a session

**Symptom.** Speed falls as the call goes on:

| Time | Audio | Transcribe | Ratio |
|---|---|---|---|
| 12:21:49 | 1.7s | 1.6s | 1.1x |
| 12:22:07 | 3.6s | 11.5s | **0.3x** |
| 12:22:32 | 1.6s | 13.4s | **0.1x** |
| 12:23:04 | 5.8s | 17.2s | **0.3x** |

Early utterances run at 1–3x real time; later ones collapse to 0.1–0.3x. The
pattern is progressive, not random, and does not correlate with Piper running
concurrently.

**Most likely cause: thermal throttling.** A fanless MacBook Air under sustained
multi-threaded inference throttles hard after a minute or two. Consistent with
degradation over time rather than spikes.

**This is a hardware ceiling, not a code defect.** It disappears on a server or
any machine with active cooling, and largely disappears with a GPU.

**Mitigations if it must run on a laptop:** `WHISPER_MODEL=base` (roughly 2x
faster, worse Hindi accuracy), or move STT to a cloud provider.

---

## Open issue 3 — Hindi STT accuracy is roughly 60–70%

Whisper `small` on accented Hindi through a laptop mic. Claude compensates well
enough to hold a conversation, but it is the binding constraint on quality and it
degrades under noise.

**Worth measuring before investing:** run the same conversation with
`STT_PROVIDER=deepgram` on a free-tier key. If accuracy is materially better,
that reframes build-vs-buy — keep TTS local, pay only for STT, which is also the
stage where local hardware costs most.

---

## Latency, measured

```
turns: 10    time-to-first-word: avg 2688 ms  (min 1538, max 3896)
```

Against a target of ~900 ms for natural conversation. Usable for testing;
callers will notice the pause. Breakdown: STT dominates, Claude ~1.5–2.5s
(includes network to the US), Piper ~70 ms warm.

---

## Bugs found and fixed in this session

| Bug | Cause | Status |
|---|---|---|
| Stereo read as mono | `av` returns packed audio as shape `(1, n*channels)`; the channel check never fired. Doubled every utterance's length and mangled the waveform. | fixed |
| `vad_filter=True` deleted all audio | Whisper's Silero VAD ran *after* our energy VAD and discarded 100% of quiet speech | fixed |
| Repetition loops | Pinning `temperature=0.0` removed Whisper's fallback ladder, which is what escapes degenerate decoding | fixed |
| Agent barged in on itself | Barge-in consulted a duplicate `EnergyVAD` with no echo awareness, not the real gate | fixed |
| Echo guard never engaged | `agent_speaking` was driven by *generation* state; Piper renders a whole sentence at once, so it went false while seconds of audio were still playing | fixed |
| Genuine answers discarded as echo | Bag-of-words overlap cannot distinguish answering a question from echoing it; a reply reusing the question's words scored 93% | fixed (order-aware) |
| 62s transcriptions | Every `/selftest` session loaded its own ~500 MB model; several resident at once forced swap | fixed (shared cache) |
| Gender drift | Prompt never pinned gender; Hindi verb agreement made Claude alternate | fixed |
| Sentences split mid-thought | 700 ms silence threshold cut people who paused to think | fixed (1100 ms) |

---

## The media path — why the first real call was silent

**Symptom.** 14 Aug 2026. The call rang, was answered, and produced no audio.
`ringtest.py` reported `ice=checking  pc=connecting  accepted=True`. Every
signalling layer had worked; only media failed.

**Evidence.** The offer contained one candidate and nothing else:

```
a=candidate:2de01941f5cf3f54e75e1817d7c5c406 1 udp 2130706431 192.168.10.121 63035 typ host
a=end-of-candidates
```

A private address, no `typ relay`. Meta's stack is ICE-LITE — it publishes
candidates and waits, and will not traverse NAT — so it had nothing reachable to
connect to and ICE never left `checking`.

**Two independent causes, found in order.**

*First, a bug in this code.* aiortc supports exactly one TURN server. From
`aiortc/rtcicetransport.py::connection_kwargs`:

```python
# only a single TURN server is supported
if "turn_server" in kwargs:
    continue
```

It uses URL `[0]` and silently discards the rest. Our list was ordered UDP-first
as a "fallback chain", so the only transport ever attempted was TURN over **UDP
to port 80** — routinely filtered — and the TCP/443 entries were never tried
once. Fixed by making the transport an explicit single choice, `TURN_TRANSPORT`,
defaulting to `tcp443`.

*Second, the relay is unreachable from this network.* With the bug fixed,
`cli.py turn-test` still failed on every transport:

| Probe | Result |
|---|---|
| STUN Binding, UDP/3478 | no reply in 4.0 s |
| TCP connect :443 | timed out, 6.1 s |
| TCP connect :80 | timed out, 6.0 s |
| Allocate, UDP 443/80/3478 | no reply after ~5 s of retries |

DNS resolves, so the name is valid. Nothing answers on any port. Open Relay
documents ports 80 and 443 *specifically* to pass corporate firewalls and claims
99.999% uptime, so a reachable relay would have answered on at least one. The
weight of evidence is that this network blocks the destination.

**Third cause, and the actual root: UAE ISP-level VoIP blocking.**

With `STUN_SERVER` set, the offer finally carried a reflexive candidate:

```
a=candidate:... 192.168.10.121 54576 typ host
a=candidate:... 217.165.96.224 60048 typ srflx raddr 192.168.10.121 rport 54576
```

The call still failed, with `138021 — WhatsApp client terminated the call due to
not receiving any media`, and:

```
Check CandidatePair(('192.168.10.121', 64783) -> ('31.13.80.130', 3480)) FAILED
```

`217.165.96.224` is Etisalat UAE. The laptop was on the local ISP; only the
handset was on a VPN. The UAE blocks VoIP at the ISP level, and that single fact
explains every observation that had looked unrelated:

| Observation | Explanation |
|---|---|
| openrelay unreachable on UDP/3478 **and** TCP/80 **and** TCP/443 | A well-known TURN/VoIP host, blocked by destination on every port. Not a dead relay. |
| STUN to Cloudflare and Google answered in ~40 ms | A STUN binding is not VoIP media, so nothing blocks it |
| ICE check to Meta's `31.13.80.130:3480` failed | Meta's media endpoint — blocked |
| Every signalling step worked perfectly | Signalling is HTTPS to `graph.facebook.com`; only the media plane is blocked |
| The handset was fine | It was on a VPN; the laptop was not |

**Conclusion.** Media must leave the machine over a path that is not subject to
UAE VoIP filtering. Either put the media host on the VPN, or run it outside the
UAE. Note that this does not depend on who is being called: the blocked hop is
laptop -> Meta, so changing the callee's country changes nothing.

A public IP is still worth having for latency and for keeping a third party out
of the audio path (see `DATA-RESIDENCY.md`), but it was never the blocker.
See DEPLOY.md, Test B, Option 1.

**Diagnostic lesson.** Every failure in this saga presented as the same symptom —
"rings, answers, silence" — with three unrelated causes stacked behind it: a URL
ordering bug, a missing STUN server, and ISP filtering. Fixing one just revealed
the next. `cli.py stun-test` is what finally made the difference, because it
prints the public address actually in use; seeing `217.165.96.224` is what
identified the VPN gap.

**Diagnostic lesson worth keeping.** A failed TURN allocation is *completely
silent* — aiortc raises nothing, logs nothing, and simply gathers no relay
candidate. The first symptom is a phone call that rings and stays mute, which is
a slow and expensive way to discover a blocked UDP port. `cli.py turn-test`
answers the same question in seconds and, critically, distinguishes DNS failure
from a filtered port from a refused credential — three causes with opposite
fixes that otherwise present identically.

---

## Recommended next steps

1. **Deepgram comparison on STT only.** Highest information per unit effort.
   One env var, free-tier key, same conversation.
2. **Move off the laptop** for any latency judgement. Thermal throttling makes
   local numbers unrepresentative.
3. **Decide the English question deliberately** rather than by default — it
   determines whether this is a Hindi-only agent or a bilingual one, and that
   changes the TTS architecture.
