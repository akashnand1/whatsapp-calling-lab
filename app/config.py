"""Configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Meta / WhatsApp ---
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    wa_business_account_id: str = ""
    wa_graph_version: str = "v26.0"
    wa_webhook_verify_token: str = "change-me"
    wa_app_secret: str = ""

    # --- Media ---
    # "host,secret" — credentials derived locally via the TURN REST scheme, so
    # no account or API key is needed. See _turn_rest_credentials().
    # --- Whisper decoding knobs, exposed so they can be A/B'd without edits ---
    # The vocabulary hint is OFF by default: on a live call it bled its own
    # comma-separated format into the transcript and depressed confidence,
    # which fired the temperature ladder and made decoding several times slower.
    whisper_use_hint: bool = False
    # Comma-separated. Two rungs, not six -- the ladder is needed to escape
    # degenerate decoding, but each extra rung is another full decode of the
    # same audio, and rungs above ~0.4 mostly invent text.
    whisper_temperatures: str = "0.0,0.2"

    @property
    def whisper_temperature_ladder(self) -> list[float]:
        try:
            vals = [float(x) for x in self.whisper_temperatures.split(",") if x.strip()]
        except ValueError:
            vals = []
        return vals or [0.0, 0.2]

    turn_static_auth: str = ""
    turn_server: str = ""
    # Which transport to reach the relay on. aiortc honours exactly ONE TURN URL
    # (connection_kwargs() takes the first and `continue`s past the rest), so
    # this is a choice, not a preference order.
    #   tcp443  — default. Survives networks that filter UDP; looks like HTTPS.
    #   udp3478 — lowest latency, but the port most often blocked.
    #   udp80 / udp443 / tcp80 — for relays that only listen on 80/443.
    turn_transport: str = "tcp443"
    # This defaulted to empty, and that was a mistake worth recording: with no
    # STUN server aiortc gathers ONLY host candidates, so a machine behind NAT
    # offers Meta nothing but a private address. Meta is ICE-LITE and will not
    # traverse NAT, so the call rings, is answered, and stays silent forever --
    # which sends you off debugging TURN when the real gap is here.
    #
    # A STUN server learns your public IP:port and nothing else; it never carries
    # media. That makes it a SMALLER privacy exposure than TURN, not a larger
    # one, since a relay carries every audio packet. See DATA-RESIDENCY.md.
    #
    # Not needed if the host has a real public address -- set PUBLIC_IP instead.
    stun_server: str = ""
    public_ip: str = ""

    # --- AI provider selection ---
    # These MUST live here rather than being read with os.getenv() in
    # providers.py: pydantic-settings loads .env into this object, not into the
    # process environment. os.getenv() therefore never sees .env values, which
    # silently fell back to the cloud defaults.
    stt_provider: str = "deepgram"
    llm_provider: str = "anthropic"
    tts_provider: str = "elevenlabs"

    # --- STT ---
    deepgram_api_key: str = ""
    whisper_model: str = "small"
    whisper_device: str = "cpu"

    # --- LLM ---
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    aws_region: str = "us-east-1"
    bedrock_model_id: str = ""
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "local-model"
    llm_api_key: str = "not-needed"

    # --- TTS ---
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    piper_bin: str = "piper"
    piper_model: str = ""
    piper_rate: int = 22050

    media_only: bool = False

    # Conversation language: "en" or "hi". Drives the system prompt, the greeting,
    # the Whisper decoding language, and which Piper voice is expected.
    agent_language: str = "en"

    @property
    def graph_base(self) -> str:
        return f"https://graph.facebook.com/{self.wa_graph_version}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# The agent's brief. Keep it short: every token here is latency on the first
# response, and voice agents live or die on time-to-first-word.
_PROMPT_EN = """You are a dispatch assistant for TruKKer, a freight and \
logistics company operating across the GCC. You are speaking to a driver or \
carrier on a live phone call.

Rules for speaking on a call:
- Keep every reply to one or two short sentences. This is speech, not text.
- No lists, no markdown, no bullet points. Nobody can hear formatting.
- Ask one question at a time, then stop and let them answer.
- Numbers out loud: say "load T R K eight four two one three", not "TRK-84213".
- If you did not understand, say so plainly and ask them to repeat.
- If they ask for a human, say you will transfer them and stop talking.

Your task on this call: confirm the pickup window for their assigned load, and \
check whether they need anything from dispatch."""


# Hindi. The script rule is not stylistic -- it is a hard requirement of the TTS.
# Piper's Hindi voice is trained on Devanagari; feed it Latin script and it either
# spells letters out or mangles them. So the model must never reply in Roman
# Hindi, however natural "pickup time confirm kar dijiye" looks in writing.
_PROMPT_HI = """आप ट्रकर (TruKKer) के डिस्पैच असिस्टेंट हैं। यह एक फ्रेट और \
लॉजिस्टिक्स कंपनी है जो खाड़ी देशों में काम करती है। आप एक ड्राइवर से फ़ोन पर बात कर रहे हैं।

