# Building our own voice model — research findings

## Did ElevenLabs use Chatterbox or an existing model?

**No. They built from scratch, and the timeline makes anything else impossible.**

From ElevenLabs' own v3 announcement (3 June 2025): *"It was built from the
ground up."* Their model line is entirely in-house — Multilingual v2, then
v2.5 Turbo and Flash, then v3, plus their own STT (Scribe v2) and dubbing.

Chatterbox could not have been an input to any of it:

| | |
|---|---|
| ElevenLabs founded | 2022; Multilingual v2 shipped 2023 |
| Chatterbox released | September 2025 |
| Chatterbox author | **Resemble AI — a direct ElevenLabs competitor** |

The causation runs the other way, if anywhere: Resemble benchmarks Chatterbox
*against* ElevenLabs. Chatterbox's own acknowledgements credit CosyVoice,
HiFT-GAN and Llama 3 — not ElevenLabs.

**What ElevenLabs actually did**, per their public material: trained proprietary
models on a proprietary corpus, at a scale they have never disclosed, covering
70+ languages. They separate cloning into two tiers, which tells you something
useful about the technology:

- **Instant Voice Clone (IVC)** — a few seconds of reference audio, zero-shot.
  Same mechanism Chatterbox gives you for free.
- **Professional Voice Clone (PVC)** — a real fine-tune on much more audio from
  one speaker. This is where their quality edge lives, and it is not zero-shot.

One detail worth more than all of the above for TruKKer's use case: ElevenLabs
themselves say v3 is **not suitable for real-time conversational use** — too slow
and too unreliable — and direct you to v2.5 Turbo/Flash instead. A phone agent
does not want their best model. It wants their fastest one. That materially
narrows the quality gap you are trying to close.

Source: <https://elevenlabs.io/blog/eleven-v3>

## Is Chatterbox the best option? — No. Reviewed 19 Aug 2026.

**Chatterbox has been overtaken.** Every model card below was read directly on
19 Aug 2026, not recalled. Chatterbox's trending score on Hugging Face is now 6;
the leaders are at 32–111.

### The licence trap you must check for yourself

**Hugging Face's licence tag is not reliable.** `k2-fsa/OmniVoice` is tagged
`license:apache-2.0`. Its model card says:

> "Our code is released under the Apache 2.0 License. **The pre-trained model is
> licensed under the CC-BY-NC** due to constraints from its training data (e.g.
> Emilia)."

The *weights* are non-commercial. Anyone filtering by the metadata tag — which is
what an API query returns — would ship a licence violation. Always open the card
and read the licence section.

### Verified, 19 Aug 2026

| Model | Licence | Commercial | Our languages | Cloning |
|---|---|---|---|---|
| **VoxCPM2** (OpenBMB) | **Apache-2.0**, "free for commercial use" | **yes** | en, hi, **tr**, **ru**, ar | yes, 3 modes + voice design |
| **Indic-Mio** (SPRINGLab) | **Apache-2.0** | **yes** | hi, **pa**, **ur**, +20 Indian | zero-shot, and **code-mixed** |
| **Qwen3-TTS CustomVoice** | **Apache-2.0** | **yes** | en, **ru** only (10 langs) | yes |
| Chatterbox Multilingual V3 | MIT | yes | en, hi, tr, ru, ar | zero-shot |
| Piper | MIT | yes | en, hi, tr, ru, **kk**, ur | **none** |
| OmniVoice (k2-fsa) | **weights CC-BY-NC** | **NO** | 600+, incl. kk, ur, pa | zero-shot |
| Fish Audio S2 Pro | Fish Audio Research Licence | **NO** | 80+, incl. kk, ur, pa | yes |
| XTTS-v2 (Coqui) | `coqui-public-model-license` | **NO** | 17 | zero-shot |
| MMS-TTS (Meta) | `cc-by-nc-4.0` | **NO** | 1,100+ | none |
| F5-TTS | `cc-by-nc-4.0` | **NO** | — | reference |
| Voxtral-4B-TTS (Mistral) | `cc-by-nc-4.0` | **NO** | en, hi, ar… | — |

Note the pattern: **the models with the widest language coverage are the
non-commercial ones.** OmniVoice and Fish S2 Pro both cover Kazakh, Urdu and
Punjabi, and neither can be used in a TruKKer product without a negotiated
licence.

