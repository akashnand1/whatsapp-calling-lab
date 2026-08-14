#!/bin/bash
# Installs local speech: faster-whisper (STT) + Piper (TTS). Both MIT, both free,
# neither sends audio anywhere.
#
# The LLM is left as Claude via the Anthropic API by default, because that is a
# text-only stage -- no caller audio reaches it, only the transcript.
#
#   bash setup-local-ai.command                  → English, LLM = Claude
#   bash setup-local-ai.command hi               → Hindi
#   bash setup-local-ai.command hi --with-ollama → Hindi + a local LLM (4.7GB)
set -e
cd "$(dirname "$0")"

LANG_CODE="en"
WITH_OLLAMA=0
for arg in "$@"; do
  case "$arg" in
    en|hi)         LANG_CODE="$arg" ;;
    --with-ollama) WITH_OLLAMA=1 ;;
  esac
done

echo "=========================================================="
echo " Local speech stack — Whisper (STT) + Piper (TTS)"
if [ "$WITH_OLLAMA" = "1" ]; then
  echo " Plus a local LLM via Ollama"
else
  echo " LLM stays as Claude (Anthropic API)"
fi
echo "=========================================================="
echo

if [ ! -d .venv ]; then
  echo "!! .venv not found. Run SETUP-MAC.command first."
  exit 1
fi
source .venv/bin/activate

# ---------------------------------------------------------------- STT
echo "--> 1/4  Speech-to-text: faster-whisper"
pip install --quiet faster-whisper
echo "         installed. Model downloads on first use (~500MB for 'small')."
echo

# ---------------------------------------------------------------- TTS
echo "--> 2/4  Text-to-speech: Piper"
pip install --quiet piper-tts
mkdir -p voices

HF="https://huggingface.co/rhasspy/piper-voices/resolve/main"

fetch_voice () {   # $1=subpath  $2=filename  $3=label
  if [ ! -f "voices/$2.onnx" ]; then
    echo "         downloading $3 voice (~60MB)…"
    if curl -fsL -o "voices/$2.onnx" "$HF/$1/$2.onnx"; then
      curl -fsL -o "voices/$2.onnx.json" "$HF/$1/$2.onnx.json" || true
      echo "         got $3."
    else
      rm -f "voices/$2.onnx"
      echo "         !! $3 voice not available at that path — skipping."
      echo "            Browse https://huggingface.co/rhasspy/piper-voices for alternatives."
    fi
  else
    echo "         $3 voice already present."
  fi
}

if [ "$LANG_CODE" = "hi" ]; then
  fetch_voice "hi/hi_IN/pratham/medium"   "hi_IN-pratham-medium"   "Hindi (male, pratham)"
  fetch_voice "hi/hi_IN/priyamvada/medium" "hi_IN-priyamvada-medium" "Hindi (female, priyamvada)"
  VOICE_FILE="hi_IN-pratham-medium.onnx"
else
  fetch_voice "en/en_US/amy/medium" "en_US-amy-medium" "English (amy)"
  VOICE_FILE="en_US-amy-medium.onnx"
fi
echo

# ---------------------------------------------------------------- LLM
if [ "$WITH_OLLAMA" = "1" ]; then
  echo "--> 3/4  Language model: Ollama (local)"
  if ! command -v ollama >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      brew install ollama
    else
      echo "!! Install Homebrew, or get Ollama from https://ollama.com/download"
      exit 1
    fi
  else
    echo "         ollama already installed."
  fi
  if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "         starting ollama in the background…"
    (ollama serve >/dev/null 2>&1 &)
    sleep 4
  fi
  MODEL="qwen2.5:7b-instruct"
  if ! ollama list 2>/dev/null | grep -q "qwen2.5:7b"; then
    echo "         pulling $MODEL (~4.7GB, one time)…"
    ollama pull "$MODEL"
  else
    echo "         $MODEL already pulled."
  fi
else
  echo "--> 3/4  Language model: Claude (Anthropic API) — nothing to install"
  echo "         Skipping Ollama. Re-run with --with-ollama if you want a local LLM."
fi
echo

# ---------------------------------------------------------------- .env
echo "--> 4/4  Writing local-AI settings into .env"
PIPER_PATH="$(command -v piper || echo piper)"
VOICE_PATH="$(pwd)/voices/$VOICE_FILE"

python3 - "$PIPER_PATH" "$VOICE_PATH" "$WITH_OLLAMA" "$LANG_CODE" <<'PY'
import re, sys, pathlib
piper_bin, voice, with_ollama, lang = (
    sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
)
p = pathlib.Path(".env")
s = p.read_text()

# Whisper: 'base' is fine for English but noticeably weak on Hindi, so Hindi gets
# 'small'. Bigger download, better accuracy, slower on CPU.
whisper_model = "small" if lang == "hi" else "base"

new = {
    "MEDIA_ONLY": "0",
    "AGENT_LANGUAGE": lang,
    # --- speech stays local ---
    "STT_PROVIDER": "whisper_local",
    "WHISPER_MODEL": whisper_model,
    "WHISPER_DEVICE": "cpu",
    "TTS_PROVIDER": "piper_local",
    "PIPER_BIN": piper_bin,
    "PIPER_MODEL": voice,
    "PIPER_RATE": "22050",
}
if with_ollama:
    new.update({
        "LLM_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "LLM_MODEL": "qwen2.5:7b-instruct",
        "LLM_API_KEY": "ollama",
    })
else:
    new["LLM_PROVIDER"] = "anthropic"
    # ANTHROPIC_API_KEY deliberately NOT written here -- see the note printed below.

for k, v in new.items():
    if re.search(rf"(?m)^{k}=", s):
        s = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", s)
    else:
        s += f"\n{k}={v}"
p.write_text(s)
print("         .env updated:")
for k, v in new.items():
    print(f"           {k}={v}")
PY

echo
echo "=========================================================="
echo " Done."
echo
if [ "$WITH_OLLAMA" != "1" ]; then
  echo " Supply your Claude key as a SHELL VARIABLE, not in .env:"
  echo
  echo "   export ANTHROPIC_API_KEY='sk-ant-...'"
  echo
  echo " Settings are read from the environment first, so this works and the"
  echo " key never gets written to a file. Set it in each terminal that runs"
  echo " uvicorn or the CLI. To persist it, add the line to ~/.zshrc."
  echo
fi
echo " Verify each stage and see the latency:"
echo "   source .venv/bin/activate"
echo "   python cli.py test-ai"
echo
echo " Then talk to it (use headphones):"
echo "   uvicorn app.main:app --reload --port 8000"
echo "   open http://localhost:8000/selftest"
echo "=========================================================="
