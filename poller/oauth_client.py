"""TagTrail's Google OAuth client credentials (Desktop application type).

Why these live in source (and not in user-provided secrets)
-----------------------------------------------------------
These identify the *application* to Google — they are the same for every
TagTrail user. They belong to a **Desktop app** OAuth client. Google's own
documentation states that for installed/desktop clients the secret is *not*
treated as confidential and is meant to be embedded in distributed code:

    "The process results in a client ID and, in some cases, a client secret,
     which you embed in the source code of your application. (In this context,
     the client secret is obviously not treated as a secret.)"
    — https://developers.google.com/identity/protocols/oauth2

This is the same model used by rclone, yt-dlp, gphotos-sync, etc. The flow is
kept safe by per-user consent + the loopback/PKCE exchange, not by hiding this
value. So it is correct to ship it here rather than asking every user to set up
their own Google Cloud project.

The SPA (browser) does NOT use these. It only uses the *public* client_id of a
separate **Web** client via the secret-free implicit token flow, and relies on
``drive.file`` being project-scoped so it can read the files the poller writes.

Override (advanced)
-------------------
Forks running their own Google Cloud project can override via the
``GOOGLE_OAUTH_CLIENT_ID`` and ``GOOGLE_OAUTH_CLIENT_SECRET`` env vars. The
override is **atomic**: both must be set, or neither is used. This matters
because a Drive refresh token is bound to the exact client that minted it —
mixing a client_id from one client with a secret from another silently breaks
token refresh. When the pair isn't fully set, the embedded Desktop client wins.
"""

from __future__ import annotations

import os

_DEFAULT_CLIENT_ID = "938229288217-ivah0ovtlk82duagugs2v45olj1tlebk.apps.googleusercontent.com"
_DEFAULT_CLIENT_SECRET = "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"


def _resolve() -> tuple[str, str]:
    env_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    env_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    # Only honor a COMPLETE override pair, so a stale lone env var can't get
    # paired with the embedded secret (or vice-versa) and break token refresh.
    if env_id and env_secret:
        return env_id, env_secret
    return _DEFAULT_CLIENT_ID, _DEFAULT_CLIENT_SECRET


def client_id() -> str:
    return _resolve()[0]


def client_secret() -> str:
    return _resolve()[1]