### Recommended: VoxCPM2 as primary

Materially better than Chatterbox on every axis we care about:

| | Chatterbox V3 | **VoxCPM2** |
|---|---|---|
| Licence | MIT | Apache-2.0 (equally permissive) |
| Parameters | 0.5B | 2B |
| Training data | 0.5M hours | **2M+ hours** |
| Languages | 23 | 30 |
| Output | — | **48 kHz**, from a 16 kHz reference |
| **Streaming** | not advertised | **`generate_streaming()`** |
| Voice design (no reference audio) | no | **yes, from a text description** |
| Fine-tuning | full | **LoRA from 5–10 min of audio** |
| RTF | — | ~0.30 on a 4090, ~0.13 with Nano-vLLM |
| VRAM | — | ~8 GB |

The streaming API matters more than the quality numbers. A phone agent needs
first-audio-out fast; batch generation of a whole sentence is what currently puts
Piper's whole utterance into the queue at once and forced the echo-guard work.

`Indic-Mio` handles what VoxCPM2 does not: **Punjabi and Urdu**, at 44 kHz with
RTF under 0.1. It is also explicitly good at **code-mixed sentences** — its own
demo text is Hinglish: *"प्लान तो बढ़िया है, but wait... Have you checked the hotel
bookings?"* That is requirement 3's Hinglish item, answered directly. It was
trained on a single A6000 in **under six hours**, which tells you how cheap a
TruKKer-specific fine-tune would be.

### The Kazakh gap is now the only real gap

Not in VoxCPM2's 30, not in Indic-Mio, not in Chatterbox. It exists in OmniVoice
and Fish S2 Pro, both non-commercial. So the options are:

1. **Piper `kk_KZ-issai-high`** — fixed voice, no cloning. What the code does now.
2. **LoRA fine-tune VoxCPM2 on Kazakh.** The 5–10 minute figure is for cloning a
   *voice*, not adding a *language*; budget a few hundred hours of Kazakh speech.
3. **Licence Fish Audio commercially** — they sell one; contact
   business@fish.audio. Worth pricing before building.

**Urdu is no longer a gap** — Indic-Mio covers `ur` natively, so the
transliterate-to-Devanagari workaround I suggested earlier is unnecessary.

### What I got wrong before this review

Recorded so the same mistake is not repeated: the previous version of this
document was written from training knowledge with a May 2026 cutoff and was wrong
in three ways. It named Chatterbox best when VoxCPM2 had superseded it; it would
have endorsed OmniVoice on the strength of a misleading Apache tag; and it
claimed Punjabi had no commercial path when Indic-Mio provides one. **Re-check
this table before any procurement or architecture decision** — the field moved
this much in three months.

## The headline: do not train from scratch

ElevenLabs-class quality is not a modelling secret. It is data, compute and
evaluation at a scale that does not make sense to reproduce.

Published details on how these systems are built are consistent across vendors:

- **Architecture.** A discrete audio codec plus an autoregressive language model
  over audio tokens, followed by a vocoder. Resemble AI's Chatterbox states this
  outright: a **0.5B Llama backbone** over audio tokens, with a HiFT-GAN style
  vocoder. The architecture is public. It is not the moat.
- **Data.** Chatterbox is trained on **0.5 million hours of cleaned speech**.
  That is the moat — sourcing it, rights-clearing it, diarising it, filtering it
  and transcribing it.
- **Zero-shot cloning is a property of the architecture, not a per-voice
  training run.** A speaker encoder turns a few seconds of reference audio into
  an embedding that conditions generation. Nothing is retrained per voice, which
  is why cloning takes seconds rather than hours.

Half a million hours at even a few cents per hour of compute-and-labour is a
multi-million-dollar programme before a single call is answered. TruKKer's
advantage is freight operations and driver relationships, not owning a speech
foundation model.

## What to do instead: fine-tune Chatterbox Multilingual V3

| | |
|---|---|
| Licence | **MIT** — commercial use permitted, no per-character fee |
| Size | 0.5B parameters |
| Languages | 23, including **Hindi, Turkish, Russian, English, Arabic** |
| Cloning | **Zero-shot from a reference clip** — `audio_prompt_path=...` |
| Training data | 0.5M hours cleaned |
| Benchmarks | Vendor reports it is preferred over ElevenLabs in side-by-side evaluation |
| Watermarking | **PerTh neural watermark on every output**, survives MP3 and editing |
| Dedicated Hindi finetune | `ResembleAI/Chatterbox-Multilingual-hi` |

