"""Render an episode to a shareable MP4 (or a single MP3).

Every turn already has its own mp3 and the hunk it is about, so the video is an
assembly job: one frame per caption chunk showing that hunk with the line being
spoken underneath, timed to the audio, with chapter markers and subtitles.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image, ImageDraw, ImageFont

from .config import CACHE_DIR
from .session import Episode, audio_for

VIDEO_DIR = CACHE_DIR / "video"
W, H = 1920, 1080
BG = (14, 17, 22)
PANEL = (21, 26, 33)
PANEL2 = (27, 34, 48)
LINE = (38, 48, 65)
TEXT = (230, 237, 243)
MUTED = (139, 152, 169)
HOST = (126, 224, 192)
GUEST = (255, 184, 108)
ADD_BG, ADD_FG = (28, 51, 38), (126, 231, 135)
DEL_BG, DEL_FG = (58, 29, 34), (255, 148, 146)

# The schema's roles; what the frame and the subtitles call them. The guest is
# a reviewer, never the author, and an exported mp4 travels further than the
# studio does - so the frame says so too.
SPEAKERS = {"host": "HOST", "guest": "REVIEWER"}
DISCLAIMER = "codecast · synthetic review, not the author"

FONTS = Path("/usr/share/fonts/truetype/dejavu")
CAPTION_CHARS = 108  # per caption chunk; ~2 lines of the caption box
CODE_ROWS = 18  # what fits between the panel's header and its floor


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


@dataclass
class Chunk:
    """One caption: a slice of a turn, with its slice of that turn's audio."""

    turn: dict[str, Any]
    text: str
    start: float
    duration: float
    chapter: str


def ffmpeg() -> str:
    return get_ffmpeg_exe()


def probe_duration(path: Path) -> float:
    """Length of an mp3 in seconds, read off ffmpeg's own report."""
    proc = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not match:
        return 3.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def split_caption(text: str, limit: int = CAPTION_CHARS) -> list[str]:
    """Break a turn into caption-sized pieces, preferring sentence ends."""
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence:
            continue
        if pieces and len(pieces[-1]) + len(sentence) + 1 <= limit:
            pieces[-1] = f"{pieces[-1]} {sentence}"
            continue
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        pieces.append(sentence)
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) < 30 and len(merged[-1]) + len(piece) < int(limit * 1.4):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged or [text.strip()]


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    """Trim to the pixel width available, not to a guessed character count."""
    if draw.textlength(text, font=font) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + "…", font=font) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _path_label(path: str, limit: int = 104) -> str:
    """Keep the informative tail of a deep Java path."""
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


