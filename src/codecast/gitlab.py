"""Fetch merge request metadata and diffs from a (self hosted) GitLab."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import httpx

from .config import settings

MR_URL = re.compile(r"^/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)")


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    diff: str
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path


@dataclass
class MergeRequest:
    host: str
    project: str
    iid: int
    title: str
    description: str
    author: str
    source_branch: str
    target_branch: str
    web_url: str
    files: list[FileDiff] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.project.replace('/', '-')}-{self.iid}"


def parse_mr_url(url: str) -> tuple[str, str, int]:
    """https://host/group/sub/project/-/merge_requests/42 -> (host, group/sub/project, 42)"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    match = MR_URL.match(parsed.path)
    if not parsed.hostname or not match:
        raise ValueError(f"Not a GitLab merge request URL: {url}")
    return parsed.hostname, match.group("project").strip("/"), int(match.group("iid"))


class GitLab:
    def __init__(self, host: str | None = None, token: str | None = None) -> None:
        s = settings()
        self.host = host or s.gitlab_host
        self.token = token or s.gitlab_token
        self._client = httpx.Client(
            base_url=f"https://{self.host}/api/v4",
            headers={"PRIVATE-TOKEN": self.token},
            timeout=60.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitLab":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get(self, path: str, **params: object) -> httpx.Response:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response

    def merge_request(self, project: str, iid: int) -> MergeRequest:
        pid = quote(project, safe="")
        mr = self._get(f"/projects/{pid}/merge_requests/{iid}").json()
        return MergeRequest(
            host=self.host,
            project=project,
            iid=iid,
            title=mr.get("title", ""),
            description=mr.get("description") or "",
            author=(mr.get("author") or {}).get("name", "unknown"),
            source_branch=mr.get("source_branch", ""),
            target_branch=mr.get("target_branch", ""),
            web_url=mr.get("web_url", ""),
            files=self.diffs(project, iid),
        )

    def diffs(self, project: str, iid: int) -> list[FileDiff]:
        pid = quote(project, safe="")
        out: list[FileDiff] = []
        page = 1
        while True:
            payload = self._get(
                f"/projects/{pid}/merge_requests/{iid}/diffs", per_page=50, page=page
            ).json()
            if not payload:
                break
            for item in payload:
                out.append(
                    FileDiff(
                        old_path=item.get("old_path") or "",
                        new_path=item.get("new_path") or "",
                        diff=item.get("diff") or "",
                        new_file=bool(item.get("new_file")),
                        deleted_file=bool(item.get("deleted_file")),
                        renamed_file=bool(item.get("renamed_file")),
                    )
                )
            if len(payload) < 50:
                break
            page += 1
        return out


def fetch(url: str) -> MergeRequest:
    host, project, iid = parse_mr_url(url)
    with GitLab(host=host) as gl:
        return gl.merge_request(project, iid)
