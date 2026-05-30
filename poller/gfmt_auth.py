"""Minimal CLI for GFMT authentication only.

Runs the GoogleFindMyTools Chrome-based auth flow, then prints the base64
secrets blob the user can paste into tagtrail.org/setup or GitHub Actions.

Usage:
    tagtrail-gfmt-auth
"""

from __future__ import annotations

import logging
import sys

from .bootstrap import mint_drive_refresh_token
from .chrome_compat import apply_chromedriver_compat_patch, clear_uc_cache
from .gfmt_secrets import GfmtSecretsError, encode_current, missing_e2e_keys
from .install_gfmt import GFMT_PINNED_COMMIT, gfmt_path
from .oauth_client import client_id as oauth_client_id
from .oauth_client import client_secret as oauth_client_secret


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("tagtrail.gfmt-auth")

    if "--reset-chromedriver" in sys.argv:
        clear_uc_cache()

    p = gfmt_path()
    if p is None:
        logger.error(
            "GoogleFindMyTools not found. Run `tagtrail-install-gfmt` first "
            "(pinned commit %s).",
            GFMT_PINNED_COMMIT,
        )
        return 2
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

    apply_chromedriver_compat_patch()

    try:
        from NovaApi.ListDevices.nbe_list_devices import request_device_list
        from ProtoDecoders.decoder import get_canonic_ids, parse_device_list_protobuf
    except ImportError as e:
        logger.error("GoogleFindMyTools import failed: %s", e)
        logger.error("Re-run `tagtrail-install-gfmt` to repair the checkout.")
        return 2

    print()
    print("=" * 64)
    print("TagTrail — Find Hub authentication")
    print("=" * 64)
    print(
        "Chrome will open. Sign in with the Google account that owns\n"
        "your trackers. After sign-in, this script prints the secret\n"
        "you need."
    )
    print()

    result_hex = request_device_list()
    device_list = parse_device_list_protobuf(result_hex)
    canonic_ids = get_canonic_ids(device_list)

    print()
    print("Your trackers:")
    for idx, (name, cid) in enumerate(canonic_ids, start=1):
        print(f"  {idx:>2}. {name}  ->  {cid}")
    print()

    # Retrieve E2E owner key (triggers shared key flow if not cached).
    # This opens Chrome a second time for the key-backup consent screen.
    # Without this, the poller would try to open Chrome in CI — which fails.
    print("=" * 64)
    print("Step 2: Retrieving end-to-end encryption keys")
    print("=" * 64)
    print(
        "Chrome will open one more time. Sign in with the same account\n"
        "to unlock your tracker encryption keys. This is a one-time step."
    )
    print()

    # GFMT's shared_key_retrieval.py calls input("Press Enter") which blocks the
    # flow. Patch it to auto-continue.
    import KeyBackup.shared_key_retrieval as skr

    _orig = skr._retrieve_shared_key

    def _auto_retrieve():
        import builtins
        orig_input = builtins.input
        builtins.input = lambda *a, **k: ""
        try:
            return _orig()
        finally:
            builtins.input = orig_input

    skr._retrieve_shared_key = _auto_retrieve

    from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key

    # GFMT's Chrome key-backup flow is flaky (it sometimes dies with "no such
    # window" before sign-in completes, swallows the error, and caches a null
    # shared_key). Retry a few times, checking the on-disk cache after each try.
    _E2E_ATTEMPTS = 3
    for attempt in range(1, _E2E_ATTEMPTS + 1):
        try:
            get_owner_key()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "E2E key retrieval attempt %d/%d failed: %s",
                attempt,
                _E2E_ATTEMPTS,
                e,
            )
        if not missing_e2e_keys():
            logger.info("E2E keys retrieved and cached in secrets.json.")
            break
        if attempt < _E2E_ATTEMPTS:
            logger.warning(
                "E2E keys not cached yet. Retrying — when the Chrome window "
                "opens, finish signing in and DO NOT close it until you see "
                "'Received Shared Key'."
            )

    # Hard stop if the keys still aren't there. Shipping a blob without them
    # produces a poller that authenticates fine but then can't decrypt any
    # location report in CI (it would try to open Chrome, which fails). Better
    # to fail loudly here than to hand the user a blob that dies 15 min later.
    missing = missing_e2e_keys()
    if missing:
        logger.error(
            "Failed to retrieve the end-to-end encryption keys (%s) after %d "
            "attempts. This is required before the poller can run headlessly.\n"
            "\n"
            "This usually means the second Chrome window closed early or sign-in "
            "didn't finish. Fixes to try:\n"
            "  1. Quit ALL Chrome windows, then re-run `tagtrail-gfmt-auth`.\n"
            "  2. When the second Chrome window opens, complete sign-in and leave "
            "it alone until the terminal prints 'Received Shared Key'.\n"
            "  3. If it keeps failing, run `tagtrail-gfmt-auth --reset-chromedriver`.",
            ", ".join(missing),
            _E2E_ATTEMPTS,
        )
        return 1

    try:
        b64 = encode_current()
    except GfmtSecretsError as e:
        logger.error("Could not read GFMT secrets: %s", e)
        return 1

    # Mint the Drive refresh token locally via the embedded Desktop OAuth client
    # (loopback flow). We never ask the user for an OAuth client secret — the
    # app credential is embedded and non-confidential (see poller/oauth_client.py).
    print("=" * 64)
    print("Step 3: Authorize Google Drive (drive.file)")
    print("=" * 64)
    print(
        "A browser window will open for Google Drive authorization.\n"
        "Sign in with the SAME Google account."
    )
    print()
    try:
        refresh_token = mint_drive_refresh_token(
            oauth_client_id(), oauth_client_secret(), port=8765
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Google Drive authorization failed: %s", e)
        return 1

    print("=" * 64)
    print("Copy BOTH lines below into tagtrail.org/setup")
    print("(or add them as repository Secrets in your GitHub fork):")
    print("=" * 64)
    print()
    print(f"GOOGLE_DRIVE_REFRESH_TOKEN={refresh_token}")
    print()
    print(f"GFMT_SECRETS_JSON_B64={b64}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
