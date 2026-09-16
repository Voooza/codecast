"""Fetch a GitHub pull request into the same shape as a GitLab merge request."""

from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import urlparse

import httpx

from .gitlab import FileDiff, MergeRequest

PR_URL = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
PER_PAGE = 100


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """https://github.com/owner/repo/pull/42 -> (host, owner/repo, 42)"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    match = PR_URL.match(parsed.path)
    if not parsed.hostname or not match:
        raise ValueError(f"Not a GitHub pull request URL: {url}")
    project = f"{match.group('owner')}/{match.group('repo')}"
    return parsed.hostname, project, int(match.group("number"))


def token() -> str:
    """Optional: public repos work unauthenticated, but the rate limit is 60/hour."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.getenv(name):
            return os.environ[name].strip()
    try:  # whatever `gh auth login` already stored
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _api_base(host: str) -> str:
    # github.com plus GitHub Enterprise Server, where the API lives under /api/v3
    return "https://api.github.com" if host in {"github.com", "www.github.com"} else f"https://{host}/api/v3"


def _status(item: dict[str, object]) -> tuple[bool, bool, bool]:
    status = str(item.get("status", ""))
    return status == "added", status == "removed", status == "renamed"


def fetch(url: str) -> MergeRequest:
    host, project, number = parse_pr_url(url)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if auth := token():
        headers["Authorization"] = f"Bearer {auth}"
    with httpx.Client(base_url=_api_base(host), headers=headers, timeout=60.0, follow_redirects=True) as client:
        pr = client.get(f"/repos/{project}/pulls/{number}")
        if pr.status_code == 404:
            raise ValueError(
                f"{project}#{number} not found. Private repositories need GITHUB_TOKEN or `gh auth login`."
            )
        pr.raise_for_status()
        detail = pr.json()

        files: list[FileDiff] = []
        page = 1
        while True:
            response = client.get(
                f"/repos/{project}/pulls/{number}/files", params={"per_page": PER_PAGE, "page": page}
            )
            response.raise_for_status()
            payload = response.json()
            if not payload:
                break
            for item in payload:
                added, deleted, renamed = _status(item)
                patch = item.get("patch")
                if not patch:
                    # GitHub omits the patch for binary or very large files.
                    patch = ""
                files.append(
                    FileDiff(
                        old_path=str(item.get("previous_filename") or item.get("filename") or ""),
                        new_path=str(item.get("filename") or ""),
                        diff=str(patch),
                        new_file=added,
                        deleted_file=deleted,
                        renamed_file=renamed,
                    )
                )
            if len(payload) < PER_PAGE:
                break
            page += 1

    return MergeRequest(
        host=host,
        project=project,
        iid=number,
        title=detail.get("title", ""),
        description=detail.get("body") or "",
        author=((detail.get("user") or {}).get("login")) or "unknown",
        source_branch=((detail.get("head") or {}).get("ref")) or "",
        target_branch=((detail.get("base") or {}).get("ref")) or "",
        web_url=detail.get("html_url", ""),
        files=[f for f in files if f.diff],
    )
