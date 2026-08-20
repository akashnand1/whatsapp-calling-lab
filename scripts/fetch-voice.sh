#!/usr/bin/env bash
# Download the Piper voice for one language.
#
#   bash scripts/fetch-voice.sh hi        # Hindi
#   bash scripts/fetch-voice.sh tr ru kk  # several at once
#   bash scripts/fetch-voice.sh all
#
# Voices are ~60MB each and gitignored, so they are fetched rather than
# committed. Paths come from app/languages.py, which is the single place a voice
# is declared -- there is no second list here to fall out of sync with it.
set -euo pipefail
cd "$(dirname "$0")/.."

HF="https://huggingface.co/rhasspy/piper-voices/resolve/main"
mkdir -p voices

codes=("$@")
if [ ${#codes[@]} -eq 0 ]; then
  echo "usage: bash scripts/fetch-voice.sh <lang-code>... | all"
  python3 -c "
from app.languages import LANGUAGES
print('available:', ', '.join(sorted(LANGUAGES)))"
  exit 1
fi
if [ "${codes[0]}" = "all" ]; then
  mapfile -t codes < <(python3 -c "
from app.languages import LANGUAGES
print('\n'.join(sorted(LANGUAGES)))")
fi

for code in "${codes[@]}"; do
  read -r dir voice < <(python3 -c "
import sys
from app.languages import spec
s = spec('$code')
if s is None:
    sys.exit('no such language: $code')
print(s.piper_dir, s.piper_voice)")

  if [ -f "voices/$voice.onnx" ]; then
    echo "  $code: $voice already present"
    continue
  fi
  echo "  $code: downloading $voice (~60MB)…"
  if curl -fsL -o "voices/$voice.onnx" "$HF/$dir/$voice.onnx"; then
    # The sidecar .json carries the true sample rate. Without it the code falls
    # back to PIPER_RATE, and a wrong rate makes every voice chipmunk-pitched.
    curl -fsL -o "voices/$voice.onnx.json" "$HF/$dir/$voice.onnx.json" \
      || echo "     !! got the model but not its .json — sample rate may be wrong"
    echo "     done."
  else
    rm -f "voices/$voice.onnx"
    echo "     !! failed. Check $HF/$dir/$voice.onnx exists."
  fi
done

echo
echo "Set the language in .env, e.g.:"
echo "  AGENT_LANGUAGE=tr"
echo "  PIPER_MODEL=$(pwd)/voices/<voice>.onnx"
echo "Then restart uvicorn. Verify with:  python cli.py test-ai"
