"""Language registry: everything that differs per spoken language, in one place.

Why this exists
---------------
Hindi support was written inline in config.py -- prompt, greeting, token budget,
voice path, script rules -- which was fine for one language and unmaintainable
for five. Adding Turkish meant touching four files and hoping nothing was missed.

The design decision worth stating: for a NEW language we do NOT hand-write a
translated system prompt. The conversation policy is written once in English and
the model is told which language to speak. Claude's Turkish, Russian and Kazakh
are better than a hand-translated prompt would be, and more importantly a
hand-translated prompt drifts -- fix a rule in the Hindi copy and the Turkish
copy silently keeps the old behaviour. One policy, one place to fix it.

Hindi is the exception and keeps its own tuned prompt in config.py, because it
carries hard-won specifics (Devanagari-only for the TTS, masculine verb
agreement, driver colloquialisms) that were paid for in real failed calls.

Per-language facts that genuinely cannot be shared live in LanguageSpec.notes:
script requirements, formality register, and grammatical gender.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageSpec:
    code: str                # Whisper language code, also the .env value
    english_name: str
    endonym: str             # what speakers call it, used in the prompt
    piper_dir: str           # path inside rhasspy/piper-voices
    piper_voice: str         # filename stem, without .onnx
    piper_rate: int          # Piper: low=16000, medium/high=22050
    turn_tokens: int         # output cap for one spoken turn
    greeting_out: str        # WE called THEM
    greeting_in: str         # THEY called US
    notes: str = ""          # script / register / gender rules
    stt_quality: str = ""    # honest note on accuracy in this language

    # --- Speech recognition backend, chosen PER LANGUAGE ------------------
    # No single model covers all seven languages. nvidia/nemotron-3.5-asr-
    # streaming-0.6b is cache-aware streaming (partial transcripts arrive while
    # the caller is still talking, instead of a 5.5s batch decode after they
    # stop) and covers en/ar/ru/hi/tr. It does NOT cover Kazakh, which falls
    # back to Whisper.
    #   "nemotron" -> streaming, low latency, better WER where supported
    #   "whisper"  -> batch decode, broader language coverage
    stt_engine: str = "nemotron"

    # Language to DECODE as, when it differs from the language we SPEAK.
    # Urdu is the case that matters: spoken Urdu and Hindi are the same language
    # (Hindustani), so Urdu audio decoded as Hindi gets streaming and Nemotron's
    # much lower WER. The transcript comes back in Devanagari rather than Urdu
    # script, which costs nothing because only the LLM reads it -- and the LLM is
    # instructed to reply in Urdu script regardless.
    stt_lang: str = ""       # empty -> same as `code`

    @property
    def decode_lang(self) -> str:
        return self.stt_lang or self.code


# ---------------------------------------------------------------------------
# The conversation policy, written ONCE. Kept in English deliberately: the model
# follows an English policy while speaking another language perfectly well, and
# this way a fix lands for every language at once.
# ---------------------------------------------------------------------------
POLICY = """You are a dispatch assistant for TruKKer, a freight and logistics \
company operating across the GCC, Turkey and Central Asia. You are speaking to a \
driver on a live phone call.

HOW TO SPEAK ON A CALL
- One or two short sentences per reply. This is speech, not text.
- No lists, no bullet points, no markdown. Nobody can hear formatting.
- The whole call must finish inside four minutes. Every sentence costs time to
  speak, so do not say words that carry no information.
- Never preface a question with a long apology. Not "I'm sorry, I didn't quite
  catch that, could you please repeat" -- just ask the short question: "Seven or
  eight?"
- Say numbers as words: "load T R K eight four two one three".
- If they ask for a human, say you are transferring them and stop talking.

YOUR TASK: collect the time of every trip milestone, plus the documents.

HOW TO WORK
1. First ask where they are right now, then call set_current_stage.
2. Call get_missing to see which milestones still need a time.
3. Ask them in GROUPS of two or three in a single question -- never one at a
   time. The first time you ask, give a short example answer so they know to
   attach each time to the event it belongs to, not just list times.
4. Call record_milestone immediately for each answer you get.
5. Fold the document question into the SAME turn as the time question, never
   as a separate turn.
6. At the end -- this matters most -- read the whole list back in order and ask
   whether it is all correct. Never skip this, however long the call has run.

FOUR RULES FOR KEEPING THE CALL SHORT. Do not break these.

