"""proj_3006 — post a message (optionally with one PNG) to a Google Chat space.

Space ids live in `..\\.env\\Path.env` under a key name (e.g. `HeatmapSpace`),
beside the other per-machine settings rather than in the committed config.
"""

from __future__ import annotations

import json
import os

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from simple_heatmap import GITHUB_DIR, HTTP_TIMEOUT, logger

ENV_FILE = os.path.join(GITHUB_DIR, ".env", "Path.env")

# Posting needs the chat.messages.create scope, which the shared
# authorized_user.json does not carry — token.json is the one that does.
CHAT_TOKEN = os.path.join(GITHUB_DIR, ".cred", "token.json")

API = "https://chat.googleapis.com"


def space_id(key: str) -> str | None:
    """The space id stored under `key` in Path.env (a full URL is accepted too)."""
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                name, _, value = line.partition("=")
                if name.strip() == key and value.strip():
                    return value.strip().split("/")[-1]
    except OSError as exc:
        logger.warning("Cannot read %s (%s).", ENV_FILE, exc)
    return None


def post(space: str, text: str, png: str | None = None) -> str:
    """Send `text` (and `png` as an attachment) to `space`; returns the message name."""
    session = AuthorizedSession(Credentials.from_authorized_user_file(CHAT_TOKEN))
    body: dict = {"text": text}
    if png:
        body["attachment"] = [_upload(session, space, png)]

    res = session.post(f"{API}/v1/spaces/{space}/messages", json=body, timeout=HTTP_TIMEOUT)
    res.raise_for_status()
    return res.json()["name"]


def _upload(session: AuthorizedSession, space: str, png: str) -> dict:
    name = os.path.basename(png)
    with open(png, "rb") as fh:
        blob = fh.read()
    res = session.post(
        f"{API}/upload/v1/spaces/{space}/attachments:upload?uploadType=multipart",
        files={
            "metadata": (None, json.dumps({"filename": name}), "application/json"),
            "file": (name, blob, "image/png"),
        },
        timeout=HTTP_TIMEOUT,
    )
    res.raise_for_status()
    return {
        "contentName": name,
        "contentType": "image/png",
        "attachmentDataRef": res.json()["attachmentDataRef"],
    }
