<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/vigilant-pr-logo-dark.svg" />
    <img src="assets/vigilant-pr-logo.svg" alt="Vigilant PR" width="440" />
  </picture>
</p>

<p align="center"><em>A portable, workflow-agnostic AI pull-request reviewer that posts review comments <b>on behalf of you</b> - your GitHub identity, not a generic bot.</em></p>

---

PR reviews get requested in Slack/Teams by @-mentioning a colleague, or by tagging
their GitHub username as a reviewer. Vigilant PR lets the tagged reviewer delegate
a rigorous first-pass review to an agent that goes to the repo, reviews the PR, and
posts comments as them - with zero repo-side setup.

It reuses the review engine proven in production (two-tier Sonnet/Opus, adversarial
"assume the code is wrong until proven correct" prompt, severity-tagged inline
comments, thread-aware re-review, incremental diff scoping, dedup, nit-ratchet,
single-review posting) and repackages it so it no longer needs a per-repo GitHub
Actions install.

## Status

Milestones 001-005 complete: engine extraction, container + GHCR image, the
review-request watcher daemon, the no-app Slack monitor, and the Teams trigger
(beta) + docs. Plus model-agnostic inference (Claude + free tiers + local
models). See `docs/` in the planning repo.

## Requirements

- Python 3.12+
- The GitHub CLI `gh`, authenticated as the user who should author the comments
  (`gh auth login`), or a `GH_TOKEN` env var with Pull requests: read/write.
