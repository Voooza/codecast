"""FastAPI app: build episodes, serve turns, synthesize audio, take questions."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import diff as diffmod
from . import forge, pipeline, providers, repo, research, script, session, transcript, video
from .config import PROJECT_ROOT, ConfigError, cast, clear_cast, set_cast, settings

WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="codecast", version="0.1.0")


class NewEpisode(BaseModel):
    url: str
    extra_context: str = ""


class Preview(BaseModel):
    voice: str
    style: str = ""
    text: str = ""


class Casting(BaseModel):
    role: str
    voice: str
    style: str = ""


class ExportRequest(BaseModel):
    audio_only: bool = False


class Question(BaseModel):
    question: str
    ref: str | None = None
    selection: str = ""       # highlighted diff lines
    quote: str = ""           # highlighted line of dialog
    after_turn: str | None = None


@app.exception_handler(ConfigError)
async def _config_error(_request: Any, exc: ConfigError) -> HTMLResponse:  # pragma: no cover
    return HTMLResponse(str(exc), status_code=500)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text())


@app.get("/api/config")
def config() -> dict[str, Any]:
    s = settings()
    return {
        "llm_provider": s.llm_provider,
        "chat_model": s.chat_model,
        "tts_provider": s.tts_provider,
        "tts_model": s.tts_model,
        "voices": {"host": s.voice_for("host"), "guest": s.voice_for("guest")},
        "styles": {"host": s.style_for("host"), "guest": s.style_for("guest")},
        "gitlab_host": s.gitlab_host,
        "gitlab_authenticated": bool(s.gitlab_token),
        "cast_source": "lab" if cast().get("provider") == s.tts_provider else "env",
    }


SAMPLE_LINE = (
    "So the fun part is the normalisation - we were flagging an address as changed "
    "when the only difference was an empty string versus a null. Two hundred pointless "
    "updates a day, and nobody noticed for months."
)

# OpenAI-style voices worth auditioning; the API accepts any name the provider takes.
OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]


def _voice_catalog() -> list[dict[str, Any]]:
    """Voices for whichever TTS provider is configured, with their styles."""
    s = settings()
    if s.tts_provider != "azure_speech":
        return [{"name": v, "styles": [], "locale": "", "gender": ""} for v in OPENAI_VOICES]
    catalog = [
        {
            "name": v["ShortName"],
            "styles": v.get("StyleList") or [],
            "locale": v.get("Locale", ""),
            "gender": v.get("Gender", ""),
        }
        for v in providers.speech_voices()
        if str(v.get("Locale", "")).startswith("en-")
    ]
    # Style-capable voices first: those are the ones that can carry the energy.
    catalog.sort(key=lambda v: (not v["styles"], v["name"]))
    return catalog


@app.get("/voices", response_class=HTMLResponse)
def voice_lab() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "voices.html").read_text())


@app.get("/api/voices")
def voices() -> dict[str, Any]:
    s = settings()
    try:
        catalog = _voice_catalog()
    except Exception as exc:  # a bad key or region should not blank the page
        raise HTTPException(status_code=400, detail=f"Could not list voices: {exc}") from exc
    return {
        "provider": s.tts_provider,
        "model": s.tts_model,
        "voices": catalog,
        "sample": SAMPLE_LINE,
        "current": {"host": s.voice_for("host"), "guest": s.voice_for("guest")},
        "styles": {"host": s.style_for("host"), "guest": s.style_for("guest")},
        "style_syntax": "ssml" if s.tts_provider == "azure_speech" else "prose",
    }


@app.post("/api/cast")
def cast_voice(body: Casting) -> dict[str, Any]:
    """Assign a voice and its direction to host or guest, for every later render."""
    s = settings()
    try:
        picked = set_cast(s.tts_provider, body.role, body.voice.strip(), body.style.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"cast": picked, "voices": {"host": s.voice_for("host"), "guest": s.voice_for("guest")}}


@app.delete("/api/cast")
def reset_cast() -> dict[str, Any]:
    """Drop the lab's casting and fall back to .env."""
    clear_cast()
    s = settings()
    return {"cast": {}, "voices": {"host": s.voice_for("host"), "guest": s.voice_for("guest")}}


