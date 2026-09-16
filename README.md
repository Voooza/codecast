# codecast

Turn a GitLab merge request or GitHub pull request into a podcast you can
interrupt.

Paste a review URL. codecast pulls the diff, writes an interview about it — a host
who asks the questions a reviewer asks, a guest who owns the change and explains
the intricacies — and reads it out in two voices while scrolling the diff to
whatever they are discussing. Highlight any lines, type a question, and the pair
picks up your question mid-episode and keeps going.

## Quick start

```bash
cp .env.example .env     # fill in OPENAI_API_KEY (or the Azure block)
uv sync
uv run codecast doctor   # shows which providers and tokens are wired up
uv run codecast serve    # http://localhost:8077
```

`serve` binds `0.0.0.0` so you can open it from the Windows host — play it
there, not in a Linux browser inside WSL, which has no audible sink. If WSL
localhost forwarding misbehaves, use the WSL IP (`ip -4 addr show eth0`).

The GitLab host and token are read from `~/.config/glab-cli/config.yml` when
`GITLAB_HOST` / `GITLAB_TOKEN` are unset, so `glab auth login` is enough.

## Where the diff comes from

The URL decides; `forge.py` routes on its shape.

| URL | source | auth |
| --- | --- | --- |
| `…/-/merge_requests/42` | GitLab REST | token from `~/.config/glab-cli/config.yml`, or `GITLAB_TOKEN` |
| `…/pull/42` | GitHub REST | none for public repos; `GITHUB_TOKEN`/`GH_TOKEN`, or whatever `gh auth login` stored |

Both arrive as the same `MergeRequest` + `FileDiff` objects, so everything
downstream — hunk anchors, script, steering, export — is source-agnostic. GitHub
Enterprise Server hosts work too; the API base flips to `/api/v3`.

Unauthenticated GitHub is capped at 60 requests an hour, and a token is picked
up automatically if you have one, so that limit rarely bites. GitHub omits the
patch for binary and very large files; those files are dropped from the episode
rather than appearing empty.

## Grounding in the repository

The diff cannot answer "what enforces this invariant?", "who calls this?" or
"what do the untouched tests cover?" — that lives in files the diff never shows.
So before writing the script, codecast checks out the review head and lets the
model read the code.

`repo.py` keeps one shallow checkout per project under `.codecast/repos`,
fetched as `refs/pull/N/head` or `refs/merge-requests/N/head` — the synthetic
refs both hosts publish, which is what makes pull requests from forks work
without knowing the fork's URL. Credentials come from the GitLab token or the
GitHub token; nothing prompts, and tokens are scrubbed from any error text.

`research.py` then runs a bounded tool-use loop — `grep`, `read_file`,
`list_dir`, confined to the checkout — and returns a brief where every finding
carries `file:line` evidence. That brief goes into the episode prompt, so the
guest can talk about code beyond the diff, and it is stored on the episode
(`brief`, plus the tool `research_trace`). The studio shows it as *"N things
they checked in the repo"* above the transcript.

Steering questions get the same treatment: a listener question triggers a short
lookup (6 steps) against the checkout, so the answer cites real code rather than
speculating.

It is an enhancement, never a blocker — a failed clone or a failed loop is
logged and the episode is written from the diff alone. `RESEARCH=0`, or
`codecast episode --no-research`, turns it off. `RESEARCH_MAX_STEPS` (default
16) caps the loop.

Costs: research roughly triples the tokens per episode and the wall clock
(about 2 minutes instead of 50 seconds for a 44-turn episode), which at
`gpt-5.6-sol` rates is still cents. Checkouts are the real disk cost — a big
monorepo shallow-clones to tens of megabytes.

## Providers

Chat and speech are selected independently, so the script can come from one
place and the voices from another:

```
LLM_PROVIDER=openai|azure
TTS_PROVIDER=openai|azure|azure_speech
```

`azure` targets an Azure AI Foundry / Azure OpenAI resource and uses deployment
names (`AZURE_OPENAI_CHAT_DEPLOYMENT`, `AZURE_OPENAI_TTS_DEPLOYMENT`) instead of
model names. TTS needs a preview `api-version`, hence the separate
`AZURE_OPENAI_TTS_API_VERSION`.

`azure_speech` is Azure AI Speech, which any AIServices resource serves with no
model deployment at all — useful because `gpt-4o-mini-tts` is not offered in
every region (of the regions checked, only eastus2). Its direction is SSML
rather than prose: `style=excited;degree=1.7;rate=+10%;pitch=+2st`, where
`style` must appear in that voice's `StyleList` (the lab shows each voice's
styles as clickable chips). A style the voice rejects falls back to plain
delivery instead of failing.

### Standing up the Azure side

One AIServices resource covers both jobs: deploy a chat model to it, and its
key also serves Azure AI Speech.

```bash
az group create -n codecast-rg -l swedencentral
az cognitiveservices account create -n <your-account> -g codecast-rg \
  -l swedencentral --kind AIServices --sku S0 --custom-domain <your-account> --yes
az cognitiveservices account deployment create -n <your-account> -g codecast-rg \
  --deployment-name gpt-5.6-sol --model-name gpt-5.6-sol --model-version 2026-07-09 \
  --model-format OpenAI --sku-name GlobalStandard --sku-capacity 100
```

Then set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and
`AZURE_OPENAI_CHAT_DEPLOYMENT` in `.env`. Model availability is regional — of
the regions I checked, `gpt-4o-mini-tts` exists only in eastus2, which is part
of why `azure_speech` is the better TTS route.

