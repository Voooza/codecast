"""The episode as markdown - the form you paste into a review thread.

One renderer behind the studio's Copy button, `codecast transcript` and the API,
so whatever lands on someone's clipboard carries the same synthetic label and
the same claim levels the studio shows.
"""

from __future__ import annotations

from .session import Episode

SPEAKERS = {"host": "Host", "guest": "Reviewer"}
FLAGS = {
    "author-voice": "reads as the author speaking - they did not say this",
    "claims-a-run": "reports a run - nothing in codecast executes anything",
}
UNVERIFIED = "_unverified: the reviewer's reading, not a cited fact_"


def _label(episode: Episode) -> str:
    """The header that has to survive a copy-paste into a review thread."""
    author = episode.mr.get("author") or "the author"
    where = f"{episode.mr.get('project') or ''} !{episode.mr.get('iid') or ''}".strip()
    url = episode.mr.get("web_url") or episode.url
    return "\n".join(
        [
            "> **Synthetic transcript, written by codecast.** Two generated voices discuss",
            f"> {author}'s change; neither speaker is {author}, and nothing here was said by",
            "> anyone. ✓ marks a claim traced to a cited line - everything else is the",
            "> reviewer's reading, and no tests, benchmarks or deployments were run.",
            ">",
            f"> {where} · {url}",
        ]
    )


def markdown(episode: Episode) -> str:
    out: list[str] = [f"# {episode.title}", ""]
    if episode.summary:
        out += [episode.summary, ""]
    out += [_label(episode), ""]
    chapter = None
    for turn in episode.turns:
        if turn.get("chapter") and turn["chapter"] != chapter:
            chapter = turn["chapter"]
            out += [f"## {chapter}", ""]
        who = SPEAKERS.get(turn.get("speaker", ""), str(turn.get("speaker", "")).title())
        ref = f" `{turn['ref']}`" if turn.get("ref") else ""
        # Two trailing spaces: a hard break, so the citation stays with its line
        # instead of being soft-wrapped into the speech.
        block = [f"**{who}**{ref} — {turn.get('text', '')}"]
        cites = [c for c in (turn.get("cite") or []) if c]
        if turn.get("claim") == "verified" and cites:
            block.append("✓ " + " · ".join(f"`{c}`" for c in cites))
        elif turn.get("claim") == "asserted":
            block.append(UNVERIFIED)
        block += [f"⚠ _{FLAGS.get(flag, flag)}_" for flag in turn.get("flags") or []]
        out += ["  \n".join(block), ""]
    if episode.open_questions:
        out += ["## Left open", ""] + [f"- {q}" for q in episode.open_questions] + [""]
    if episode.lint:
        out += [
            "## Checker notes",
            "",
            "What the claim checker corrected while writing this episode:",
            "",
        ] + [f"- {issue}" for issue in episode.lint] + [""]
    return "\n".join(out).rstrip() + "\n"
