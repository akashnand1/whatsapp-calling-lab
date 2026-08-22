"""Trip milestone ladder, and the state collected during a call.

The nine milestones are strictly sequential, which is what makes the whole
interaction tractable: if a driver says he is at the destination border, then
everything up to that point has already happened, and the agent's job is to
recover the timestamps he never reported.

The hard part is not the ladder -- it is the vocabulary. Drivers do not know
these names; they are internal. "Enroute to Destination Border" is not a thing
anyone says. What a driver understands is "origin border se kab nikle". So every
milestone carries the phrasing a human would actually use, and the internal name
is never spoken aloud.

Two mappings are especially easy to get wrong, and both were called out
explicitly:

    "origin border kab clear kiya"       -> ENROUTE_TO_DESTINATION_BORDER (5)
    "destination border kab clear kiya"  -> ENROUTE_TO_DESTINATION (7)

Clearing a border is *leaving* it, so it is the departure milestone, not the
arrival one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Milestone:
    order: int
    code: str
    internal_name: str          # for the TMS. Never spoken.
    ask_hi: str                 # what the agent actually says
    ask_en: str
    label_hi: str = ""          # statement form, for the confirmation read-back
    document_hi: str = ""       # document to collect, if any
    document_en: str = ""

    @property
    def needs_document(self) -> bool:
        return bool(self.document_hi or self.document_en)


LADDER: tuple[Milestone, ...] = (
    Milestone(
        1, "reached_loading_point", "Reached At Loading Point",
        ask_hi="लोडिंग पॉइंट पर कब पहुँचे थे?",
        ask_en="When did you reach the loading point?",
        label_hi="लोडिंग पॉइंट पर पहुँचे",
    ),
    Milestone(
        2, "loading_completed", "Loading Completed",
        ask_hi="लोडिंग कब पूरी हुई?",
        ask_en="When was loading completed?",
        label_hi="लोडिंग पूरी हुई",
        document_hi="लोडिंग की रसीद या बिल्टी",
        document_en="loading receipt or bilty",
    ),
    Milestone(
        3, "enroute_to_origin_border", "Enroute to Origin Border",
        # The departure event. Drivers say "nikla", not "enroute".
        ask_hi="लोडिंग पॉइंट से कब निकले थे?",
        ask_en="When did you leave the loading point?",
        label_hi="लोडिंग पॉइंट से निकले",
    ),
    Milestone(
        4, "at_origin_border", "At Origin Border",
        ask_hi="ओरिजिन बॉर्डर पर कब पहुँचे थे?",
        ask_en="When did you reach the origin border?",
        label_hi="ओरिजिन बॉर्डर पर पहुँचे",
    ),
    Milestone(
        5, "enroute_to_destination_border", "Enroute to Destination Border",
        # "origin border clear kiya" == left the origin border.
        ask_hi="ओरिजिन बॉर्डर कब क्लियर हुआ, यानी वहाँ से कब निकले?",
        ask_en="When did you clear the origin border, that is, when did you leave it?",
        label_hi="ओरिजिन बॉर्डर क्लियर किया",
        document_hi="ओरिजिन बॉर्डर के कस्टम्स कागज़",
        document_en="origin border customs paperwork",
    ),
    Milestone(
        6, "at_destination_border", "At Destination Border",
        ask_hi="डेस्टिनेशन बॉर्डर पर कब पहुँचे?",
        ask_en="When did you reach the destination border?",
        label_hi="डेस्टिनेशन बॉर्डर पर पहुँचे",
    ),
    Milestone(
        7, "enroute_to_destination", "Enroute to Destination",
        # "destination border clear kiya" == left the destination border.
        ask_hi="डेस्टिनेशन बॉर्डर कब क्लियर हुआ, यानी वहाँ से कब निकले?",
        ask_en="When did you clear the destination border, that is, when did you leave it?",
        label_hi="डेस्टिनेशन बॉर्डर क्लियर किया",
        document_hi="डेस्टिनेशन बॉर्डर के कस्टम्स कागज़",
        document_en="destination border customs paperwork",
    ),
    Milestone(
        8, "at_unloading_point", "At Unloading Point",
        ask_hi="अनलोडिंग पॉइंट पर कब पहुँचे?",
        ask_en="When did you reach the unloading point?",
        label_hi="अनलोडिंग पॉइंट पर पहुँचे",
    ),
    Milestone(
        9, "completed", "Completed",
        ask_hi="अनलोडिंग कब पूरी हुई?",
        ask_en="When was unloading completed?",
        label_hi="अनलोडिंग पूरी हुई",
        document_hi="पीओडी यानी डिलीवरी की रसीद",
        document_en="POD, the proof of delivery",
    ),
)

BY_CODE = {m.code: m for m in LADDER}
BY_ORDER = {m.order: m for m in LADDER}


# Phrases a driver might use, mapped to the milestone they imply. Used to seed
# the prompt with examples rather than to parse -- the model does the matching,
# but it needs to see how loosely people speak.
COLLOQUIAL_HI: dict[str, str] = {
    "लोडिंग पॉइंट पर पहुँच गया": "reached_loading_point",
    "गाड़ी लग गई": "reached_loading_point",
    "माल भर गया / लोडिंग हो गई": "loading_completed",
    "लोड लेकर निकल गया": "enroute_to_origin_border",
    "बॉर्डर पर पहुँच गया": "at_origin_border",
    "ओरिजिन बॉर्डर क्लियर हो गया": "enroute_to_destination_border",
    "बॉर्डर से निकल गया": "enroute_to_destination_border",
    "डेस्टिनेशन बॉर्डर पहुँच गया": "at_destination_border",
    "डेस्टिनेशन बॉर्डर क्लियर हो गया": "enroute_to_destination",
    "अनलोडिंग पॉइंट पहुँच गया": "at_unloading_point",
    "माल उतर गया / अनलोडिंग हो गई": "completed",
}


@dataclass
class MilestoneRecord:
    code: str
    time_text: str                     # exactly as the driver said it
    time_iso: Optional[str] = None     # resolved "YYYY-MM-DD HH:MM", 24-hour
    document_status: Optional[str] = None

    @property
    def display(self) -> str:
        """24-hour time if we resolved it, with the driver's words for context."""
        if self.time_iso:
            return f"{self.time_iso} ({self.time_text})"
        return self.time_text


