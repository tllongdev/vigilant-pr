"""Vigilant PR command-line interface.

    vigilant review <pr-url-or-number> [--repo owner/repo] [--opus|--sonnet] [--dry-run]
    vigilant threads <pr-url-or-number> [--repo owner/repo] [--dry-run]
    vigilant watch [--once] [--poll-interval N] [--daily-cap N]
    vigilant slack                      # Slack Socket Mode listener
    vigilant teams [--host H] [--port P] # Microsoft Teams webhook (beta)

`watch` polls for PRs where you are a requested reviewer. `slack`/`teams` review
PRs on request from chat. All surfaces post on behalf of your GitHub token.
"""

from __future__ import annotations

import argparse
import sys

from .engine import (
    DEFAULT_MODEL,
    MODEL_PROFILES,
    OPUS_MODEL,
    SONNET_MODEL,
    Config,
    run_review,
    run_threads_only,
    run_watch,
)


def _resolve_model(args: argparse.Namespace) -> str:
    if getattr(args, "opus", False) and getattr(args, "sonnet", False):
        sys.stderr.write("Cannot pass both --opus and --sonnet.\n")
        sys.exit(1)
    if getattr(args, "opus", False):
        return OPUS_MODEL
    if getattr(args, "sonnet", False):
        return SONNET_MODEL
    return getattr(args, "model", DEFAULT_MODEL)


def _add_model_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--handle",
        help="GitHub handle to attribute the review to. Defaults to the authenticated gh user.",
    )
    sub.add_argument(
        "--model", default=DEFAULT_MODEL, choices=sorted(MODEL_PROFILES),
        help=f"Model to use. Default: {DEFAULT_MODEL}.",
    )
    sub.add_argument("--opus", action="store_true", help=f"Shortcut for --model {OPUS_MODEL}.")
    sub.add_argument("--sonnet", action="store_true", help=f"Shortcut for --model {SONNET_MODEL}.")


def _add_common_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("pr", help="PR number or full GitHub PR URL")
    sub.add_argument("--repo", help="Repo as OWNER/REPO. Defaults to the current dir's gh repo.")
    sub.add_argument("--dry-run", action="store_true", help="Print the review; do not post.")
    _add_model_flags(sub)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vigilant",
        description=(
            "Vigilant PR - review a pull request and post comments on your behalf. "
            "Defaults to Sonnet 4.6; pass --opus for Opus 4.7 on hard PRs."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    review_p = subparsers.add_parser("review", help="Review a PR and post as you.")
    _add_common_flags(review_p)

    threads_p = subparsers.add_parser(
        "threads", help="Validate human replies to prior Vigilant PR comments only."
    )
    _add_common_flags(threads_p)

    watch_p = subparsers.add_parser(
        "watch",
        help="Daemon: auto-review PRs where you are requested as a reviewer.",
    )
    watch_p.add_argument(
        "--once", action="store_true", help="Run a single poll pass and exit (cron/testing)."
    )
    watch_p.add_argument("--poll-interval", type=int, help="Seconds between polls (default 120).")
    watch_p.add_argument("--daily-cap", type=int, help="Max reviews per UTC day (default 50).")
    _add_model_flags(watch_p)

    slack_p = subparsers.add_parser(
        "slack",
        help="Daemon: listen on Slack (Socket Mode) and review PRs on request.",
    )
    _add_model_flags(slack_p)

    teams_p = subparsers.add_parser(
        "teams",
        help="Daemon (beta): serve a Microsoft Teams outgoing-webhook endpoint.",
    )
    teams_p.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0).")
    teams_p.add_argument("--port", type=int, default=8080, help="Bind port (default 8080).")
    _add_model_flags(teams_p)

    args = parser.parse_args(argv)
    model = _resolve_model(args)
    config = Config.from_env(
        model=model,
        dry_run=getattr(args, "dry_run", False),
        repo=getattr(args, "repo", None),
        handle=args.handle,
        poll_interval=getattr(args, "poll_interval", None),
        daily_cap=getattr(args, "daily_cap", None),
    )

    if args.command == "review":
        return run_review(args.pr, config)
    if args.command == "threads":
        return run_threads_only(args.pr, config)
    if args.command == "watch":
        return run_watch(config, once=args.once)
    if args.command == "slack":
        from .triggers.slack import run_slack

        return run_slack(config)
    if args.command == "teams":
        from .triggers.teams import run_teams

        return run_teams(config, host=args.host, port=args.port)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
