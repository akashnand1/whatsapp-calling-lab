#!/usr/bin/env bash
# Container setup. Runs once, when the Codespace is first created.
#
# Deliberately does NOT install into a .venv: the container is already an
# isolated environment, and a nested venv only creates a second thing to
# remember to activate.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Python packages"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> Local speech stack"
# Not in requirements.txt because the cloud providers (Deepgram/ElevenLabs) are
# a valid configuration and these are large. They are installed here because the
# whole point of this container is running the self-hosted stack.
pip install --quiet faster-whisper piper-tts

# Streaming ASR. Several GB and slow to install, so it is skipped on small
# machines where it could not be loaded anyway -- Nemotron refuses to start
# below ~5GB free rather than be OOM-killed mid-call.
TOTAL_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$TOTAL_MB" -ge 12000 ]; then
  echo "    ${TOTAL_MB}MB RAM — installing nemo_toolkit for streaming ASR (several GB, slow)…"
  pip install --quiet 'nemo_toolkit[asr]' || echo "    !! nemo install failed; Whisper will be used"
else
  echo "    ${TOTAL_MB}MB RAM — SKIPPING nemo_toolkit."
  echo "       Streaming ASR needs ~5GB free and would be OOM-killed here."
  echo "       Rebuild on a 4-core/16GB machine to enable it, or set"
  echo "       STT_ENGINE=whisper in .env to make the slower path explicit."
fi

echo "==> Hindi voice for Piper"
mkdir -p voices
HF="https://huggingface.co/rhasspy/piper-voices/resolve/main"
fetch_voice () {   # $1=subpath  $2=name  $3=label
  if [ -f "voices/$2.onnx" ]; then
    echo "    $3 already present"
    return
  fi
  echo "    downloading $3 (~60MB)…"
  if curl -fsL -o "voices/$2.onnx" "$HF/$1/$2.onnx"; then
    curl -fsL -o "voices/$2.onnx.json" "$HF/$1/$2.onnx.json" || true
  else
    rm -f "voices/$2.onnx"
    echo "    !! could not fetch $3"
  fi
}
fetch_voice "hi/hi_IN/pratham/medium"    "hi_IN-pratham-medium"    "Hindi male (pratham)"
fetch_voice "hi/hi_IN/priyamvada/medium" "hi_IN-priyamvada-medium" "Hindi female (priyamvada)"

echo "==> .env"
if [ -f .env ]; then
  echo "    .env already exists, leaving it alone"
else
  cp .env.example .env
  # The macOS paths in .env.example are wrong inside the container, and a wrong
  # PIPER_BIN fails at the first spoken word rather than at startup -- which is
  # a slow way to find a one-line problem. Rewrite them now.
  PIPER_BIN="$(command -v piper || echo piper)"
  python3 - "$PIPER_BIN" "$(pwd)" <<'PY'
import pathlib, re, sys
piper_bin, root = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
s = p.read_text()
for key, val in {
    "PIPER_BIN": piper_bin,
    "PIPER_MODEL": f"{root}/voices/hi_IN-pratham-medium.onnx",
    "AGENT_LANGUAGE": "hi",
    "STT_PROVIDER": "whisper_local",
    "TTS_PROVIDER": "piper_local",
    "LLM_PROVIDER": "anthropic",
    "WHISPER_MODEL": "small",
    "MEDIA_ONLY": "0",
    # This host has no public IPv4 of its own and sits behind Azure NAT, so a
    # reflexive candidate is what makes it reachable -- same mechanism as on the
    # laptop, minus the UAE VoIP filtering that blocked it there.
    "STUN_SERVER": "stun:stun.cloudflare.com:3478",
    "TURN_STATIC_AUTH": "",
    "PUBLIC_IP": "",
}.items():
    if re.search(rf"(?m)^{key}=", s):
        s = re.sub(rf"(?m)^{key}=.*$", f"{key}={val}", s)
    else:
        s += f"\n{key}={val}"
p.write_text(s)
print("    wrote container paths into .env")
PY
  echo
  echo "    !! .env still needs your secrets:"
  echo "         WA_ACCESS_TOKEN, WA_PHONE_NUMBER_ID, WA_WEBHOOK_VERIFY_TOKEN"
  echo "       and export ANTHROPIC_API_KEY in the shell (not in .env)."
fi

cat <<'EOF'

============================================================
 Container ready.

 FIRST command to run -- before anything else:

     python cli.py stun-test

 It prints the public address this container uses. Anything
 that is not a UAE ISP means the media path is finally clear.
 If STUN gets no reply at all, this platform blocks outbound
 UDP and no amount of config will fix it -- say so and we
 will move to a plain VM instead.
============================================================
EOF
