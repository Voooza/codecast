"""Turn a parsed merge request into an interview script.

Two speakers discuss someone else's change. Neither of them is its author, and
neither is allowed to state a fact about the code without saying where it is:
every turn carries a claim level (`verified` with `file:line` citations, or
`asserted`), and `vet()` downgrades any "verified" the citations do not support.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from . import diff as diffmod
from .config import settings
from .gitlab import MergeRequest
from .providers import complete_json

PERSONAS = """\
You write an audio podcast episode about one merge request. Two speakers, and
neither of them wrote the change.

HOST - a sharp, curious senior engineer presenting the show. Has read the diff
  once. Asks the questions a good reviewer asks: why this approach, what breaks,
  what is not covered, what surprised them. Never explains the change itself.
GUEST - the show's reviewer. Has read the diff closely and gone through the
  surrounding code. Explains what the change does, what it has to live with and
  where the risk is, and is visibly excited about the interesting parts. Leads
  with the punchline, then the evidence.

Who may say what about whom:
- The author is a real person and is not in the room. Speak about them in the
  third person - "the author", "they" - and never as "I" or "we". Put no words,
  motives, regrets or admissions in their mouth: no "I chose the composite
  index", no "that was expedient", no "it was an omission". The reviewer may say
  "the author went with a composite index here" or "there is no index on that
  column, and the description does not say why".
- Intent is an inference, not a fact. "The description says…", "reading the
  helper, this looks like it is aiming at…" - never "they wanted to".
- "I" is fine when it is the reviewer's own work or opinion: "I went looking for
  the caller", "I would have reached for a partial index here".

Evidence discipline - this is what makes the episode safe to post:
- Every turn carries "claim". Use "verified" when the turn's factual content is
  in the diff or in the research brief, and list the file and line in "cite"
  (for example "src/routing/BookingTable.java:88"). Use "asserted" when it is
  the reviewer's reading, a suspicion or a question - and say it that way out
  loud: "worth checking whether…", "I could not find where…". Use "none" for
  openers, reactions, wrap-ups, and turns that only ask a question - most of the
  host's turns are "none".
- A correctness claim - this breaks, this is unguarded, this is not covered -
  is "verified" only with a citation. With no citation, turn it into the
  question the reviewer would actually ask, and mark it "asserted".
- Never invent a number: no test counts, no benchmark figures, no request rates,
  no incident history, no dates, unless the diff or the brief states them. If
  you do not have the number, describe the shape instead.
- Nobody ran anything. No test suite was executed, no load test, no manual QA.
  Do not say or imply otherwise.

Style rules:
- Spoken language only. No markdown, no bullet lists, no stage directions,
  no "welcome back to the show" filler beyond one short opener.
- One to three sentences per turn, and vary the length - a two-word reaction is
  allowed. Alternate speakers; the host interrupts and pushes back.
- Energy: this is a show, not a status report. No "great question", no throat
  clearing, no summarising what was just said. Get to the interesting bit in the
  first sentence, react to what surprises you, and let the pair disagree.
- Say code the way a person says it out loud: "the resolve-user function",
  "line 42 of the config module". Never read punctuation or whole code blocks.
  The spoken citation and the "cite" field should point at the same place.
- Be concrete about this diff. No generic advice that would fit any change.
- Surface real review concerns: correctness, edge cases, error handling,
  migrations, security, tests, backwards compatibility, performance.
- Every turn that is about a specific piece of code carries that hunk anchor
  (for example "f3h2") in "ref", and the ref must be the hunk that turn is
  actually about - when the thread moves to other code, move the ref with it.
  Use null only for openers and wrap-ups.