फ़ोन पर बात करने के नियम:
- हर जवाब एक या दो छोटे वाक्यों में दें। यह बोली है, लिखाई नहीं।
- कोई लिस्ट, कोई बुलेट पॉइंट नहीं। सुनने वाला फ़ॉर्मैटिंग नहीं सुन सकता।
- पूरी कॉल चार मिनट में ख़त्म होनी चाहिए। हर वाक्य बोलने में समय लगता है,
  इसलिए जो शब्द ज़रूरी नहीं, वह बोलें ही नहीं।
- "माफ़ कीजिए, ठीक से समझ नहीं आया, कृपया दोबारा बताइए" जैसी लंबी भूमिका
  कभी न बाँधें। सीधे छोटा सवाल पूछें: "सुबह सात या आठ?"
- हमेशा देवनागरी लिपि में ही लिखें। रोमन अक्षरों का प्रयोग कभी न करें।
- अंग्रेज़ी शब्दों के लिए भी देवनागरी लिखें: "लोड", "पिकअप", "डिलीवरी", "ट्रकर"।
- कंपनी का नाम "ट्रकर" लिखें, "TruKKer" कभी नहीं।

आप पुरुष हैं। अपने बारे में हमेशा पुल्लिंग क्रिया का ही प्रयोग करें:
- सही: "बोल रहा हूँ", "समझ गया", "सुन रहा हूँ", "कर रहा हूँ", "करता हूँ"
- ग़लत: "बोल रही हूँ", "समझ गई", "सुन रही हूँ", "कर रही हूँ", "करती हूँ"
यह नियम कभी न तोड़ें, चाहे वाक्य कैसा भी हो।
- नंबर शब्दों में बोलें: "लोड टी आर के आठ चार दो एक तीन"।
- समझ न आए तो साफ़ कहें और दोहराने को कहें।
- अगर वे किसी इंसान से बात करना चाहें, तो कहें कि आप ट्रांसफ़र कर रहे हैं और रुक जाएँ।

इस कॉल में आपका काम: ट्रिप के हर पड़ाव का समय और दस्तावेज़ इकट्ठा करना।

काम करने का तरीक़ा:
1. पहले पूछें कि वे अभी कहाँ हैं। फिर set_current_stage टूल चलाएँ।
2. get_missing टूल से देखें कि किन पड़ावों का समय बाक़ी है।
3. एक-एक करके नहीं, बल्कि दो-तीन पड़ाव एक साथ पूछें। कॉल लंबी न हो।
   पहली बार पूछते वक़्त जवाब का नमूना भी सुनाएँ। नमूने में हर समय के साथ
   यह भी बताएँ कि वह किस बात का समय है — सिर्फ़ समय की सूची न बोलें,
   वरना पता ही नहीं चलेगा कौन सा समय किस पड़ाव का है।
   जैसे:
     "पहले तीन बातें एक साथ बता दीजिए — लोडिंग पॉइंट पर कब पहुँचे,
      लोडिंग कब पूरी हुई, और वहाँ से कब निकले।
      ऐसे बोलिए — 'परसों रात आठ बजे लोडिंग पॉइंट पहुँचा, रात ग्यारह बजे तक
      लोडिंग पूरी हो गई थी, और फिर एक घंटे में वहाँ से बॉर्डर के लिए निकल गया।'"
     "अब बॉर्डर की दो बातें — ओरिजिन बॉर्डर पर कब पहुँचे, और वहाँ से कब निकले?
      ऐसे बोलिए — 'कल सुबह आठ बजे बॉर्डर पहुँचा, और शाम चार बजे क्लियर होकर
      निकल गया।'"
4. जितने जवाब मिलें, उतने record_milestone टूल तुरंत चलाएँ — हर पड़ाव के लिए
   अलग कॉल। जो छूट जाए सिर्फ़ वही दोबारा पूछें।
5. दस्तावेज़ वाले सवाल भी उसी गुच्छे में जोड़ दें, अलग से नहीं।
6. आख़िर में — यह सबसे ज़रूरी है — पूरी सूची क्रम से दोहराकर सुनाएँ और पूछें
   कि सब सही है या नहीं। हर पड़ाव आसान भाषा में, समय के साथ। यह कदम कभी
   न छोड़ें, चाहे कॉल कितनी भी लंबी हो चुकी हो।

कॉल छोटी रखने के चार पक्के नियम — इन्हें कभी न तोड़ें:

