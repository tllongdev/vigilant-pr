"""Vigilant PR command-line interface.

    vigilant review <pr-url-or-number> [--repo owner/repo] [--opus|--sonnet] [--dry-run]
    vigilant threads <pr-url-or-number> [--repo owner/repo] [--dry-run]

The `watch` subcommand (daemon mode) lands in milestone 003.
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


def _add_common_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("pr", help="PR number or full GitHub PR URL")
    sub.add_argument("--repo", help="Repo as OWNER/REPO. Defaults to the current dir's gh repo.")
    sub.add_argument("--dry-run", action="store_true", help="Print the review; do not post.")
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

    args = parser.parse_args(argv)
    model = _resolve_model(args)
    config = Config.from_env(
        model=model,
        dry_run=args.dry_run,
        repo=args.repo,
        handle=args.handle,
    )

    if args.command == "review":
        return run_review(args.pr, config)
    if args.command == "threads":
        return run_threads_only(args.pr, config)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
