"""Configuration loading. Reads env vars (with optional .env file)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .oauth_client import client_id as oauth_client_id
from .oauth_client import client_secret as oauth_client_secret

try:
    from dotenv import load_dotenv

    # Pick up secrets from the file bootstrap writes (`poller.env`) first, then
    # fall back to a generic `.env`. Real env vars set in the shell still win.
    load_dotenv("poller.env", override=False)
    load_dotenv(".env", override=False)
except ImportError:  # python-dotenv is optional at runtime
    pass

logger = logging.getLogger(__name__)


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(
            f"Missing required env var {name}. "
            "Did you forget to import the .env from `tagtrail-bootstrap` into your deployment?"
        )
    return v


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _csv(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


@dataclass(frozen=True)
class PollerConfig:
    google_oauth_client_id: str
    google_oauth_client_secret: str
    google_drive_refresh_token: str
    tracker_ids: list[str]
    tracker_names: dict[str, str]
    tracker_colors: dict[str, str]
    poll_interval_seconds: int
    request_timeout_seconds: int
    folder_name: str

    @classmethod
    def from_env(cls) -> PollerConfig:
        names_raw = _optional("TAGTRAIL_TRACKER_NAMES", "{}")
        colors_raw = _optional("TAGTRAIL_TRACKER_COLORS", "{}")
        try:
            names = json.loads(names_raw)
            colors = json.loads(colors_raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "TAGTRAIL_TRACKER_NAMES / TAGTRAIL_TRACKER_COLORS must be JSON objects."
            ) from e

        return cls(
            # App-level OAuth client (Desktop type). Embedded in source, not a
            # user secret — see poller/oauth_client.py. Users only provide the
            # per-account refresh token below.
            google_oauth_client_id=oauth_client_id(),
            google_oauth_client_secret=oauth_client_secret(),
            google_drive_refresh_token=_required("GOOGLE_DRIVE_REFRESH_TOKEN"),
            # Optional override. When unset, the poller follows the SPA's
            # selection in TagTrail/config.json. When set, env wins and the
            # SPA picker becomes read-only with a banner.
            tracker_ids=_csv(_optional("TAGTRAIL_TRACKER_IDS", "")),
            tracker_names=names,
            tracker_colors=colors,
            poll_interval_seconds=int(_optional("TAGTRAIL_POLL_INTERVAL_SECONDS", "900")),
            request_timeout_seconds=int(_optional("TAGTRAIL_REQUEST_TIMEOUT_SECONDS", "90")),
            folder_name=_optional("TAGTRAIL_FOLDER", "TagTrail"),
        )

    def tracker_registry(self) -> dict[str, dict]:
        registry: dict[str, dict] = {}
        for tid in self.tracker_ids:
            entry: dict = {}
            if tid in self.tracker_names:
                entry["name"] = self.tracker_names[tid]
            if tid in self.tracker_colors:
                entry["color"] = self.tracker_colors[tid]
            registry[tid] = entry
        return registry