@dataclass
class TripState:
    """What the agent has collected so far on this call."""

    records: dict[str, MilestoneRecord] = field(default_factory=dict)
    current_stage: Optional[str] = None      # where the driver says he is now

    # Wall-clock start, so the agent can be told how long it has been talking.
    # A prompt rule saying "keep the call short" is a hope; a number returned
    # from a tool it already calls every turn is a feedback loop. The first real
    # call ran 688s for what a human dispatcher does in three minutes, and the
    # agent had no way to know it was running long.
    started_at: float = field(default_factory=time.monotonic)

    # Items the agent gave up on after repeated mishearing. Recorded so the
    # read-back can flag them instead of silently inventing a value.
    uncertain: set[str] = field(default_factory=set)

    # Set by the end_call tool when the agent has finished the conversation.
    # Without it the line stayed open after the goodbye: on the last test call
    # the driver said thank you, waited, and hung up 31 seconds later -- all of it
    # billed, and it reads as the agent having frozen rather than finished.
    finished: bool = False

    @property
    def elapsed_s(self) -> int:
        return int(time.monotonic() - self.started_at)

    # -- updates ------------------------------------------------------------

    def set_stage(self, code: str) -> None:
        if code in BY_CODE:
            self.current_stage = code

    def record(
        self,
        code: str,
        time_text: str,
        time_iso: str | None = None,
        document_status: str | None = None,
    ) -> bool:
        if code not in BY_CODE:
            return False
        self.records[code] = MilestoneRecord(code, time_text, time_iso, document_status)
        # Reaching a milestone implies being at least that far along.
        if (
            self.current_stage is None
            or BY_CODE[code].order > BY_CODE[self.current_stage].order
        ):
            self.current_stage = code
        return True

    # -- queries ------------------------------------------------------------

    def missing(self) -> list[Milestone]:
        """Milestones that must have happened but have no timestamp yet.

        Everything up to and including the driver's current stage is in the past
        by definition -- that is the whole point of a sequential ladder. Anything
        beyond it has not happened and must NOT be asked about.
        """
        if self.current_stage is None:
            return []
        upto = BY_CODE[self.current_stage].order
        # Abandoned items are excluded, otherwise get_missing keeps offering
        # them back and the agent asks a fourth and fifth time.
        return [
            m for m in LADDER
            if m.order <= upto
            and m.code not in self.records
            and m.code not in self.uncertain
        ]

    def missing_documents(self) -> list[Milestone]:
        return [
            m for m in LADDER
            if m.needs_document
            and m.code in self.records
            and not self.records[m.code].document_status
            and m.code not in self.uncertain
        ]

    def is_complete(self) -> bool:
        return not self.missing()

    # -- output -------------------------------------------------------------

    def summary(self, lang: str = "hi") -> list[str]:
        """Ordered, human-readable lines for the confirmation read-back."""
        out: list[str] = []
        for m in LADDER:
            r = self.records.get(m.code)
            if not r:
                continue
            # Use the statement label, not the question. Mangling the question
            # with string replacement left read-backs like "... कब निकले? — कल
            # रात 11 बजे", which reads as an unanswered question.
            label = (m.label_hi or m.internal_name) if lang == "hi" else m.internal_name
            doc = f" [{r.document_status}]" if r.document_status else ""
            out.append(f"{m.order}. {label} — {r.display}{doc}")
        return out

    def to_dict(self) -> dict:
        """Structured payload, ready to post into a TMS."""
        return {
            "current_stage": self.current_stage,
            "milestones": [
                {
                    "order": BY_CODE[c].order,
                    "code": c,
                    "internal_name": BY_CODE[c].internal_name,
                    "time_reported": r.time_text,
                    "time_iso": r.time_iso,
                    "document_status": r.document_status,
                }
                for c, r in sorted(
                    self.records.items(), key=lambda kv: BY_CODE[kv[0]].order
                )
            ],
            "still_missing": [m.code for m in self.missing()],
        }


