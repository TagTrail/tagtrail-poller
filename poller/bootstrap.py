"""One-time local bootstrap for the poller.

Runs two flows interactively:
  (a) GoogleFindMyTools' own Chrome-based auth flow, to produce its secret
      bundle (`Auth/secrets.json` in the GFMT install). The poller process
      will reuse this on every cycle.
  (b) A loopback OAuth 2.0 flow against the SAME Web OAuth client used by the
      SPA, to mint a long-lived `refresh_token` with the narrow `drive.file`
      scope.

The result is printed as a copy-paste block and written to a `.env` file that
the user pastes into their deployment's secrets.

Run on the user's own machine (Chrome required), once.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

try:
    from dotenv import load_dotenv

    # Loads `bootstrap.env` (or `.env`) from the current working directory so the
    # user can keep the client_id / client_secret out of their shell history.
    load_dotenv("bootstrap.env")
    load_dotenv(".env")
except ImportError:
    pass

from .chrome_compat import apply_chromedriver_compat_patch, clear_uc_cache
from .drive_writer import DRIVE_SCOPES
from .gfmt_secrets import GfmtSecretsError, encode_current
from .install_gfmt import GFMT_PINNED_COMMIT, gfmt_path
from .oauth_client import client_id as oauth_client_id
from .oauth_client import client_secret as oauth_client_secret

logger = logging.getLogger("tagtrail.bootstrap")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _build_client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def mint_drive_refresh_token(client_id: str, client_secret: str, port: int) -> str:
    """Run the loopback flow; print URL; user signs in once; returns a refresh token."""
    flow = InstalledAppFlow.from_client_config(
        _build_client_config(client_id, client_secret),
        scopes=DRIVE_SCOPES,
    )
    # `prompt='consent'` forces a refresh_token to be issued even for clients the
    # user has already authorized.
    creds = flow.run_local_server(
        port=port,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message=(
            "Open this URL in your browser to authorise TagTrail's poller "
            "(it should also auto-open):\n  {url}\nThe redirect comes back to "
            f"http://localhost:{port}/."
        ),
        success_message=(
            "TagTrail bootstrap: Drive authorisation complete. You may close this tab."
        ),
    )
    if not creds.refresh_token:
        raise RuntimeError(
            "No refresh_token was returned. Ensure the OAuth client is type "
            "'Web application' and that you accepted the consent screen fresh."
        )
    return creds.refresh_token


def run_gfmt_auth() -> None:
    """Trigger the GoogleFindMyTools Chrome-based auth flow.

    Side effect: writes Auth/secrets.json inside the GFMT checkout.
    """
    p = gfmt_path()
    if p is None:
        logger.error(
            "GoogleFindMyTools checkout not found. Run `tagtrail-install-gfmt` first "
            "(it clones the pinned commit %s and installs its requirements).",
            GFMT_PINNED_COMMIT,
        )
        raise SystemExit(2)
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

    # Force uc to use the local Chrome's major version. On org-managed Chromes
    # that lag a release behind, uc's auto-detect picks the newest published
    # version instead, causing a session-not-created mismatch.
    apply_chromedriver_compat_patch()

    try:
        from NovaApi.ListDevices.nbe_list_devices import request_device_list
        from ProtoDecoders.decoder import get_canonic_ids, parse_device_list_protobuf
    except ImportError as e:
        logger.error("GoogleFindMyTools import failed: %s", e)
        logger.error("Re-run `tagtrail-install-gfmt` to repair the checkout.")
        raise SystemExit(2) from e

    print("")
    print("=" * 64)
    print("Step 1: GoogleFindMyTools (Find Hub) auth")
    print("=" * 64)
    print(
        "A Chrome browser will open in a moment. Sign in to your THROWAWAY\n"
        "Google account (the one that owns the trackers). After sign-in, GFMT\n"
        "will fetch the tracker list."
    )
    print("")

    result_hex = request_device_list()
    device_list = parse_device_list_protobuf(result_hex)
    canonic_ids = get_canonic_ids(device_list)

    print("")
    print("Your trackers:")
    for idx, (name, cid) in enumerate(canonic_ids, start=1):
        print(f"  {idx:>2}. {name}  ->  {cid}")
    print("")

    # Retrieve E2E owner key now so it's cached in secrets.json before we
    # base64-encode it. Without this, the CI poller would try to open Chrome
    # interactively — which fails headless.
    print("=" * 64)
    print("Step 1b: Retrieving end-to-end encryption keys")
    print("=" * 64)
    print(
        "Chrome will open one more time. Sign in with the same account\n"
        "to unlock your tracker encryption keys."
    )
    print("")
    try:
        # GFMT's shared_key_retrieval.py calls input("Press Enter") — skip it.
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

        get_owner_key()
        logger.info("E2E owner key retrieved and cached.")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not retrieve E2E owner key: %s", e)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Mint the credentials TagTrail's poller needs. Run once locally."
    )
    parser.add_argument(
        "--client-id",
        default=oauth_client_id(),
        help="Override the embedded app OAuth client_id (advanced; forks running their own Google Cloud project).",
    )
    parser.add_argument(
        "--client-secret",
        default=oauth_client_secret(),
        help="Override the embedded app OAuth client_secret (advanced).",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Loopback port for the OAuth redirect."
    )
    parser.add_argument(
        "--skip-gfmt",
        action="store_true",
        help="Skip the GoogleFindMyTools Chrome auth (e.g. if you already have secrets.json).",
    )
    parser.add_argument(
        "--env-out",
        default=".env",
        help="Where to write the .env file with the resulting secrets.",
    )
    parser.add_argument(
        "--reset-chromedriver",
        action="store_true",
        help="Delete the cached undetected-chromedriver before running. Use this if you saw a 'session not created / Chrome version' error on a previous run.",
    )
    args = parser.parse_args(argv)

    if args.reset_chromedriver:
        clear_uc_cache()

    if not args.skip_gfmt:
        run_gfmt_auth()

    print("")
    print("=" * 64)
    print("Step 2: Google Drive OAuth (loopback) — scope: drive.file")
    print("=" * 64)
    refresh_token = mint_drive_refresh_token(args.client_id, args.client_secret, args.port)

    # GFMT secrets blob: only available if step 1 ran (and so wrote
    # Auth/secrets.json). For `--skip-gfmt` we leave the line out and tell the
    # user how to repopulate it.
    gfmt_b64: str | None = None
    if not args.skip_gfmt:
        try:
            gfmt_b64 = encode_current()
        except GfmtSecretsError as e:
            logger.warning("Could not read GFMT secrets after auth: %s", e)

    env_path = Path(args.env_out).resolve()
    lines: list[str] = [
        "# TagTrail poller credentials. Paste these into your deployment secrets.",
        "# (The OAuth client id/secret are NOT here — they're embedded in the",
        "#  poller as a non-confidential Desktop-app credential. You only need",
        "#  the two per-account values below.)",
        f"GOOGLE_DRIVE_REFRESH_TOKEN={refresh_token}",
        "",
        "# Base64-encoded GoogleFindMyTools secrets bundle. The poller decodes",
        "# this on first start into <gfmt_dir>/Auth/secrets.json, so cloud",
        "# deploys do not need a mounted volume. Treat this like a password.",
    ]
    if gfmt_b64 is not None:
        lines.append(f"GFMT_SECRETS_JSON_B64={gfmt_b64}")
    else:
        lines.extend(
            [
                "# Re-run `tagtrail-bootstrap` (without --skip-gfmt) to populate this:",
                "# GFMT_SECRETS_JSON_B64=",
            ]
        )
    lines.extend(
        [
            "",
            "# Comma-separated canonical tracker IDs you want to poll. Edit this:",
            "TAGTRAIL_TRACKER_IDS=",
            "# Optional: friendly names and colors (JSON objects keyed by tracker id).",
            '# TAGTRAIL_TRACKER_NAMES={"abc123":"Keys"}',
            '# TAGTRAIL_TRACKER_COLORS={"abc123":"#2b6cb0"}',
            "",
            "# Optional tuning:",
            "TAGTRAIL_POLL_INTERVAL_SECONDS=900",
            "TAGTRAIL_REQUEST_TIMEOUT_SECONDS=90",
            "TAGTRAIL_FOLDER=TagTrail",
            "",
        ]
    )
    contents = "\n".join(lines)
    env_path.write_text(contents, encoding="utf-8")
    env_path.chmod(0o600)

    print("")
    print("=" * 64)
    print("Done. Secrets written to:", env_path)
    print("=" * 64)
    # Don't echo the long base64 blob to the terminal — it scrolls a screen
    # and reveals secrets in a `cmd | tee` style audit log.
    sanitized = "\n".join(
        line if not line.startswith("GFMT_SECRETS_JSON_B64=")
        else "GFMT_SECRETS_JSON_B64=<elided; see " + str(env_path) + ">"
        for line in lines
    )
    print(sanitized)
    print("Next steps:")
    print("  1. Edit TAGTRAIL_TRACKER_IDS in", env_path, "to include the canonical")
    print("     IDs of the trackers you want TagTrail to follow.")
    print("  2. Copy these values into your deployment's secrets manager:")
    print("     - Fly.io:         fly secrets import < " + str(env_path))
    print("     - GitHub Actions: repository Settings -> Secrets -> Actions")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
