"""Read-append-upload to the user's Google Drive.

Maintains:
    TagTrail/
        manifest.json            # registry of trackers + index of day files
        status.json              # poller liveness for the SPA header
        YYYY-MM-DD.ndjson        # one fix per line, UTC date

The Drive API has no atomic append. The pattern for day files is:
    1. Find or create the TagTrail folder.
    2. Find or create the day file.
    3. Download its current bytes.
    4. De-duplicate the incoming records against the existing `(id, ts)` set.
    5. If nothing new, skip the upload entirely.
    6. Otherwise upload-update (PATCH /upload/drive/v3/files/{id}) with the
       existing bytes + newly appended lines.
    7. Update manifest.json only when a new day appeared or the tracker
       registry changed (avoids rewriting the manifest on idle cycles).
    8. Always write status.json so the SPA can prove the poller is alive even
       when no new fixes were appended.

A single poller process is the only writer, so this is race-free.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from .models import Fix

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DEFAULT_FOLDER_NAME = "TagTrail"
MANIFEST_NAME = "manifest.json"
STATUS_NAME = "status.json"
CONFIG_NAME = "config.json"
FOLDER_MIME = "application/vnd.google-apps.folder"
NDJSON_MIME = "application/x-ndjson"
JSON_MIME = "application/json"

SELECTION_MODE_ENV = "env"
SELECTION_MODE_SPA = "spa"


class DriveWriterError(RuntimeError):
    """Drive-side failure. The caller should back off and retry on next interval."""


def _build_drive_service(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> Any:
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=DRIVE_SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class DriveWriter:
    """Owns the TagTrail folder lifecycle and the read-append-upload writes."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_name: str = DEFAULT_FOLDER_NAME,
        tracker_registry: dict[str, dict] | None = None,
    ) -> None:
        """tracker_registry maps canonical_id -> {"name": str, "color": str}."""
        self._service = _build_drive_service(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        self._folder_name = folder_name
        self._folder_id: str | None = None
        self._manifest_id: str | None = None
        self._status_id: str | None = None
        self._config_id: str | None = None
        self._day_file_ids: dict[str, str] = {}
        self._tracker_registry = dict(tracker_registry or {})

    # ----- folder / file lookups -----

    def _find_folder(self) -> str | None:
        q = (
            f"name = '{self._folder_name}' "
            f"and mimeType = '{FOLDER_MIME}' "
            "and trashed = false "
            "and 'root' in parents"
        )
        resp = (
            self._service.files()
            .list(q=q, spaces="drive", fields="files(id,name)", pageSize=10)
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self) -> str:
        meta = {"name": self._folder_name, "mimeType": FOLDER_MIME, "parents": ["root"]}
        f = self._service.files().create(body=meta, fields="id").execute()
        return f["id"]

    def _ensure_folder(self) -> str:
        if self._folder_id is None:
            self._folder_id = self._find_folder() or self._create_folder()
        return self._folder_id

    def _find_child(self, name: str) -> str | None:
        folder_id = self._ensure_folder()
        q = (
            f"name = '{name}' "
            "and trashed = false "
            f"and '{folder_id}' in parents"
        )
        resp = (
            self._service.files()
            .list(q=q, spaces="drive", fields="files(id,name)", pageSize=10)
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def _download_bytes(self, file_id: str) -> bytes:
        buf = io.BytesIO()
        request = self._service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    def _upload_replace(self, file_id: str, data: bytes, mime: str) -> None:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        self._service.files().update(fileId=file_id, media_body=media).execute()

    def _create_text_file(self, name: str, data: bytes, mime: str) -> str:
        folder_id = self._ensure_folder()
        meta = {"name": name, "parents": [folder_id], "mimeType": mime}
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        f = (
            self._service.files()
            .create(body=meta, media_body=media, fields="id")
            .execute()
        )
        return f["id"]

    # ----- manifest -----

    def _read_manifest(self) -> dict:
        if self._manifest_id is None:
            self._manifest_id = self._find_child(MANIFEST_NAME)
        if self._manifest_id is None:
            return self._default_manifest()
        try:
            raw = self._download_bytes(self._manifest_id)
            return json.loads(raw.decode("utf-8")) if raw else self._default_manifest()
        except (HttpError, json.JSONDecodeError) as e:
            logger.warning("Failed to read existing manifest, starting fresh: %s", e)
            return self._default_manifest()

    def _default_manifest(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "trackers": [],
            "days": [],
            "updatedAt": _now_iso(),
        }

    def _write_manifest(self, manifest: dict) -> None:
        data = json.dumps(manifest, sort_keys=False, indent=2).encode("utf-8")
        if self._manifest_id is None:
            self._manifest_id = self._create_text_file(MANIFEST_NAME, data, JSON_MIME)
        else:
            self._upload_replace(self._manifest_id, data, JSON_MIME)

    def _merge_trackers(self, manifest: dict, fixes: Iterable[Fix]) -> None:
        """Reconcile `manifest.trackers` with the configured registry + observed fix IDs.

        Policy:
        - If `TAGTRAIL_TRACKER_IDS` is set (registry non-empty), the manifest is
          rebuilt to exactly: (configured IDs) ∪ (IDs that appeared in this cycle).
          Stale entries (e.g. from a tag that was unpaired and re-paired with a
          new canonical ID) get pruned. Configured `name` / `color` win.
        - If the registry is empty (auto-discovery mode), behave additively: keep
          all existing entries and add anything new we saw fixes for.
        """
        existing_by_id = {t["id"]: t for t in manifest.get("trackers", [])}
        fix_ids = {f.id for f in fixes}

        if self._tracker_registry:
            next_by_id: dict[str, dict] = {}
            for tid, meta in self._tracker_registry.items():
                entry = existing_by_id.get(tid, {"id": tid})
                entry["name"] = meta.get("name", entry.get("name", tid[:8]))
                entry["color"] = meta.get("color", entry.get("color", _color_for_id(tid)))
                next_by_id[tid] = entry
            for fid in fix_ids:
                if fid in next_by_id:
                    continue
                next_by_id[fid] = existing_by_id.get(
                    fid,
                    {"id": fid, "name": fid[:8], "color": _color_for_id(fid)},
                )
            manifest["trackers"] = list(next_by_id.values())
        else:
            for fid in fix_ids:
                if fid in existing_by_id:
                    continue
                existing_by_id[fid] = {
                    "id": fid,
                    "name": fid[:8],
                    "color": _color_for_id(fid),
                }
            manifest["trackers"] = list(existing_by_id.values())

    # ----- config.json (tracker picker) -----

    def _read_config(self) -> dict:
        if self._config_id is None:
            self._config_id = self._find_child(CONFIG_NAME)
        if self._config_id is None:
            return {}
        try:
            raw = self._download_bytes(self._config_id)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (HttpError, json.JSONDecodeError) as e:
            logger.warning("Failed to read existing config, starting fresh: %s", e)
            return {}

    def _write_config(self, payload: dict) -> None:
        data = json.dumps(payload, sort_keys=False, indent=2).encode("utf-8")
        if self._config_id is None:
            self._config_id = self._create_text_file(CONFIG_NAME, data, JSON_MIME)
        else:
            self._upload_replace(self._config_id, data, JSON_MIME)

    def read_poll_targets(
        self,
        *,
        available: list[tuple[str, str]],
        env_tracker_ids: list[str] | None,
    ) -> tuple[list[str], str]:
        """Read ``TagTrail/config.json`` and decide which IDs to poll this
        cycle. Does **not** write — see :meth:`commit_config` for that.

        Selection precedence:
          1. ``env_tracker_ids`` (``TAGTRAIL_TRACKER_IDS`` set) wins
             unconditionally. ``selectionMode`` reports ``"env"`` so the SPA
             can show a banner explaining its checkboxes won't change polling
             until the env var is unset.
          2. Otherwise the SPA's ``selectedTrackerIds`` is the truth.
          3. If neither exists yet, default to "all available" so the user
             sees data on first run without needing to touch the SPA.
        """
        avail_ids = [aid for (aid, _name) in available]
        existing = self._read_config()
        selection, selection_mode = _decide_selection(
            available_ids=avail_ids,
            existing=existing,
            env_tracker_ids=env_tracker_ids,
        )
        return _intersect_available(selection, avail_ids), selection_mode

    def commit_config(
        self,
        *,
        available: list[tuple[str, str]],
        env_tracker_ids: list[str] | None,
    ) -> None:
        """Persist the up-to-date device inventory to ``TagTrail/config.json``.

        Called at the **end** of the cycle, after the slow poll, so we
        minimise the race with the SPA: if the user toggled a checkbox while
        we were polling, this re-read picks up their value before we
        overwrite. The remaining race window (read here → write below) is
        single-digit milliseconds. Splitting this from :meth:`read_poll_targets`
        is what makes that small.

        Skips the Drive write when nothing material changed (idle steady
        state).
        """
        avail_ids = [aid for (aid, _name) in available]
        existing = self._read_config()
        selection, selection_mode = _decide_selection(
            available_ids=avail_ids,
            existing=existing,
            env_tracker_ids=env_tracker_ids,
        )

        new_payload: dict = {
            "schemaVersion": SCHEMA_VERSION,
            "availableTrackers": [
                {"id": aid, "name": name} for (aid, name) in available
            ],
            "selectedTrackerIds": selection,
            "selectionMode": selection_mode,
        }

        if not _config_meaningfully_changed(existing, new_payload):
            return
        new_payload["updatedAt"] = _now_iso()
        try:
            self._write_config(new_payload)
        except HttpError as e:
            raise DriveWriterError(f"Drive config write failed: {e}") from e

    # ----- day file append -----

    def _ensure_day_file(self, date_key: str) -> str:
        fid = self._day_file_ids.get(date_key)
        if fid is not None:
            return fid
        name = f"{date_key}.ndjson"
        fid = self._find_child(name)
        if fid is None:
            fid = self._create_text_file(name, b"", NDJSON_MIME)
        self._day_file_ids[date_key] = fid
        return fid

    def _append_day_file(self, date_key: str, records: list[dict]) -> int:
        """Dedup `records` against the existing day file by `(id, ts)`, append
        only the new ones, and skip the upload entirely if nothing's new.

        Returns the number of records actually appended.
        """
        if not records:
            return 0
        fid = self._ensure_day_file(date_key)
        existing = self._download_bytes(fid)

        seen: set[tuple[str, str]] = set()
        for line in existing.splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            rid = obj.get("id")
            rts = obj.get("ts")
            if isinstance(rid, str) and isinstance(rts, str):
                seen.add((rid, rts))

        new_lines: list[str] = []
        for rec in records:
            key = (rec["id"], rec["ts"])
            if key in seen:
                continue
            seen.add(key)
            new_lines.append(json.dumps(rec, separators=(",", ":")))

        if not new_lines:
            return 0

        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        appended = existing + ("\n".join(new_lines) + "\n").encode("utf-8")
        self._upload_replace(fid, appended, NDJSON_MIME)
        return len(new_lines)

    # ----- public API -----

    def write_fixes(self, fixes: list[Fix]) -> dict:
        """Append new fixes to the appropriate day files, refresh the manifest
        when something actually changed, and always write status.json.

        Find Hub's `locateTracker` returns whatever reports Google has cached for
        a tracker — typically the same reports across consecutive polls until a
        new sighting lands. So we de-duplicate by `(id, ts)`, both within this
        batch and against what's already on Drive, and skip the upload entirely
        when nothing's new. The SPA's parser already dedupes on read, but the
        Drive file would otherwise grow without bound.

        Returns a small report dict for logging (no PII):
            received        – fixes returned by Find Hub this cycle
            new             – fixes actually appended after dedup
            days_touched    – day files we appended to (0 if everything dup'd)
            last_check_at   – ISO-8601 UTC for this cycle
            last_new_fix_at – ISO-8601 UTC of latest `ts` we appended, or None
        """
        received = len(fixes)
        last_check_at = _now_iso()

        # Group by UTC date, deduping the in-memory batch against itself by (id, ts).
        # The Nova response can repeat the same report across the trackers'
        # recentLocation + networkLocations arrays, so this is non-trivial.
        seen_batch: set[tuple[str, str]] = set()
        by_day: dict[str, list[dict]] = {}
        for f in fixes:
            rec = f.to_json_record()
            key = (rec["id"], rec["ts"])
            if key in seen_batch:
                continue
            seen_batch.add(key)
            by_day.setdefault(f.utc_date_key, []).append(rec)

        new_total = 0
        days_appended: list[str] = []
        latest_new_ts: str | None = None
        try:
            for date_key in sorted(by_day.keys()):
                appended = self._append_day_file(date_key, by_day[date_key])
                if appended <= 0:
                    continue
                new_total += appended
                days_appended.append(date_key)
                # Track the highest fix `ts` that was actually appended this
                # cycle so the SPA can show "last new fix at X".
                for rec in by_day[date_key][-appended:]:
                    ts = rec["ts"]
                    if latest_new_ts is None or ts > latest_new_ts:
                        latest_new_ts = ts
        except HttpError as e:
            raise DriveWriterError(f"Drive append failed: {e}") from e

        # Only touch the manifest when something it cares about changed: a new
        # day file appeared, or the tracker registry diff'd. Otherwise idle
        # cycles still rewrite the manifest just to bump `updatedAt`.
        manifest = self._read_manifest()
        manifest.setdefault("schemaVersion", SCHEMA_VERSION)
        days = set(manifest.get("days", []))
        days_before = sorted(days)
        days.update(days_appended)
        days_after = sorted(days)

        trackers_before = json.dumps(manifest.get("trackers", []), sort_keys=True)
        self._merge_trackers(manifest, fixes)
        trackers_after = json.dumps(manifest.get("trackers", []), sort_keys=True)

        if days_before != days_after or trackers_before != trackers_after:
            manifest["days"] = days_after
            manifest["updatedAt"] = last_check_at
            try:
                self._write_manifest(manifest)
            except HttpError as e:
                raise DriveWriterError(f"Drive manifest write failed: {e}") from e

        try:
            last_new_fix_at = self._write_status(
                last_check_at=last_check_at,
                latest_new_ts=latest_new_ts,
                received=received,
                new_count=new_total,
            )
        except HttpError as e:
            raise DriveWriterError(f"Drive status write failed: {e}") from e

        return {
            "received": received,
            "new": new_total,
            "days_touched": len(days_appended),
            "last_check_at": last_check_at,
            "last_new_fix_at": last_new_fix_at,
        }

    # ----- status.json -----

    def _read_status(self) -> dict:
        if self._status_id is None:
            self._status_id = self._find_child(STATUS_NAME)
        if self._status_id is None:
            return {}
        try:
            raw = self._download_bytes(self._status_id)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (HttpError, json.JSONDecodeError) as e:
            logger.warning("Failed to read existing status, starting fresh: %s", e)
            return {}

    def _write_status(
        self,
        *,
        last_check_at: str,
        latest_new_ts: str | None,
        received: int,
        new_count: int,
    ) -> str | None:
        """Persist a small liveness blob for the SPA header.

        `lastNewFixAt` is preserved across idle cycles: if no new fix appeared
        this cycle, we keep whatever was previously stored. Returns the
        effective `lastNewFixAt` so the caller can log it.
        """
        prev = self._read_status()
        last_new_fix_at = latest_new_ts or prev.get("lastNewFixAt")

        status = {
            "schemaVersion": SCHEMA_VERSION,
            "lastCheckAt": last_check_at,
            "lastNewFixAt": last_new_fix_at,
            "fixesReceived": received,
            "fixesNew": new_count,
        }
        data = json.dumps(status, sort_keys=False, indent=2).encode("utf-8")
        if self._status_id is None:
            self._status_id = self._create_text_file(STATUS_NAME, data, JSON_MIME)
        else:
            self._upload_replace(self._status_id, data, JSON_MIME)
        return last_new_fix_at


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_PALETTE = [
    "#2b6cb0",
    "#dd6b20",
    "#38a169",
    "#d53f8c",
    "#805ad5",
    "#319795",
    "#d69e2e",
    "#e53e3e",
]


def _color_for_id(tracker_id: str) -> str:
    """Deterministic colour so unconfigured trackers still look distinct on the map."""
    h = sum(ord(c) for c in tracker_id) % len(_PALETTE)
    return _PALETTE[h]


def _config_meaningfully_changed(prev: dict, new: dict) -> bool:
    """Compare the fields the writer owns, ignoring ``updatedAt``. Used so
    idle cycles don't churn config.json with timestamp-only updates."""
    for key in ("availableTrackers", "selectedTrackerIds", "selectionMode", "schemaVersion"):
        if json.dumps(prev.get(key), sort_keys=True) != json.dumps(new.get(key), sort_keys=True):
            return True
    return False


def _decide_selection(
    *,
    available_ids: list[str],
    existing: dict,
    env_tracker_ids: list[str] | None,
) -> tuple[list[str], str]:
    """Resolve the selection precedence in one place so the start-of-cycle
    read and end-of-cycle commit can't disagree."""
    if env_tracker_ids:
        return list(env_tracker_ids), SELECTION_MODE_ENV
    prev = existing.get("selectedTrackerIds")
    if isinstance(prev, list) and all(isinstance(x, str) for x in prev):
        return list(prev), SELECTION_MODE_SPA
    return list(available_ids), SELECTION_MODE_SPA


def _intersect_available(selection: list[str], available_ids: list[str]) -> list[str]:
    """Drop IDs that are not currently in the device inventory so we don't
    waste a Nova request on a tag that was unpaired since the last cycle. If
    the device list looks empty (transient list failure), trust the stored
    selection rather than zeroing it out for one cycle."""
    if not available_ids:
        return list(selection)
    avail_set = set(available_ids)
    return [tid for tid in selection if tid in avail_set]
