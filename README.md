# myst-wire-collector

Overnight newswire collector for the MYST Engine's Morning Brief.

A scheduled GitHub Actions job (`.github/workflows/collect.yml` — when adding the files on GitHub, create `collect.yml` at exactly that path) runs `collector.py`
every 30 minutes (every 15 minutes in U.S. pre-market hours). Each run pulls the
wire-wide RSS feeds from PR Newswire, GlobeNewswire, Business Wire and ACCESS
Newswire, merges every release into `items.json` (rolling 36 hours), and commits.

Nothing here is filtered: no ticker list or universe lives in this repo. The MYST
Engine downloads `items.json` and filters it against the active universe on the PC.

- `items.json` — the rolling store the Engine reads (`{ updatedAt, count, items: [...] }`)
- `status.json` — last run: per-feed item counts and any feed errors

**Check it's alive:** the Actions tab shows each run; `status.json` shows the last
run time. GitHub pauses scheduled workflows in a repo with no activity for 60 days —
the collector's own commits count, so this should not happen while it is running,
but if the Engine reports a stale collector, open the Actions tab and press
"Run workflow" once to re-enable it.