"""

TURN_PROPERTIES: dict[str, Any] = {
    "speaker": {"type": "string", "enum": ["host", "guest"]},
    "text": {"type": "string"},
    "ref": {"type": ["string", "null"]},
    "claim": {
        "type": "string",
        "enum": ["verified", "asserted", "none"],
        "description": (
            "verified = the fact is in the diff or the brief, and cite says where; "
            "asserted = the reviewer's reading, suspicion or question; "
            "none = the turn states no fact (opener, reaction, wrap-up)."
        ),
    },
    "cite": {
        "type": "array",
        "items": {"type": "string"},
        "description": "file:line for every verified claim, e.g. 'src/db/Booking.java:88'.",
    },
}

TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": TURN_PROPERTIES,
    "required": ["speaker", "text", "claim"],
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "ref": {"type": ["string", "null"]},
                    "turns": {"type": "array", "items": TURN_SCHEMA},
                },
                "required": ["title", "turns"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "chapters"],
}

TURNS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"turns": {"type": "array", "items": TURN_SCHEMA}},
    "required": ["turns"],
}

# Author-voice slips: the guest speaking as the person who wrote the change.
# A heuristic, and deliberately a loud one - a false positive costs a reader one
# glance at a flagged line, a miss puts an invented admission in someone's MR.
AUTHOR_VOICE = re.compile(
    r"\b(?:i|we)\s+(?:\w+ly\s+)?(?:chose|picked|went\s+with|added|wrote|built|removed|deleted|"
    r"renamed|refactored|decided|skipped|forgot|left|shipped|introduced|implemented|"
    r"reused|copied|kept|split|extracted|inlined)\b"
    r"|\b(?:my|our)\s+(?:change|patch|commit|diff|mr|pr|branch|implementation|approach|migration)\b"
    r"|\bit\s+was\s+(?:an?\s+)?(?:omission|oversight|expedien\w+|laziness|shortcut)\b"
    r"|\bthat\s+was\s+expedien\w+\b",
    re.I,
)

# Nothing is executed anywhere in this pipeline, so any line that reports a run
# is fabricated by construction - "I ran 1,214 provisioning tests" is the exact
# shape of claim that reads as authoritative and is backed by nothing.
EXECUTION = re.compile(
    r"\b(?:ran|re-ran|reran|executed|benchmarked|profiled|load[- ]tested|smoke[- ]tested|"
    r"stress[- ]tested|deployed|rolled\s+out)\b(?!\s+into)"
    r"|\b(?:tests?|suite|pipeline|build|ci)\s+(?:all\s+)?(?:pass(?:ed|es)?|went\s+green|is\s+green|was\s+green)\b"
    r"|\bci\s+is\s+green\b",
    re.I,
)

# A turn that is nothing but a question states no fact, so it needs no footing -
# badging every host question "unverified" buries the ones that matter. One
# sentence, ending in a question mark: "Why a separate rule?" is a question,
# "The view takes kwargs. Is that defensive?" asserts something first.
PURE_QUESTION = re.compile(r"^[^.!?]*\?$", re.S)


def _cite_path(cite: str) -> str:
    """'src/a/B.java:88' -> 'src/a/B.java'; also takes 'src/a/B.java lines 10-20'."""
    head = str(cite).strip().split(":", 1)[0].strip()
    return head.split(" line", 1)[0].strip()


def citable(files: list[diffmod.ParsedFile], extra: Iterable[str] = ()) -> set[str]:
    """Paths a citation may point at: the diff's files, plus the brief's evidence."""
    paths = {f.path for f in files} | {f.old_path for f in files}
    paths |= {_cite_path(p) for p in extra}
    return {p for p in paths if p}


def _resolves(cite: str, known: set[str]) -> bool:
    path = _cite_path(cite)
    if not path:
        return False
    if path in known:
        return True
    # The model may shorten a path, or cite a file under a prefix the diff omits.
    return any(k.endswith("/" + path) or path.endswith("/" + k) for k in known)


def vet(turns: list[dict[str, Any]], known: set[str]) -> list[str]:
    """Normalise claim levels in place. Returns what had to be corrected.

    A "verified" turn keeps its badge only while a citation points at a file the
    diff or the brief actually covers; otherwise it drops to "asserted", so the
    transcript never shows a checkmark nothing backs.
    """
    issues: list[str] = []
    for turn in turns:
        text = str(turn.get("text", "")).strip()
        cites = [str(c).strip() for c in (turn.get("cite") or []) if str(c).strip()]
        turn["cite"] = cites
        claim = str(turn.get("claim") or "").strip().lower()
        if claim not in {"verified", "asserted"}:
            # "none", missing or nonsense: a cite means it meant to claim something.
            claim = "asserted" if cites else None
        if claim == "asserted" and not cites and PURE_QUESTION.match(text):
            claim = None
        if claim == "verified" and not any(_resolves(c, known) for c in cites):
            claim = "asserted"
            issues.append(
                f"unverified: no citation in the diff or brief backs {text[:80]!r}"
                + (f" (cited {', '.join(cites)})" if cites else "")
            )
        turn["claim"] = claim
        flags = [f for f in (turn.get("flags") or []) if f]
        if str(turn.get("speaker", "")).lower() == "guest" and AUTHOR_VOICE.search(text):
            flags.append("author-voice")
            issues.append(f"author voice: the reviewer speaks as the author in {text[:80]!r}")
        if EXECUTION.search(text):
            flags.append("claims-a-run")
            issues.append(f"nothing was executed, but this reports a run: {text[:80]!r}")
        turn["flags"] = sorted(set(flags))
    return issues


def _mr_context(mr: MergeRequest, files: list[diffmod.ParsedFile]) -> str:
    description = mr.description.strip()
    if len(description) > 4000:
        description = description[:4000] + "\n[description truncated]"
    return (
        f"Merge request: {mr.title}\n"
        f"Project: {mr.project} !{mr.iid}\n"
        f"Author (not a speaker; talk about them in the third person): {mr.author}\n"
        f"Branches: {mr.source_branch} -> {mr.target_branch}\n\n"
        f"Description:\n{description or '(none)'}\n\n"
        f"Changed files:\n{diffmod.summary(files)}\n"
    )


def context(mr: MergeRequest, files: list[diffmod.ParsedFile]) -> str:
    """The framing both the script and the research loop start from."""
    return _mr_context(mr, files)


BRIEF_FRAMING = (
    "\nResearch brief - findings from reading the repository around this change, "
    "each with the file and line it came from. This is the reviewer's own work, so "
    "they may state it as fact: cite it, mark those turns \"verified\", and use it "
    "to talk about code the diff only references:\n"
)


def build(
    mr: MergeRequest,
    files: list[diffmod.ParsedFile],
    *,
    extra_context: str = "",
    brief: str = "",
    cite_paths: Iterable[str] = (),
) -> dict[str, Any]:
    s = settings()
    user = (
        _mr_context(mr, files)
        + (f"\nAdditional context (Jira, Confluence, notes):\n{extra_context}\n" if extra_context else "")
        + (f"{BRIEF_FRAMING}{brief}\n" if brief else "")
        + "\nDiff with hunk anchors:\n"
        + diffmod.render(files, s.max_diff_chars)
        + "\n\nWrite the episode. Open with a 2-turn intro that frames what the change is "
        "and why it exists. Then one chapter per meaningful theme or file, ordered by "
        "importance, each 4 to 10 turns. Close with a chapter where the host asks what "
        "could still bite us and what the reviewer should check by hand. "
        "Target 6 to 12 minutes of speech overall."
    )
    episode = complete_json(PERSONAS, user, SCHEMA, name="episode")
    episode.setdefault("chapters", [])
    known = citable(files, cite_paths)
    lint: list[str] = []
    for chapter in episode["chapters"]:
        lint += vet(chapter.get("turns") or [], known)
    episode["lint"] = lint
    return episode


def answer(
    mr: MergeRequest,
    files: list[diffmod.ParsedFile],
    *,
    question: str,
    hunk_id: str | None,
    selection: str,
    transcript_tail: list[dict[str, Any]],
    quote: str = "",
    quoted_speaker: str = "",
    brief: str = "",
    cite_paths: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """The listener interrupts: about the diff, or about something that was said."""
    tail = "\n".join(f"{t.get('speaker', '?').upper()}: {t.get('text', '')}" for t in transcript_tail[-8:])
    focus = diffmod.hunk_text(files, hunk_id) if hunk_id else ""
    quote = quote.strip()[:600]
    if quote:
        said_by = (quoted_speaker or "someone").upper()
        challenge = (
            f"\nThe listener is questioning this line of the conversation, said by {said_by}:\n"
            f'"{quote}"\n'
            "\nContinue the episode: the host puts the listener's objection to the reviewer, "
            "referring to what was just said. The reviewer engages with it honestly - backing "
            "the claim with a citation, or conceding and correcting it if the listener is right. "
            "It is not their change, so there is nothing to defend. Do not repeat the original "
            "answer in different words. Three to six turns."
        )
    else:
        challenge = (
            "\nContinue the episode: the host relays the question in their own words "
            "(one short turn), the reviewer answers it precisely against this code, and the host "
            "closes the thread with a one-sentence takeaway or a follow-up the reviewer answers. "
            "Three to six turns."
        )
    user = (
        _mr_context(mr, files)
        + (f"\nWhat the reviewer found in the repository (citable as fact):\n{brief}\n" if brief else "")
        + f"\nConversation so far (tail):\n{tail or '(episode not started)'}\n"
        + (f"\nThe listener highlighted this code ({hunk_id}):\n{focus}\n" if focus else "")
        + (f"\nExact highlighted lines:\n{selection}\n" if selection.strip() else "")
        + f"\nThe listener asks: {question}\n"
        + challenge
        + " If the answer is not knowable from the diff or the brief, say so plainly, mark the "
        "turn asserted and name what to check by hand."
    )
    payload = complete_json(PERSONAS, user, TURNS_SCHEMA, name="turns")
    turns = payload.get("turns") or []
    for turn in turns:
        turn.setdefault("ref", hunk_id)
    issues = vet(turns, citable(files, cite_paths))
    return turns, issues
