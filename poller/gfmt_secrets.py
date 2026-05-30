"""Ship GoogleFindMyTools' `Auth/secrets.json` through an env var.

Why this module exists
----------------------
GFMT writes its credential bundle (and an FCM token, owner keys, etc) to
``<gfmt_dir>/Auth/secrets.json`` after the interactive bootstrap. Production
deploys (Fly volumes, GitHub Actions, etc) traditionally need that file on
disk, which means SFTP-ing it onto a persistent volume — a horrible step for
a non-technical user.

Instead we let bootstrap base64-encode that file once and write it into
``poller.env`` as ``GFMT_SECRETS_JSON_B64``. On poller start, if the GFMT
secrets file is missing (fresh container, fresh deploy), we materialize it
from the env var **before** GFMT is imported. The file is only created when
missing — once GFMT is running it owns the file and writes cached values into
it (FCM tokens, etc), and we don't want to overwrite those mid-run.

Public API
~~~~~~~~~~
- ``materialize_if_missing()``: idempotent, called from the adapter before
  the first GFMT import.
- ``encode_current() -> str``: read the current on-disk secrets and return the
  base64 blob, for bootstrap to drop into the env file.
- ``target_path() -> Path``: where GFMT expects the file to live.

GFMT itself hardcodes the path in ``Auth/token_cache.py``:
``os.path.join(<dir-of-token_cache>, 'secrets.json')``. So the materialize
target is always ``<gfmt_dir>/Auth/secrets.json``; the legacy
``GFMT_SECRETS_PATH`` env var is unused by GFMT and we ignore it here too.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from pathlib import Path

from .install_gfmt import gfmt_path

logger = logging.getLogger(__name__)

ENV_VAR = "GFMT_SECRETS_JSON_B64"
_SECRETS_RELATIVE = ("Auth", "secrets.json")

# Keys GFMT must cache for the poller to decrypt location reports headlessly.
# Without both, the poller would try to open Chrome in CI (impossible) — so we
# treat a blob missing either as unusable. GFMT stores them at the top level of
# secrets.json and writes ``null`` when its key-backup flow is interrupted.
_REQUIRED_E2E_KEYS = ("shared_key", "owner_key")


class GfmtSecretsError(RuntimeError):
    """The env-provided secrets blob couldn't be decoded or written."""


def target_path() -> Path | None:
    """Return where GFMT will look for ``secrets.json``, or ``None`` if GFMT
    isn't installed yet (e.g. running before ``tagtrail-install-gfmt``)."""
    p = gfmt_path()
    if p is None:
        return None
    return p.joinpath(*_SECRETS_RELATIVE)


def materialize_if_missing() -> bool:
    """If ``GFMT_SECRETS_JSON_B64`` is set and the on-disk file is missing,
    decode the env var and write it. Returns ``True`` if we created the file.

    Safe to call repeatedly. Once the file exists we leave it alone — GFMT
    treats it as a mutable cache (new FCM tokens, refreshed owner keys) and
    overwriting it from a stale env blob would silently undo those updates.
    """
    blob = os.environ.get(ENV_VAR)
    if not blob:
        return False

    dest = target_path()
    if dest is None:
        # GFMT isn't installed yet. The caller (findhub_adapter) will raise a
        # FindHubError with the right message after this; we just no-op.
        return False

    if dest.exists():
        return False

    try:
        decoded = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as e:
        raise GfmtSecretsError(
            f"${ENV_VAR} is not valid base64. Re-run `tagtrail-bootstrap` to "
            "regenerate it."
        ) from e

    if not decoded.lstrip().startswith(b"{"):
        # Cheap sanity check — secrets.json is always a JSON object.
        raise GfmtSecretsError(
            f"${ENV_VAR} decoded to something that isn't a JSON object. "
            "Did the value get truncated when copy-pasting into your deploy?"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Be paranoid about file permissions: this blob includes long-lived
    # credentials and an owner key.
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(decoded)
    except Exception:
        # Best-effort cleanup so a partial write doesn't poison the next start.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    logger.info(
        "Materialized GFMT secrets from $%s into %s (file was missing).",
        ENV_VAR,
        dest,
    )
    return True


def encode_current() -> str:
    """Read the GFMT secrets file from disk and return a base64 blob suitable
    for ``GFMT_SECRETS_JSON_B64=...`` in the user's env file."""
    src = target_path()
    if src is None or not src.is_file():
        raise GfmtSecretsError(
            "No GFMT secrets.json on disk yet. Run `tagtrail-bootstrap` first."
        )
    return base64.b64encode(src.read_bytes()).decode("ascii")


def missing_e2e_keys() -> list[str]:
    """Return which E2E keys are absent or ``null`` in the on-disk secrets.json.

    An empty list means both ``shared_key`` and ``owner_key`` are present and
    truthy — i.e. the blob is safe to use in a headless CI poller. Used by the
    auth CLI to fail fast instead of shipping a blob that can't decrypt reports.
    """
    src = target_path()
    if src is None or not src.is_file():
        return list(_REQUIRED_E2E_KEYS)
    try:
        data = json.loads(src.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return list(_REQUIRED_E2E_KEYS)
    if not isinstance(data, dict):
        return list(_REQUIRED_E2E_KEYS)
    return [k for k in _REQUIRED_E2E_KEYS if not data.get(k)]
