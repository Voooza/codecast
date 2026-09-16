"""Environment driven configuration, including provider selection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import json
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / ".codecast"
# Casting picked in the voice lab; overrides the .env voices without a restart.
CAST_FILE = CACHE_DIR / "cast.json"
GLAB_CONFIG = Path.home() / ".config" / "glab-cli" / "config.yml"

# How the two voices are told to perform. Override with HOST_STYLE / GUEST_STYLE.
DEFAULT_HOST_STYLE = (
    "Podcast host, genuinely curious and quick. Brisk pace, bright tone, clear "
    "emphasis on the key word of each question. Sounds like you cannot wait to hear "
    "the answer. Never flat, never ceremonial."
)
# Azure AI Speech takes direction as SSML, so its "style" is a small directive
# string: style=<name> from the voice's StyleList, degree/rate/pitch optional.
DEFAULT_SPEECH_HOST_STYLE = "style=chat;degree=1.4;rate=+8%"
DEFAULT_SPEECH_GUEST_STYLE = "style=excited;degree=1.7;rate=+10%;pitch=+2st"

DEFAULT_GUEST_STYLE = (
    "An engineer who loves this code and is delighted to be asked about it. High "
    "energy, fast but articulate, varied pitch, leaning in. Emphasise the surprising "
    "parts, let a grin come through on the clever bits, sound faintly outraged at the "
    "bug you had to fix. No monotone, no lecturing, no trailing off."
)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@lru_cache(maxsize=1)
def _load_dotenv_once() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def _glab_defaults() -> tuple[str, str]:
    """Reuse the host and token glab is already authenticated with."""
    if not GLAB_CONFIG.exists():
        return "", ""
    try:
        data = yaml.safe_load(GLAB_CONFIG.read_text()) or {}
    except yaml.YAMLError:
        return "", ""
    host = str(data.get("host") or "")
    hosts = data.get("hosts") or {}
    entry = hosts.get(host) or next(iter(hosts.values()), {}) or {}
    if not host:
        host = next(iter(hosts), "")
    return host, str(entry.get("token") or "")


_cast_cache: tuple[float, dict[str, Any]] = (0.0, {})


def cast() -> dict[str, Any]:
    """The lab's current casting, re-read whenever the file changes."""
    global _cast_cache
    if not CAST_FILE.exists():
        return {}
    stamp = CAST_FILE.stat().st_mtime
    if stamp != _cast_cache[0]:
        try:
            _cast_cache = (stamp, json.loads(CAST_FILE.read_text()))
        except (OSError, json.JSONDecodeError):
            return {}
    return _cast_cache[1]


def set_cast(provider: str, role: str, voice: str, style: str) -> dict[str, Any]:
    """Assign a voice (and its delivery direction) to host or guest."""
    if role not in {"host", "guest"}:
        raise ValueError(f"role must be host or guest, not {role!r}")
    current = dict(cast())
    # A casting only makes sense for the provider whose voices it names.
    if current.get("provider") != provider:
        current = {"provider": provider}
    current[role] = {"voice": voice, "style": style}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CAST_FILE.write_text(json.dumps(current, indent=2))
    global _cast_cache
    _cast_cache = (CAST_FILE.stat().st_mtime, current)
    return current


def clear_cast() -> None:
    """Back to whatever .env says."""
    CAST_FILE.unlink(missing_ok=True)
    global _cast_cache
    _cast_cache = (0.0, {})


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    tts_provider: str

    openai_api_key: str
    openai_chat_model: str
    openai_tts_model: str

    azure_endpoint: str
    azure_api_key: str
    azure_api_version: str
    azure_chat_deployment: str
    azure_tts_deployment: str
    azure_tts_api_version: str

    host_voice: str
    guest_voice: str
    host_style: str
    guest_style: str

    speech_region: str
    speech_key: str
    speech_host_voice: str
    speech_guest_voice: str
    speech_host_style: str
    speech_guest_style: str

    gitlab_host: str
    gitlab_token: str

    max_diff_chars: int
    research: bool
    research_max_steps: int

    @property
    def chat_model(self) -> str:
        return self.azure_chat_deployment if self.llm_provider == "azure" else self.openai_chat_model

    @property
    def tts_model(self) -> str:
        if self.tts_provider == "azure_speech":
            return "azure-ai-speech"
        return self.azure_tts_deployment if self.tts_provider == "azure" else self.openai_tts_model

    def _cast_role(self, speaker: str) -> dict[str, str]:
        picked = cast()
        if picked.get("provider") != self.tts_provider:
            return {}
        return picked.get("guest" if speaker == "guest" else "host") or {}

    def voice_for(self, speaker: str) -> str:
        chosen = self._cast_role(speaker).get("voice")
        if chosen:
            return chosen
        if self.tts_provider == "azure_speech":
            return self.speech_guest_voice if speaker == "guest" else self.speech_host_voice
        return self.guest_voice if speaker == "guest" else self.host_voice

    def style_for(self, speaker: str) -> str:
        role = self._cast_role(speaker)
        if role.get("voice"):  # the lab saves voice and direction together
            return role.get("style", "")
        if self.tts_provider == "azure_speech":
            return self.speech_guest_style if speaker == "guest" else self.speech_host_style
        return self.guest_style if speaker == "guest" else self.host_style

    def check(self, *, need_llm: bool = True, need_tts: bool = False, need_gitlab: bool = False) -> None:
        missing: list[str] = []
        if need_llm:
            missing += _provider_gaps(self, self.llm_provider, "chat")
        if need_tts:
            missing += _provider_gaps(self, self.tts_provider, "tts")
        if need_gitlab and not self.gitlab_token:
            missing.append("GITLAB_TOKEN (or run `glab auth login`)")
        if missing:
            raise ConfigError("Missing configuration: " + ", ".join(sorted(set(missing))))