# ---------------------------------------------------------------------------
# Prompt fragment, generated from the ladder so there is ONE source of truth.
# ---------------------------------------------------------------------------

def prompt_fragment(lang: str = "hi") -> str:
    if lang != "hi":
        rows = "\n".join(
            f"  {m.order}. {m.code} — ask: \"{m.ask_en}\""
            + (f" | document: {m.document_en}" if m.needs_document else "")
            for m in LADDER
        )
        return (
            "TRIP MILESTONES (sequential). Never say the internal names aloud.\n"
            f"{rows}\n"
        )

    rows = "\n".join(
        f"  {m.order}. {m.code} — पूछें: \"{m.ask_hi}\""
        + (f" | दस्तावेज़: {m.document_hi}" if m.needs_document else "")
        for m in LADDER
    )
    colloquial = "\n".join(
        f"  \"{phrase}\" → {code}" for phrase, code in COLLOQUIAL_HI.items()
    )
    return f"""
ट्रिप के नौ पड़ाव (क्रम से)। ये अंदरूनी नाम कभी बोलकर न बताएं:
{rows}

ड्राइवर आम बोलचाल में ऐसे कहते हैं:
{colloquial}

ज़रूरी नियम:
- ये पड़ाव क्रम से होते हैं। अगर ड्राइवर कहे कि वह छठे पड़ाव पर है, तो पहले से
  छठे तक सब हो चुके हैं — उन सबका समय पूछें जो अभी तक दर्ज नहीं है।
- जो पड़ाव अभी आए ही नहीं, उनके बारे में कभी न पूछें।
- "ओरिजिन बॉर्डर क्लियर किया" का मतलब है वहाँ से निकल गए → पड़ाव 5।
- "डेस्टिनेशन बॉर्डर क्लियर किया" का मतलब है वहाँ से निकल गए → पड़ाव 7।
"""
