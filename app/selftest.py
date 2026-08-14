"""Browser self-test: talk to the agent with no WhatsApp involved.

Open http://localhost:8000/selftest, click Connect, and speak into your laptop
mic. Your browser becomes the "WhatsApp client".

This exercises the *exact same code path* as a real call --
`OutboundAudioTrack`, `Pipeline`, STT, LLM, TTS, barge-in, the 20 ms/48 kHz frame
geometry -- with only two differences:

  * Signalling is a local POST instead of Meta's Graph API
  * Media goes browser <-> aiortc directly, so no NAT traversal problem

Which is exactly why it is useful. It isolates "is my agent any good?" from
"can I reach Meta's media relay?" -- two problems that are miserable to debug
at the same time.

Prove the conversation feels right here first. Then go fight ICE.
"""

from __future__ import annotations

import asyncio
import logging

from aiortc import RTCPeerConnection, RTCSessionDescription
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .ai import Pipeline
from .audio import InboundResampler, OutboundAudioTrack
from .config import GREETING, SYSTEM_PROMPT
from .providers import describe_stack

log = logging.getLogger("selftest")
router = APIRouter(prefix="/selftest")

_sessions: dict[str, dict] = {}


class Offer(BaseModel):
    sdp: str
    type: str


@router.post("/offer")
async def offer(o: Offer) -> dict[str, str]:
    """Accept the browser's SDP offer and wire it to the AI pipeline."""
    pc = RTCPeerConnection()
    track = OutboundAudioTrack()
    pc.addTrack(track)

    pipeline = Pipeline(
        system_prompt=SYSTEM_PROMPT,
        on_audio=lambda pcm, rate: track.push_pcm(pcm, rate),
        interrupt_playback=track.interrupt,
    )
    sid = str(id(pc))
    _sessions[sid] = {"pc": pc, "pipeline": pipeline, "tasks": []}

    log.info("selftest stack: %s", describe_stack())

    @pc.on("track")
    def _on_track(inbound):  # noqa: ANN001
        if inbound.kind != "audio":
            return

        resampler = InboundResampler()   # stateful: one per track

        async def pump() -> None:
            while True:
                try:
                    frame = await inbound.recv()
                except Exception:
                    log.info("selftest: inbound track ended")
                    return
                pcm16 = resampler.to_pcm16(frame)

                # Drive the echo guard from ACTUAL PLAYBACK, not generation.
                # Piper renders a whole sentence in one call, so generation can
                # finish seconds before the audio has left the speaker -- and it
                # is the speaker that causes echo.
                audible = pipeline.is_speaking or track.is_playing
                pipeline.stt.agent_speaking = audible
                pipeline.stt.playback_frames = track.frames_emitted

                # Feed the provider FIRST so its gate is up to date, then use the
                # gate's own verdict. Running a separate VAD here was the bug that
                # let the agent barge in on its own greeting: the duplicate VAD had
                # a fixed threshold and no idea the agent was speaking.
                await pipeline.stt.send_audio(pcm16)

                if audible and pipeline.stt.caller_speaking:
                    pipeline.barge_in()

        _sessions[sid]["tasks"].append(asyncio.create_task(pump()))

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        log.info("selftest pc state=%s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await _teardown(sid)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=o.sdp, type=o.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Start the agent and greet, exactly as we would on ACCEPTED.
    await pipeline.start()
    _sessions[sid]["tasks"].append(asyncio.create_task(pipeline.run_stt_loop()))
    asyncio.create_task(pipeline.say(GREETING))

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "id": sid}


async def _teardown(sid: str) -> None:
    s = _sessions.pop(sid, None)
    if not s:
        return
    for t in s["tasks"]:
        if not t.done():
            t.cancel()
    log.info("selftest transcript:\n%s", s["pipeline"].transcript_text())
    log.info("selftest stats: %s", s["pipeline"].stats())
    await s["pipeline"].close()
    await s["pc"].close()


@router.post("/hangup/{sid}")
async def hangup(sid: str) -> dict[str, object]:
    s = _sessions.get(sid)
    out = {
        "transcript": s["pipeline"].transcript_text() if s else "",
        "stats": s["pipeline"].stats() if s else {},
    }
    await _teardown(sid)
    return out


