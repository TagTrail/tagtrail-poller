"""Tests for the base64-secrets shim that lets the GFMT credential bundle
travel as an env var instead of a mounted file.

These tests fake `gfmt_path()` so they don't need GFMT actually installed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from poller import gfmt_secrets


@pytest.fixture
def fake_gfmt(monkeypatch, tmp_path):
    """Pretend GFMT is installed at `tmp_path/gfmt/` and that Auth/ exists."""
    gfmt = tmp_path / "gfmt"
    (gfmt / "Auth").mkdir(parents=True)
    monkeypatch.setattr(gfmt_secrets, "gfmt_path", lambda: gfmt)
    return gfmt


def _b64(payload: dict | bytes) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return base64.b64encode(raw).decode("ascii")


def test_target_path_resolves_under_auth_dir(fake_gfmt):
    assert gfmt_secrets.target_path() == fake_gfmt / "Auth" / "secrets.json"


def test_target_path_is_none_when_gfmt_missing(monkeypatch):
    monkeypatch.setattr(gfmt_secrets, "gfmt_path", lambda: None)
    assert gfmt_secrets.target_path() is None


def test_materialize_creates_file_when_missing(fake_gfmt, monkeypatch):
    payload = {"sample_token": "abc123"}
    monkeypatch.setenv(gfmt_secrets.ENV_VAR, _b64(payload))

    created = gfmt_secrets.materialize_if_missing()
    assert created is True

    out = fake_gfmt / "Auth" / "secrets.json"
    assert out.is_file()
    assert json.loads(out.read_text()) == payload


def test_materialize_does_not_overwrite_existing_file(fake_gfmt, monkeypatch):
    """GFMT mutates secrets.json at runtime (FCM tokens etc). Overwriting
    those updates from a stale env blob would silently corrupt the cache."""
    pre_existing = {"cached_by_gfmt": "leave_me_alone"}
    out = fake_gfmt / "Auth" / "secrets.json"
    out.write_text(json.dumps(pre_existing))

    monkeypatch.setenv(gfmt_secrets.ENV_VAR, _b64({"different": "value"}))

    created = gfmt_secrets.materialize_if_missing()
    assert created is False
    assert json.loads(out.read_text()) == pre_existing


def test_materialize_noop_without_env(fake_gfmt, monkeypatch):
    monkeypatch.delenv(gfmt_secrets.ENV_VAR, raising=False)
    created = gfmt_secrets.materialize_if_missing()
    assert created is False
    assert not (fake_gfmt / "Auth" / "secrets.json").exists()


def test_materialize_noop_when_gfmt_not_installed(monkeypatch):
    """If GFMT itself isn't installed yet, the caller (findhub_adapter) will
    raise its own clear error. We must not raise here."""
    monkeypatch.setattr(gfmt_secrets, "gfmt_path", lambda: None)
    monkeypatch.setenv(gfmt_secrets.ENV_VAR, _b64({"x": 1}))
    assert gfmt_secrets.materialize_if_missing() is False


def test_materialize_rejects_invalid_base64(fake_gfmt, monkeypatch):
    monkeypatch.setenv(gfmt_secrets.ENV_VAR, "not-base64!!!")
    with pytest.raises(gfmt_secrets.GfmtSecretsError):
        gfmt_secrets.materialize_if_missing()


def test_materialize_rejects_non_json_payload(fake_gfmt, monkeypatch):
    """Cheap sanity check: catches truncated copy-pastes that happen to still
    decode (e.g. trailing newline trimmed) but aren't a JSON object."""
    monkeypatch.setenv(gfmt_secrets.ENV_VAR, _b64(b"not a json object"))
    with pytest.raises(gfmt_secrets.GfmtSecretsError):
        gfmt_secrets.materialize_if_missing()


def test_materialize_file_permissions_are_strict(fake_gfmt, monkeypatch):
    monkeypatch.setenv(gfmt_secrets.ENV_VAR, _b64({"x": 1}))
    gfmt_secrets.materialize_if_missing()
    out = fake_gfmt / "Auth" / "secrets.json"
    mode = out.stat().st_mode & 0o777
    # Owner-read/write only. We never want group/world readable.
    assert mode == 0o600, oct(mode)


def test_encode_current_round_trips(fake_gfmt):
    src = fake_gfmt / "Auth" / "secrets.json"
    payload = {"hello": "world", "n": 42}
    src.write_text(json.dumps(payload))

    encoded = gfmt_secrets.encode_current()
    assert json.loads(base64.b64decode(encoded)) == payload


def test_encode_current_raises_without_file(fake_gfmt):
    with pytest.raises(gfmt_secrets.GfmtSecretsError):
        gfmt_secrets.encode_current()
