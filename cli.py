#!/usr/bin/env python3
"""Command-line companion to the lab server.

The server must be running for `call` to work (it needs to receive webhooks),
but the read-only commands here talk straight to Meta and are useful on their own.

    python cli.py preflight
    python cli.py enable-calling
    python cli.py permission 971501234567
    python cli.py request-permission 971501234567
    python cli.py call 971501234567
    python cli.py hangup wacid.HBgL...
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.graph import GraphClient, GraphError

app = typer.Typer(
    add_completion=False,
    help="WhatsApp Calling Lab CLI",
    # Typer prints locals in tracebacks by default. In this codebase the locals
    # are audio buffers, so a single unset API key produced 300 lines of raw PCM
    # and the actual cause scrolled off the screen. Errors are only useful if
    # they can be read.
    pretty_exceptions_show_locals=False,
)
console = Console()
SERVER = "http://127.0.0.1:8000"


def _run(coro):
    return asyncio.run(coro)


def _show(title: str, data) -> None:
    console.print(Panel(json.dumps(data, indent=2), title=title, border_style="cyan"))


@app.command("test-ai")
def test_ai(
    text: str = typer.Option(
        "My truck is delayed at the border, what should I do?", "--text",
        help="pretend the driver said this",
    ),
) -> None:
    """Exercise STT, LLM and TTS separately and report latency for each.

    Run this BEFORE testing a call. It tells you which stage is slow, which is
    impossible to work out from a live conversation. No WhatsApp, no cost.

    Rule of thumb for time-to-first-word:
      under  900 ms  feels natural
      1.0-1.5 s      noticeable but usable
      over  1.5 s    callers start talking over the agent
    """
    import asyncio as aio
    import logging as _logging

    import numpy as np

    # cli.py never configured logging, so every log.info() in the providers went
    # nowhere -- including the thread-count and per-utterance diagnostics that
    # exist precisely to explain a slow result. Only warnings got through, which
    # is why the picture was always partial.
    _logging.basicConfig(level=_logging.INFO, format="%(name)-9s %(message)s")
    _logging.getLogger("nemo_logger").setLevel(_logging.WARNING)   # NeMo is very chatty

    from app.config import get_settings as _gs0
    from app.providers import describe_stack, make_llm, make_stt, make_tts

    def _is_hi() -> bool:
        return _gs0().agent_language.lower()[:2] == "hi"

    console.print(Panel("\n".join(f"{k:4s} {v}" for k, v in describe_stack().items()),
                        title="configured stack", border_style="cyan"))

    # Check credentials BEFORE loading models. Whisper and Piper take 30-60s to
    # load on first run, and it is a poor trade to spend that only to fail on an
    # unset environment variable that could have been checked in a millisecond.
    _cfg = _gs0()
    if _cfg.llm_provider.lower() == "anthropic" and not _cfg.anthropic_api_key:
        console.print(
            "\n[red]ANTHROPIC_API_KEY is not set in this shell.[/]\n"
            "It is read from the environment, not .env, so every terminal needs "
            "it:\n\n"
            "    [bold]export ANTHROPIC_API_KEY='sk-ant-...'[/]\n\n"
            "The terminal running uvicorn needs it too — restart uvicorn after "
            "setting it, or the agent will greet the caller and then fail on the "
            "first thing they say.\n"
            "To avoid re-exporting on every rebuild, add it once as a Codespaces "
            "secret: https://github.com/settings/codespaces\n"
        )
        raise typer.Exit(1)

    results: dict[str, float] = {}

    async def go() -> None:
        # ---- TTS -----------------------------------------------------------
        console.print("\n[bold]TTS[/] — synthesising a short reply…")
        tts = make_tts()
        t0 = time.monotonic()
        first_byte = None
        total = 0
        try:
            async for chunk in tts.stream("Understood. I will let dispatch know."):
                if first_byte is None:
                    first_byte = time.monotonic() - t0
                total += len(chunk)
        except Exception as e:
            console.print(f"  [red]FAILED[/] {type(e).__name__}: {e}")
            console.print("  [dim]Piper: check PIPER_BIN and PIPER_MODEL in .env[/]")
            return
        finally:
            await tts.close()

        if not total:
            from app.config import get_settings as _gs
            which = _gs().tts_provider
            console.print(f"  [red]no audio produced[/] (TTS_PROVIDER={which})")
            if which == "piper_local":
                console.print("  [dim]check PIPER_BIN and PIPER_MODEL point at real files[/]")
            else:
                console.print(f"  [dim]{which} rejected the request — see the error above[/]")
            return
        secs = total / 2 / tts.rate          # int16 mono
        results["tts_first_byte"] = (first_byte or 0) * 1000
        console.print(
            f"  [green]OK[/] first byte in [bold]{results['tts_first_byte']:.0f} ms[/], "
            f"{total:,} bytes = {secs:.1f}s of speech at {tts.rate} Hz"
        )

        # Second call, so the reported figure excludes one-off model loading.
        # This is the number that matters: it is what every turn after the first
        # will actually cost.
        t1 = time.monotonic()
        warm_first = None
        warm = bytearray()
        async for chunk in tts.stream("ठीक है, मैं देख लेता हूँ।" if _is_hi() else "Right, let me check."):
            if warm_first is None:
                warm_first = time.monotonic() - t1
            warm.extend(chunk)
        if warm_first is not None:
            results["tts_first_byte"] = warm_first * 1000     # prefer the warm number
            console.print(
                f"  [green]warm[/] second call first byte in "
                f"[bold]{warm_first * 1000:.0f} ms[/] "
                f"[dim](cold start above was mostly model loading)[/]"
            )

        # Save it so you can actually LISTEN. test-ai only measured before, which
        # is useless for judging whether the voice is intelligible.
        if warm:
            import wave
            with wave.open("tts-sample.wav", "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(tts.rate)
                w.writeframes(bytes(warm))
            console.print("  [cyan]wrote tts-sample.wav[/] — play it:  afplay tts-sample.wav")

        # ---- LLM -----------------------------------------------------------
        console.print("\n[bold]LLM[/] — generating a spoken reply…")
        llm = make_llm(
            "You are a TruKKer dispatch assistant on a phone call. "
            "Reply in one or two short sentences. No lists, no markdown."
        )
        t0 = time.monotonic()
        chunks: list[str] = []
        try:
            async for c in llm.respond(text):
                if not chunks:
                    results["llm_first_chunk"] = (time.monotonic() - t0) * 1000
                chunks.append(c)
        except Exception as e:
            console.print(f"  [red]FAILED[/] {type(e).__name__}: {e}")
            # Give the hint that matches the CONFIGURED provider. Printing the
            # Ollama suggestion unconditionally sent someone looking at a local
            # LLM server when the actual problem was an unset Anthropic key.
            # Import locally. cli.py has no module-level `get_settings`, so the
            # earlier version of this block raised NameError *while reporting the
            # real error* -- which replaced a one-line "your API key is unset"
            # with 300 lines of traceback ending in the wrong exception. An error
            # handler that can itself fail is worse than no handler.
            from app.config import get_settings as _gs_err
            s = _gs_err()
            hint = {
                "anthropic":
                    "ANTHROPIC_API_KEY is not set in THIS shell. It is read from the\n"
                    "  environment, not .env, so every terminal needs it:\n"
                    "      export ANTHROPIC_API_KEY='sk-ant-...'\n"
                    "  The terminal running uvicorn needs it too — restart uvicorn after\n"
                    "  setting it, or the agent will fail on the first spoken turn.",
                "openai_compatible":
                    f"Is the server at {s.llm_base_url} up?  "
                    "curl localhost:11434/api/tags",
                "bedrock":
                    f"Check AWS credentials and that {s.bedrock_model_id or '<BEDROCK_MODEL_ID>'} "
                    f"is enabled in {s.aws_region or '<AWS_REGION>'}.",
            }.get(s.llm_provider, "Check LLM_PROVIDER in .env.")
            console.print(f"  [dim]{hint}[/]")
            return
        if not chunks:
            console.print("  [red]no output[/]")
            return
        console.print(
            f"  [green]OK[/] first clause in [bold]{results.get('llm_first_chunk', 0):.0f} ms[/], "
            f"total {(time.monotonic() - t0) * 1000:.0f} ms"
        )
        console.print(f'  driver: [dim]"{text}"[/]')
        console.print(f'  agent : "{" ".join(chunks)}"')

        # Second call on the SAME client, so TLS and connection setup are already
        # paid. On a real call the greeting warms this, so every later turn looks
        # like this number -- not the cold one above.
        t1 = time.monotonic()
        warm_llm = None
        async for _c in llm.respond("ठीक है।" if _is_hi() else "Okay."):
            if warm_llm is None:
                warm_llm = (time.monotonic() - t1) * 1000
                break
        if warm_llm is not None:
            results["llm_first_chunk"] = warm_llm
            console.print(
                f"  [green]warm[/] second call first clause in [bold]{warm_llm:.0f} ms[/] "
                f"[dim](cold figure above included TLS setup)[/]"
            )

        # ---- STT -----------------------------------------------------------
        console.print("\n[bold]STT[/] — transcribing the audio we just synthesised…")
        stt = make_stt()
        try:
            await stt.connect()
        except Exception as e:
            console.print(f"  [red]FAILED to connect[/] {type(e).__name__}: {e}")
            return

        # The probe phrase MUST match the configured language. Feeding English
        # text to a Hindi voice and then transcribing with Whisper pinned to 'hi'
        # measures nothing except how badly a Hindi voice mispronounces English.
        from app.config import get_settings as _gs2
        _lang = _gs2().agent_language.lower()[:2]
        phrase = (
            "मेरा ट्रक बॉर्डर पर खड़ा है।" if _lang == "hi"
            else "The truck is at the border crossing."
        )
        tts2 = make_tts()
        pcm = b""
        async for c in tts2.stream(phrase):
            pcm += c
        await tts2.close()

        from app.audio import resample_pcm
        pcm16 = resample_pcm(pcm, tts2.rate, 16000)
        pcm16 += b"\x00" * (16000 * 2)          # 1s of silence, to trigger endpointing

        t0 = time.monotonic()
        heard: list[str] = []

        # Record WHEN the first final arrived; do the arithmetic later. Closing
        # over a variable assigned after the task starts raises NameError inside
        # the task -- which is exactly what happened, and it silently fell back
        # to a meaningless number instead of failing loudly.
        first_at: list[float] = []

        async def listen() -> None:
            async for kind, txt in stt.events():
                if kind == "final" and txt:
                    if not first_at:
                        first_at.append(time.monotonic())
                    # Streaming engines emit incremental finals; keep a few for
                    # display and stop, or a chatty one floods the terminal.
                    if len(heard) < 8 and txt not in heard:
                        heard.append(txt)
                    # STOP once we have a result. Continuing to iterate meant
                    # waiting for the socket to die of idleness, which is how a
                    # successful transcription still ended in a 1011 traceback.
                    return

        task = aio.create_task(listen())
        step = 16000 * 2 * 20 // 1000           # 20 ms of 16-bit 16 kHz
        audio_secs = len(pcm16) / 2 / 16000
        wall0 = time.monotonic()
        for i in range(0, len(pcm16), step):
            await stt.send_audio(pcm16[i:i + step])
            # REAL TIME, deliberately. Feeding 10x faster made every streaming
            # engine look terrible: it piles all the compute into a burst AFTER
            # the speech, when the entire point of streaming is that the work
            # happens DURING it. For a batch engine the two are identical, which
            # is why this went unnoticed until a streaming engine arrived.
            await aio.sleep(0.02)
        sent_at = time.monotonic()
        results["_audio_secs"] = audio_secs
        results["_fed_wall"] = sent_at - wall0
        # Tell the engine we are done sending. Without this, a cloud engine sits
        # waiting for more audio and eventually closes the socket -- Deepgram
        # returns 1011 "did not receive audio data within the timeout window".
        await stt.finalize()
        try:
            await aio.wait_for(task, timeout=25)
        except aio.TimeoutError:
            task.cancel()
        await stt.close()

        # Latency the CALLER experiences: end of their speech -> transcript.
        # Measuring from the start of the clip just re-reports its duration,
        # which is why this read 6054ms for 3.4s of audio.
        if first_at:
            results["stt_latency"] = max((first_at[0] - sent_at) * 1000, 0.0)

        if heard:
            console.print(
                f"  [green]OK[/] transcript in [bold]{results.get('stt_latency', 0):.0f} ms[/] "
                f"[dim](cold — includes loading the model)[/]"
            )
            got = " ".join(heard)
            console.print(f'  said : [dim]"{phrase}"[/]')
            console.print(f'  heard: "{got[:160]}{"…" if len(got) > 160 else ""}"')

            # Measure STT a SECOND time, on the now-cached model.
            #
            # This was missing, and the omission made the summary actively
            # misleading: TTS and LLM both reported warm figures while STT
            # reported a cold one that included loading ~500MB of weights, and
            # the three were then added together. On a real call the greeting
            # warms every stage, so the cold number describes a moment that
            # never happens while someone is speaking.
            stt2 = make_stt()
            try:
                await stt2.connect()
                t2 = time.monotonic()
                warm_stt: float | None = None
                heard2: list[str] = []

                first_at2: list[float] = []

                async def listen2() -> None:
                    async for kind, txt in stt2.events():
                        if kind == "final" and txt:
                            if not first_at2:
                                first_at2.append(time.monotonic())
                            heard2.append(txt)
                            break

                task2 = aio.create_task(listen2())
                compute0 = time.monotonic()
                for i in range(0, len(pcm16), step):
                    await stt2.send_audio(pcm16[i:i + step])
                    await aio.sleep(0.02)          # real time
                fed_at = time.monotonic()
                await stt2.finalize()
                try:
                    await aio.wait_for(task2, timeout=25)
                except aio.TimeoutError:
                    task2.cancel()
                await stt2.close()

                if first_at2:
                    warm_stt = (first_at2[0] - fed_at) * 1000
                if warm_stt is not None:
                    # A NEGATIVE value is meaningful, not an error to hide: the
                    # transcript arrived before we finished feeding, which means
                    # send_audio() was BLOCKING on the forward pass and the engine
                    # is running behind real time. Clamping it to 0 reported
                    # "instant" for an engine that cannot keep up -- the most
                    # flattering possible reading of the worst possible result.
                    behind = warm_stt < 0
                    if behind:
                        # The honest wait when the engine cannot keep up is the
                        # BACKLOG: compute minus audio duration. Reporting the
                        # small negative gap as "121 ms" made a 2.4x shortfall
                        # look like the best number in the table.
                        results["stt_latency"] = max(
                            (time.monotonic() - compute0 - (results.get("_audio_secs") or 0)) * 1000,
                            0.0,
                        )
                    else:
                        results["stt_latency"] = warm_stt
                    audio_secs = results.get("_audio_secs", 0) or 0
                    compute_s = time.monotonic() - compute0
                    rtf = (compute_s / audio_secs) if audio_secs else 0
                    if behind:
                        console.print(
                            f"  [red]BEHIND REAL TIME[/] — send_audio() blocked on "
                            f"the forward pass, so the engine never caught up. The "
                            f"caller waits about [bold]{results['stt_latency']:.0f} ms[/] "
                            f"of backlog after they stop talking, and that grows "
                            f"with every second they speak."
                        )
                    else:
                        console.print(
                            f"  [green]warm[/] [bold]{results['stt_latency']:.0f} ms[/] "
                            f"after end-of-speech [dim](what the caller waits)[/]"
                        )
                    console.print(
                        f"  [{'red' if rtf >= 1 else 'green'}]RTF {rtf:.2f}[/] — "
                        f"{compute_s:.1f}s of compute for {audio_secs:.1f}s of audio. "
                        + ("Above 1.0: it CANNOT stream in real time here, so "
                           "streaming buys no latency and a 10s utterance costs "
                           f"~{rtf*10:.0f}s of compute."
                           if rtf >= 1 else
                           "Under 1.0: keeps up live, so the work hides inside "
                           "the caller's speech.")
                    )
            except Exception as e:
                console.print(f"  [dim]warm STT pass skipped: {type(e).__name__}[/]")
        else:
            # Name the engine that actually ran. Saying "with whisper_local" while
            # nemotron was configured sent me looking in the wrong file.
            engine = describe_stack()["stt"].split()[0]
            console.print(
                f"  [yellow]no transcript[/] from [bold]{engine}[/] — the audio may "
                "have been too short or too quiet, or the engine errored above. "
                "Scroll up for a traceback before assuming it was the audio."
            )

    _run(go())

    # ---- verdict -----------------------------------------------------------
    if len(results) >= 2:
        est = (
            results.get("stt_latency", 0)
            + results.get("llm_first_chunk", 0)
            + results.get("tts_first_byte", 0)
        )
        t = Table("stage", "latency")
        for k, label in (
            ("stt_latency", "STT (speech → text)"),
            ("llm_first_chunk", "LLM (first clause)"),
            ("tts_first_byte", "TTS (first byte)"),
        ):
            if k in results:
                t.add_row(label, f"{results[k]:.0f} ms")
        t.add_row("[bold]estimated time-to-first-word[/]", f"[bold]{est:.0f} ms[/]")
        console.print("\n", t)

        if est < 900:
            console.print("[green]Feels natural.[/] Good to go.")
        elif est < 1500:
            console.print(
                "[yellow]Usable but noticeable.[/] Callers will sense the pause. "
                "A GPU for Whisper is the biggest single win."
            )
        else:
            # Name the stage that is actually slowest, from the numbers just
            # measured. Blaming STT unconditionally was wrong the moment STT got
            # fast, and it would have sent someone optimising the wrong thing.
            worst, ms = max(
                (("STT", results.get("stt_latency", 0.0)),
                 ("LLM", results.get("llm_first_chunk", 0.0)),
                 ("TTS", results.get("tts_first_byte", 0.0))),
                key=lambda kv: kv[1],
            )
            advice = {
                "STT": "Check the RTF line above. Under 1.0 means a streaming engine "
                       "keeps up live, so most of what remains is the endpointing "
                       "wait — tune STT_SILENCE_MS, not the model.",
                "LLM": "Set ANTHROPIC_MODEL=claude-haiku-4-5-20251001 and enable "
                       "prompt caching. The system prompt and tool schemas are "
                       "resent every turn and are the bulk of the input.",
                "TTS": "Piper is normally 150-250 ms. If it is the worst stage, "
                       "suspect the voice file or a sample-rate mismatch.",
            }[worst]
            console.print(
                f"[red]Too slow — callers will talk over the agent.[/]\n"
                f"Slowest stage is [bold]{worst}[/] at {ms:.0f} ms. {advice}"
            )
        console.print(
            "[dim]Note: this excludes network and jitter on a real call — add ~100 ms.[/]"
        )


@app.command()
def doctor() -> None:
    """Diagnose the media path locally. No server or network calls needed.

    Run this FIRST if calls connect but you hear silence.
    """
    from app.doctor import diagnose, render

    text, media_ok = render(diagnose())
    console.print(text)
    if media_ok:
        console.print("[green]Media path looks viable.[/]")
    else:
        console.print(
            "[yellow]Media will NOT work from this host.[/] Calls will still ring "
            "and connect — you just won't hear anything. See DEPLOY.md."
        )


@app.command("stun-test")
def stun_test(timeout: float = 4.0) -> None:
    """Can Meta reach us WITHOUT a relay? Run this before touching TURN.

    A server-reflexive candidate -- your real public IP:port, discovered via
    STUN -- is usually all that is needed. Meta is ICE-LITE and CONTROLLED, so
    we send the connectivity checks; that punches the NAT hole, and Meta replies
    to the address it sees. Media then flows straight between this machine and
    Meta, with no relay and no third party in the audio path.

    That is both simpler and better for data residency than TURN, which carries
    every packet through someone else's server. TURN is the fallback, not the
    default.
    """
    from app.turntest import nat_check

    for p in _run(nat_check(timeout)):
        mark = "OK  " if p.ok else "FAIL"
        colour = "green" if p.ok else "yellow"
        console.print(f"[{colour}][{mark}][/] {p.name:22s} {p.ms:>5d}ms  {p.detail}")


@app.command("turn-test")
def turn_test(timeout: float = 6.0) -> None:
    """Ask the TURN relay for an address, one transport at a time.

    A failed TURN allocation is completely silent: aiortc raises nothing, logs
    nothing, and simply gathers no relay candidate. You find out when a real call
    rings, gets answered, and then stays mute while ICE sits in "checking".

    This asks the same question in seconds. It also tests each transport
    separately, because that is usually where the difference is -- UDP to odd
    ports is routinely dropped by home routers and mobile networks, while TCP/443
    is indistinguishable from HTTPS and almost always passes.
    """
    from app.turntest import render, run

    results, winner, health, source = _run(run(timeout))
    console.print(render(results, winner, health, source))


@app.command()
def preflight() -> None:
    """Check everything that must be true before calling can work."""
    try:
        r = httpx.get(f"{SERVER}/api/preflight", timeout=30)
        data = r.json()
    except httpx.ConnectError:
        console.print("[red]Server not running.[/] Start it with:")
        console.print("  uvicorn app.main:app --reload --port 8000")
        raise typer.Exit(1)

    warnings = data.pop("warnings", [])
    _show("preflight", data)
    if warnings:
        for w in warnings:
            console.print(f"[yellow]![/] {w}")
    else:
        console.print("[green]No blockers found.[/]")


@app.command("enable-calling")
def enable_calling() -> None:
    """Turn on calling + callback permission for the number."""
    async def go():
        g = GraphClient()
        try:
            return await g.enable_calling(callback_permission=True)
        finally:
            await g.close()

    try:
        _show("enable-calling", _run(go()))
    except GraphError as e:
        _show("error", e.payload)
        raise typer.Exit(1)


@app.command()
def permission(user_wa_id: str) -> None:
    """Show permission status and remaining quota for one user."""
    async def go():
        g = GraphClient()
        try:
            return await g.get_call_permission(user_wa_id)
        finally:
            await g.close()

    try:
        data = _run(go())
    except GraphError as e:
        _show("error", e.payload)
        raise typer.Exit(1)

    perm = data.get("permission", {})
    st = perm.get("status")
    if st == "temporary":
        exp = int(perm.get("expiration_time", 0))
        left = exp - int(time.time())
        detail = (
            f"expires: {datetime.fromtimestamp(exp):%Y-%m-%d %H:%M:%S}"
            f"  ({left // 86400}d {left % 86400 // 3600}h left)"
        )
    elif st == "permanent":
        detail = "expires: never (until the user revokes it)"
    else:
        detail = "You cannot call this user yet. See the hints below."
    console.print(
        Panel(
            f"status: [bold]{st}[/]\n{detail}",
            title=f"permission for {user_wa_id}",
            border_style="green" if st != "no_permission" else "red",
        )
    )
    if st == "no_permission":
        console.print(
            "[yellow]How to get 7-day temporary permission:[/]\n"
            f"  A. From {user_wa_id}, place a WhatsApp CALL to your business number\n"
            "     (callback_permission is ENABLED, so this auto-grants temporary)\n"
            f"  B. Or: send it any message, then  python cli.py request-temporary {user_wa_id}\n"
            "     and tap 'Temporarily allow calls' on the handset"
        )

    t = Table("action", "allowed now", "window", "used / max")
    for a in data.get("actions", []):
        for lim in a.get("limits", []) or [{}]:
            t.add_row(
                a.get("action_name", ""),
                "yes" if a.get("can_perform_action") else "NO",
                lim.get("time_period", ""),
                f"{lim.get('current_usage', '?')} / {lim.get('max_allowed', '?')}",
            )
    console.print(t)


@app.command("request-permission")
def request_permission(
    to: str,
    text: str = typer.Option(
        "May we call you to confirm your pickup window?", "--text"
    ),
) -> None:
    """Send a free-form permission request (needs an open 24h window)."""
    r = httpx.post(
        f"{SERVER}/api/permission/request", json={"to": to, "body_text": text}, timeout=30
    )
    _show(f"request-permission ({r.status_code})", r.json())


@app.command("subscribed-apps")
def subscribed_apps() -> None:
    """Which Meta apps receive webhooks for this WABA? RUN BEFORE editing a webhook.

    A live third-party chatbot on your number means someone else is already
    consuming these webhooks. This tells you whether they are on a different app
    (safe) or the same app you are about to edit (dangerous).
    """
    async def go():
        g = GraphClient()
        try:
            return await g.subscribed_apps()
        finally:
            await g.close()

    try:
        data = _run(go())
    except GraphError as e:
        _show("error", e.payload)
        raise typer.Exit(1)

    apps = data.get("data", [])
    OUR_APP = "3405368092946688"

    t = Table("app name", "app id", "is this our lab app?")
    for a in apps:
        info = a.get("whatsapp_business_api_data", a)
        aid = str(info.get("id", "?"))
        t.add_row(
            info.get("name", "?"),
            aid,
            "[bold yellow]YES[/]" if aid == OUR_APP else "no — someone else's",
        )
    console.print(t)

    others = [
        a for a in apps
        if str(a.get("whatsapp_business_api_data", a).get("id")) != OUR_APP
    ]
    ours = [
        a for a in apps
        if str(a.get("whatsapp_business_api_data", a).get("id")) == OUR_APP
    ]

    if others and ours:
        console.print(
            "\n[green]SAFE.[/] Your app and the vendor's app are BOTH subscribed, "
            "separately.\nEach app has its own Callback URL, so setting yours does "
            "not touch theirs."
        )
    elif others and not ours:
        console.print(
            "\n[yellow]Your app is NOT subscribed yet.[/] The vendor's app is.\n"
            "Adding yours is additive and safe — but make sure you edit the "
            "Callback URL on\n[bold]TruKKer Partner[/] only, never on their app."
        )
    elif ours and not others:
        console.print(
            "\n[yellow]Only your app is subscribed to this WABA.[/]\n"
            "But a live chatbot IS replying on the number, so the vendor is "
            "probably reaching\nit another way — a different WABA, or as a "
            "Solution Partner. Confirm with them\nbefore changing the Callback "
            "URL on this app."
        )
    else:
        console.print("\n[red]No apps subscribed.[/] Webhooks will not be delivered.")


@app.command()
def window(user_wa_id: str) -> None:
    """Is the 24h window open? Decides whether we need a paid template.

    Open   -> free-form permission request, no template needed.
    Closed -> requires an approved call_permission_request template, billed per send.
    """
    try:
        d = httpx.get(f"{SERVER}/window/{user_wa_id}", timeout=15).json()
    except httpx.ConnectError:
        console.print("[red]Server not running.[/] uvicorn app.main:app --port 8000")
        raise typer.Exit(1)

    state = d.get("state")
    if state == "open":
        console.print(
            Panel(
                f"[bold green]OPEN[/] — {d['remaining_human']} left\n\n"
                "Free-form permission request is allowed.\n"
                "[dim]No template needed.[/]",
                title=f"24h window for {user_wa_id}",
                border_style="green",
            )
        )
    elif state == "closed":
        console.print(
            Panel(
                "[bold red]CLOSED[/]\n\n"
                "Free-form will be rejected. Either:\n"
                "  1. message the business number again from that handset, or\n"
                "  2. create + get approval for a call_permission_request template\n"
                "     (billed per send, whether or not they accept)",
                title=f"24h window for {user_wa_id}",
                border_style="red",
            )
        )
    else:
        console.print(
            Panel(
                f"[bold yellow]UNKNOWN[/]\n\n{d.get('detail','')}\n\n{d.get('hint','')}",
                title=f"24h window for {user_wa_id}",
                border_style="yellow",
            )
        )


@app.command("request-temporary")
def request_temporary(
    to: str,
    text: str = typer.Option(
        "We would like to call you to confirm your pickup window.", "--text"
    ),
    timeout: int = typer.Option(180, "--timeout", help="seconds to wait for your tap"),
) -> None:
    """Request 7-day TEMPORARY call permission, and wait for you to accept.

    Important: temporary vs permanent is the USER'S choice, not ours. The API has
    no flag for it. Both options appear on the same prompt, so you must tap
    'Temporarily allow calls' rather than 'Allow'.

    This uses the FREE-FORM request, which needs an open 24-hour customer service
    window -- so message the business number from the handset first. That path
    avoids creating and paying for a template.
    """
    async def send():
        g = GraphClient()
        try:
            return await g.request_permission_freeform(to, text)
        finally:
            await g.close()

    async def check():
        g = GraphClient()
        try:
            return await g.get_call_permission(to)
        finally:
            await g.close()

    # Where do we start from?
    try:
        before = _run(check())
    except GraphError as e:
        _show("error reading permission", e.payload)
        raise typer.Exit(1)

    cur = (before.get("permission") or {}).get("status")
    if cur == "temporary":
        exp = (before.get("permission") or {}).get("expiration_time")
        console.print(f"[green]Already have temporary permission[/] (expires {exp}).")
        console.print("Nothing to do — go straight to the call.")
        return
    if cur == "permanent":
        console.print(
            "[yellow]This user already granted PERMANENT permission.[/]\n"
            "There is no API to downgrade it. To test the 7-day path you would have\n"
            "to revoke it on the handset first: chat → tap the business number →\n"
            "Business Calling Permission → turn it off. Then re-run this."
        )
        raise typer.Exit(1)

    # Check we are allowed to ask at all (1/day, 2/week).
    for a in before.get("actions", []):
        if a.get("action_name") == "send_call_permission_request":
            if a.get("can_perform_action") is False:
                console.print(
                    "[red]Permission-request rate limit reached.[/] "
                    "Max 1 per 24h and 2 per 7 days per user."
                )
                for lim in a.get("limits", []) or []:
                    console.print(
                        f"  {lim.get('current_usage')}/{lim.get('max_allowed')} "
                        f"used in {lim.get('time_period')}"
                    )
                raise typer.Exit(1)

    console.print(f"Sending free-form permission request to [bold]{to}[/]…")
    try:
        res = _run(send())
        console.print(f"[green]sent[/] — message id {res['messages'][0]['id']}")
    except GraphError as e:
        msg = str(e.payload)
        _show("send failed", e.payload)
        if "24" in msg or "re-engag" in msg.lower() or e.code in (131047, 470):
            console.print(
                "\n[yellow]This looks like a closed customer service window.[/]\n"
                "A free-form request only works if the user messaged you in the last\n"
                "24 hours. Fix: send any WhatsApp message from the handset to your\n"
                "business number, then re-run this immediately.\n\n"
                "The alternative is a call_permission_request TEMPLATE, which works\n"
                "any time but is billed per send — marketing or utility rate."
            )
        raise typer.Exit(1)

    console.print(
        Panel(
            "On [bold]" + to + "[/] a prompt will appear in the chat.\n\n"
            "Tap [bold yellow]'Temporarily allow calls'[/]  ← 7 days\n"
            "[dim]NOT 'Allow calls', which is permanent[/]",
            title="do this on the handset",
            border_style="yellow",
        )
    )

    with console.status("waiting for you to tap…", spinner="dots"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                now = _run(check())
            except GraphError:
                continue
            st = (now.get("permission") or {}).get("status")
            if st in ("temporary", "permanent"):
                break
        else:
            console.print(
                "[red]Timed out.[/] No response recorded. The request stays valid for\n"
                "7 days, so you can accept later and just re-check with:\n"
                f"  python cli.py permission {to}"
            )
            raise typer.Exit(1)

    perm = now.get("permission") or {}
    st = perm.get("status")
    if st == "permanent":
        console.print(
            "[yellow]You tapped 'Allow' (permanent), not 'Temporarily allow'.[/]\n"
            "It will work for calling, but it is not the 7-day behaviour you wanted.\n"
            "To retest: revoke on the handset, then run this again."
        )
        return

    exp = int(perm.get("expiration_time", 0))
    left = exp - int(time.time())
    console.print(
        Panel(
            f"status: [bold green]temporary[/]\n"
            f"expires: {datetime.fromtimestamp(exp):%Y-%m-%d %H:%M:%S} "
            f"({left // 86400}d {left % 86400 // 3600}h from now)",
            title="7-day permission granted",
            border_style="green",
        )
    )
    console.print(
        "[dim]Note: this clock runs from your approval and does NOT extend when you\n"
        "chat. No webhook fires when it expires — track expiration_time yourself.[/]"
    )
    console.print(f"\nNow ring the phone:\n  [bold]python ringtest.py {to}[/]")


@app.command()
def call(to: str, skip_check: bool = typer.Option(False, "--skip-check")) -> None:
    """Place a call and hand it to the AI agent."""
    r = httpx.post(
        f"{SERVER}/api/call",
        json={"to": to, "skip_permission_check": skip_check},
        timeout=60,
    )
    _show(f"call ({r.status_code})", r.json())
    if r.status_code == 200:
        console.print("[dim]Watch the server logs for ICE / DTLS / ACCEPTED.[/]")


@app.command()
def hangup(call_id: str) -> None:
    """Terminate an active call."""
    r = httpx.post(f"{SERVER}/hangup/{call_id}", timeout=30)
    _show("hangup", r.json())


@app.command()
def active() -> None:
    """List calls currently in progress."""
    r = httpx.get(f"{SERVER}/calls", timeout=15)
    calls = r.json().get("active", [])
    if not calls:
        console.print("[dim]No active calls.[/]")
        return
    t = Table("call_id", "to", "accepted", "dur", "pc", "ice", "speaking")
    for c in calls:
        t.add_row(
            (c["call_id"] or "")[:26],
            c["to"],
            str(c["accepted"]),
            f"{c['duration_s']}s",
            str(c["pc_state"]),
            str(c["ice_state"]),
            str(c["speaking"]),
        )
    console.print(t)


@app.command()
def events(n: int = 10) -> None:
    """Show the last N webhooks Meta sent."""
    r = httpx.get(f"{SERVER}/events", timeout=15)
    for e in r.json().get("events", [])[-n:]:
        console.print(Panel(json.dumps(e, indent=2), border_style="dim"))


if __name__ == "__main__":
    app()
