---
name: vigilant-pr
description: Review a GitHub pull request and post comments on behalf of the running user (their GitHub identity, not a bot). Use when the user is asked to review a PR - in Slack/Teams by @-mention, or by being tagged as a reviewer on the PR - and wants to delegate a rigorous first-pass review. Runs the Vigilant PR engine (two-tier Sonnet/Opus, adversarial review, severity-tagged inline comments, thread-aware re-review) via the `vigilant` CLI or the container.
---

<!--
SOURCE OF TRUTH: canonical at .cursor/skills/vigilant-pr/SKILL.md
The .claude/skills/vigilant-pr/SKILL.md mirror must be kept identical.
-->

# Vigilant PR

Review a pull request and post the review **as the running user**. Comments are
authored by the user's own GitHub token, so they are the user's review - every
comment carries a signature block making clear it is an AI-assisted first pass,
naming the model that ran, so colleagues are never misled.

## When to use

- The user was asked to review a PR (Slack/Teams @-mention, or tagged as a
  reviewer on GitHub) and wants to delegate the first pass.
- The user says "review PR <n>", "vigilant review", "review this on my behalf".

## Prerequisites

- `gh` authenticated as the user who should author the comments (`gh auth status`).
- `ANTHROPIC_API_KEY` in the environment.

## How to run

Default tier is Sonnet 4.6 (fast, cheap). Escalate to Opus 4.7 on hard PRs.

```bash
# CLI (pipx/uv install)
vigilant review <pr-url-or-number> [--repo owner/repo] [--opus] [--dry-run]

# Container (no local install)
docker run --rm -e ANTHROPIC_API_KEY -e GH_TOKEN \
  ghcr.io/tllongdev/vigilant-pr review <pr-url-or-number>
```

Always run `--dry-run` first and show the user the drafted review (findings
count + inline comments) before posting. Only post on explicit confirmation.

## Severity scheme

- Critical: would break behavior in production, leak data, corrupt state, or
  block rollback. Fix before merge.
- Medium: real issue to fix before merge but not an immediate production failure.
- Nit: style/naming/minor. Capped at 5 inline; overflow noted in the summary.

## Behavior guarantees

- Posts the whole review as a single GitHub review (one notification).
- Never uses `REQUEST_CHANGES` or `APPROVE` - severity is conveyed by markers;
  humans own approval.
- Re-review aware: incremental diff since the last reviewed SHA, dedups
  already-raised findings, suppresses nits on re-runs, and validates human
  replies to prior comments.
- Skips what CI already enforces (ruff, mypy, gofmt, go vet) and generated files.