def _code(text: str) -> str:
    return text.replace("\t", "    ")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    words = text.split()
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _hunk_window(episode: Episode, ref: str | None) -> tuple[str, list[dict[str, Any]]]:
    """The file path and the lines to show for a hunk anchor."""
    if not ref:
        return "", []
    for file in episode.files:
        for hunk in file["hunks"]:
            if hunk["id"] != ref:
                continue
            lines = hunk["lines"]
            changed = [i for i, l in enumerate(lines) if l["kind"] in ("add", "del")]
            if len(lines) > CODE_ROWS and changed:
                centre = (changed[0] + changed[-1]) // 2
                start = max(0, min(centre - CODE_ROWS // 2, len(lines) - CODE_ROWS))
            else:
                start = 0
            return file["path"], lines[start : start + CODE_ROWS]
    return "", []


def _draw_frame(episode: Episode, chunk: Chunk, total: float, path: Path) -> None:
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    title_font = _font("DejaVuSans-Bold.ttf", 34)
    meta_font = _font("DejaVuSans.ttf", 24)
    code_font = _font("DejaVuSansMono.ttf", 23)
    caption_font = _font("DejaVuSans.ttf", 37)
    who_font = _font("DejaVuSans-Bold.ttf", 22)

    # header
    draw.rectangle([0, 0, W, 96], fill=PANEL)
    draw.text((48, 30), episode.title[:74], font=title_font, fill=TEXT)
    stamp = f"{episode.mr['project']} !{episode.mr['iid']}"
    draw.text((W - 48 - draw.textlength(stamp, font=meta_font), 36), stamp, font=meta_font, fill=MUTED)

    # code panel
    top, bottom = 132, 700
    draw.rounded_rectangle([48, top, W - 48, bottom], radius=12, fill=PANEL, outline=LINE)
    path_text, lines = _hunk_window(episode, chunk.turn.get("ref"))
    if lines:
        draw.rectangle([49, top + 1, W - 49, top + 50], fill=PANEL2)
        label_font = _font("DejaVuSansMono.ttf", 22)
        label = f"{_path_label(path_text)}   {chunk.turn['ref']}"
        draw.text((70, top + 14), _fit(draw, label, label_font, W - 160), font=label_font, fill=MUTED)
        y = top + 66
        for line in lines:
            mark = {"add": "+", "del": "-", "context": " ", "meta": "\\"}[line["kind"]]
            number = line["new_no"] or line["old_no"] or ""
            if line["kind"] == "add":
                draw.rectangle([56, y - 4, W - 56, y + 26], fill=ADD_BG)
                colour = ADD_FG
            elif line["kind"] == "del":
                draw.rectangle([56, y - 4, W - 56, y + 26], fill=DEL_BG)
                colour = DEL_FG
            else:
                colour = TEXT
            gutter = draw.textlength("      + ", font=code_font)
            draw.text((70, y), f"{str(number):>5} {mark} ", font=code_font, fill=MUTED)
            draw.text(
                (70 + gutter, y),
                _fit(draw, _code(line["text"]), code_font, W - 150 - gutter),
                font=code_font,
                fill=colour,
            )
            y += 27
    else:
        # no anchor: show what the episode covers
        draw.text((72, top + 40), "In this episode", font=title_font, fill=MUTED)
        y = top + 110
        for file in episode.files[:9]:
            draw.text((72, y), f"{file['path'][:96]}", font=_font("DejaVuSansMono.ttf", 24), fill=TEXT)
            draw.text((72, y + 30), f"  {file['status']}  +{file['added']} / -{file['removed']}",
                      font=_font("DejaVuSansMono.ttf", 21), fill=MUTED)
            y += 68

    # caption
    speaker = chunk.turn["speaker"]
    colour = GUEST if speaker == "guest" else HOST
    draw.rounded_rectangle([48, 736, W - 48, 992], radius=12, fill=PANEL, outline=LINE)
    label = SPEAKERS.get(speaker, speaker.upper())
    if chunk.turn.get("claim") == "verified" and chunk.turn.get("cite"):
        label += "  ✓ " + _path_label(str(chunk.turn["cite"][0]), 46)
    elif chunk.turn.get("claim") == "asserted":
        label += "  · unverified"
    draw.text((72, 762), label, font=who_font, fill=colour)
    if chunk.chapter:
        chapter = chunk.chapter[:70]
        draw.text((W - 72 - draw.textlength(chapter, font=meta_font), 762), chapter, font=meta_font, fill=MUTED)
    y = 812
    for row in _wrap(draw, chunk.text, caption_font, W - 160)[:4]:
        draw.text((72, y), row, font=caption_font, fill=TEXT)
        y += 48

    # progress
    played = min(1.0, (chunk.start + chunk.duration) / total) if total else 0
    draw.rectangle([48, 1030, W - 48, 1036], fill=PANEL2)
    draw.rectangle([48, 1030, 48 + int((W - 96) * played), 1036], fill=colour)
    clock = f"{int(chunk.start // 60):d}:{int(chunk.start % 60):02d} / {int(total // 60):d}:{int(total % 60):02d}"
    draw.text((48, 1046), clock, font=_font("DejaVuSansMono.ttf", 20), fill=MUTED)
    draw.text((W - 48 - draw.textlength(DISCLAIMER, font=meta_font), 1044), DISCLAIMER,
              font=meta_font, fill=MUTED)

    image.save(path)


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(chunks: Iterable[Chunk], path: Path) -> None:
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        blocks.append(
            f"{i}\n{_srt_time(chunk.start)} --> {_srt_time(chunk.start + chunk.duration)}\n"
            f"{SPEAKERS.get(chunk.turn['speaker'], chunk.turn['speaker'].upper())}: {chunk.text}\n"
        )
    path.write_text("\n".join(blocks))


def _write_chapters(episode: Episode, timeline: list[tuple[str, float, float]], path: Path) -> None:
    lines = [";FFMETADATA1", f"title={episode.title}", "artist=codecast"]
    for title, start, end in timeline:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(start * 1000)}",
            f"END={int(end * 1000)}",
            f"title={title}",
        ]
    path.write_text("\n".join(lines) + "\n")


