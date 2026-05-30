"""In-memory tests for the de-dup + status.json behaviour of `DriveWriter`.

We don't want to hit Google in tests, so we build a fake "Drive" — a dict of
`file_id -> bytes` plus a child index — and subclass `DriveWriter` to swap the
side-effectful methods. That keeps the dedup / no-op-skip / status logic under
real coverage while everything else (auth, HTTP, the discovery client) stays
out of the test path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("googleapiclient")

from poller.drive_writer import (  # noqa: E402
    CONFIG_NAME,
    MANIFEST_NAME,
    NDJSON_MIME,
    SELECTION_MODE_ENV,
    SELECTION_MODE_SPA,
    STATUS_NAME,
    DriveWriter,
)
from poller.models import Fix  # noqa: E402


class FakeDriveWriter(DriveWriter):
    """A `DriveWriter` whose Drive calls are backed by an in-memory dict.

    We deliberately skip `DriveWriter.__init__` (no auth, no service build) and
    populate the attributes by hand.
    """

    def __init__(self, tracker_registry: dict[str, dict] | None = None) -> None:
        self._service = None  # type: ignore[assignment]
        self._folder_name = "TagTrail"
        self._folder_id = "folder-1"
        self._manifest_id = None
        self._status_id = None
        self._config_id = None
        self._day_file_ids = {}
        self._tracker_registry = dict(tracker_registry or {})

        self.files: dict[str, bytes] = {}
        self.names: dict[str, str] = {}
        self.upload_count = 0

    # ----- side-effect-free overrides -----

    def _ensure_folder(self) -> str:
        return self._folder_id  # type: ignore[return-value]

    def _find_child(self, name: str):  # type: ignore[override]
        for fid, fname in self.names.items():
            if fname == name:
                return fid
        return None

    def _download_bytes(self, file_id: str) -> bytes:
        return self.files[file_id]

    def _upload_replace(self, file_id: str, data: bytes, mime: str) -> None:
        self.files[file_id] = data
        self.upload_count += 1

    def _create_text_file(self, name: str, data: bytes, mime: str) -> str:
        fid = f"file-{len(self.files) + 1}"
        self.files[fid] = data
        self.names[fid] = name
        return fid


def _fix(id_: str, hms: str, lat: float = 51.5, lng: float = -0.1) -> Fix:
    h, m, s = (int(x) for x in hms.split(":"))
    return Fix(
        id=id_,
        ts=datetime(2026, 5, 28, h, m, s, tzinfo=timezone.utc),
        lat=lat,
        lng=lng,
    )


def _day_lines(writer: FakeDriveWriter, day: str) -> list[dict]:
    fid = writer._find_child(f"{day}.ndjson")
    assert fid is not None
    raw = writer.files[fid].decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _status(writer: FakeDriveWriter) -> dict:
    fid = writer._find_child(STATUS_NAME)
    assert fid is not None
    return json.loads(writer.files[fid].decode("utf-8"))


def test_first_write_appends_fix_and_writes_status_and_manifest():
    w = FakeDriveWriter()
    report = w.write_fixes([_fix("tag-a", "10:00:00")])

    assert report == {
        "received": 1,
        "new": 1,
        "days_touched": 1,
        "last_check_at": report["last_check_at"],
        "last_new_fix_at": "2026-05-28T10:00:00Z",
    }
    assert _day_lines(w, "2026-05-28") == [
        {
            "id": "tag-a",
            "ts": "2026-05-28T10:00:00Z",
            "lat": 51.5,
            "lng": -0.1,
            "src": "network",
        }
    ]
    assert w._find_child(MANIFEST_NAME) is not None
    status = _status(w)
    assert status["lastCheckAt"] == report["last_check_at"]
    assert status["lastNewFixAt"] == "2026-05-28T10:00:00Z"
    assert status["fixesReceived"] == 1
    assert status["fixesNew"] == 1


def test_second_write_with_same_fix_is_a_no_op_on_day_file_and_manifest():
    w = FakeDriveWriter()
    f = _fix("tag-a", "10:00:00")
    w.write_fixes([f])
    day_fid = w._find_child("2026-05-28.ndjson")
    manifest_fid = w._find_child(MANIFEST_NAME)
    day_bytes_before = w.files[day_fid]
    manifest_bytes_before = w.files[manifest_fid]
    uploads_before = w.upload_count

    report = w.write_fixes([f])

    assert report["received"] == 1
    assert report["new"] == 0
    assert report["days_touched"] == 0
    # Day file and manifest weren't rewritten.
    assert w.files[day_fid] == day_bytes_before
    assert w.files[manifest_fid] == manifest_bytes_before
    # Only the status file should have been re-uploaded.
    assert w.upload_count == uploads_before + 1


def test_idle_cycle_preserves_last_new_fix_at():
    w = FakeDriveWriter()
    w.write_fixes([_fix("tag-a", "10:00:00")])
    assert _status(w)["lastNewFixAt"] == "2026-05-28T10:00:00Z"

    # Find Hub returns nothing for several cycles (sparse network).
    w.write_fixes([])
    w.write_fixes([])
    s = _status(w)
    # lastCheckAt advances every cycle…
    assert s["fixesReceived"] == 0
    assert s["fixesNew"] == 0
    # …but lastNewFixAt is preserved across idle cycles.
    assert s["lastNewFixAt"] == "2026-05-28T10:00:00Z"


def test_batch_dedupes_within_itself():
    """The Nova response can repeat the same report across the tracker's
    `recentLocation` and `networkLocations` arrays. Our adapter would surface
    those as duplicate Fix entries; the writer must collapse them."""
    w = FakeDriveWriter()
    f = _fix("tag-a", "10:00:00")
    report = w.write_fixes([f, f, f])
    assert report["received"] == 3
    assert report["new"] == 1
    assert len(_day_lines(w, "2026-05-28")) == 1


def test_new_fix_is_appended_alongside_existing():
    w = FakeDriveWriter()
    w.write_fixes([_fix("tag-a", "10:00:00")])
    report = w.write_fixes([_fix("tag-a", "10:00:00"), _fix("tag-a", "10:15:00")])
    assert report["received"] == 2
    assert report["new"] == 1
    assert report["last_new_fix_at"] == "2026-05-28T10:15:00Z"
    lines = _day_lines(w, "2026-05-28")
    assert [l["ts"] for l in lines] == [
        "2026-05-28T10:00:00Z",
        "2026-05-28T10:15:00Z",
    ]


def _config(writer: FakeDriveWriter) -> dict | None:
    fid = writer._find_child(CONFIG_NAME)
    if fid is None:
        return None
    return json.loads(writer.files[fid].decode("utf-8"))


def test_config_seeded_with_all_available_on_first_cycle():
    """If config.json doesn't exist, the poller should seed selection with
    everything we can see, so a fresh user gets data on first run without
    needing to open the SPA at all."""
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys"), ("tag-b", "Backpack")]
    poll_ids, mode = w.read_poll_targets(available=avail, env_tracker_ids=None)
    assert mode == SELECTION_MODE_SPA
    assert set(poll_ids) == {"tag-a", "tag-b"}

    w.commit_config(available=avail, env_tracker_ids=None)
    cfg = _config(w)
    assert cfg is not None
    assert cfg["selectionMode"] == SELECTION_MODE_SPA
    assert set(cfg["selectedTrackerIds"]) == {"tag-a", "tag-b"}
    assert cfg["availableTrackers"] == [
        {"id": "tag-a", "name": "Keys"},
        {"id": "tag-b", "name": "Backpack"},
    ]


def test_env_override_wins_and_disables_spa_mode():
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys"), ("tag-b", "Backpack")]
    poll_ids, mode = w.read_poll_targets(
        available=avail, env_tracker_ids=["tag-a"]
    )
    assert mode == SELECTION_MODE_ENV
    assert poll_ids == ["tag-a"]

    w.commit_config(available=avail, env_tracker_ids=["tag-a"])
    cfg = _config(w)
    assert cfg["selectionMode"] == SELECTION_MODE_ENV
    assert cfg["selectedTrackerIds"] == ["tag-a"]


def test_spa_selection_is_preserved_across_cycles():
    """The poller must not clobber the SPA's selectedTrackerIds when it
    refreshes availableTrackers."""
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys"), ("tag-b", "Backpack")]
    w.commit_config(available=avail, env_tracker_ids=None)

    # Simulate the SPA writing a narrower selection between cycles.
    fid = w._find_child(CONFIG_NAME)
    cfg = json.loads(w.files[fid].decode("utf-8"))
    cfg["selectedTrackerIds"] = ["tag-b"]
    w.files[fid] = json.dumps(cfg).encode("utf-8")

    poll_ids, mode = w.read_poll_targets(available=avail, env_tracker_ids=None)
    assert mode == SELECTION_MODE_SPA
    assert poll_ids == ["tag-b"]

    w.commit_config(available=avail, env_tracker_ids=None)
    assert _config(w)["selectedTrackerIds"] == ["tag-b"]


def test_empty_spa_selection_means_poll_nothing():
    """If the user unchecks everything in the SPA, we trust that — even
    though "no trackers" is unusual. Re-seeding to "all" here would silently
    undo the user's explicit empty selection."""
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys")]
    w.commit_config(available=avail, env_tracker_ids=None)

    fid = w._find_child(CONFIG_NAME)
    cfg = json.loads(w.files[fid].decode("utf-8"))
    cfg["selectedTrackerIds"] = []
    w.files[fid] = json.dumps(cfg).encode("utf-8")

    poll_ids, mode = w.read_poll_targets(available=avail, env_tracker_ids=None)
    assert mode == SELECTION_MODE_SPA
    assert poll_ids == []


