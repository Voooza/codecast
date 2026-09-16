"""A shallow checkout of the reviewed branch, so the script can be grounded.

The diff alone cannot answer "what enforces this invariant?" - that lives in
files the diff never touches. We keep one cheap clone per project under
.codecast/repos and update it per episode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import CACHE_DIR, settings
from .gitlab import MergeRequest

REPO_DIR = CACHE_DIR / "repos"
CLONE_TIMEOUT = 300
GIT_TIMEOUT = 120


class RepoError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"}
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
    )


def clone_url(mr: MergeRequest) -> str:
    """Prefer credentials we already have over interactive auth."""
    if mr.host in {"github.com", "www.github.com"}:
        from . import github

        token = github.token()
        return (
            f"https://x-access-token:{token}@github.com/{mr.project}.git"
            if token
            else f"https://github.com/{mr.project}.git"
        )
    token = settings().gitlab_token
    if token:
        return f"https://oauth2:{token}@{mr.host}/{mr.project}.git"
    return f"git@{mr.host}:{mr.project}.git"


def path_for(mr: MergeRequest) -> Path:
    return REPO_DIR / mr.host / mr.project.replace("/", "__")


def refspecs(mr: MergeRequest) -> list[str]:
    """The review head, then fallbacks.

    A pull request from a fork has no branch in the base repo, but both hosts
    publish the merged head under a synthetic ref, so fetch that first.
    """
    server_ref = (
        f"refs/pull/{mr.iid}/head"
        if mr.host in {"github.com", "www.github.com"} or "github" in mr.host
        else f"refs/merge-requests/{mr.iid}/head"
    )
    return [r for r in (server_ref, mr.source_branch, mr.target_branch) if r]


def ensure(mr: MergeRequest, *, branch: str | None = None) -> Path:
    """Clone or update the project, checked out at the head under review.

    init + fetch rather than clone, because `git clone --branch` cannot take a
    synthetic ref like refs/pull/42/head.
    """
    target = path_for(mr)
    url = clone_url(mr)
    candidates = [branch] if branch else refspecs(mr)
    if not candidates:
        raise RepoError("merge request has no ref to check out")

    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        init = _run(["init", "--quiet"], cwd=target)
        if init.returncode != 0:
            raise RepoError(init.stderr.strip()[:200] or "git init failed")
        _run(["remote", "add", "origin", url], cwd=target)

    failures: list[str] = []
    for ref in candidates:
        fetch = _run(["fetch", "--depth", "1", "--force", "origin", ref], cwd=target, timeout=CLONE_TIMEOUT)
        if fetch.returncode != 0:
            failures.append(f"{ref}: {_sanitize(fetch.stderr, url)}")
            continue
        checkout = _run(["checkout", "--force", "--detach", "FETCH_HEAD"], cwd=target)
        if checkout.returncode != 0:
            failures.append(f"{ref}: {_sanitize(checkout.stderr, url)}")
            continue
        return target
    raise RepoError("could not check out the review head — " + "; ".join(failures)[:400])


def _sanitize(message: str, url: str) -> str:
    """Never let a token reach a log line or an API response."""
    text = message.strip()
    for secret in (settings().gitlab_token, url):
        if secret and secret in text:
            text = text.replace(secret, "***")
    if "@" in text:
        text = " ".join("***" if "@" in word and "://" in word else word for word in text.split())
    return text[:400]


def existing(mr: MergeRequest) -> Path | None:
    target = path_for(mr)
    return target if (target / ".git").exists() else None


def head_sha(repo: Path) -> str:
    result = _run(["rev-parse", "--short", "HEAD"], cwd=repo)
    return result.stdout.strip() if result.returncode == 0 else ""
