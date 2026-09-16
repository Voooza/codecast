"""Turn a parsed merge request into an interview script."""

from __future__ import annotations

from typing import Any

from . import diff as diffmod
from .config import settings
from .gitlab import MergeRequest
from .providers import complete_json

PERSONAS = """\
You write an audio podcast episode about one merge request. Two speakers:

HOST - a sharp, curious senior engineer presenting the show. Has read the diff
  once. Asks the questions a good reviewer asks: why this approach, what breaks,
  what is not covered, what surprised them. Never explains the change itself.
GUEST - the engineer who owns the change and is visibly excited to talk about it.
  Explains intent, trade-offs, the intricacies, and is honest about risk, leftovers
  and things done for expedience. Leads with the punchline, then the reason.

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
- Be concrete about this diff. No generic advice that would fit any change.
- Surface real review concerns: correctness, edge cases, error handling,
  migrations, security, tests, backwards compatibility, performance.
- Every turn that is about a specific piece of code carries that hunk anchor
  (for example "f3h2") in "ref", and the ref must be the hunk that turn is
  actually about - when the thread moves to other code, move the ref with it.
  Use null only for openers and wrap-ups.
"""

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
                    "turns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "speaker": {"type": "string", "enum": ["host", "guest"]},
                                "text": {"type": "string"},
                                "ref": {"type": ["string", "null"]},
                            },
                            "required": ["speaker", "text"],
                        },
                    },
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
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["host", "guest"]},
                    "text": {"type": "string"},
                    "ref": {"type": ["string", "null"]},
                },
                "required": ["speaker", "text"],
            },
        }
    },
    "required": ["turns"],
}


def _mr_context(mr: MergeRequest, files: list[diffmod.ParsedFile]) -> str:
    description = mr.description.strip()
    if len(description) > 4000:
        description = description[:4000] + "\n[description truncated]"
    return (
        f"Merge request: {mr.title}\n"
        f"Project: {mr.project} !{mr.iid}\n"
        f"Author: {mr.author}\n"
        f"Branches: {mr.source_branch} -> {mr.target_branch}\n\n"
        f"Description:\n{description or '(none)'}\n\n"
        f"Changed files:\n{diffmod.summary(files)}\n"
    )


def context(mr: MergeRequest, files: list[diffmod.ParsedFile]) -> str:
    """The framing both the script and the research loop start from."""
    return _mr_context(mr, files)


def build(
    mr: MergeRequest,
    files: list[diffmod.ParsedFile],
    *,
    extra_context: str = "",
    brief: str = "",
) -> dict[str, Any]:
    s = settings()
    user = (
        _mr_context(mr, files)
        + (f"\nAdditional context (Jira, Confluence, notes):\n{extra_context}\n" if extra_context else "")
        + (
            "\nResearch brief - verified by reading the repository around this change. "
            "The guest knows all of this first hand; use it to go beyond what the diff "
            "shows, and cite the file or line the way a person would say it out loud:\n"
            f"{brief}\n"
            if brief
            else ""
        )
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
) -> list[dict[str, Any]]:
    """The listener interrupts: about the diff, or about something that was said."""
    tail = "\n".join(f"{t.get('speaker', '?').upper()}: {t.get('text', '')}" for t in transcript_tail[-8:])
    focus = diffmod.hunk_text(files, hunk_id) if hunk_id else ""
    quote = quote.strip()[:600]
    if quote:
        said_by = (quoted_speaker or "someone").upper()
        challenge = (
            f"\nThe listener is questioning this line of the conversation, said by {said_by}:\n"
            f'"{quote}"\n'
            "\nContinue the episode: the host puts the listener's objection to the guest, "
            "referring to what was just said. The guest engages with it honestly - defending "
            "the claim with specifics from the diff, or conceding and correcting it if the "
            "listener is right. Do not repeat the original answer in different words. "
            "Three to six turns."
        )
    else:
        challenge = (
            "\nContinue the episode: the host relays the question in their own words "
            "(one short turn), the guest answers it precisely against this code, and the host "
            "closes the thread with a one-sentence takeaway or a follow-up the guest answers. "
            "Three to six turns."
        )
    user = (
        _mr_context(mr, files)
        + (f"\nWhat the guest knows from the repository:\n{brief}\n" if brief else "")
        + f"\nConversation so far (tail):\n{tail or '(episode not started)'}\n"
        + (f"\nThe listener highlighted this code ({hunk_id}):\n{focus}\n" if focus else "")
        + (f"\nExact highlighted lines:\n{selection}\n" if selection.strip() else "")
        + f"\nThe listener asks: {question}\n"
        + challenge
        + " If the answer is not knowable from the diff, say so plainly and name what to check."
    )
    payload = complete_json(PERSONAS, user, TURNS_SCHEMA, name="turns")
    turns = payload.get("turns") or []
    for turn in turns:
        turn.setdefault("ref", hunk_id)
    return turns
