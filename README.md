<p align="center">
  <img src="logo.svg" alt="TagTrail logo" width="120" height="120">
</p>

# TagTrail Poller

This is the background poller for [TagTrail](https://tagtrail.org). TT tracks your
Google Find Hub trackers and writes their location history to your own Google
Drive. The TagTrail web app reads directly from your Drive. We don't store or see your data.

---

## Quick start

Go to **[tagtrail.org/setup](https://tagtrail.org/setup)** and follow the
two steps. It gives you one command to run locally; you paste the two secrets
it prints, then it creates your repo's secrets for you from the browser.

**Use a secondary google account and share with it the tags you want to track!**

**This is a research project. It comes with no support and no guarantees!**

[Terms of Use](https://tagtrail.org/terms)
[Disclaimer](https://tagtrail.org/terms#disclaimer)
[Privacy Policy](https://tagtrail.org/privacy)


The setup guide will take care of this for you, but if you opt to do things manually:
**Use this as a template, not a fork.** GitHub disables scheduled workflows on
forks, so a fork would never poll. Click **Use this template** to
get an independent repo, add two secrets, and GitHub Actions polls your trackers
every 15 minutes for free (GitHub doesn't reliably honor the scheduling but it's good-enough).

The workflow installs the poller from upstream on each run, so your repo is just
the workflow file plus your two secrets. Poller updates flow downstream automatically.
