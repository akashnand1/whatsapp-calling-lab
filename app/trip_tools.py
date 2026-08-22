"""Tools the agent uses to record trip milestones during a call.

Three tools, deliberately few. A voice agent under time pressure should not be
choosing between a dozen options, and every extra tool is another chance to pick
the wrong one mid-conversation.

    set_current_stage   the driver said where he is now
    record_milestone    a timestamp (and optionally a document) was confirmed
    get_missing         what still needs asking, in order

`get_missing` matters more than it looks. Without it the model has to track nine
milestones across a noisy conversation from memory, and it will drift -- asking
twice, or skipping one. Letting it query authoritative state keeps the interview
on rails.
"""

from __future__ import annotations

import logging

from .milestones import BY_CODE, LADDER, TripState

log = logging.getLogger("trip")

_CODES = [m.code for m in LADDER]


TOOLS: list[dict] = [
    {
        "name": "set_current_stage",
        "description": (
            "Record where the driver says he is RIGHT NOW. Call this as soon as "
            "he indicates his position, even loosely ('destination border pe "
            "khada hoon'). Because milestones are sequential, this establishes "
            "that every earlier milestone has already happened."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "milestone": {
                    "type": "string",
                    "enum": _CODES,
                    "description": "The milestone matching where the driver is now.",
                }
            },
            "required": ["milestone"],
        },
    },
    {
        "name": "record_milestones",
        "description": (
            "Record the times the driver gave. Put EVERY milestone he just "
            "mentioned into a single call with several entries -- one drop of "
            "five milestones is one call, not five. Emitting five separate tool "
            "calls means writing five copies of this whole argument block, which "
            "on a real call took seventeen seconds of the driver's silence to "
            "generate. Say your next question first, then record."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
              "entries": {
                "type": "array",
                "description": "One object per milestone the driver just gave.",
                "items": {
                  "type": "object",
                  "properties": {
                    "milestone": {"type": "string", "enum": _CODES},
                    "time_reported": {
                    "type": "string",
                    "description": (
                        "The time exactly as the driver expressed it, e.g. "
                        "'parson raat 8 baje'. Keep his words verbatim."
                    ),
                },
                "time_iso": {
                    "type": "string",
                    "description": (
                        "The SAME time resolved to 'YYYY-MM-DD HH:MM' in 24-hour "
                        "form, using TODAY'S DATE given in your instructions. "
                        "Rules: subah=morning (05:00-11:59); dopahar=12:00-16:59; "
                        "shaam=17:00-19:59; raat=20:00-23:59 OR 00:00-04:00. "
                        "'raat 12 baje' means MIDNIGHT = 00:00 of the NEXT day. "
                        "'dopahar 12 baje' means 12:00. kal=yesterday, "
                        "parson=day before yesterday. "
                        "RELATIVE times: drivers often say 'phir 1 ghante mein "
                        "nikal gaya' or 'uske turant baad' instead of a clock "
                        "time. Add that offset to the PREVIOUS milestone's time "
                        "and put the computed result here. E.g. loading finished "
                        "23:00 + 'ek ghante mein nikla' = 00:00 the next day. "
                        "If the driver truly gave no usable time, omit this field "
                        "rather than inventing one."
                    ),
                },
                    "document_status": {
                      "type": "string",
                      "description": (
                        "Optional. What he said about the document for this "
                        "milestone, e.g. 'bhej diya', 'abhi nahi bheja'. To add "
                        "ONLY a document status to a milestone whose time you "
                        "already recorded, send that milestone with just this "
                        "field -- do not resend a placeholder time like "
                        "'confirmed', and never resend a time without its "
                        "time_iso."
                      ),
                    },
                  },
                  "required": ["milestone", "time_reported"],
                },
              },
            },
            "required": ["entries"],
        },
    },
    {
        "name": "get_missing",
        "description": (
            "List the milestones that must have already happened but whose time "
            "you have not yet recorded, plus any documents still outstanding. "
            "Call this before asking your next question, and again before the "
            "final summary, so you neither repeat a question nor miss one."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "give_up_on",
        "description": (
            "Mark one milestone as not obtainable after two failed attempts to "
            "hear the answer. Call this INSTEAD of asking a third time. Asking "
            "the same question repeatedly is the single largest waste of call "
            "time and the driver finds it insulting. Once marked, the item stops "
            "appearing in get_missing, and you mention it as unconfirmed in the "
            "final summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "milestone": {"type": "string", "enum": _CODES},
            },
            "required": ["milestone"],
        },
    },
    {
        "name": "end_call",
        "description": (
            "End the call. Call this in the SAME reply as your closing line, "
            "once the driver has confirmed the read-back or has clearly finished "
            "talking. The line stays open for a few more seconds so he can add "
            "one last thing, and hangs up by itself after that. Never finish a "
            "call by simply going quiet: the driver is left holding a live line, "
            "which sounds like a fault and is billed by the minute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Short note, e.g. 'all milestones confirmed'.",
                },
            },
        },
    },
]