Deployments belong to the *resource*; a Foundry *project* is a scope on top of
one, and the portal's "Create project" button provisions a new resource rather
than attaching to an existing one. If the portal shows no models, check which
resource you are looking at.

`uv run codecast doctor --probe` actually calls both providers — plain `doctor`
only checks that variables are non-empty, which is not the same thing.

Edit `.env` with `scripts/set-env.py KEY VALUE` rather than by hand-rolled
substring replacement: `OPENAI_API_KEY=` is a suffix of
`AZURE_OPENAI_API_KEY=`, and that silently corrupts the wrong line.

Voices are `HOST_VOICE` / `GUEST_VOICE`, and how they perform is `HOST_STYLE` /
`GUEST_STYLE` — free text sent to the TTS model as delivery direction, which
matters more than the voice choice. Leave them blank for the defaults in
`config.py`.

## Voice lab

`http://localhost:8077/voices` auditions every voice with the exact direction
the episode will use, on one line you choose. The audio cache is keyed by voice
*and* direction, so editing the style re-renders instead of replaying the old
take.

**Cast as host / guest** saves that voice plus the direction box beside it to
`.codecast/cast.json`, and every line rendered after that uses it — no restart,
because `voice_for()` consults the casting on each call. The casting records
which provider its voice names belong to and is ignored if `TTS_PROVIDER`
changes underneath it. **reset to .env** deletes it.

## In the studio

| key | action |
| --- | --- |
| space | play / pause |
| → or j | next turn |
| ← or k | previous turn |
| select code | ask about that hunk (Ctrl+Enter sends) |
| select dialog | ask about what was just said — push back on a claim |
| `/` | ask about the episode with nothing selected |

Whichever way you ask, the answer is spliced in right after the line that is
playing and picks up immediately, so the episode carries on rather than
branching. Quoting a line of dialog is the interesting one: the pair is told to
engage with the objection and either defend the claim with specifics or concede
and correct it.

Clicking any line of the transcript plays from there. The URL hash holds the
episode id, so a reload resumes the same episode.

## Export

`Export video` in the footer (or `uv run codecast export <episode-id>`) renders
the episode to a 1080p mp4: the hunk under discussion on screen, the line being
spoken captioned underneath, a progress bar, and the chapter titles as real mp4
chapter markers. An `.srt` comes with it. `--audio-only` gives a plain mp3 with
the same chapters.

Timing comes from the audio: each turn's mp3 is measured, then split across
caption chunks by length, so the captions track the speech without any
alignment pass. ffmpeg arrives via the `imageio-ffmpeg` wheel, so there is
nothing to install with apt.

A 46-turn episode renders in about 95 seconds to 19 MB of 9:45 video, most of
that spent synthesizing turns that were not played yet.

## What it costs, and where to watch it

Everything except the forge read is metered on your own Azure resource. Retail
prices in swedencentral for what this project uses:

| meter | price |
| --- | --- |
| `gpt-5.6-sol` input (GlobalStandard) | $4 / 1M tokens |
| `gpt-5.6-sol` output | $20 / 1M tokens |
| `gpt-5.6-sol` cached input | $0.40 / 1M tokens |
| Neural TTS (standard voices) | $15 / 1M characters |
| Neural TTS HD (`:MAI-`, HD voices) | $22 / 1M characters |

A 46-turn episode is about 9,600 characters of speech (~$0.14) plus roughly
30K tokens of diff and script (~$0.20), so **about 30 cents an episode**, and
re-listening or re-exporting is free because the audio is cached on disk.

`uv run codecast cost [--days N]` reads the resource's own metrics and applies
those prices:

```
<your-account>  last 1 day(s)
  InputTokens                    53,587   $  0.214
  OutputTokens                    8,961   $  0.179
  SynthesizedCharacters          22,819   $  0.342
  estimated spend                         $  0.736
```

Metrics are near real time; billing is authoritative but lags. For the invoice
view use Cost analysis scoped to that resource group in the portal, or set a
budget alert there.

## CLI

```bash
uv run codecast fetch <mr-url>      # what changed, as hunk anchors
uv run codecast episode <mr-url>    # print the transcript (add --json)
uv run codecast serve --reload
uv run codecast doctor --probe
uv run codecast export <episode-id> [--audio-only]
uv run codecast cost [--days 7]
```

## How it hangs together

```
gitlab.py   MR URL -> metadata + per-file diffs (REST, glab token)
diff.py     unified diff -> files/hunks with anchors (f2h1) shared by
            model, transcript and the diff pane
repo.py     shallow checkout of the review head, per project
research.py bounded grep/read/list loop -> brief with file:line evidence
pipeline.py fetch -> ground -> script, shared by the API and the CLI
script.py   personas + diff + brief -> chapters of host/guest turns, and
            answer() for a mid-episode listener question
session.py  episode as a flat turn list, persisted under .codecast/,
            mp3 cache keyed by voice + text
api.py      POST /api/episodes, GET .../audio/{turn}, POST .../ask
web/        one-file studio: diff pane, transcript, player, ask box
```

Audio is synthesized per turn on demand and cached, so playback starts after the
first line instead of after the whole episode.

## Not built yet

- Jira / Confluence context (the `extra_context` field on `POST /api/episodes`
  already feeds the prompt; nothing fills it)
- Bitbucket / Azure DevOps (one more module beside `github.py`)
- review report export
- posting the conclusions back as MR comments
- an episode list in the UI (`.codecast/episodes/` is the only index today)
- picking up existing MR discussions so the pair can react to review threads
