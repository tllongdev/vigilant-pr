# Vigilant PR

A portable, workflow-agnostic AI pull-request reviewer that posts review comments
**on behalf of you** - your GitHub identity, not a generic bot.

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

Milestones 001-003 complete: engine extraction, container + GHCR image, and the
review-request watcher daemon. See `docs/` in the planning repo.

## Requirements

- Python 3.12+
- The GitHub CLI `gh`, authenticated as the user who should author the comments
  (`gh auth login`), or a `GH_TOKEN` env var with Pull requests: read/write.
- `ANTHROPIC_API_KEY` in the environment.

The core engine is dependency-free (standard library only).

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

## Identity and honesty

Comments are authored by your GitHub token, so they are *your* review. Every
comment carries a signature block making clear it is an AI-assisted first pass
run by you, and naming the model that ran. Vigilant PR never uses
`REQUEST_CHANGES` or `APPROVE` - severity is conveyed by markers, and humans
still own approval.

## License

MIT (c) LongIntel
