from datetime import datetime, timedelta, timezone

from poller.models import Fix


def test_to_json_record_omits_acc_when_none():
    fix = Fix(
        id="abc",
        ts=datetime(2026, 5, 28, 9, 14, 33, tzinfo=timezone.utc),
        lat=51.5072,
        lng=-0.1276,
    )
    rec = fix.to_json_record()
    assert rec == {
        "id": "abc",
        "ts": "2026-05-28T09:14:33Z",
        "lat": 51.5072,
        "lng": -0.1276,
        "src": "network",
    }


def test_to_json_record_includes_acc_when_given():
    fix = Fix(
        id="abc",
        ts=datetime(2026, 5, 28, 9, 14, 33, tzinfo=timezone.utc),
        lat=51.5072,
        lng=-0.1276,
        acc=32.0,
    )
    rec = fix.to_json_record()
    assert rec["acc"] == 32.0


def test_utc_date_key_is_utc_not_local():
    fix = Fix(
        id="abc",
        ts=datetime(2026, 5, 28, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5))),
        lat=0,
        lng=0,
    )
    assert fix.utc_date_key == "2026-05-29"


def test_to_json_record_converts_non_utc_to_utc():
    fix = Fix(
        id="abc",
        ts=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        lat=0,
        lng=0,
    )
    rec = fix.to_json_record()
    assert rec["ts"] == "2026-05-28T10:00:00Z"