class ConfigError(RuntimeError):
    pass


def _provider_gaps(s: Settings, provider: str, kind: str) -> list[str]:
    if provider == "openai":
        return [] if s.openai_api_key else ["OPENAI_API_KEY"]
    if provider == "azure":
        gaps = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", s.azure_endpoint),
                ("AZURE_OPENAI_API_KEY", s.azure_api_key),
            )
            if not value
        ]
        deployment = s.azure_chat_deployment if kind == "chat" else s.azure_tts_deployment
        if not deployment:
            gaps.append(f"AZURE_OPENAI_{kind.upper()}_DEPLOYMENT")
        return gaps
    if provider == "azure_speech" and kind == "tts":
        gaps = [] if s.speech_key else ["AZURE_SPEECH_KEY (or AZURE_OPENAI_API_KEY on the same resource)"]
        if not s.speech_region:
            gaps.append("AZURE_SPEECH_REGION")
        return gaps
    return [f"unknown provider {provider!r} (use openai or azure)"]


@lru_cache(maxsize=1)
def settings() -> Settings:
    _load_dotenv_once()
    glab_host, glab_token = _glab_defaults()
    azure_version = _env("AZURE_OPENAI_API_VERSION", "2024-10-21")
    return Settings(
        llm_provider=_env("LLM_PROVIDER", "openai").lower(),
        tts_provider=_env("TTS_PROVIDER", "openai").lower(),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_chat_model=_env("OPENAI_CHAT_MODEL", "gpt-4.1"),
        openai_tts_model=_env("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        azure_endpoint=_env("AZURE_OPENAI_ENDPOINT").rstrip("/"),
        azure_api_key=_env("AZURE_OPENAI_API_KEY"),
        azure_api_version=azure_version,
        azure_chat_deployment=_env("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        azure_tts_deployment=_env("AZURE_OPENAI_TTS_DEPLOYMENT"),
        azure_tts_api_version=_env("AZURE_OPENAI_TTS_API_VERSION", azure_version),
        host_voice=_env("HOST_VOICE", "nova"),
        guest_voice=_env("GUEST_VOICE", "ash"),
        host_style=_env("HOST_STYLE", DEFAULT_HOST_STYLE),
        guest_style=_env("GUEST_STYLE", DEFAULT_GUEST_STYLE),
        speech_region=_env("AZURE_SPEECH_REGION", "swedencentral"),
        speech_key=_env("AZURE_SPEECH_KEY") or _env("AZURE_OPENAI_API_KEY"),
        speech_host_voice=_env("AZURE_SPEECH_HOST_VOICE", "en-US-AriaNeural"),
        speech_guest_voice=_env("AZURE_SPEECH_GUEST_VOICE", "en-US-DavisNeural"),
        speech_host_style=_env("AZURE_SPEECH_HOST_STYLE", DEFAULT_SPEECH_HOST_STYLE),
        speech_guest_style=_env("AZURE_SPEECH_GUEST_STYLE", DEFAULT_SPEECH_GUEST_STYLE),
        gitlab_host=_env("GITLAB_HOST") or glab_host,
        gitlab_token=_env("GITLAB_TOKEN") or glab_token,
        max_diff_chars=int(_env("MAX_DIFF_CHARS", "90000")),
        research=_env("RESEARCH", "1").lower() not in {"0", "false", "no", "off"},
        research_max_steps=int(_env("RESEARCH_MAX_STEPS", "16")),
    )