@app.post("/api/preview")
def preview(body: Preview) -> FileResponse:
    """Render one line with an arbitrary voice and delivery direction."""
    try:
        settings().check(need_llm=False, need_tts=True)
        path = session.render(body.text.strip() or SAMPLE_LINE, body.voice, body.style.strip())
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/episodes")
def create_episode(body: NewEpisode) -> dict[str, Any]:
    s = settings()
    try:
        # A GitHub PR needs no GitLab token, and vice versa.
        host = forge.kind(body.url)
        s.check(need_llm=True, need_gitlab=host == "gitlab")
    except (ConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        episode = pipeline.record(body.url, extra_context=body.extra_context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _episode_payload(episode)


@app.get("/api/episodes/{episode_id}")
def get_episode(episode_id: str) -> dict[str, Any]:
    return _episode_payload(_load(episode_id))


@app.post("/api/episodes/{episode_id}/ask")
def ask(episode_id: str, body: Question) -> dict[str, Any]:
    episode = _load(episode_id)
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Empty question.")
    files = episode.parsed_files()
    at = episode.index_of(body.after_turn) if body.after_turn else len(episode.turns) - 1
    # What the guest already knows, plus a quick look in the repository if the
    # question needs something the brief does not cover.
    brief = dict(findings=list(episode.brief))
    mr = episode.merge_request()
    checkout = repo.existing(mr) if settings().research else None
    if checkout is not None:
        try:
            extra, _trace = research.lookup(
                checkout,
                script.context(mr, files) + "\n\nAlready known:\n" + research.render(brief),
                body.question.strip(),
            )
            brief["findings"] = brief["findings"] + list(extra.get("findings") or [])
        except Exception:  # the pair can still answer from the diff
            pass

    quoted_speaker = ""
    if body.quote.strip():
        needle = body.quote.strip()[:80]
        quoted = next((t for t in episode.turns if needle and needle in t["text"]), None)
        quoted_speaker = (quoted or {}).get("speaker", "")
    turns, issues = script.answer(
        episode.merge_request(),
        files,
        question=body.question.strip(),
        hunk_id=body.ref,
        selection=body.selection,
        transcript_tail=episode.turns[: at + 1],
        quote=body.quote,
        quoted_speaker=quoted_speaker,
        brief=research.render(brief),
        cite_paths=research.evidence_paths(brief),
    )
    inserted = episode.insert_after(body.after_turn, turns)
    episode.lint += issues
    session.save(episode)
    return {"turns": inserted, "lint": episode.lint}


@app.get("/api/episodes/{episode_id}/transcript.md", response_class=PlainTextResponse)
def transcript_md(episode_id: str) -> PlainTextResponse:
    """The episode as markdown, for the studio's copy button and for curl."""
    return PlainTextResponse(
        transcript.markdown(_load(episode_id)), media_type="text/markdown; charset=utf-8"
    )


@app.post("/api/episodes/{episode_id}/export")
def export_episode(episode_id: str, body: ExportRequest) -> dict[str, Any]:
    """Render the whole episode to mp4 (or mp3). Synthesizes any missing audio."""
    episode = _load(episode_id)
    try:
        settings().check(need_llm=False, need_tts=True)
        result = video.export(episode, audio_only=body.audio_only)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace")[-400:] or "ffmpeg failed"
        raise HTTPException(status_code=500, detail=detail) from exc
    kind = result["kind"]
    return {
        **result,
        "download": f"/api/episodes/{episode_id}/download/{kind}",
        "subtitles": f"/api/episodes/{episode_id}/download/srt" if kind == "mp4" else None,
    }


@app.get("/api/episodes/{episode_id}/download/{kind}")
def download(episode_id: str, kind: str) -> FileResponse:
    episode = _load(episode_id)
    if kind not in {"mp4", "mp3", "srt"}:
        raise HTTPException(status_code=404, detail="Unknown artifact.")
    path = video.VIDEO_DIR / episode.id / f"{episode.id}.{kind}"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No {kind} rendered yet for this episode.")
    media = {"mp4": "video/mp4", "mp3": "audio/mpeg", "srt": "text/plain"}[kind]
    name = re.sub(r"[^A-Za-z0-9]+", "-", episode.title).strip("-").lower() or episode.id
    return FileResponse(path, media_type=media, filename=f"{name}.{kind}")


@app.get("/api/episodes/{episode_id}/audio/{turn_id}")
def audio(episode_id: str, turn_id: str) -> FileResponse:
    episode = _load(episode_id)
    index = episode.index_of(turn_id)
    if index < 0:
        raise HTTPException(status_code=404, detail="No such turn.")
    turn = episode.turns[index]
    try:
        settings().check(need_llm=False, need_tts=True)
        path = session.audio_for(turn["text"], turn["speaker"])
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


def _load(episode_id: str) -> session.Episode:
    episode = session.load(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Unknown episode.")
    return episode


def _episode_payload(episode: session.Episode) -> dict[str, Any]:
    data = episode.to_dict()
    data.pop("counter", None)
    return data


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
