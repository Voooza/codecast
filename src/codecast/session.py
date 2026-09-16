"""Episode state: flat turn list, disk persistence, audio cache."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from . import diff as diffmod
from .config import CACHE_DIR, settings
from .gitlab import FileDiff, MergeRequest
from .providers import synthesize

EPISODE_DIR = CACHE_DIR / "episodes"
AUDIO_DIR = CACHE_DIR / "audio"
_lock = threading.Lock()


@dataclass
class Episode:
    id: str
    url: str
    title: str
    summary: str
    mr: dict[str, Any]
    files: list[dict[str, Any]]
    turns: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    brief: list[dict[str, Any]] = field(default_factory=list)
    research_trace: list[str] = field(default_factory=list)
    counter: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        return cls(**data)

    # -- turns ---------------------------------------------------------------
    def next_turn_id(self, prefix: str = "t") -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def index_of(self, turn_id: str) -> int:
        for i, turn in enumerate(self.turns):
            if turn["id"] == turn_id:
                return i
        return -1

    def insert_after(self, turn_id: str | None, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = [
            {
                "id": self.next_turn_id("s"),
                "speaker": "guest" if str(t.get("speaker", "")).lower() == "guest" else "host",
                "text": str(t.get("text", "")).strip(),
                "ref": t.get("ref") or None,
                "chapter": "Your question",
                "origin": "steer",
            }
            for t in turns
            if str(t.get("text", "")).strip()
        ]
        at = self.index_of(turn_id) + 1 if turn_id else len(self.turns)
        if at <= 0:
            at = len(self.turns)
        self.turns[at:at] = prepared
        return prepared

    # -- parsed diff ---------------------------------------------------------
    def parsed_files(self) -> list[diffmod.ParsedFile]:
        return diffmod.parse(
            [
                FileDiff(
                    old_path=f.get("old_path", ""),
                    new_path=f.get("path", ""),
                    diff=f.get("diff", ""),
                    new_file=f.get("status") == "added",
                    deleted_file=f.get("status") == "deleted",
                    renamed_file=f.get("status") == "renamed",
                )
                for f in self.files
            ]
        )

    def merge_request(self) -> MergeRequest:
        return MergeRequest(**self.mr, files=[])


def _episode_path(episode_id: str) -> Any:
    return EPISODE_DIR / f"{episode_id}.json"


def save(episode: Episode) -> None:
    EPISODE_DIR.mkdir(parents=True, exist_ok=True)
    _episode_path(episode.id).write_text(json.dumps(episode.to_dict(), indent=2))


def load(episode_id: str) -> Episode | None:
    path = _episode_path(episode_id)
    if not path.exists():
        return None
    return Episode.from_dict(json.loads(path.read_text()))


def create(
    url: str,
    mr: MergeRequest,
    files: list[diffmod.ParsedFile],
    script: dict[str, Any],
    *,
    brief: dict[str, Any] | None = None,
    trace: list[str] | None = None,
) -> Episode:
    episode = Episode(
        id=f"{mr.slug}-{uuid.uuid4().hex[:6]}",
        url=url,
        title=script.get("title") or mr.title,
        summary=script.get("summary") or "",
        mr={
            "host": mr.host,
            "project": mr.project,
            "iid": mr.iid,
            "title": mr.title,
            "description": mr.description,
            "author": mr.author,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "web_url": mr.web_url,
        },
        files=[
            {
                "id": f.id,
                "path": f.path,
                "old_path": f.old_path,
                "status": f.status,
                "added": f.added,
                "removed": f.removed,
                "diff": next((fd.diff for fd in mr.files if (fd.new_path or fd.old_path) == f.path), ""),
                "hunks": [
                    {
                        "id": h.id,
                        "header": h.header,
                        "lines": [
                            {"kind": l.kind, "old_no": l.old_no, "new_no": l.new_no, "text": l.text}
                            for l in h.lines
                        ],
                    }
                    for h in f.hunks
                ],
            }
            for f in files
        ],
        open_questions=list(script.get("open_questions") or []),
        brief=list((brief or {}).get("findings") or []),
        research_trace=list(trace or []),
    )
    for chapter in script.get("chapters") or []:
        for turn in chapter.get("turns") or []:
            text = str(turn.get("text", "")).strip()
            if not text:
                continue
            episode.turns.append(
                {
                    "id": episode.next_turn_id(),
                    "speaker": "guest" if str(turn.get("speaker", "")).lower() == "guest" else "host",
                    "text": text,
                    "ref": turn.get("ref") or chapter.get("ref") or None,
                    "chapter": chapter.get("title") or "",
                    "origin": "script",
                }
            )
    save(episode)
    return episode


# -- audio -------------------------------------------------------------------
def audio_path(voice: str, style: str, text: str) -> Any:
    """Cache key covers delivery too, so restyling a voice re-renders it."""
    s = settings()
    material = f"{s.tts_provider}|{s.tts_model}|{voice}|{style}|{text}"
    return AUDIO_DIR / f"{hashlib.sha256(material.encode()).hexdigest()[:20]}.mp3"


def render(text: str, voice: str, style: str) -> Any:
    """Synthesize once, then serve from the cache forever."""
    path = audio_path(voice, style, text)
    if path.exists() and path.stat().st_size > 0:
        return path
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    data = synthesize(text, voice, style)
    with _lock:
        path.write_bytes(data)
    return path


def audio_for(text: str, speaker: str) -> Any:
    s = settings()
    return render(text, s.voice_for(speaker), s.style_for(speaker))
