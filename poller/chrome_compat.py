"""Make GoogleFindMyTools' Chrome driver flow work with org-managed Chrome.

The problem
-----------
GFMT's `chrome_driver.py` calls `undetected_chromedriver.Chrome(version_main=None)`.
When `version_main` is None, uc tries to auto-detect the installed Chrome's
major version and download a matching ChromeDriver. On some systems
(especially org-managed Chromes that lag behind the latest stable), uc's
auto-detect quietly falls back to "the newest ChromeDriver Google publishes",
which yields a driver one major ahead of the local Chrome → session-not-created
crash.

The fix
-------
Detect the local Chrome major version ourselves (via `--version`) and
monkey-patch `uc.Chrome.__init__` so that calls with `version_main=None`
receive our detected major. No GFMT files are modified.

We also expose a cache-clear helper so a stale driver downloaded under the
wrong version gets re-resolved on next run.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_CHROME_CANDIDATES_DARWIN = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_CHROME_CANDIDATES_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]
_CHROME_CANDIDATES_WIN = [
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]


def _chrome_paths() -> list[str]:
    if sys.platform == "darwin":
        return _CHROME_CANDIDATES_DARWIN
    if sys.platform.startswith("linux"):
        return _CHROME_CANDIDATES_LINUX
    if sys.platform == "win32":
        return [p for p in _CHROME_CANDIDATES_WIN if p]
    return []


def _detect_chrome_version_win() -> int | None:
    """On Windows, read Chrome's version from the registry."""
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(root, r"SOFTWARE\Google\Chrome\BLBeacon")
                val, _ = winreg.QueryValueEx(key, "version")
                winreg.CloseKey(key)
                m = re.match(r"(\d+)\.", val)
                if m:
                    major = int(m.group(1))
                    logger.info("Detected Chrome major version %d from Windows registry", major)
                    return major
            except OSError:
                continue
    except ImportError:
        pass

    for path in _chrome_paths():
        if not Path(path).exists():
            continue
        try:
            out = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out.stdout)
                if m:
                    major = int(m.group(1))
                    logger.info("Detected Chrome major version %d from %s", major, path)
                    return major
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            continue
    return None


def detect_chrome_major_version() -> int | None:
    """Return the integer major version of the locally-installed Chrome, or None.

    Tries platform-specific methods: registry on Windows, ``--version`` on
    macOS/Linux.
    """
    if sys.platform == "win32":
        return _detect_chrome_version_win()

    for path in _chrome_paths():
        if not Path(path).exists():
            continue
        try:
            out = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError) as e:
            logger.debug("Could not run %s --version: %s", path, e)
            continue
        if out.returncode != 0:
            continue
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out.stdout)
        if m:
            major = int(m.group(1))
            logger.info("Detected Chrome major version %d from %s", major, path)
            return major
    return None


def _uc_cache_dir() -> Path:
    """Where undetected-chromedriver caches its downloaded driver."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "undetected_chromedriver"
    if sys.platform.startswith("linux"):
        return Path.home() / ".local" / "share" / "undetected_chromedriver"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "undetected_chromedriver"
    return Path.home() / ".undetected_chromedriver"


def clear_uc_cache() -> bool:
    """Remove any cached chromedriver. Returns True if something was removed."""
    cache = _uc_cache_dir()
    if cache.exists():
        logger.info("Clearing stale undetected_chromedriver cache at %s", cache)
        shutil.rmtree(cache, ignore_errors=True)
        return True
    return False


_PATCHED = False


def apply_chromedriver_compat_patch() -> int | None:
    """Patch undetected_chromedriver so a None `version_main` becomes the local Chrome's major.

    Returns the detected major version (or None if Chrome wasn't found, in
    which case uc is left untouched and will surface its own error).

    Idempotent — safe to call multiple times.
    """
    global _PATCHED

    detected = detect_chrome_major_version()
    if detected is None:
        logger.warning(
            "Could not detect a local Chrome. Skipping chromedriver compat patch; "
            "GFMT's Chrome flow may fail with a version-mismatch error."
        )
        return None

    if _PATCHED:
        return detected

    try:
        import undetected_chromedriver as uc
    except ImportError:
        logger.warning("undetected_chromedriver not installed; nothing to patch.")
        return detected

    orig_init = uc.Chrome.__init__

    def patched_init(self, *args, **kwargs):
        # If the caller (GFMT) left version_main unset/None, pin it to the
        # detected local Chrome major so uc downloads a matching driver.
        if kwargs.get("version_main") is None:
            kwargs["version_main"] = detected
        return orig_init(self, *args, **kwargs)

    uc.Chrome.__init__ = patched_init
    _PATCHED = True
    logger.info(
        "Patched undetected_chromedriver.Chrome to use version_main=%d.", detected
    )
    return detected
