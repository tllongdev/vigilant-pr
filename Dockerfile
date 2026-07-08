# Vigilant PR - single image that serves both `review` (one-shot) and, later,
# `watch` (daemon). Bundles Python 3.12 + the GitHub CLI so the only inputs a
# user supplies at runtime are ANTHROPIC_API_KEY and a GitHub token (GH_TOKEN),
# or a mounted `gh` config. No secrets are baked into the image.
FROM python:3.12-slim

# GitHub CLI from the official apt repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# `gh` reads GH_TOKEN from the environment; no interactive login needed.
ENTRYPOINT ["vigilant"]
CMD ["--help"]