- An API key for **any supported model provider** - including free, no-card
  tiers (Groq, Google Gemini, NVIDIA NIM). See [Models](#models-run-any-model-including-free-tiers).

The core engine is dependency-free (standard library only) - model calls go over
plain HTTP, no SDKs.

Vigilant runs a quick preflight before any GitHub command and tells you exactly
what to do if `gh` is missing or not logged in, so you are never left guessing.

## Configuration (`.env`)

You can put your keys in a `.env` file in the directory you run from instead of
exporting them each time. Real environment variables always take precedence, so
`.env` is just a convenience default. Copy the template and fill in what you use:

```bash
cp .env.example .env
# then edit .env
```

If you set no model, Vigilant auto-selects one from whichever provider key it
finds (Anthropic preferred), and prints which model it chose - so a free-tier
user with only `GROQ_API_KEY` is never told to "set `ANTHROPIC_API_KEY`".

## Install

```bash
pipx install git+https://github.com/tllongdev/vigilant-pr
# or, from a clone:
uv tool install .
```

## Quick start (`vigilant init`)

New here? Run the setup wizard - it checks GitHub access, lets you pick a model
provider (leading with free, no-credit-card options), validates the key, and
writes a `.env` for you:

```bash
vigilant init
```

Then review a PR:

```bash
vigilant review https://github.com/owner/repo/pull/123
```

That's the whole BYO-model flow: install, `init`, review. Everything below is
for when you want a specific model, the watcher, or Slack.

## Usage

```bash
# Review a PR and post as you (Sonnet 5, the default tier)
vigilant review https://github.com/owner/repo/pull/123

# Escalate to Opus 4.8 for a hard PR
vigilant review 123 --repo owner/repo --opus

# Preview without posting
vigilant review 123 --repo owner/repo --dry-run
```

## Models (run any model, including free tiers)

Vigilant PR is model-agnostic. Pick a model with a `provider/model` string via
`--model` or the `VIGILANT_MODEL` env var, and supply that provider's key. A bare
name (e.g. `claude-sonnet-5`) is treated as Anthropic, so existing setups keep
working. Under the hood there are just two wire protocols - the Anthropic
Messages API and the OpenAI-compatible `/chat/completions` API - so most
providers, local servers, and gateways work out of the box.

| You have... | `VIGILANT_MODEL` | Also set |
|---|---|---|
| **Nothing - want a free real model** | `groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` (free, no card) |
| A free Gemini key | `gemini/gemini-2.5-flash` | `GEMINI_API_KEY` (free tier) |
| A free NVIDIA key | `nvidia_nim/deepseek-ai/deepseek-v3.2-exp` | `NVIDIA_NIM_API_KEY` (free, no card) |
| A Claude / Anthropic key (best results) | `anthropic/claude-sonnet-5` (or `-opus-4-8`) | `ANTHROPIC_API_KEY` |
| An OpenAI key | `openai/gpt-5.5` | `OPENAI_API_KEY` |
| An OpenRouter key | `openrouter/meta-llama/llama-3.3-70b-instruct` | `OPENROUTER_API_KEY` |
| A local model (Ollama) | `ollama/qwen2.5:14b` | `VIGILANT_API_BASE=http://localhost:11434/v1` if not default |
| Any OpenAI-compatible server (vLLM, LM Studio, TGI) | `openai_compatible/<model>` | `VIGILANT_API_BASE`, `VIGILANT_API_KEY` (if required) |
| Just want to see it run | `mock` | nothing (scripted output, no key, no cost) |

Free tiers get you started in ~2 minutes:

- **Groq** (fastest): https://console.groq.com/keys (key starts with `gsk_`)
- **Gemini**: https://aistudio.google.com/apikey
- **NVIDIA NIM**: https://build.nvidia.com (key starts with `nvapi-`)

```bash
export GROQ_API_KEY=gsk_...
export VIGILANT_MODEL=groq/llama-3.3-70b-versatile
vigilant review https://github.com/owner/repo/pull/123
```

Run `vigilant models` to see which providers your credentials can reach (and, where
the provider exposes a list endpoint, the exact model ids you can use).

> **For the deepest reviews, use a frontier model.** Adversarial bug-finding
> scales with model quality; Claude Sonnet 5 (default) or Opus 4.8 catch subtler
> issues than small free models. The free tiers are great for trying it out and
> for lighter PRs. Extended-thinking tuning (Opus adaptive thinking) applies only
> to the Anthropic path; other providers run with a low review temperature.

## Watcher (daemon mode)

`vigilant watch` polls GitHub for open PRs where **you** are a requested reviewer
and auto-reviews them on your behalf. It is idempotent (never re-reviews the same
head SHA), bounded (poll interval + per-day cap), and resilient (a failure on one
PR never crashes the loop). No GitHub App, no webhooks - just your token.

```bash
# Run continuously (default: poll every 120s, cap 50 reviews/UTC-day)
vigilant watch

# One pass and exit - ideal for cron
vigilant watch --once

# Tune cadence and cap
vigilant watch --poll-interval 300 --daily-cap 20
```

### Scoping which repos it touches

By default the watcher reviews any PR you are requested on. Constrain it with
env vars (comma-separated). Deny always wins; a non-empty allow list is
exclusive:

```bash
export VIGILANT_ORG_ALLOW="acme,acme-labs"      # only these orgs
export VIGILANT_REPO_DENY="acme/secret-repo"    # never this repo
export VIGILANT_MODEL="claude-opus-4-8"          # default tier for the daemon
```

### Deploy as a container

The seen-cache lives at `~/.vigilant/seen.json` (override with
`VIGILANT_SEEN_PATH`). Mount a volume so idempotency survives restarts:

```bash
docker run -d --name vigilant-pr --restart unless-stopped \
  -e ANTHROPIC_API_KEY \
  -e GH_TOKEN \
  -e VIGILANT_ORG_ALLOW="acme" \
  -v vigilant-state:/root/.vigilant \
  -e VIGILANT_SEEN_PATH=/root/.vigilant/seen.json \
  ghcr.io/tllongdev/vigilant-pr:latest watch
```

### Token scopes

The watcher uses only your token. It needs:
- **Contents: read** and **Pull requests: read/write** on the target repos
  (post reviews, read diffs).
- Repo read access sufficient for `gh search prs --review-requested=@me` to see
  the PRs you are tagged on.

## Slack watch (no app)

`vigilant slack-watch` polls a Slack channel and reviews any PR you're
**@-mentioned** on - whether the mention is a top-level message or a reply
inside a thread. It needs **no Slack app and no workspace-admin approval** - it
authenticates with a token you already have and only reads a channel you can
already read. It's dependency-free (stdlib only).

There are two app-free ways to give it a token:

**Auto (recommended) - `--auto-token`.** Vigilant reads the token straight from
your logged-in Slack session in Chrome and **automatically re-extracts it when
Slack expires it**, so a long-running monitor never dies on an expired session.
This needs the optional refresh extra (one-time):

```bash
pipx install 'vigilant-pr[slack-refresh]'   # or: pip install 'vigilant-pr[slack-refresh]'
python -m playwright install chromium

export GH_TOKEN="ghp_..."
export VIGILANT_MODEL="groq/llama-3.3-70b-versatile"   # or any provider
vigilant slack-watch --auto-token --channel C0123ABCD
```

If you belong to multiple Slack workspaces, Vigilant picks the one that can read
your channel automatically; set `VIGILANT_SLACK_TEAM=T0123` to force one.

**Static - set the token yourself.** No refresh (an `xoxc-` token expires in a
few hours; an `xoxb-`/`xoxp-` OAuth token lasts):

```bash
export SLACK_TOKEN="xoxc-..."               # or xoxb-/xoxp-
export SLACK_COOKIE_D="xoxd-..."            # required only for xoxc- tokens
export GH_TOKEN="ghp_..."
export VIGILANT_MODEL="groq/llama-3.3-70b-versatile"
vigilant slack-watch --channel C0123ABCD    # repeatable, or VIGILANT_SLACK_CHANNELS=C1,C2
```

A message triggers a review only when it both @-mentions you **and** contains a
GitHub PR link, so it won't fire on every PR posted in a busy channel (and it
never loops on its own reply). By default it posts the outcome back in-thread;
pass `--no-reply` to stay silent. Your Slack user id is auto-detected from the
token via `auth.test`; override with `VIGILANT_SLACK_USER_ID`. Find a channel ID
from the channel's "View channel details" footer, or the `/archives/C…` URL.

