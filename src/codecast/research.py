"""Let the model read the repository before it writes the script.

The diff says what changed; the checkout says what the change has to live with.
This is a bounded tool-use loop - grep, read, list - that produces a short
grounded brief with file:line evidence, which then feeds the episode prompt.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import settings
from .providers import chat_client

MAX_OUTPUT = 6000
MAX_READ_LINES = 160
GREP_TIMEOUT = 20

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search the repository with git grep. Use it to find definitions, callers and tests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Fixed string or basic regex."},
                    "glob": {"type": "string", "description": "Optional pathspec, e.g. '*.java' or 'src/**'."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a slice of a file at the reviewed commit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a directory of the repository.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "file:line for each fact in detail, e.g. 'src/db/Booking.java:88'.",
                    },
                },
                "required": ["topic", "detail", "evidence"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings"],
}


def _inside(repo: Path, path: str) -> Path:
    """Refuse to read outside the checkout."""
    candidate = (repo / path.lstrip("/")).resolve()
    if not str(candidate).startswith(str(repo.resolve())):
        raise ValueError("path escapes the repository")
    return candidate


def _grep(repo: Path, pattern: str, glob: str = "") -> str:
    args = ["git", "grep", "-n", "-I", "--max-count", "40", "-e", pattern]
    if glob:
        args += ["--", glob]
    try:
        result = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=GREP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "(grep timed out)"
    if result.returncode not in (0, 1):
        return f"(grep failed: {result.stderr.strip()[:200]})"
    return result.stdout[:MAX_OUTPUT] or "(no matches)"


def _read(repo: Path, path: str, start: int | None, end: int | None) -> str:
    try:
        target = _inside(repo, path)
    except ValueError as exc:
        return f"(refused: {exc})"
    if not target.is_file():
        return "(no such file)"
    try:
        lines = target.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(unreadable: {exc})"
    first = max(1, start or 1)
    last = min(len(lines), end or first + MAX_READ_LINES - 1)
    last = min(last, first + MAX_READ_LINES - 1)
    body = "\n".join(f"{n:>5} {lines[n - 1]}" for n in range(first, last + 1))
    return f"{path} lines {first}-{last} of {len(lines)}\n{body}"[:MAX_OUTPUT]


def _list(repo: Path, path: str) -> str:
    try:
        target = _inside(repo, path or ".")
    except ValueError as exc:
        return f"(refused: {exc})"
    if not target.is_dir():
        return "(not a directory)"
    names = sorted(
        f"{child.name}/" if child.is_dir() else child.name
        for child in target.iterdir()
        if child.name != ".git"
    )
    return "\n".join(names[:300])[:MAX_OUTPUT] or "(empty)"


def _dispatch(repo: Path, name: str, args: dict[str, Any]) -> str:
    if name == "grep":
        return _grep(repo, str(args.get("pattern", "")), str(args.get("glob") or ""))
    if name == "read_file":
        return _read(repo, str(args.get("path", "")), args.get("start_line"), args.get("end_line"))
    if name == "list_dir":
        return _list(repo, str(args.get("path", "")))
    return f"(unknown tool {name})"


def investigate(
    repo: Path,
    system: str,
    task: str,
    *,
    max_steps: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run the tool loop, then ask for a structured brief. Returns (brief, trace)."""
    s = settings()
    steps = max_steps or s.research_max_steps
    client = chat_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    trace: list[str] = []

    for _ in range(steps):
        response = client.chat.completions.create(
            model=s.chat_model, messages=messages, tools=TOOLS, tool_choice="auto"
        )
        message = response.choices[0].message
        calls = message.tool_calls or []
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in calls
                ],
            }
            if calls
            else {"role": "assistant", "content": message.content or ""}
        )
        if not calls:
            break
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            output = _dispatch(repo, call.function.name, args)
            summary = ", ".join(f"{k}={v}" for k, v in args.items() if v)
            trace.append(f"{call.function.name}({summary[:120]}) -> {len(output)} chars")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    messages.append(
        {
            "role": "user",
            "content": (
                "Now write the brief as JSON. Only include findings you verified in the files "
                "you actually read, each with file:line evidence a reader can open and check. "
                "Anything you inferred but did not read belongs in open_questions. Leave "
                "findings empty rather than guessing."
            ),
        }
    )
    final = client.chat.completions.create(
        model=s.chat_model,
        messages=messages,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "brief", "schema": BRIEF_SCHEMA, "strict": False},
        },
    )
    try:
        brief = json.loads(final.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        brief = {}
    return brief, trace


RESEARCH_SYSTEM = """\
You are preparing an expert for a recorded code review of one change. You have a
checkout of the branch under review and three tools: grep, read_file, list_dir.

Answer the questions the diff raises but cannot settle:
- what calls the changed code, and what the callers assume
- whether an invariant the change relies on is actually enforced anywhere
- what the tests do and do not cover, including tests the diff did not touch
- prior art: how the codebase already solves this elsewhere
- anything that makes the change riskier or safer than it looks

Go where the diff only points. Open the files it names but does not show: the
function it calls, the interface it implements, the entity or schema behind the
query, the migration, the config, the test that should have moved with it. A
finding about a file the diff already contains is worth little; a finding about
a file it merely references is the whole point of you being here.

Every finding must be checkable by someone who opens the file you cite, so give
file:line for each fact and quote nothing you did not read. Numbers - test
counts, timeouts, limits - only if you read them in a file. What you could not
settle goes in open_questions, named as what to check, not guessed at.

Be efficient: grep first to locate, then read only the relevant slice. Do not
re-read the diff - you already have it. Stop as soon as you can support your
findings with file:line evidence.
"""


def brief_for(repo: Path, context: str, diff_digest: str) -> tuple[dict[str, Any], list[str]]:
    task = (
        f"{context}\n\nThe change under review:\n{diff_digest}\n\n"
        "Investigate the repository around this change, then brief the expert."
    )
    return investigate(repo, RESEARCH_SYSTEM, task)


LOOKUP_SYSTEM = """\
You are checking the repository to answer one question a listener asked during a
recorded review. You have grep, read_file and list_dir over the branch under
review. Find the specific evidence that settles the question, then report it.
If the repository does not settle it, say so and name what you checked.
"""


def lookup(repo: Path, context: str, question: str, *, max_steps: int = 6) -> tuple[dict[str, Any], list[str]]:
    task = f"{context}\n\nThe listener asks: {question}\n\nCheck the repository, then report."
    return investigate(repo, LOOKUP_SYSTEM, task, max_steps=max_steps)


def evidence_paths(brief: dict[str, Any]) -> set[str]:
    """Every file the brief cites - the extra paths a turn is allowed to claim."""
    paths: set[str] = set()
    for finding in brief.get("findings") or []:
        for item in finding.get("evidence") or []:
            head = str(item).strip().split(":", 1)[0].strip().split(" line", 1)[0].strip()
            if head:
                paths.add(head)
    return paths


def render(brief: dict[str, Any]) -> str:
    """The brief as prompt text."""
    lines: list[str] = []
    for finding in brief.get("findings") or []:
        evidence = ", ".join(finding.get("evidence") or [])
        lines.append(f"- {finding.get('topic', '')}: {finding.get('detail', '')}"
                     + (f" [{evidence}]" if evidence else ""))
    for question in brief.get("open_questions") or []:
        lines.append(f"- open question: {question}")
    return "\n".join(lines)
