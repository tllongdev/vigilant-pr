# Releasing Vigilant PR

## Branch model

- `develop` - default branch. All work lands here (directly or via feature-branch PRs).
- `main` - protected, stable release line. It only advances through a `develop -> main`
  pull request; direct pushes are blocked (enforced for admins too), and CI must be green.
  The unpinned install (`pipx install git+https://github.com/tllongdev/vigilant-pr`)
  tracks `main`, so `main` must always be releasable.

## Versioning

- Single source of truth: `version` in `pyproject.toml`.
- `vigilant.__version__` is derived from the installed package metadata, so it can never
  drift from the release tag. Do not hard-code a version anywhere else.

## Cut a release

1. On `develop`, bump `version` in `pyproject.toml` (semver) and commit.
2. Run the local gate: `ruff check src tests && mypy src && pytest -q`.
3. Open a `develop -> main` PR. Wait for the `check` CI job to pass, then merge.
   (Zero approvals are required, so you can self-merge once CI is green.)
4. Tag the merge commit on `main` and push the tag - the tag is what ships:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z: <summary>"
   git push origin vX.Y.Z
   ```
   Pushing the tag triggers:
   - **Release** - builds the sdist + wheel and publishes the GitHub Release.
   - **Publish image** - builds and pushes `ghcr.io/tllongdev/vigilant-pr:<tag>` and `:latest`.
5. Verify the release assets are named for the new version
   (`vigilant_pr-X.Y.Z-*`). If the version bump was missed, the assets will carry
   the old number - fix `pyproject.toml`, delete the release/tag
   (`gh release delete vX.Y.Z --yes --cleanup-tag`), and re-tag.

## Hotfixes

Same flow: branch off `develop`, fix, PR into `develop`, then promote `develop -> main`
and tag. Avoid tagging directly off `main` without going through `develop` so the two
branches never diverge.