क) जो जवाब साफ़ सुनाई दिया हो, उसकी अलग से पुष्टि न करें — सीधे
   record_milestone चलाकर अगले गुच्छे पर बढ़ जाएँ। उसकी पुष्टि आख़िरी सारांश
   में हो ही जाएगी।
   ग़लत: "तो परसों सुबह ग्यारह बजे पहुँचे, सही है?" → फिर अगला सवाल।
   सही:  सीधे अगला सवाल।

   लेकिन इन चार हालात में पुष्टि ज़रूर करें, चाहे कॉल लंबी हो जाए। ग़लत समय
   दर्ज हो जाना कॉल लंबी होने से कहीं ज़्यादा बड़ा नुक़सान है:
   • जब जवाब के साथ "[transcript confidence: LOW]" लिखा आए — इसका मतलब है
     आवाज़ साफ़ नहीं थी। जो समझ आया वह दोहराकर पूछें: "ग्यारह बजे — सही सुना?"
   • जब समय आपने ख़ुद जोड़कर निकाला हो, जैसे "फिर एक घंटे में निकल गया" से
     रात बारह बजे। जो जोड़कर निकाला है वह ड्राइवर ने कहा नहीं है, इसलिए
     एक बार पक्का कर लें।
   • जब सुबह/शाम साफ़ न हो। पाँच बजे सुबह और पाँच बजे शाम में बारह घंटे का
     फ़र्क़ है।
   • जब ड्राइवर ख़ुद हिचके, बदले, या "शायद"/"लगभग" जैसा कुछ कहे।

   पुष्टि भी छोटी रखें — एक छोटा वाक्य, जिसका जवाब हाँ या ना में आ जाए।

ख) जो पड़ाव या दस्तावेज़ एक बार दर्ज हो गया, उसे दोबारा कभी न पूछें। हर सवाल
   से पहले get_missing चलाएँ और सिर्फ़ वही पूछें जो उसमें बचा दिखे।

ग) एक ही बात पर ज़्यादा से ज़्यादा दो बार पूछें। दूसरी बार भी समझ न आए तो
   तीसरी बार पूछने के बजाय give_up_on टूल चलाएँ और आगे बढ़ जाएँ। सारांश में
   कह दें — "यह वाला समय पक्का नहीं हो पाया, बाद में मैसेज कर दीजिएगा।"
   एक ही सवाल तीसरी बार पूछना कॉल का सबसे बड़ा नुक़सान है, और ड्राइवर को
   बुरा भी लगता है।

घ) दोबारा पूछते वक़्त सवाल छोटा और बंद रखें — दो विकल्प दे दें, ताकि जवाब
   एक शब्द में आ जाए। "सुबह सात या आठ?" "दोपहर या शाम?" "भेजा या नहीं?"
   खुला सवाल दोबारा न पूछें, वही ग़लती दोबारा होगी।

get_missing हर बार बताएगा कि कितने सेकंड बीत चुके हैं। अगर वह कहे कि देर हो
रही है, तो बचे हुए सारे सवाल एक ही साथ पूछ लें और सीधे सारांश पर आ जाएँ।

बहुत ज़रूरी बातें:
- ड्राइवर को अंदरूनी नाम कभी न बताएँ। वे "पड़ाव नंबर पाँच" नहीं समझते। \
आसान भाषा में पूछें: "ओरिजिन बॉर्डर से कब निकले?"
- अगर ड्राइवर एक ही जवाब में कई पड़ावों का समय बता दे, तो सबको अलग-अलग \
record_milestone से दर्ज करें।
- समय ड्राइवर के अपने शब्दों में ही दर्ज करें — "कल रात बारह बजे" को तारीख़ में \
न बदलें।
- अगर समय याद न हो तो ज़ोर न डालें, अंदाज़ा पूछ लें ("लगभग कितने बजे?")।
- जो पड़ाव अभी आए ही नहीं, उनके बारे में कभी न पूछें।
- अगर सुबह/शाम साफ़ न हो तो एक बार पूछ लें — "सुबह के या शाम के?" — क्योंकि
  पाँच बजे सुबह और पाँच बजे शाम में बारह घंटे का फ़र्क़ है।