def test_unpaired_tracker_is_filtered_from_poll_targets():
    """A tag that has fallen off the device list shouldn't waste a Nova
    request, but we keep it in selectedTrackerIds in case it comes back."""
    w = FakeDriveWriter()
    avail_full = [("tag-a", "Keys"), ("tag-b", "Backpack")]
    w.commit_config(available=avail_full, env_tracker_ids=None)

    # Next cycle: tag-b has disappeared from Find Hub.
    avail_partial = [("tag-a", "Keys")]
    poll_ids, _ = w.read_poll_targets(
        available=avail_partial, env_tracker_ids=None
    )
    assert poll_ids == ["tag-a"]
    # But the stored selection still mentions tag-b.
    w.commit_config(available=avail_partial, env_tracker_ids=None)
    cfg = _config(w)
    assert cfg["selectedTrackerIds"] == ["tag-a", "tag-b"]
    assert [t["id"] for t in cfg["availableTrackers"]] == ["tag-a"]


def test_empty_available_list_does_not_zero_out_selection():
    """If request_device_list() transiently fails (and returns []), we must
    keep polling whatever we polled last cycle. Otherwise one bad Google
    response would silently halt the system."""
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys")]
    w.commit_config(available=avail, env_tracker_ids=None)

    poll_ids, _ = w.read_poll_targets(available=[], env_tracker_ids=None)
    assert poll_ids == ["tag-a"]