def make_handlers(state: TripState) -> dict:
    """Bind the tools to one call's state."""

    def set_current_stage(args: dict) -> str:
        code = args.get("milestone", "")
        if code not in BY_CODE:
            return f"unknown milestone '{code}'"
        state.set_stage(code)
        missing = state.missing()
        log.info("stage set to %s; %d milestone(s) missing", code, len(missing))
        if not missing:
            return f"Stage set to {code}. Nothing missing — you can summarise."
        lines = "\n".join(f"- {m.code}: ask \"{m.ask_hi}\"" for m in missing)
        # Wording matters more here than in the system prompt. This text arrives
        # as a tool result immediately before the model speaks, so it outweighs
        # an instruction given thousands of tokens earlier. An earlier version
        # said "ask them one at a time", which is exactly what the agent then
        # did -- 30 turns for 9 milestones -- while the system prompt was asking
        # it to group. Two sources of truth, and the nearer one won.
        return (
            f"Stage set to {code}. These earlier milestones still need a time. "
            f"Ask them in GROUPS of two or three in one question, with the "
            f"matching document question folded into the same turn. Do not ask "
            f"one at a time, and do not confirm each answer separately — there "
            f"is a single confirmation at the end:\n{lines}"
        )

    def record_milestones(args: dict) -> str:
        """Record any number of milestones from one tool call."""
        entries = args.get("entries")
        if entries is None:
            # Tolerate the single-entry shape. The model occasionally reverts to
            # the old flat form, and a hard failure there costs a whole turn.
            entries = [args] if args.get("milestone") else []
        if not entries:
            return "entries was empty — nothing recorded."
        notes = [_record_one(e) for e in entries]

        remaining = state.missing()
        if remaining:
            notes.append(
                "Still missing: "
                + ", ".join(f"{m.code} (\"{m.ask_hi}\")" for m in remaining[:4])
                + ". Ask for these in ONE grouped question."
            )
        else:
            notes.append("All required milestones collected — read the summary back.")
        return " ".join(notes)

    def _record_one(args: dict) -> str:
        code = args.get("milestone", "")
        when = (args.get("time_reported") or "").strip()
        iso = (args.get("time_iso") or "").strip() or None
        doc = (args.get("document_status") or "").strip() or None
        if not when:
            # A document-only update is legitimate: he already gave the time and
            # is now confirming the paperwork. Only reject it if we have nothing.
            if doc and code in BY_CODE and code in state.records:
                state.record(code, "", None, doc)
                log.info("recorded document for %s: %r", code, doc)
                return f"Document recorded for {code}: {doc}."
            return "time_reported was empty — ask the driver again."
        if not state.record(code, when, iso, doc):
            return f"unknown milestone '{code}'"
        log.info("recorded %s = %r iso=%r doc=%r", code, when, iso, doc)

        m = BY_CODE[code]
        parts = [f"Recorded {code} at '{when}'."]
        if m.needs_document and not doc:
            parts.append(f"Document still needed: {m.document_hi}.")
        return " ".join(parts)

    def get_missing(_args: dict) -> str:
        if state.current_stage is None:
            return (
                "The driver has not said where he is yet. Ask where he is right "
                "now, then call set_current_stage."
            )
        missing = state.missing()
        docs = state.missing_documents()

        # Pace advice, computed from the clock rather than left to the model's
        # judgement. The first production-shaped call took 688 seconds because
        # nothing in the loop knew that was too long. The agent calls this tool
        # before every question, so this is the natural place to say so.
        el = state.elapsed_s
        budget = 240
        remaining_items = len(missing) + len(docs)
        if el > budget:
            pace = (
                f"\n\nPACE: {el}s elapsed, OVER the {budget}s budget. Ask ALL "
                f"{remaining_items} remaining item(s) in ONE question now, then "
                "go straight to the summary. Do not confirm items individually."
            )
        elif el > budget * 0.6:
            pace = (
                f"\n\nPACE: {el}s elapsed of {budget}s. Running long — group the "
                "remaining items into a single question and skip any "
                "confirmation until the final summary."
            )
        else:
            pace = f"\n\nPACE: {el}s elapsed of {budget}s. On track."

        if not missing and not docs:
            return (
                "Nothing missing. Read the summary back ONCE for confirmation, "
                "then end the call." + pace
            )
        out = []
        if missing:
            out.append(
                "Times still needed — ask these TOGETHER in one question:\n"
                + "\n".join(f"- {m.code}: \"{m.ask_hi}\"" for m in missing)
            )
        if docs:
            out.append(
                "Documents still outstanding — fold these into the SAME question, "
                "never a separate turn:\n"
                + "\n".join(f"- {m.code}: {m.document_hi}" for m in docs)
            )
        if state.uncertain:
            out.append(
                "Given up on (mention as uncertain in the summary, do NOT ask "
                "again): " + ", ".join(sorted(state.uncertain))
            )
        return "\n".join(out) + pace

    def give_up_on(args: dict) -> str:
        code = args.get("milestone", "")
        if code not in BY_CODE:
            return f"unknown milestone '{code}'"
        state.uncertain.add(code)
        log.info("gave up on %s after repeated mishearing", code)
        return (
            f"Marked {code} as unconfirmed. Move on to the next question now. "
            "Mention it in the summary as something to be sent by message later."
        )

    def end_call(args: dict) -> str:
        state.finished = True
        log.info("agent ended the conversation: %s",
                 args.get("reason") or "no reason given")
        return (
            "Noted. Say your closing line now and then stop talking — the line "
            "closes on its own shortly after."
        )

    return {
        "set_current_stage": set_current_stage,
        "record_milestones": record_milestones,
        "get_missing": get_missing,
        "give_up_on": give_up_on,
        "end_call": end_call,
    }