```bash
docker run -d --name vigilant-slack-watch --restart unless-stopped \
  -e GH_TOKEN -e GROQ_API_KEY -e VIGILANT_MODEL \
  -e SLACK_TOKEN -e SLACK_COOKIE_D -e VIGILANT_SLACK_CHANNELS \
  ghcr.io/tllongdev/vigilant-pr:latest slack-watch
```

## Teams trigger (beta)

`vigilant teams` serves a Microsoft Teams **Outgoing Webhook** endpoint. Teams
has no Socket-Mode equivalent, so this surface needs an inbound HTTPS URL (put it
behind your reverse proxy or a tunnel). It is dependency-free (stdlib HMAC +
HTTP).

Because a review outlasts Teams' ~5s response budget, the webhook acks
immediately and posts the result to a Teams **Incoming Webhook**
(`TEAMS_INCOMING_WEBHOOK_URL`) when the review finishes.

```bash
export TEAMS_HMAC_SECRET="<base64 secret Teams shows on webhook creation>"
export TEAMS_INCOMING_WEBHOOK_URL="https://outlook.office.com/webhook/..."  # optional
export ANTHROPIC_API_KEY="sk-ant-..." GH_TOKEN="ghp_..."
vigilant teams --port 8080
```

Then @-mention the outgoing webhook with a PR link in a channel.

## Identity and honesty

Comments are authored by your GitHub token, so they are *your* review and read
as your own writing - there is no visible bot disclaimer. Each body carries a
hidden HTML-comment marker (invisible on GitHub) that lets the tool recognize
its own prior comments for dedup and re-review.

Approval is mechanical and honest: the review is submitted as **APPROVE** when
there are no blocking findings (no critical, no medium) - so nit-only or clean
PRs get approved with their comments attached - and as a **COMMENT** when
anything blocks (or a prior concern is re-flagged as unresolved). It never uses
`REQUEST_CHANGES`, so it surfaces problems without hard-blocking the PR. The
goal is to move PRs forward unless something genuinely blocks merge.

## Branding

Brand assets live in [`assets/`](assets):

| Asset | Use |
|---|---|
| `vigilant-pr-mark.svg` / `.png` | Icon only - GitHub/app avatar, favicon |
| `vigilant-pr-logo.svg` | Horizontal lockup for light backgrounds |
| `vigilant-pr-logo-dark.svg` | Horizontal lockup for dark backgrounds |
| `vigilant-pr-social.svg` / `.png` | 1200x630 social / OpenGraph banner for link previews |
| `favicon.ico` | Multi-resolution (16-256px) favicon; also in `docs/site/` |

The mark is a watchful eye whose iris is a scanner aperture. Palette: `#4da3ff`
(blue) to `#a98bff` (violet) on `#0b0f1a` ink.

## License

MIT (c) LongIntel