@router.get("/stack")
async def stack() -> dict[str, str]:
    """Which providers are configured and what leaves your network."""
    return describe_stack()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def page() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>WhatsApp Calling Lab — self test</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; max-width: 720px;
         margin: 40px auto; padding: 0 20px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { opacity: .65; margin-bottom: 24px; }
  button { font: inherit; padding: 9px 18px; border-radius: 7px;
           border: 1px solid currentColor; background: transparent;
           cursor: pointer; margin-right: 8px; }
  button:disabled { opacity: .4; cursor: default; }
  #state { margin: 18px 0; padding: 10px 14px; border-radius: 7px;
           border: 1px solid rgba(128,128,128,.35); font-variant-numeric: tabular-nums; }
  #log { margin-top: 20px; }
  .turn { padding: 8px 12px; border-radius: 7px; margin-bottom: 7px; }
  .you   { background: rgba(120,120,120,.14); }
  .agent { background: rgba(80,140,255,.14); }
  .who { font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
         opacity: .6; display:block; }
  .meter { height: 5px; background: rgba(128,128,128,.25); border-radius: 3px;
           overflow: hidden; margin-top: 10px; }
  .meter > div { height: 100%; width: 0; background: currentColor; }
  code { background: rgba(128,128,128,.16); padding: 1px 5px; border-radius: 4px; }
</style></head>
<body>
<h1>Agent self-test</h1>
<div class="sub">Your browser stands in for the WhatsApp client. Same pipeline,
same 48&nbsp;kHz / 20&nbsp;ms frames — no Meta account needed.</div>

<button id="go">Connect &amp; talk</button>
<button id="stop" disabled>Hang up</button>

<div id="state">idle</div>
<div class="meter"><div id="lvl"></div></div>
<div id="log"></div>
<audio id="out" autoplay></audio>

<script>
let pc, sid, stream, ctx, raf;
const $ = id => document.getElementById(id);
const setState = t => $("state").textContent = t;

function log(who, text) {
  const d = document.createElement("div");
  d.className = "turn " + (who === "you" ? "you" : "agent");
  d.innerHTML = '<span class="who">' + who + '</span>';
  d.appendChild(document.createTextNode(text));
  $("log").appendChild(d);
  window.scrollTo(0, document.body.scrollHeight);
}

function meter(s) {
  ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(s);
  const an = ctx.createAnalyser(); an.fftSize = 512;
  src.connect(an);
  const buf = new Uint8Array(an.frequencyBinCount);
  (function tick() {
    an.getByteFrequencyData(buf);
    const avg = buf.reduce((a, b) => a + b, 0) / buf.length;
    $("lvl").style.width = Math.min(100, avg * 2.2) + "%";
    raf = requestAnimationFrame(tick);
  })();
}

$("go").onclick = async () => {
  $("go").disabled = true;
  setState("requesting microphone…");
  try {
    // Ask for the cleanup a phone would do. Without echo cancellation the
    // agent hears its own voice through your speakers and barges in on itself.
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
  } catch (e) {
    setState("microphone denied: " + e.message); $("go").disabled = false; return;
  }
  meter(stream);

  pc = new RTCPeerConnection();
  stream.getTracks().forEach(t => pc.addTrack(t, stream));
  pc.ontrack = e => { $("out").srcObject = e.streams[0]; };
  pc.onconnectionstatechange = () => setState("webrtc: " + pc.connectionState);

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  // Wait for ICE gathering so the offer carries all candidates.
  await new Promise(r => {
    if (pc.iceGatheringState === "complete") return r();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && r();
    setTimeout(r, 2000);
  });

  setState("connecting…");
  const res = await fetch("/selftest/offer", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
  });
  if (!res.ok) { setState("server error " + res.status); return; }
  const ans = await res.json();
  sid = ans.id;
  await pc.setRemoteDescription(ans);

  $("stop").disabled = false;
  setState("connected — say something");
  log("agent", "(greeting playing… check server logs for the transcript)");
};

$("stop").onclick = async () => {
  $("stop").disabled = true;
  cancelAnimationFrame(raf);
  if (ctx) ctx.close();
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (pc) pc.close();
  setState("hanging up…");
  if (sid) {
    const r = await fetch("/selftest/hangup/" + sid, { method: "POST" });
    const d = await r.json();
    $("log").innerHTML = "";
    (d.transcript || "").split("\\n").filter(Boolean).forEach(line => {
      const i = line.indexOf(":");
      const who = line.slice(0, i) === "user" ? "you" : "agent";
      log(who, line.slice(i + 1).trim());
    });
    const t = d.stats && d.stats.ttfw_ms;
    setState(t && t.avg
      ? "done — time-to-first-word avg " + t.avg + " ms (min " + t.min + ", max " + t.max + ")"
      : "done");
  }
  $("go").disabled = false;
};
</script>
</body></html>
"""
