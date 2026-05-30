"""Install GoogleFindMyTools at a pinned commit.

GFMT is not a Python package. It's a clone-and-run project. This module:
  1. Holds the pinned commit hash (single source of truth).
  2. Clones GFMT into a `vendor/GoogleFindMyTools/` directory.
  3. `pip install` its `requirements.txt` into the active venv.
  4. Exposes `gfmt_path()` so `findhub_adapter` can put it on `sys.path` at
     import time.

This keeps the brief's guarantee ("treat as external library, pin commit, do
not fork or rewrite") while working around GFMT not being pip-installable.

Run once after `pip install ./poller`:

    tagtrail-install-gfmt

To override the install location:

    GFMT_DIR=/path/to/GoogleFindMyTools tagtrail-install-gfmt
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("tagtrail.install_gfmt")

# ---- pinned external dependency ----
# Bump deliberately, after testing. The Find Hub protocol is reverse-engineered
# and unpinned upgrades break silently. See README.md / docs/SETUP.md.
GFMT_REPO_URL = "https://github.com/leonboe1/GoogleFindMyTools.git"
GFMT_PINNED_COMMIT = "d46e9528578015b51d3b84dd91bf8f16e9ab850f"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def default_gfmt_dir() -> Path:
    """Resolve where GFMT should live.

    Precedence:
      1. $GFMT_DIR env var (absolute or relative to cwd).
      2. ./vendor/GoogleFindMyTools/ in the current working directory.
    """
    env = os.environ.get("GFMT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd() / "vendor" / "GoogleFindMyTools"


def gfmt_path() -> Path | None:
    """Return the GFMT directory if it exists and looks like a valid checkout."""
    candidates: list[Path] = []
    env = os.environ.get("GFMT_DIR")
    if env:
        candidates.append(Path(env).expanduser().resolve())
    # Common locations relative to cwd:
    cwd = Path.cwd()
    candidates.extend(
        [
            cwd / "vendor" / "GoogleFindMyTools",
            cwd / "GoogleFindMyTools",
            cwd.parent / "vendor" / "GoogleFindMyTools",
        ]
    )
    for p in candidates:
        if _looks_like_gfmt(p):
            return p
    return None


def _looks_like_gfmt(p: Path) -> bool:
    return (
        p.is_dir()
        and (p / "main.py").is_file()
        and (p / "NovaApi").is_dir()
        and (p / "Auth").is_dir()
    )


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    logger.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def install_gfmt(target_dir: Path, *, install_requirements: bool = True) -> Path:
    """Clone GFMT at the pinned commit into `target_dir`, install requirements.

    Idempotent: if `target_dir` already exists and is on the right commit, no-op
    for the clone. Requirements are re-installed (cheap).
    """
    target_dir = target_dir.expanduser().resolve()
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    if not target_dir.exists():
        _run(["git", "clone", GFMT_REPO_URL, str(target_dir)])
    else:
        if not _looks_like_gfmt(target_dir):
            raise RuntimeError(
                f"{target_dir} exists but doesn't look like a GoogleFindMyTools "
                "checkout. Remove it and rerun, or set GFMT_DIR to a clean path."
            )
        _run(["git", "-C", str(target_dir), "fetch", "--all", "--tags"])

    # Pin to the exact commit.
    _run(["git", "-C", str(target_dir), "checkout", "--detach", GFMT_PINNED_COMMIT])

    if install_requirements:
        req = target_dir / "requirements.txt"
        if not req.is_file():
            raise RuntimeError(f"Missing {req} in GFMT checkout.")
        _run([sys.executable, "-m", "pip", "install", "-r", str(req)])

    return target_dir


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Clone GoogleFindMyTools at the pinned commit and install its Python requirements."
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Where to clone GFMT. Defaults to ./vendor/GoogleFindMyTools (or $GFMT_DIR).",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="Clone+checkout only; don't install requirements.txt.",
    )
    args = parser.parse_args(argv)

    target = Path(args.dir).expanduser().resolve() if args.dir else default_gfmt_dir()
    logger.info("Installing GoogleFindMyTools @ %s into %s", GFMT_PINNED_COMMIT, target)
    install_gfmt(target, install_requirements=not args.skip_pip)
    logger.info("Done. GFMT ready at %s", target)
    print("")
    print(f"GFMT_DIR={target}")
    print("Export this in your shell, or rely on the default ./vendor/ location.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
