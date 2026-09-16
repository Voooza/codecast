"""Unified diff parsing into per-file hunks with stable anchors.

Anchors (`f2h1`) are the shared vocabulary between the model, the transcript
and the diff pane: the model tags every line of dialog with the hunk it is
talking about, and the UI scrolls there while that line plays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gitlab import FileDiff

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Line:
    kind: str  # context | add | del | meta
    old_no: int | None
    new_no: int | None
    text: str


@dataclass
class Hunk:
    id: str
    header: str
    old_start: int
    new_start: int
    lines: list[Line] = field(default_factory=list)


@dataclass
class ParsedFile:
    id: str
    path: str
    status: str  # added | deleted | renamed | modified
    old_path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.kind == "add")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.kind == "del")


def _status(file: FileDiff) -> str:
    if file.new_file:
        return "added"
    if file.deleted_file:
        return "deleted"
    if file.renamed_file:
        return "renamed"
    return "modified"


def parse(files: list[FileDiff]) -> list[ParsedFile]:
    parsed: list[ParsedFile] = []
    for findex, file in enumerate(files, start=1):
        pf = ParsedFile(
            id=f"f{findex}", path=file.path, status=_status(file), old_path=file.old_path
        )
        hunk: Hunk | None = None
        old_no = new_no = 0
        for raw in file.diff.splitlines():
            header = HUNK_HEADER.match(raw)
            if header:
                old_no, new_no = int(header.group(1)), int(header.group(3))
                hunk = Hunk(
                    id=f"{pf.id}h{len(pf.hunks) + 1}",
                    header=raw,
                    old_start=old_no,
                    new_start=new_no,
                )
                pf.hunks.append(hunk)
                continue
            if hunk is None:
                continue
            if raw.startswith("+"):
                hunk.lines.append(Line("add", None, new_no, raw[1:]))
                new_no += 1
            elif raw.startswith("-"):
                hunk.lines.append(Line("del", old_no, None, raw[1:]))
                old_no += 1
            elif raw.startswith("\\"):
                hunk.lines.append(Line("meta", None, None, raw[1:].strip()))
            else:
                hunk.lines.append(Line("context", old_no, new_no, raw[1:] if raw else ""))
                old_no += 1
                new_no += 1
        parsed.append(pf)
    return parsed


def summary(files: list[ParsedFile]) -> str:
    rows = [
        f"- {f.id} {f.path} ({f.status}, +{f.added}/-{f.removed}, {len(f.hunks)} hunk(s))"
        for f in files
    ]
    return "\n".join(rows)


def render(files: list[ParsedFile], max_chars: int) -> str:
    """Diff text for the model, with anchors and a hard size budget."""
    chunks: list[str] = []
    used = 0
    skipped: list[str] = []
    for file in files:
        head = f"\n=== {file.id} {file.path} [{file.status}] +{file.added}/-{file.removed}\n"
        body: list[str] = []
        for hunk in file.hunks:
            body.append(f"--- {hunk.id} {hunk.header}\n")
            for line in hunk.lines:
                mark = {"add": "+", "del": "-", "context": " ", "meta": "\\"}[line.kind]
                number = line.new_no or line.old_no or 0
                body.append(f"{mark}{number:>5} {line.text}\n")
        block = head + "".join(body)
        if used + len(block) > max_chars:
            skipped.append(f"{file.id} {file.path}")
            continue
        chunks.append(block)
        used += len(block)
    if skipped:
        chunks.append(
            "\n=== OMITTED (diff budget exceeded, mention that these were not reviewed) ===\n"
            + "\n".join(skipped)
            + "\n"
        )
    return "".join(chunks)


def hunk_index(files: list[ParsedFile]) -> dict[str, tuple[ParsedFile, Hunk]]:
    return {hunk.id: (file, hunk) for file in files for hunk in file.hunks}


def hunk_text(files: list[ParsedFile], hunk_id: str) -> str:
    entry = hunk_index(files).get(hunk_id)
    if not entry:
        return ""
    file, hunk = entry
    lines = [f"{file.path} {hunk.header}"]
    for line in hunk.lines:
        mark = {"add": "+", "del": "-", "context": " ", "meta": "\\"}[line.kind]
        lines.append(f"{mark} {line.text}")
    return "\n".join(lines)
