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
review-request watcher daemon, the Slack trigger, and the Teams trigger (beta) +
docs. Plus model-agnostic inference (Claude + free tiers + local models). See
`docs/` in the planning repo.

## Requirements

- Python 3.12+
- The GitHub CLI `gh`, authenticated as the user who should author the comments
  (`gh auth login`), or a `GH_TOKEN` env var with Pull requests: read/write.
- An API key for **any supported model provider** - including free, no-card
  tiers (Groq, Google Gemini, NVIDIA NIM). See [Models](#models-run-any-model-including-free-tiers).

The core engine is dependency-free (standard library only) - model calls go over
plain HTTP, no SDKs.

## Install

```bash
pipx install git+https://github.com/tllongdev/vigilant-pr
# or, from a clone:
uv tool install .
```

## Usage

```bash
# Review a PR and post as you (Sonnet 4.6, the default tier)
vigilant review https://github.com/owner/repo/pull/123

# Escalate to Opus 4.7 for a hard PR
vigilant review 123 --repo owner/repo --opus

# Preview without posting
vigilant review 123 --repo owner/repo --dry-run
```

## Models (run any model, including free tiers)

Vigilant PR is model-agnostic. Pick a model with a `provider/model` string via
`--model` or the `VIGILANT_MODEL` env var, and supply that provider's key. A bare
name (e.g. `claude-sonnet-4-6`) is treated as Anthropic, so existing setups keep
working. Under the hood there are just two wire protocols - the Anthropic
Messages API and the OpenAI-compatible `/chat/completions` API - so most
providers, local servers, and gateways work out of the box.

| You have... | `VIGILANT_MODEL` | Also set |
|---|---|---|
| **Nothing - want a free real model** | `groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` (free, no card) |
| A free Gemini key | `gemini/gemini-2.5-flash` | `GEMINI_API_KEY` (free tier) |
| A free NVIDIA key | `nvidia_nim/deepseek-ai/deepseek-v3.2-exp` | `NVIDIA_NIM_API_KEY` (free, no card) |
| A Claude / Anthropic key (best results) | `anthropic/claude-sonnet-4-6` (or `-opus-4-7`) | `ANTHROPIC_API_KEY` |
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
> scales with model quality; Claude Sonnet 4.6 (default) or Opus 4.7 catch subtler
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
export VIGILANT_MODEL="claude-opus-4-7"          # default tier for the daemon
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

## Slack trigger

`vigilant slack` runs a Slack listener that reviews a PR when you ask it to in
chat. It uses **Socket Mode** (an outbound WebSocket), so like the watcher it
needs no inbound ports. Install the Slack extra (`pipx install
'vigilant-pr[slack]'`) or use the container image, which bundles it.

Three ways to trigger a review:
- Slash command: `/review https://github.com/owner/repo/pull/123` (add `--opus`
  for the hard-PR tier).
- @-mention the app in a message that contains a PR link.
- React to any message containing a PR link with a trigger emoji (default
  :eyes:; configurable via `VIGILANT_TRIGGER_EMOJIS`).

Every review still posts on GitHub as **your** identity (the process's GitHub
token). Because of that, restrict who may trigger it:

```bash
export SLACK_BOT_TOKEN="xoxb-..."          # bot token
export SLACK_APP_TOKEN="xapp-..."          # app-level token (connections:write)
export SLACK_ALLOWED_USERS="U012ABCDEF"    # Slack user IDs allowed to trigger
export ANTHROPIC_API_KEY="sk-ant-..."
export GH_TOKEN="ghp_..."
vigilant slack
```

If `SLACK_ALLOWED_USERS` is unset the listener starts but warns loudly - anyone
in the workspace could otherwise post reviews under your GitHub identity.

Slack app setup (once): create an app, enable **Socket Mode**, add bot scopes
`chat:write`, `app_mentions:read`, `commands`, `reactions:read`,
`channels:history`; add the `/review` slash command; subscribe to the
`app_mention` and `reaction_added` events; install to your workspace.

```bash
docker run -d --name vigilant-slack --restart unless-stopped \
  -e ANTHROPIC_API_KEY -e GH_TOKEN \
  -e SLACK_BOT_TOKEN -e SLACK_APP_TOKEN -e SLACK_ALLOWED_USERS \
  ghcr.io/tllongdev/vigilant-pr:latest slack
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

Comments are authored by your GitHub token, so they are *your* review. Every
comment carries a signature block making clear it is an AI-assisted first pass
run by you, and naming the model that ran. Vigilant PR never uses
`REQUEST_CHANGES` or `APPROVE` - severity is conveyed by markers, and humans
still own approval.

## Branding

Brand assets live in [`assets/`](assets):

| Asset | Use |
|---|---|
| `vigilant-pr-mark.svg` / `.png` | Icon only - GitHub/app avatar, favicon |
| `vigilant-pr-logo.svg` | Horizontal lockup for light backgrounds |
| `vigilant-pr-logo-dark.svg` | Horizontal lockup for dark backgrounds |
| `vigilant-pr-social.svg` / `.png` | 1200x630 social / OpenGraph banner for link previews |
| `favicon.ico` | Multi-resolution (16-256px) favicon; also in `docs/site/` |

The mark is a watchful eye whose iris is a scanner aperture - vigilance, not
approval (Vigilant PR never auto-approves). Palette: `#4da3ff` (blue) to
`#a98bff` (violet) on `#0b0f1a` ink.

## License

MIT (c) LongIntel
