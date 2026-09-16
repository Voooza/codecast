"""Thin provider layer so OpenAI and Azure AI Foundry are interchangeable."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any
from xml.sax.saxutils import escape

import httpx
from openai import AzureOpenAI, OpenAI

from .config import Settings, settings


def _client(provider: str, s: Settings, *, api_version: str | None = None) -> OpenAI | AzureOpenAI:
    if provider == "azure":
        return AzureOpenAI(
            azure_endpoint=s.azure_endpoint,
            api_key=s.azure_api_key,
            api_version=api_version or s.azure_api_version,
        )
    return OpenAI(api_key=s.openai_api_key)


@lru_cache(maxsize=1)
def chat_client() -> OpenAI | AzureOpenAI:
    s = settings()
    return _client(s.llm_provider, s)


@lru_cache(maxsize=1)
def tts_client() -> OpenAI | AzureOpenAI:
    s = settings()
    return _client(s.tts_provider, s, api_version=s.azure_tts_api_version)


def complete_json(system: str, user: str, schema: dict[str, Any], *, name: str = "result") -> dict[str, Any]:
    """Ask the chat model for a JSON object matching `schema`.

    Uses structured outputs, falling back to plain JSON mode on deployments
    (older Azure api-versions) that do not support json_schema.
    """
    s = settings()
    client = chat_client()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        response = client.chat.completions.create(
            model=s.chat_model,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": False},
            },
        )
    except Exception:
        response = client.chat.completions.create(
            model=s.chat_model,
            messages=[
                {"role": "system", "content": system + "\n\nReply with a single JSON object."},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def speech_endpoint(kind: str) -> str:
    region = settings().speech_region
    base = f"https://{region}.tts.speech.microsoft.com/cognitiveservices"
    return f"{base}/voices/list" if kind == "voices" else f"{base}/v1"


def speech_voices() -> list[dict[str, Any]]:
    """The Azure AI Speech catalog, with each voice's supported styles."""
    s = settings()
    response = httpx.get(
        speech_endpoint("voices"),
        headers={"Ocp-Apim-Subscription-Key": s.speech_key},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def _parse_directives(style: str) -> dict[str, str]:
    """"style=excited;degree=1.7;rate=+10%" -> dict"""
    out: dict[str, str] = {}
    for part in style.split(";"):
        key, _, value = part.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key and value:
            out[key] = value
    return out


def _ssml(text: str, voice: str, style: str) -> str:
    d = _parse_directives(style)
    inner = escape(text)
    prosody = "".join(
        f' {attr}="{d[attr]}"' for attr in ("rate", "pitch", "volume") if attr in d
    )
    if prosody:
        inner = f"<prosody{prosody}>{inner}</prosody>"
    if d.get("style"):
        degree = f' styledegree="{d["degree"]}"' if "degree" in d else ""
        inner = f'<mstts:express-as style="{escape(d["style"])}"{degree}>{inner}</mstts:express-as>'
    lang = "-".join(voice.split("-")[:2]) or "en-US"
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{lang}">'
        f'<voice name="{escape(voice)}">{inner}</voice></speak>'
    )


def _synthesize_speech(text: str, voice: str, style: str) -> bytes:
    """Azure AI Speech: SSML in, mp3 out. Styles come from the voice's StyleList."""
    s = settings()
    response = httpx.post(
        speech_endpoint("v1"),
        headers={
            "Ocp-Apim-Subscription-Key": s.speech_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "User-Agent": "codecast",
        },
        content=_ssml(text, voice, style).encode("utf-8"),
        timeout=120.0,
    )
    if response.status_code == 400 and "style" in style:
        # The voice does not support the requested style: fall back to plain delivery.
        response = httpx.post(
            speech_endpoint("v1"),
            headers={
                "Ocp-Apim-Subscription-Key": s.speech_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
                "User-Agent": "codecast",
            },
            content=_ssml(text, voice, "").encode("utf-8"),
            timeout=120.0,
        )
    response.raise_for_status()
    return response.content


def synthesize(text: str, voice: str, instructions: str | None = None) -> bytes:
    """Render one line of dialog to mp3 bytes."""
    s = settings()
    if s.tts_provider == "azure_speech":
        return _synthesize_speech(text, voice, instructions or "")
    kwargs: dict[str, Any] = {
        "model": s.tts_model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        kwargs["instructions"] = instructions
    try:
        result = tts_client().audio.speech.create(**kwargs)
    except Exception:
        kwargs.pop("instructions", None)  # tts-1 / older deployments reject it
        result = tts_client().audio.speech.create(**kwargs)
    return result.read() if hasattr(result, "read") else result.content
