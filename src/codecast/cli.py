"""codecast command line: serve the studio, or drive it from the terminal."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import typer
import uvicorn

from . import diff as diffmod
from . import forge, pipeline, providers, session, video
from . import transcript as transcriptmod
from .config import ConfigError, settings

app = typer.Typer(add_completion=False, help="Steerable podcast reviews of GitLab merge requests.")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8077, reload: bool = False) -> None:
    """Run the web studio.

    Binds every interface by default so a browser on the Windows host can reach
    it through WSL: Linux Chrome inside WSL has no working audio sink.
    """
    typer.echo(f"codecast studio on http://localhost:{port} (also from the Windows host)")
    uvicorn.run("codecast.api:app", host=host, port=port, reload=reload)


@app.command()
def doctor(probe: bool = typer.Option(False, "--probe", help="Actually call each provider.")) -> None:
    """Show which providers and credentials are wired up (--probe to verify them)."""
    s = settings()
    typer.echo(f"gitlab      {s.gitlab_host or '(unset)'}  token={'yes' if s.gitlab_token else 'NO'}")
    typer.echo(f"llm         {s.llm_provider}:{s.chat_model or '(unset)'}")
    typer.echo(
        f"tts         {s.tts_provider}:{s.tts_model or '(unset)'}  "
        f"{s.voice_for('host')} / {s.voice_for('guest')}"
    )
    for label, kwargs in (("chat", {"need_llm": True}), ("tts", {"need_llm": False, "need_tts": True})):
        try:
            s.check(**kwargs)
        except ConfigError as exc:
            typer.echo(f"{label:11} {exc}")
            continue
        if not probe:
            typer.echo(f"{label:11} configured (run --probe to verify)")
            continue
        try:
            if label == "chat":
                reply = providers.complete_json(
                    "Reply as JSON.", "Say ok.", {"type": "object", "properties": {"ok": {"type": "boolean"}}}
                )
                typer.echo(f"{label:11} live ({s.chat_model} answered {reply})")
            else:
                audio = providers.synthesize("Microphone check.", s.voice_for("guest"), s.style_for("guest"))
                typer.echo(f"{label:11} live ({s.tts_model}, {len(audio)} bytes of mp3)")
        except Exception as exc:  # credentials, quota, region, deployment name
            typer.echo(f"{label:11} FAILED {type(exc).__name__}: {str(exc)[:160]}")


@app.command()
def fetch(url: str) -> None:
    """Fetch a merge request and print its diff shape."""
    mr = forge.fetch(url)
    files = diffmod.parse(mr.files)
    typer.echo(f"{mr.project} !{mr.iid}  {mr.title}")
    typer.echo(f"by {mr.author}  {mr.source_branch} -> {mr.target_branch}")
    typer.echo(diffmod.summary(files))


@app.command()
def episode(
    url: str,
    as_json: bool = typer.Option(False, "--json"),
    research: bool = typer.Option(True, "--research/--no-research", help="Read the repo before writing."),
) -> None:
    """Generate an episode script for a merge request and print the transcript."""
    if not research:
        os.environ["RESEARCH"] = "0"
    settings().check(need_llm=True, need_gitlab=forge.kind(url) == "gitlab")
    ep = pipeline.record(url)
    if as_json:
        typer.echo(json.dumps(ep.to_dict(), indent=2))
        return
    typer.echo(transcriptmod.markdown(ep), nl=False)
    typer.echo(f"\n(episode id: {ep.id})")


@app.command()
def transcript(episode_id: str) -> None:
    """Print a recorded episode as markdown - pipe it to clip.exe, pbcopy or xclip."""
    ep = session.load(episode_id)
    if ep is None:
        raise typer.BadParameter(f"unknown episode {episode_id!r}")
    typer.echo(transcriptmod.markdown(ep), nl=False)



@app.command()
def export(
    episode_id: str,
    audio_only: bool = typer.Option(False, "--audio-only", help="Render an mp3 instead of an mp4."),
) -> None:
    """Render an episode to a shareable video (or audio) file."""
    ep = session.load(episode_id)
    if ep is None:
        raise typer.BadParameter(f"unknown episode {episode_id!r}")
    typer.echo(f"rendering {len(ep.turns)} turns of {ep.title!r}…")
    result = video.export(ep, audio_only=audio_only)
    typer.echo(
        f"{result['kind']}  {result['path']}  "
        f"{result['bytes'] / 1_000_000:.1f} MB  {result['seconds']}s"
    )
    if result.get("srt"):
        typer.echo(f"subtitles  {result['srt']}")
    for chapter in result.get("chapters") or []:
        typer.echo(f"  {int(chapter['start']) // 60}:{int(chapter['start']) % 60:02d}  {chapter['title']}")


# swedencentral / GlobalStandard retail prices, from the public Azure retail
# price API (prices.azure.com). Enterprise agreements and credits differ.
PRICES = {
    "InputTokens": ("$/1M input tokens", 4.00),
    "OutputTokens": ("$/1M output tokens", 20.00),
    "SynthesizedCharacters": ("$/1M TTS characters", 15.00),
}
METRICS = ["InputTokens", "OutputTokens", "TotalTokens", "SynthesizedCharacters", "TotalCalls"]


def _az(*args: str) -> str:
    proc = subprocess.run(["az", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300] or "az failed")
    return proc.stdout.strip()


@app.command()
def cost(days: int = typer.Option(1, help="How many days back to total.")) -> None:
    """What the Azure resource has metered, and roughly what it costs."""
    s = settings()
    if "azure" not in s.llm_provider and "azure" not in s.tts_provider:
        typer.echo("Neither provider is Azure; this reports Azure metering only.")
        raise typer.Exit()
    account = s.azure_endpoint.replace("https://", "").split(".")[0]
    if not account:
        raise typer.BadParameter("AZURE_OPENAI_ENDPOINT is not set, so the resource is unknown.")
    resource = _az(
        "resource", "list", "-n", account,
        "--resource-type", "Microsoft.CognitiveServices/accounts",
        "--query", "[0].id", "-o", "tsv",
    )
    group = resource.split("/resourceGroups/")[-1].split("/")[0] if resource else ""
    if not resource:
        raise typer.BadParameter(f"no Cognitive Services resource named {account!r} in this subscription")
    # Both ends are required: with only --start-time, ARM returns one bucket.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    interval = "PT1H" if days <= 2 else "PT6H" if days <= 14 else "P1D"
    typer.echo(f"{account}  last {days} day(s)")
    total = 0.0
    for metric in METRICS:
        raw = _az(
            "monitor", "metrics", "list", "--resource", resource, "--metric", metric,
            "--aggregation", "Total", "--interval", interval,
            "--start-time", start, "--end-time", end, "-o", "json",
        )
        series = json.loads(raw).get("value", [])
        amount = sum(
            (point.get("total") or 0)
            for entry in series
            for ts in entry.get("timeseries", [])
            for point in ts.get("data", [])
        )
        label, rate = PRICES.get(metric, ("", 0.0))
        if rate:
            spend = amount / 1_000_000 * rate
            total += spend
            typer.echo(f"  {metric:24} {amount:>12,.0f}   ${spend:>7.3f}  ({label} {rate})")
        else:
            typer.echo(f"  {metric:24} {amount:>12,.0f}")
    typer.echo(f"  {'estimated spend':24} {'':>12}   ${total:>7.3f}")
    typer.echo("\nHD voices (names with ':MAI-' or 'HD') meter at $22/1M instead of $15/1M.")
    typer.echo(f"Authoritative billing: Cost analysis on the {group or 'resource'} resource group.")


if __name__ == "__main__":
    app()