(a) Do not confirm an answer you heard clearly. Record it and move to the next
    group; the final read-back confirms everything anyway.
      Wrong: "So you arrived at eleven, correct?" then the next question.
      Right: the next question.

    BUT always confirm in these four cases, even if it lengthens the call. A
    wrong time recorded as fact is far more damaging than a slow call:
      - The turn arrives tagged "[transcript confidence: LOW]". The audio was
        unclear. Repeat what you think you heard: "Eleven o'clock -- correct?"
      - You DERIVED the time by arithmetic, e.g. "then I left an hour later"
        becoming midnight. They never actually said it, so check it.
      - Morning versus evening is ambiguous. Five o'clock has a twelve-hour
        failure mode.
      - They hedged, corrected themselves, or said "about" / "maybe".
    Keep the confirmation to one short sentence answerable with yes or no.

(b) Never ask again about a milestone or document already recorded. Call
    get_missing before each question and ask only what it still lists.

(c) Ask about any one item at most twice. If the second attempt also fails,
    call give_up_on instead of asking a third time, move on, and mention it in
    the summary as something to send by message later. Asking the same question
    a third time is the single largest waste of call time, and drivers find it
    insulting.

(d) When re-asking, make the question short and CLOSED -- offer two options so
    the answer is one word. "Seven or eight?" "Afternoon or evening?" "Sent or
    not sent?" Never repeat an open question; it will fail the same way.

get_missing tells you how many seconds have elapsed. If it says you are running
long, ask every remaining item in one question and go straight to the summary.

IMPORTANT
- Never say the internal milestone names aloud. Drivers do not know them. Ask in
  plain language: "When did you leave the origin border?"
- If one answer contains several milestones, record each one separately.
- Record the time in the driver's OWN words in time_reported, and the resolved
  24-hour timestamp in time_iso.
- Never ask about a milestone that has not happened yet.
- Drivers often give a gap from the previous milestone rather than a clock time
  -- "then I left an hour later", "right after that", "half an hour later". Add
  the gap to the previous milestone's time and work out the real time yourself.
