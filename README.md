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

Milestone 001 (engine extraction) - in progress. See `docs/` in the planning repo.

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

## Identity and honesty

Comments are authored by your GitHub token, so they are *your* review. Every
comment carries a signature block making clear it is an AI-assisted first pass
run by you, and naming the model that ran. Vigilant PR never uses
`REQUEST_CHANGES` or `APPROVE` - severity is conveyed by markers, and humans
still own approval.

## License

MIT (c) LongIntel
