"""Vigilant PR command-line interface.

    vigilant review <pr-url-or-number> [--repo owner/repo] [--model P/M|--opus|--sonnet] [--dry-run]
    vigilant threads <pr-url-or-number> [--repo owner/repo] [--dry-run]
    vigilant watch [--once] [--poll-interval N] [--daily-cap N]
    vigilant slack                      # Slack Socket Mode listener
    vigilant teams [--host H] [--port P] # Microsoft Teams webhook (beta)
    vigilant models                     # list models your credentials can reach

`watch` polls for PRs where you are a requested reviewer. `slack`/`teams` review
PRs on request from chat. All surfaces post on behalf of your GitHub token, and
run against any model (Claude, or free tiers like Groq/Gemini/NVIDIA, or a local
model) via a `provider/model` string - see `vigilant models`.
"""

from __future__ import annotations

import argparse
import os
import sys

from .engine import (
    DEFAULT_MODEL,
    OPUS_MODEL,
    PROVIDERS,
    SONNET_MODEL,
    Config,
    auto_select_model,
    github_preflight,
    list_models,
    load_dotenv,
    provider_api_key,
    resolve_provider,
    run_review,
    run_threads_only,
    run_watch,
)

# Commands that touch GitHub and therefore need `gh`/GH_TOKEN available.
_GITHUB_COMMANDS = {"review", "threads", "watch", "slack", "teams"}


def _resolve_model(args: argparse.Namespace) -> str | None:
    """Return the explicit model to use, or None to defer to env / default.

    Returning None (rather than DEFAULT_MODEL) is important: it lets a
    VIGILANT_MODEL env var take effect instead of being clobbered by the
    argparse default.
    """
    if getattr(args, "opus", False) and getattr(args, "sonnet", False):
        sys.stderr.write("Cannot pass both --opus and --sonnet.\n")
        sys.exit(1)
    if getattr(args, "opus", False):
        return OPUS_MODEL
    if getattr(args, "sonnet", False):
        return SONNET_MODEL
    return getattr(args, "model", None)


def _add_model_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--handle",
        help="GitHub handle to attribute the review to. Defaults to the authenticated gh user.",
    )
    sub.add_argument(
        "--model", default=None,
        help="Model as 'provider/model' (e.g. groq/llama-3.3-70b-versatile, "
             "anthropic/claude-sonnet-4-6, ollama/qwen2.5:14b). A bare name is "
             f"treated as Anthropic. Defaults to VIGILANT_MODEL or {DEFAULT_MODEL}.",
    )
    sub.add_argument("--opus", action="store_true", help=f"Shortcut for --model {OPUS_MODEL}.")
    sub.add_argument("--sonnet", action="store_true", help=f"Shortcut for --model {SONNET_MODEL}.")


def _add_common_flags(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("pr", help="PR number or full GitHub PR URL")
    sub.add_argument("--repo", help="Repo as OWNER/REPO. Defaults to the current dir's gh repo.")
    sub.add_argument("--dry-run", action="store_true", help="Print the review; do not post.")
    _add_model_flags(sub)


def _effective_model(args: argparse.Namespace) -> str | None:
    """Resolve the model to use, auto-selecting from available keys if needed.

    Explicit flags and VIGILANT_MODEL always win. Otherwise, if there is no
    Anthropic key but another provider key is present, auto-pick that provider
    so a free-tier user isn't told to "set ANTHROPIC_API_KEY". Returns None to
    defer to the built-in default (Anthropic Sonnet).
    """
    explicit = _resolve_model(args)
    if explicit is not None or os.environ.get("VIGILANT_MODEL"):
        return explicit
    auto = auto_select_model()
    if auto and resolve_provider(auto)[0] != "anthropic":
        provider = resolve_provider(auto)[0]
        sys.stderr.write(
            f"No model set and no Anthropic key found; using {auto} "
            f"({PROVIDERS[provider]['key_env']} detected). "
            f"Override with --model or VIGILANT_MODEL.\n"
        )
        return auto
    return None


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # convenience: load ./.env (real env vars still win)
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

    subparsers.add_parser(
        "models",
        help="List providers and the models your credentials can reach.",
    )

    args = parser.parse_args(argv)
    if args.command == "models":
        return run_models(Config.from_env())

    if args.command in _GITHUB_COMMANDS:
        gh_problem = github_preflight()
        if gh_problem:
            sys.stderr.write(gh_problem + "\n")
            return 1

    model = _effective_model(args)
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


# Example model strings shown for providers whose key is present but that expose
# no (or a very large) list endpoint.
_EXAMPLE_MODELS = {
    "anthropic": "anthropic/claude-sonnet-4-6",
    "openai": "openai/gpt-5.5",
    "groq": "groq/llama-3.3-70b-versatile",
    "gemini": "gemini/gemini-2.5-flash",
    "nvidia_nim": "nvidia_nim/deepseek-ai/deepseek-v3.2-exp",
    "openrouter": "openrouter/meta-llama/llama-3.3-70b-instruct",
    "ollama": "ollama/qwen2.5:14b",
    "openai_compatible": "openai_compatible/<model> (set VIGILANT_API_BASE)",
}


def run_models(config: Config) -> int:
    """Print each provider's status and the models the credentials can reach."""
    print("Vigilant PR - model providers\n")
    any_ready = False
    for provider in PROVIDERS:
        if provider == "mock":
            continue
        needs_key = bool(PROVIDERS[provider].get("key_env"))
        has_key = provider_api_key(provider) is not None
        ready = (not needs_key) or has_key
        # Count only cloud providers with a real key toward "any_ready" so the
        # free-tier hint still shows when the user has nothing configured.
        any_ready = any_ready or (has_key and provider != "openai_compatible")
        status = "ready " if ready else "no key"
        key_env = PROVIDERS[provider].get("key_env") or "(keyless)"
        print(f"[{status}] {provider:<18} key: {key_env}")
        if ready:
            models = list_models(provider, config)
            if models:
                for m in models[:12]:
                    print(f"           {provider}/{m}")
                if len(models) > 12:
                    print(f"           ... and {len(models) - 12} more")
            else:
                print(f"           e.g. {_EXAMPLE_MODELS.get(provider, provider + '/<model>')}")
        else:
            print(f"           set {key_env} to enable  (e.g. {_EXAMPLE_MODELS.get(provider, '')})")
        print()

    print("Set the model with VIGILANT_MODEL or --model 'provider/model'.")
    if not any_ready:
        print("\nNo provider keys detected. Free options (no credit card):")
        print("  Groq:   https://console.groq.com/keys      -> export GROQ_API_KEY=gsk_...")
        print("  Gemini: https://aistudio.google.com/apikey  -> export GEMINI_API_KEY=...")
        print("  NVIDIA: https://build.nvidia.com             -> export NVIDIA_NIM_API_KEY=nvapi-...")
        print("Then: export VIGILANT_MODEL=groq/llama-3.3-70b-versatile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
