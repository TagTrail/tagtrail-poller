"""Persistent scheduler entrypoint for the container deploy.

The Find Hub FCM channel requires the process to stay alive long enough to
receive the async reply. APScheduler keeps a long-running event loop while
firing `run_once` at the configured interval, with jitter to avoid being a
detectable cron pattern.
"""

from __future__ import annotations

import logging
import random
import signal
import sys
import threading

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import PollerConfig
from .findhub_adapter import FindHubError
from .poller import _setup_logging, run_once_with_retry

logger = logging.getLogger("tagtrail.scheduler")


def main() -> int:
    _setup_logging()
    cfg = PollerConfig.from_env()

    interval = cfg.poll_interval_seconds
    if interval < 60:
        logger.warning(
            "Poll interval %ds is very aggressive; consider >= 600 to avoid account flags.",
            interval,
        )

    # Max jitter = 20% of the interval, capped at 120s.
    max_jitter = min(int(interval * 0.2), 120)

    stop_event = threading.Event()

    def _job() -> None:
        try:
            run_once_with_retry(cfg)
        except FindHubError as e:
            logger.error(
                "Find Hub error (this often means credentials are revoked or GFMT changed): %s",
                e,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Poll cycle failed: %s", e)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _job,
        trigger=IntervalTrigger(seconds=interval, jitter=max_jitter),
        next_run_time=None,  # do not run immediately; first run is interval + jitter from now.
        id="tagtrail-poll",
        max_instances=1,
        coalesce=True,
    )

    # Optional immediate kickoff with small random delay, so a fresh container
    # actually produces a fix in a reasonable time without all instances firing
    # at the exact same second after a redeploy.
    initial_delay = random.uniform(5, 30)
    threading.Timer(initial_delay, _job).start()

    def _shutdown(*_args: object) -> None:
        logger.info("Shutting down scheduler.")
        stop_event.set()
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info(
        "Scheduler started: interval=%ds, jitter<=%ds, trackers=%d",
        interval,
        max_jitter,
        len(cfg.tracker_ids),
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