- If they do not remember a time, do not press. Ask for an approximation.
"""


LANGUAGES: dict[str, LanguageSpec] = {
    "en": LanguageSpec(
        code="en",
        english_name="English",
        endonym="English",
        piper_dir="en/en_US/amy/medium",
        piper_voice="en_US-amy-medium",
        piper_rate=22050,
        # Latin script is roughly one token per word, so a turn needs far fewer
        # tokens than the same speech in Devanagari or Cyrillic.
        turn_tokens=800,
        greeting_out=(
            "Hello, this is TruKKer dispatch calling about your assigned load. "
            "Is now a good time to go through your trip?"
        ),
        greeting_in="TruKKer dispatch, this is the automated assistant. How can I help?",
        stt_quality="Whisper's strongest language.",
    ),
    # Hindi is registered for its VOICE, RATE and TOKEN BUDGET, but its system
    # prompt comes from the hand-tuned copy in config.py -- see _TUNED there.
    # Leaving it out of this registry meant `fetch-voice.sh hi` failed and the
    # voice/language mismatch check could not see the Hindi voice at all.
    "hi": LanguageSpec(
        code="hi",
        english_name="Hindi",
        endonym="हिन्दी",
        piper_dir="hi/hi_IN/pratham/medium",
        piper_voice="hi_IN-pratham-medium",
        piper_rate=22050,
        # Devanagari costs roughly 3-4x the tokens of Latin script, and the
        # nine-milestone read-back is long. At 420 the model ran out of budget
        # mid-tool-call and the caller heard no summary at all.
        turn_tokens=1400,
        greeting_out=(
            "नमस्ते, मैं ट्रकर डिस्पैच से बोल रहा हूँ, आपके लोड के बारे में। "
            "क्या मैं अभी आपकी ट्रिप की जानकारी ले सकता हूँ?"
        ),
        greeting_in="ट्रकर डिस्पैच, मैं ऑटोमेटेड असिस्टेंट बोल रहा हूँ। मैं आपकी क्या मदद कर सकता हूँ?",
        notes=(
            "Devanagari only. Piper's Hindi voice spells Latin script out letter "
            "by letter, so 'TruKKer' must be written ट्रकर."
        ),
        stt_quality=(
            "Whisper 'small' manages roughly 60-70% on accented Hindi over a "
            "phone line. This is the current accuracy ceiling -- see FINDINGS.md."
        ),
    ),
    "tr": LanguageSpec(
        code="tr",
        english_name="Turkish",
        endonym="Türkçe",
        piper_dir="tr/tr_TR/dfki/medium",
        piper_voice="tr_TR-dfki-medium",
        piper_rate=22050,
        turn_tokens=1100,
        greeting_out=(
            "Merhaba, ben TruKKer sevkiyattan arıyorum, yükünüzle ilgili. "
            "Şu an seferinizi konuşmak için uygun bir zaman mı?"
        ),
        greeting_in="TruKKer sevkiyat, ben otomatik asistanım. Nasıl yardımcı olabilirim?",
        notes=(
            "Speak Turkish only, in Latin script with correct Turkish characters "
            "(ı, İ, ş, ğ, ç, ö, ü). Use the polite 'siz' form throughout -- a "
            "driver you have not met is never 'sen'. Turkish is agglutinative, so "
            "keep sentences short; long suffix chains are hard to follow on a "
            "noisy phone line. Say the company name as 'TruKKer'."
        ),
        stt_quality="Whisper handles Turkish well.",
    ),
    "ru": LanguageSpec(
        code="ru",
        english_name="Russian",
        endonym="русский",
        piper_dir="ru/ru_RU/dmitri/medium",
        piper_voice="ru_RU-dmitri-medium",
        piper_rate=22050,
        # Cyrillic tokenises worse than Latin, though better than Devanagari.
        turn_tokens=1200,
        greeting_out=(
            "Здравствуйте, это диспетчерская ТракКер по вашему грузу. "
            "Удобно сейчас пройти по рейсу?"
        ),
        greeting_in="Диспетчерская ТракКер, автоматический помощник. Чем могу помочь?",
        notes=(
            "Speak Russian only, in Cyrillic script. Never use Latin letters -- "
            "the TTS voice is trained on Cyrillic and will spell Latin words out "
            "letter by letter. Write the company name phonetically in Cyrillic as "
            "'ТракКер'. Address the driver as 'вы', never 'ты'. "
            "You are male: use masculine past-tense forms about yourself "
            "('я понял', 'я записал'), never feminine ('я поняла', 'я записала')."
        ),
        stt_quality="Whisper is strong on Russian -- among its best languages.",
    ),
    "ar": LanguageSpec(
        code="ar",
        english_name="Arabic",
        endonym="العربية",
        piper_dir="ar/ar_JO/kareem/medium",
        piper_voice="ar_JO-kareem-medium",
        piper_rate=22050,
        turn_tokens=1400,
        greeting_out=(
            "مرحبا، أنا من قسم التشغيل في تراكر، بخصوص حمولتك. "
            "هل الوقت مناسب الآن لنمر على تفاصيل الرحلة؟"
        ),
        greeting_in="قسم التشغيل في تراكر، أنا المساعد الآلي. كيف أستطيع مساعدتك؟",
        notes=(
            "Speak Arabic only, in Arabic script, right-to-left. Never use Latin "
            "letters -- the voice would spell them out. Write the company name as "
            "تراكر. The voice is Jordanian/Levantine; use Modern Standard Arabic "
            "for clarity but accept Gulf and Levantine dialect words from the "
            "driver, since GCC drivers rarely speak formal MSA. Say the company "
            "name and load numbers slowly."
        ),
        stt_quality="Whisper handles Arabic reasonably; dialect hurts more than accent.",
    ),
    "ur": LanguageSpec(
        code="ur",
        english_name="Urdu",
        endonym="اردو",
        piper_dir="ur/ur_PK/fasih/medium",
        piper_voice="ur_PK-fasih-medium",
        piper_rate=22050,
        turn_tokens=1400,
        greeting_out=(
            "السلام علیکم، میں ٹرکر ڈسپیچ سے بات کر رہا ہوں، آپ کے لوڈ کے بارے میں۔ "
            "کیا ابھی آپ سے ٹرپ کی تفصیل پوچھ سکتا ہوں؟"
        ),
        greeting_in="ٹرکر ڈسپیچ، میں آٹومیٹڈ اسسٹنٹ بول رہا ہوں۔ میں آپ کی کیا مدد کر سکتا ہوں؟",
        notes=(
            "Speak Urdu only, in Nastaliq/Arabic script, right-to-left. Never use "
            "Latin or Devanagari -- the voice is trained on Urdu script. Write the "
            "company name as ٹرکر. You are male: use masculine verb forms about "
            "yourself. Spoken Urdu and Hindi are nearly identical, so freight "
            "loanwords like لوڈ, پک اپ, بارڈر are normal and correct -- do not "
            "substitute formal Persian or Arabic vocabulary for them."
        ),
        # Decode Urdu audio with the HINDI model. Spoken Urdu and Hindi are
        # one language with two scripts, and Nemotron covers Hindi but not Urdu
        # -- so this buys streaming and a much better WER than Whisper's Urdu.
        # The transcript arrives in Devanagari; only the LLM reads it, and the
        # prompt already requires replies in Urdu script.
        stt_engine="nemotron",
        stt_lang="hi",
        stt_quality=(
            "Decoded as Hindi via Nemotron (same spoken language, different "
            "script), which gives streaming and better accuracy than Whisper's "
            "Urdu. Watch for Perso-Arabic vocabulary and names, which a Hindi "
            "model may render oddly -- set STT_ENGINE=whisper to compare."
        ),
    ),
    "kk": LanguageSpec(
        code="kk",
        english_name="Kazakh",
        endonym="қазақша",
        piper_dir="kk/kk_KZ/issai/high",
        piper_voice="kk_KZ-issai-high",
        piper_rate=22050,
        turn_tokens=1300,
        greeting_out=(
            "Сәлеметсіз бе, бұл ТракКер диспетчері, жүгіңіз туралы. "
            "Қазір рейс бойынша сөйлесуге қолайлы ма?"
        ),
        greeting_in="ТракКер диспетчері, автоматты көмекші. Қалай көмектесе аламын?",
        notes=(
            "Speak Kazakh only, in Cyrillic script (Kazakh Cyrillic includes ә, ғ, "
            "қ, ң, ө, ұ, ү, һ, і). Never use Latin script: the TTS voice is "
            "Cyrillic-trained. Use the polite plural form of address. Many freight "
            "terms are Russian loanwords in everyday Kazakh speech -- accept them "
            "when the driver uses them rather than insisting on pure Kazakh."
        ),
        # Kazakh is absent from Nemotron's 35 languages, so it is the one
        # language that cannot have streaming ASR. It stays on Whisper and will
        # therefore feel slower AND less accurate than the other six.
        stt_engine="whisper",
        stt_quality=(
            "WEAKEST of the seven. Not covered by Nemotron, so no streaming: "
            "Kazakh uses whisper-large-v3-turbo and is low-resource even there. "
            "Expect to lean on the two-attempt limit and closed re-asks."
        ),
    ),
}


def spec(code: str) -> LanguageSpec | None:
    return LANGUAGES.get((code or "").lower()[:2])


def today_fragment_en() -> str:
    """Anchor relative dates for any non-Hindi language.

    Without today's date the model cannot turn "the day before yesterday at
    eight" into a timestamp -- it has no reference point, so it guesses a year
    or omits the date. The Hindi version of this lives in config.py because it
    also carries Hindi-specific words for the relative days.
    """
    from datetime import datetime, timedelta
    now = datetime.now()
    y = now - timedelta(days=1)
    dby = now - timedelta(days=2)
    return (
        f"\nToday's date and time: {now:%Y-%m-%d %H:%M} ({now:%A}).\n"
        f"'Yesterday' means {y:%Y-%m-%d}; 'the day before yesterday' means "
        f"{dby:%Y-%m-%d}.\n"
        "Record every time in record_milestone's time_iso as "
        "'YYYY-MM-DD HH:MM' on a 24-hour clock.\n"
        "Remember: midnight is 00:00 of the FOLLOWING day. "
        "Five in the evening is 17:00; five in the morning is 05:00.\n"
    )


def build_prompt(code: str) -> str | None:
    """Policy + language directive + language notes. None for unregistered codes."""
    s = spec(code)
    if s is None or s.code == "en":
        # English needs no directive; it is the language the policy is written in.
        return POLICY if s else None
    directive = (
        f"\nLANGUAGE: Speak ONLY {s.english_name} ({s.endonym}). Every word you "
        f"say is passed to a {s.english_name} text-to-speech voice, so any other "
        f"language or script will be mispronounced or spelled out letter by "
        f"letter.\n{s.notes}\n"
    )
    return POLICY + directive