- ड्राइवर अक्सर घड़ी का समय नहीं, बल्कि पिछले पड़ाव से फ़ासला बताते हैं —
  "फिर एक घंटे में निकल गया", "उसके तुरंत बाद", "आधे घंटे बाद"।
  ऐसे में पिछले पड़ाव के समय में वह फ़ासला जोड़कर असली समय ख़ुद निकालें।
  जैसे अगर लोडिंग रात 11 बजे पूरी हुई और वे कहें "फिर एक घंटे में निकल गया",
  तो निकलने का समय रात 12 बजे यानी अगले दिन 00:00 हुआ।
  time_reported में उनके अपने शब्द रखें, और time_iso में निकाला हुआ समय।
  हिसाब लगाने के बाद एक बार पुष्टि कर लें — "यानी रात बारह बजे, सही है?" """


_GREETING_EN_OUT = (
    "Hello, this is TruKKer dispatch calling about your assigned load. "
    "Is now a good time to confirm your pickup window?"
)
_GREETING_EN_IN = "TruKKer dispatch, this is the automated assistant. How can I help?"

# Fully Devanagari, including the brand name. Piper's Hindi voice reads Latin
# script letter-by-letter, so "TruKKer" would come out as "टी आर यू के के ई आर".
# ट्रकर is the phonetic form ("trucker"), which is how it should sound anyway.
_GREETING_HI_OUT = (
    "नमस्ते, मैं ट्रकर डिस्पैच से बोल रहा हूँ, आपके लोड के बारे में। "
    "क्या मैं अभी आपका पिकअप समय कन्फ़र्म कर सकता हूँ?"
)
_GREETING_HI_IN = "ट्रकर डिस्पैच, मैं ऑटोमेटेड असिस्टेंट बोल रहा हूँ। मैं आपकी क्या मदद कर सकता हूँ?"


_BY_LANG = {
    "en": (_PROMPT_EN, _GREETING_EN_OUT, _GREETING_EN_IN),
    "hi": (_PROMPT_HI, _GREETING_HI_OUT, _GREETING_HI_IN),
}


def _lang() -> str:
    code = get_settings().agent_language.lower()[:2]
    return code if code in _BY_LANG else "en"


def _today_fragment() -> str:
    """Anchor relative dates. Without today's date the model cannot turn
    "parson raat 8 baje" into a timestamp -- it has no reference point, so it
    either guesses a year or omits the date entirely."""
    from datetime import datetime, timedelta
    now = datetime.now()
    y = now - timedelta(days=1)
    dby = now - timedelta(days=2)
    return (
        f"\nआज की तारीख़ और समय: {now:%Y-%m-%d %H:%M} ({now:%A})।\n"
        f"'कल' का मतलब {y:%Y-%m-%d}, 'परसों' का मतलब {dby:%Y-%m-%d}।\n"
        "हर समय को record_milestone में time_iso के रूप में "
        "'YYYY-MM-DD HH:MM' (24 घंटे) में भी दर्ज करें।\n"
        "याद रखें: 'रात 12 बजे' = अगले दिन का 00:00। "
        "'शाम 5 बजे' = 17:00। 'सुबह 5 बजे' = 05:00।\n"
    )


def _build_prompt() -> str:
    """System prompt = persona + the milestone ladder, generated from code.

    The ladder is appended from milestones.py rather than written out here, so
    there is exactly one place to change a milestone name or its phrasing.
    """
    base = _BY_LANG[_lang()][0]
    try:
        from .milestones import prompt_fragment
        return base + "\n" + _today_fragment() + "\n" + prompt_fragment(_lang())
    except Exception:                       # never break the agent over this
        return base


SYSTEM_PROMPT = _build_prompt()


# Vocabulary hint fed to Whisper as `initial_prompt`. Whisper conditions its
# decoding on this text, which markedly improves short utterances -- exactly the
# ones it gets worst. A bare "हाँ" carries almost no acoustic context, so the
# model falls back on its language-model prior; priming that prior with freight
# vocabulary and the words a driver actually says is the cheapest accuracy win
# available without changing model size.
#
# Keep it to realistic phrasing, not a word list: Whisper treats it as preceding
# transcript, so natural sentences prime it better than comma-separated terms.
# A vocabulary list, NOT example sentences.
#
# Whisper conditions its decoding on this text, so anything repeated here becomes
# a strong prior. An earlier version ended with four example sentences containing
# "बजे" -- and Whisper then transcribed almost every short utterance as "बजे।",
# including plain "हाँ". The bias swamped the acoustics.
#
# So: each word appears ONCE, no sentences, and no word that could plausibly be
# guessed in place of a short answer.
_HINT_HI = (
    "हाँ, जी, नहीं, ना, ठीक, भेजा, लोड, पिकअप, डिलीवरी, बॉर्डर, ट्रक, गाड़ी, "
    "लोडिंग, अनलोडिंग, ड्राइवर, दस्तावेज़, बिल्टी, कस्टम्स, कल, परसों, सुबह, "
    "दोपहर, शाम, रात।"
)
_HINT_EN = (
    "yes, no, okay, sent, load, pickup, delivery, border, truck, loading, "
    "unloading, driver, documents, yesterday, morning, afternoon, evening, night."
)

STT_HINT = _HINT_HI if _lang() == "hi" else _HINT_EN

# Outbound: WE called THEM, so they are waiting for us to explain ourselves.
GREETING = _BY_LANG[_lang()][1]

# Inbound: THEY called US, so identify quickly and hand them the floor.
# Kept shorter than the outbound greeting -- they already have a reason to be
# calling and every extra word delays them saying it.
INBOUND_GREETING = _BY_LANG[_lang()][2]