def _plan(episode: Episode, work: Path) -> tuple[list[Chunk], list[Path], list[tuple[str, float, float]]]:
    """Synthesize every turn, then lay audio, captions and chapters on a timeline."""
    chunks: list[Chunk] = []
    audio_files: list[Path] = []
    chapters: list[tuple[str, float, float]] = []
    clock = 0.0
    for turn in episode.turns:
        audio = audio_for(turn["text"], turn["speaker"])
        audio_files.append(audio)
        duration = probe_duration(audio)
        pieces = split_caption(turn["text"])
        weights = [max(len(p), 1) for p in pieces]
        total_weight = sum(weights)
        chapter = turn.get("chapter") or ""
        if chapter and (not chapters or chapters[-1][0] != chapter):
            if chapters:
                chapters[-1] = (chapters[-1][0], chapters[-1][1], clock)
            chapters.append((chapter, clock, clock + duration))
        offset = clock
        for piece, weight in zip(pieces, weights):
            slice_duration = duration * weight / total_weight
            chunks.append(Chunk(turn=turn, text=piece, start=offset, duration=slice_duration, chapter=chapter))
            offset += slice_duration
        clock += duration
    if chapters:
        chapters[-1] = (chapters[-1][0], chapters[-1][1], clock)
    return chunks, audio_files, chapters


def export(episode: Episode, *, audio_only: bool = False) -> dict[str, Any]:
    """Render the episode. Returns the artifact paths and their sizes."""
    work = VIDEO_DIR / episode.id
    work.mkdir(parents=True, exist_ok=True)
    chunks, audio_files, chapters = _plan(episode, work)
    total = sum(c.duration for c in chunks)

    audio_list = work / "audio.txt"
    audio_list.write_text("".join(f"file '{p.resolve()}'\n" for p in audio_files))
    meta = work / "chapters.txt"
    _write_chapters(episode, chapters, meta)

    if audio_only:
        out = work / f"{episode.id}.mp3"
        cmd = [
            ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(audio_list),
            "-i", str(meta), "-map_metadata", "1",
            "-c:a", "libmp3lame", "-b:a", "192k", str(out),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return {"kind": "mp3", "path": str(out), "bytes": out.stat().st_size, "seconds": round(total, 1)}

    frames = work / "frames"
    frames.mkdir(exist_ok=True)
    for stale in frames.glob("*.png"):
        stale.unlink()
    entries: list[str] = []
    for i, chunk in enumerate(chunks):
        frame = frames / f"f{i:05d}.png"
        _draw_frame(episode, chunk, total, frame)
        entries.append(f"file '{frame.resolve()}'\nduration {chunk.duration:.3f}\n")
    entries.append(f"file '{(frames / f'f{len(chunks) - 1:05d}.png').resolve()}'\n")
    frame_list = work / "frames.txt"
    frame_list.write_text("".join(entries))

    srt = work / f"{episode.id}.srt"
    _write_srt(chunks, srt)

    out = work / f"{episode.id}.mp4"
    cmd = [
        ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(frame_list),
        "-f", "concat", "-safe", "0", "-i", str(audio_list),
        "-i", str(meta), "-map_metadata", "2",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", "25", "-vsync", "cfr",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return {
        "kind": "mp4",
        "path": str(out),
        "bytes": out.stat().st_size,
        "seconds": round(total, 1),
        "frames": len(chunks),
        "srt": str(srt),
        "chapters": [{"title": t, "start": round(s, 1)} for t, s, _ in chapters],
    }
