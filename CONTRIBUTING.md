# Contributing to Vigilant PR

Thanks for your interest in improving Vigilant PR. Bug reports, feature
requests, and pull requests are all welcome.

## License of contributions

Vigilant PR is released under the Apache License 2.0. By submitting a
contribution (a pull request, patch, or any other material) you agree that:

- Your contribution is provided under the Apache License 2.0
  (inbound = outbound).
- You also grant Timothy Long / Longitudinal Intelligence Technologies (LIT) (the maintainer) a perpetual,
  worldwide, non-exclusive, royalty-free, irrevocable license to use,
  reproduce, modify, sublicense, and **relicense** your contribution,
  including as part of a commercial or dual-licensed offering. This keeps the
  project's future licensing options open without having to track down every
  past contributor.
- You have the right to submit the contribution - it is your original work, or
  you are otherwise authorized to contribute it.

## Sign your commits (DCO)

Sign off every commit to certify the above under the
[Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "your message"
```

This appends a `Signed-off-by: Your Name <you@example.com>` line to the commit.

## Dev setup

- Python 3.12+
- Install with dev extras: `pip install -e '.[dev]'`
- Run the full gate before pushing:

```bash
ruff check src tests
mypy src
pytest -q
```

## Branching

Work targets the `develop` branch. `main` is the stable release branch and is
updated via PRs from `develop`. See [docs/RELEASING.md](docs/RELEASING.md).

## Reporting bugs / requesting features

Open a GitHub issue on the repository.
