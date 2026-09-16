"""Route a review URL to the host that can serve it."""

from __future__ import annotations

from . import github, gitlab
from .gitlab import MergeRequest


def kind(url: str) -> str:
    if "/-/merge_requests/" in url or "/merge_requests/" in url:
        return "gitlab"
    if "/pull/" in url:
        return "github"
    raise ValueError(
        "Paste a GitLab merge request URL (…/-/merge_requests/42) "
        "or a GitHub pull request URL (…/pull/42)."
    )


def fetch(url: str) -> MergeRequest:
    url = url.strip()
    return github.fetch(url) if kind(url) == "github" else gitlab.fetch(url)
