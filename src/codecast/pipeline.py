"""Recording an episode: fetch, ground in the repository, write the script."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import diff as diffmod
from . import forge, repo, research, script, session
from .config import settings
from .gitlab import MergeRequest

log = logging.getLogger("codecast")
RESEARCH_DIFF_CHARS = 30000


def checkout_for(mr: MergeRequest, *, update: bool) -> Path | None:
    """The repository at the review head, or None if we cannot get one."""
    if not settings().research:
        return None
    existing = repo.path_for(mr)
    if not update and (existing / ".git").exists():
        return existing
    try:
        return repo.ensure(mr)
    except (repo.RepoError, OSError) as exc:
        log.warning("no checkout for %s: %s", mr.project, exc)
        return None


def ground(mr: MergeRequest, files: list[diffmod.ParsedFile]) -> tuple[dict[str, Any], list[str]]:
    """Read the repository around the change and brief the guest."""
    checkout = checkout_for(mr, update=True)
    if checkout is None:
        return {}, []
    try:
        return research.brief_for(
            checkout,
            script.context(mr, files),
            diffmod.render(files, RESEARCH_DIFF_CHARS),
        )
    except Exception as exc:  # research is an enhancement, never a blocker
        log.warning("research failed for %s: %s", mr.project, exc)
        return {}, [f"research failed: {exc}"]


def record(url: str, *, extra_context: str = "") -> session.Episode:
    mr = forge.fetch(url)
    if not mr.files:
        raise ValueError("This merge request has no diff to talk about.")
    files = diffmod.parse(mr.files)
    brief, trace = ground(mr, files)
    episode = session.create(
        url,
        mr,
        files,
        script.build(
            mr,
            files,
            extra_context=extra_context,
            brief=research.render(brief),
            cite_paths=research.evidence_paths(brief),
        ),
        brief=brief,
        trace=trace,
    )
    return episode