Source: <https://huggingface.co/ResembleAI/chatterbox>

Minimal cloning call — this is the whole of requirement 2:

```python
from chatterbox.mtl_tts import ChatterboxMultilingualTTS
model = ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")
wav = model.generate(text, language_id="tr", audio_prompt_path="reference.wav")
```

"Training our own model" should mean **fine-tuning this on TruKKer audio** —
driver calls, our own dispatcher voices, freight vocabulary, GCC and Central
Asian accents. That is a GPU-days problem, not a GPU-months one, and it captures
the part that actually differentiates us: our domain and our accents.

## Language coverage — verified, not assumed

Checked directly against the model card and the Piper voice repository:

| Language | Chatterbox (cloning) | Piper (current TTS) | Whisper (STT) |
|---|---|---|---|
| English | yes | `en_US-amy-medium` | strongest |
| Hindi | yes, + dedicated finetune | `hi_IN-pratham-medium` | ~60-70% on accented phone audio |
| Turkish | yes | `tr_TR-dfki-medium` | good |
| Russian | yes | `ru_RU-dmitri-medium` | among its best |
| **Kazakh** | **no** | `kk_KZ-issai-high` | **weak — low-resource** |
| **Urdu** | **no** | `ur_PK` available | moderate |
| **Punjabi** | **no** | **none** | supported, untested |
| Hinglish | via Hindi | via Hindi | code-switching is a known weakness |

**The three gaps, and what closes each one.** Kazakh, Urdu and Punjabi are not in
Chatterbox's 23 languages, so zero-shot cloning does not work for them out of the
box. But each has a different answer, and only one is genuinely open:

- **Punjabi → IndicF5.** MIT licensed, covers `pa`, clones from reference audio.
  Superseded my earlier claim that Punjabi had no self-hosted path.
- **Urdu → Chatterbox Hindi, with transliteration.** Spoken Urdu and Hindi are
  the same language; convert the text to Devanagari and the Hindi voice speaks
  it. A text problem, not a model problem.
- **Kazakh → Piper only, no cloning.** The one real gap. Closing it means
  fine-tuning Chatterbox on Kazakh speech — same Llama-over-audio-tokens
  architecture, a known procedure, but it needs a few hundred hours of clean
  Kazakh audio.

Do **not** reach for Meta's MMS to fill these, however tempting its 1,100
languages look: it is `cc-by-nc-4.0`.

**Hinglish is not a language to add**, it is code-switching. Chatterbox's Hindi
model handles Devanagari; the practical problem is that drivers say "load",
"pickup" and "border" in English mid-sentence. That is handled by writing those
words in Devanagari in the prompt (already done) rather than by a Hinglish model.

## Requirement 2 — cloning a voice from a few seconds

Technically this is solved: it is one argument to `generate()`. The hard part is
not technical.

**Cloning a person's voice from a short sample is the primary abuse vector for
this technology** — authorising payments, impersonating a manager to a driver,
defeating voice biometrics at a bank. If TruKKer builds this, it needs to be
built so that it *cannot* casually be used that way:

- **Recorded consent, per voice.** A stored consent utterance from the person
  whose voice it is, captured before enrolment, tied to the voice ID. Not a
  checkbox in an admin panel.
- **Keep the watermark on.** Chatterbox watermarks every output by default.
  Do not strip it. It is the only way to prove after the fact that audio came
  from our system.
- **Log every synthesis** — which voice, which text, which user, when.
- **Never clone from audio that arrived unsolicited.** A driver's recorded call
  is not consent to clone them.
- **Legal review before launch.** The UAE, Turkey, Kazakhstan and India all
  regulate biometric and personal data differently, and voice is biometric data
  under several of them. This needs an actual opinion, not an engineering
  assumption.

The legitimate uses are real and worth building for: one consistent TruKKer
dispatcher voice across all languages, a named account manager's voice for their
own regular drivers, and per-region voices that sound local. All of those are
consented, first-party voices.

## Sequencing — with cloning DEPRIORITISED (20 Aug 2026)

