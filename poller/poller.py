"""TagTrail poller: one-shot polling cycle.

This is the entry point invoked once per interval (by `scheduler.py` for the
persistent-container deploy, or by the GitHub Actions cron for the fallback).

Log to stdout only. Never write location data to logs.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time

from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import PollerConfig
from .drive_writer import DriveWriter, DriveWriterError
from .findhub_adapter import FindHubError, get_locations, list_available_trackers

logger = logging.getLogger("tagtrail.poller")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def run_once(cfg: PollerConfig) -> int:
    """Run a single poll cycle. Returns number of fixes written.

    Cycle order:
      1. List available trackers via Find Hub (cheap HTTP call).
      2. Decide which IDs to poll (env override > SPA selection > all).
      3. Send Nova locate requests and wait for FCM replies (slow, ~90s).
      4. Append new fixes to Drive (deduped); refresh status.json.
      5. Commit ``config.json`` LAST. The end-of-cycle re-read minimises the
         race with the SPA: if the user toggled a checkbox while we were
         polling, we pick up their value rather than overwriting it.

    Exceptions:
      - FindHubError      -> propagate (caller may surface to user / re-bootstrap)
      - DriveWriterError  -> propagate (caller will retry with backoff)
    """
    writer = DriveWriter(
        client_id=cfg.google_oauth_client_id,
        client_secret=cfg.google_oauth_client_secret,
        refresh_token=cfg.google_drive_refresh_token,
        folder_name=cfg.folder_name,
        tracker_registry=cfg.tracker_registry(),
    )

    env_override = cfg.tracker_ids or None
    available = list_available_trackers()
    if available:
        logger.info(
            "Find Hub sees %d tracker(s) on this account: %s",
            len(available),
            ", ".join(t.name for t in available),
        )
    elif env_override is None:
        logger.warning(
            "Find Hub returned no trackers and no TAGTRAIL_TRACKER_IDS override "
            "is set. We'll trust the previous selection in config.json this cycle."
        )

    available_pairs = [(t.id, t.name) for t in available]
    poll_ids, selection_mode = writer.read_poll_targets(
        available=available_pairs,
        env_tracker_ids=env_override,
    )
    logger.info(
        "Polling %d tracker(s) (selection=%s), timeout=%ds, folder=%s",
        len(poll_ids),
        selection_mode,
        cfg.request_timeout_seconds,
        cfg.folder_name,
    )

    fixes = get_locations(poll_ids, timeout_s=cfg.request_timeout_seconds)
    logger.info("Received %d fix(es) from Find Hub", len(fixes))

    report = writer.write_fixes(fixes)
    logger.info(
        "Drive write done: received=%d, new=%d, days_touched=%d, last_new_fix_at=%s",
        report["received"],
        report["new"],
        report["days_touched"],
        report.get("last_new_fix_at") or "none",
    )

    writer.commit_config(
        available=available_pairs,
        env_tracker_ids=env_override,
    )

    return report["new"]


def run_once_with_retry(cfg: PollerConfig) -> int:
    """Wraps run_once with exponential backoff for transient Drive errors.

    FindHubError is NOT retried automatically because most causes (revoked
    credential, dependency mismatch) need human attention.
    """
    for attempt in Retrying(
        retry=retry_if_exception_type(DriveWriterError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    ):
        with attempt:
            return run_once(cfg)
    return 0  # unreachable, satisfies type-checker


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="TagTrail poller (one-shot poll cycle).")
    parser.add_argument(
        "--jitter-seconds",
        type=int,
        default=0,
        help="Sleep a random duration up to this many seconds before polling (to spread cron load and avoid pattern detection).",
    )
    args = parser.parse_args(argv)

    cfg = PollerConfig.from_env()

    if args.jitter_seconds > 0:
        delay = random.uniform(0, args.jitter_seconds)
        logger.info("Sleeping %.1fs of jitter before poll.", delay)
        time.sleep(delay)

    try:
        run_once_with_retry(cfg)
        return 0
    except FindHubError as e:
        logger.error("Find Hub error: %s", e)
        return 2
    except DriveWriterError as e:
        logger.error("Drive write error after retries: %s", e)
        return 3
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
