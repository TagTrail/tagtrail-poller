# TagTrail Poller

The background poller for [TagTrail](https://tagtrail.org) — tracks your
Google Find Hub trackers and writes their location history to your own Google
Drive. The TagTrail web app reads directly from your Drive; nothing transits a
server.

**Use this as a template, not a fork.** GitHub disables scheduled workflows on
forks, so a fork would never poll. Click **Use this template** (or
[generate a repo](https://github.com/TagTrail/tagtrail-poller/generate)) to
get an independent repo, add two secrets, and GitHub Actions polls your trackers
every 15 minutes for free.

The workflow installs the poller from upstream on each run, so your repo is just
the workflow file plus your two secrets — and you get poller updates
automatically.

---

## Quick start

Go to **[tagtrail.org/setup](https://tagtrail.org/setup)** and follow the
two steps. It gives you one command to run locally; you paste the two secrets
it prints, then it creates your repo's secrets for you from the browser.

If you prefer the command line, read on.

### CLI bootstrap (alternative)

Prerequisites: Python 3.11+, Google Chrome.

```bash
git clone https://github.com/TagTrail/tagtrail-poller.git
cd tagtrail-poller
python -m venv .venv && source .venv/bin/activate
pip install ./poller
tagtrail-install-gfmt

tagtrail-bootstrap --port 8765 --env-out poller.env
```

This opens Chrome twice (Find Hub auth, then Drive auth). It writes
`poller.env` with the two secrets you need.

### Create your repo and deploy

1. **[Use this template](https://github.com/TagTrail/tagtrail-poller/generate)**
   to create your own (preferably private) repo. Don't fork — forks don't run
   scheduled jobs.

2. Set the two secrets, the easy way: [tagtrail.org/setup](https://tagtrail.org/setup)
   does it from your browser, or run the Colab notebook
   [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/TagTrail/tagtrail-poller/blob/main/deploy/deploy_to_github.ipynb).
   By hand: in your repo, **Settings → Secrets and variables →
   Actions → New repository secret**, and add:

   | Secret name | Value |
   |---|---|
   | `GOOGLE_DRIVE_REFRESH_TOKEN` | From setup page or `poller.env` |
   | `GFMT_SECRETS_JSON_B64` | From setup page or `poller.env` |

3. Go to **Actions → tagtrail-poll → Run workflow** to trigger the first run.

4. From now on, the workflow runs every 15 minutes automatically.

### View your data

Go to [tagtrail.org](https://tagtrail.org), sign in with the same Google
account. Your trackers appear on the map. Use the checkboxes to choose which
trackers the poller follows.

---

## How it works

```
┌─────────────┐    locate     ┌──────────────┐
│   GitHub     │──────────────▶│  Google Find  │
│   Actions    │◀──────────────│  Hub (Nova)   │
│  (this repo) │   FCM reply   └──────────────┘
│              │
│  every 15min │   write fixes  ┌──────────────┐
│              │───────────────▶│  Google Drive │
└─────────────┘                │  (your acct)  │
                               └───────┬───────┘
                                       │ read
                               ┌───────▼───────┐
                               │  tagtrail.org  │
                               │  (static SPA)  │
                               └───────────────┘
```

- The poller asks Google's Find Hub for your trackers' latest locations.
- It writes the results as NDJSON files into a `TagTrail/` folder in your
  Google Drive (deduped — identical fixes are never written twice).
- The SPA at tagtrail.org reads those files directly from your Drive. It uses
  a separate OAuth client in the same Google Cloud project as the poller; the
  `drive.file` scope is project-scoped, so the SPA sees the poller's files. No
  data leaves your account.

---

## Troubleshooting

**Workflow says "Find Hub error: credentials revoked"** — Go to
[tagtrail.org/setup](https://tagtrail.org/setup) again (or re-run
`tagtrail-bootstrap`) and update the two secrets in your repo.

**Workflow stopped running** — GitHub disables scheduled workflows after 60
days of no commits. This repo includes a keepalive job that prevents that, but
if it somehow stops, go to Actions → tagtrail-poll → Enable workflow, then
Run workflow.

**No fixes on the map** — Find Hub only updates when an Android phone passes
near your tracker. Fixes are sparse. Try a wider date range.

**"Polling 0 tracker(s)"** — You unchecked all trackers in the TagTrail SPA.
Go to tagtrail.org, sign in, and check at least one tracker.

---

## License

MIT
