# TagTrail Poller

The background poller for [TagTrail](https://tagtrail.org) — tracks your
Google Find Hub trackers and writes their location history to your own Google
Drive. The TagTrail web app reads directly from your Drive. We don't store your data.

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

** Use a secondary google account and share with it the tag you want to track **
** This is a research project. It comes with no support or guarantees **

[Terms of Use](https://tagtrail.org/terms)
[Disclaimer](https://tagtrail.org/terms#disclaimer)
[Privacy Policy](https://tagtrail.org/privacy)

---

## License

MIT