def test_commit_config_is_noop_when_nothing_changed():
    """Idle steady state should not churn Drive."""
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys")]
    w.commit_config(available=avail, env_tracker_ids=None)
    uploads_before = w.upload_count
    fid_before = w._find_child(CONFIG_NAME)
    bytes_before = w.files[fid_before]

    w.commit_config(available=avail, env_tracker_ids=None)
    assert w.upload_count == uploads_before
    assert w.files[fid_before] == bytes_before


def test_spa_edit_during_poll_is_preserved_by_commit():
    """The race we actually care about: SPA writes a new selection between
    read_poll_targets (start of cycle) and commit_config (end of cycle).
    commit_config must re-read and pick up the SPA's value, not overwrite it
    with the stale read."""
    w = FakeDriveWriter()
    avail = [("tag-a", "Keys"), ("tag-b", "Backpack")]
    w.commit_config(available=avail, env_tracker_ids=None)  # seed: both selected
    assert set(_config(w)["selectedTrackerIds"]) == {"tag-a", "tag-b"}

    # Start of cycle.
    poll_ids, _ = w.read_poll_targets(available=avail, env_tracker_ids=None)
    assert set(poll_ids) == {"tag-a", "tag-b"}

    # ... user toggles in SPA mid-poll ...
    fid = w._find_child(CONFIG_NAME)
    cfg = json.loads(w.files[fid].decode("utf-8"))
    cfg["selectedTrackerIds"] = ["tag-b"]
    w.files[fid] = json.dumps(cfg).encode("utf-8")

    # End of cycle: poller commits. Should NOT clobber the SPA write.
    w.commit_config(available=avail, env_tracker_ids=None)
    assert _config(w)["selectedTrackerIds"] == ["tag-b"]


def test_writer_repairs_stale_ndjson_with_dedupe_on_read():
    """If an older poller (pre-fix) left duplicates in the day file, we
    shouldn't re-append duplicates of those — but we also shouldn't rewrite
    the file just to clean them up (that's not the writer's job; the SPA
    parser already dedupes on read)."""
    w = FakeDriveWriter()
    # Seed a day file that already contains the same (id, ts) twice.
    rec = {
        "id": "tag-a",
        "ts": "2026-05-28T10:00:00Z",
        "lat": 51.5,
        "lng": -0.1,
        "src": "network",
    }
    line = json.dumps(rec, separators=(",", ":"))
    seeded = (line + "\n" + line + "\n").encode("utf-8")
    fid = w._create_text_file("2026-05-28.ndjson", seeded, NDJSON_MIME)
    w._day_file_ids["2026-05-28"] = fid

    report = w.write_fixes([_fix("tag-a", "10:00:00")])

    assert report["new"] == 0
    # Existing dupes are not collapsed (writer is not a janitor).
    assert w.files[fid] == seeded
