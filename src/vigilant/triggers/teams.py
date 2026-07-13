"""Microsoft Teams trigger (optional / beta) - milestone 005.

Teams has no Socket-Mode equivalent, so this uses the standard no-App path: a
Teams *Outgoing Webhook*. When you @-mention the webhook in a channel, Teams
POSTs an HMAC-signed Bot Framework activity to this server. Unlike the poll-based
watcher and Slack Socket Mode, this surface needs an inbound HTTPS endpoint
(expose it via your own reverse proxy or a tunnel).

Because a review takes longer than Teams' ~5s response budget, we ack the POST
immediately and, when the review finishes, post the result to a Teams *Incoming
Webhook* (TEAMS_INCOMING_WEBHOOK_URL) if one is configured.

Dependency-free: verification uses stdlib `hmac`, delivery uses stdlib
`urllib`. Set TEAMS_HMAC_SECRET to the base64 secret Teams shows when you create
the outgoing webhook.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..engine import Config, model_key_missing
from .core import format_reply, review_from_text


def _verify_signature(secret_b64: str, body: bytes, auth_header: str | None) -> bool:
    """Validate the `Authorization: HMAC <sig>` header Teams sends.

    Signature is base64(HMAC-SHA256(key=base64decode(secret), msg=raw_body)).
    """
    if not auth_header or not auth_header.startswith("HMAC "):
        return False
    provided = auth_header[len("HMAC "):].strip()
    try:
        key = base64.b64decode(secret_b64)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    digest = hmac.new(key, body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(provided, expected)


def _post_incoming_webhook(url: str, text: str) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as e:  # noqa: BLE001 - delivery is best-effort
        sys.stderr.write(f"Failed to post Teams incoming webhook result: {e}\n")


def _make_handler(config: Config, secret_b64: str, result_webhook: str | None) -> type:
    class TeamsHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
            sys.stderr.write("Teams webhook: " + (fmt % args) + "\n")

        def _reply(self, text: str, status: int = 200) -> None:
            body = json.dumps({"type": "message", "text": text}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            if not _verify_signature(secret_b64, raw, self.headers.get("Authorization")):
                self._reply("Signature verification failed.", status=401)
                return
            try:
                activity = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply("Could not parse the request.", status=400)
                return

            text = activity.get("text", "") or ""

            def _work() -> None:
                outcomes = review_from_text(text, config)
                if result_webhook and outcomes:
                    _post_incoming_webhook(result_webhook, format_reply(outcomes))

            threading.Thread(target=_work, daemon=True).start()
            ack = "On it - reviewing now. I'll post the result here when it's ready."
            if not result_webhook:
                ack = (
                    "On it - reviewing now. (Set TEAMS_INCOMING_WEBHOOK_URL to have me "
                    "post the result back here.)"
                )
            self._reply(ack)

    return TeamsHandler


def run_teams(config: Config, host: str = "0.0.0.0", port: int = 8080) -> int:
    """Start the Teams outgoing-webhook HTTP server. Blocks until interrupted."""
    secret = os.environ.get("TEAMS_HMAC_SECRET")
    if not secret:
        sys.stderr.write(
            "TEAMS_HMAC_SECRET (base64) is required - it is shown when you create the "
            "Teams outgoing webhook. See the README Teams section.\n"
        )
        return 1
    key_problem = model_key_missing(config)
    if key_problem:
        sys.stderr.write(key_problem + "\n")
        return 1

    result_webhook = os.environ.get("TEAMS_INCOMING_WEBHOOK_URL")
    handler = _make_handler(config, secret, result_webhook)
    server = ThreadingHTTPServer((host, port), handler)
    sys.stderr.write(
        f"Vigilant PR Teams listener on http://{host}:{port} "
        f"(results -> {'incoming webhook' if result_webhook else 'ack only'}). Ctrl-C to stop.\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0