The current objective is different from the one this document was first written
for: **cover Turkish, Kazakh, Russian, Hindi, Urdu, Arabic and English, with no
perceptible lag, asking the right questions.** Cloning is explicitly not a
priority. That changes the answer, and simplifies it a great deal.

### TTS is already solved. Do nothing.

**Piper covers all seven languages**, verified against the voice repository:

| Language | Piper voice | Verified |
|---|---|---|
| English | `en_US-amy-medium` | yes |
| Hindi | `hi_IN-pratham-medium` | yes |
| Turkish | `tr_TR-dfki-medium` | yes |
| Russian | `ru_RU-dmitri-medium` | yes |
| Kazakh | `kk_KZ-issai-high` | yes |
| **Arabic** | `ar_JO-kareem-medium` | yes |
| **Urdu** | `ur_PK-fasih-medium` | yes |

All MIT, all CPU-only, and Piper measured **210 ms to first byte** on the
Codespace. VoxCPM2 and Indic-Mio are better *voices*, but they need a GPU and
buy nothing against the stated goal. They belong in this document as the plan
for when cloning and voice quality become the objective — not now.

### The lag is entirely STT

Measured on the Codespace, warm:

| Stage | Now | Share |
|---|---|---|
| STT (Whisper `small`, CPU, batch) | **~5 500 ms** | **76%** |
| LLM first clause (Sonnet) | ~1 500–2 500 ms | 21% |
| TTS first byte (Piper) | 210 ms | 3% |
| **Time to first word** | **~7 200 ms** | |

People perceive lag above roughly one second. TTS is 3% of the problem; **STT is
three quarters of it**, and the cause is architectural, not model size: we wait
for end-of-speech and *then* transcribe the whole utterance. Nothing happens
while the driver is talking.

### The fix: streaming ASR

**`nvidia/nemotron-3.5-asr-streaming-0.6b`** — verified 19 Aug 2026, updated
5 Aug 2026:

| | |
|---|---|
| Architecture | cache-aware FastConformer / RNNT, purpose-built for streaming |
| Chunk size | 1.12 s — partial transcripts arrive *during* speech |
| Size | 0.6B, and a **741 MB q8_0 GGUF** for CPU |
| Licence | `openmdw-1.1` (Linux Foundation OpenMDW) — permissive, **confirm before production** |
| Published WER (FLEURS) | **Hindi 6.81%**, English 7.91%, Spanish 4.11%, German 8.31% |
| Languages | en, **ar**, **ru**, **hi**, **tr**, +30 — **not kk, not ur** |

Whisper `small` manages roughly 30–40% WER on accented Hindi over a phone. Even
allowing that FLEURS is clean read speech and a phone line is much harder, that
is a different class of accuracy — and it arrives incrementally instead of in a
5.5-second block.

**Kazakh and Urdu are not in it.** Those two stay on
`openai/whisper-large-v3-turbo` (MIT, covers `kk` and `ur`), which means the STT
layer needs to select a backend per language — the same pattern
`app/languages.py` already applies to voices.

### Target budget

| Stage | Now | Target | How |
|---|---|---|---|
| STT | 5 500 ms | **~400 ms** | streaming ASR; most decoding done before they stop talking |
| LLM | 1 500–2 500 ms | **~600 ms** | Haiku instead of Sonnet, plus prompt caching |
| TTS | 210 ms | 210 ms | unchanged |
| **Total** | **~7 200 ms** | **~1 200 ms** | |

Roughly a 6× reduction, and it needs no new TTS model. Combined with the
conversation work already done — 30 agent turns down to a projected 10 — that is
what turns an 11.5-minute call into a 3–4 minute one.

## For later: when voice quality becomes the goal

The TTS layer is already swappable: `TTS_PROVIDER` selects the backend, and
`app/languages.py` holds the per-language voice. Adding `voxcpm_local` is a new
class behind the same interface, not a rewrite. When cloning matters:

1. **VoxCPM2** on a GPU for en/hi/tr/ru/ar — Apache-2.0, streaming, 48 kHz.
2. **Indic-Mio** for ur/pa and Hinglish code-mixing — Apache-2.0.
3. **Piper** stays as the Kazakh fallback and the no-GPU path.
4. **LoRA fine-tune** VoxCPM2 on TruKKer audio for accent and freight vocabulary
   — 5–10 minutes of audio per voice, per its own documentation.

Do not train a base model. See the section above on why.
