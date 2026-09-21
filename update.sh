#!/usr/bin/env bash
# Cron entry point. Runs on a residential-IP host (Shotgun blocks datacenter
# IPs, so GitHub Actions can't do this). Rebuilds the feeds and pushes only
# when something changed; GitHub Pages picks up the commit automatically.
#
#   17 */6 * * *  /home/ubuntu/miami-calendars/update.sh >> /home/ubuntu/miami-calendars/update.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git pull --rebase --autostash --quiet
python3 build_feeds.py          # non-zero exit leaves the previous feeds in place
git add ./*.ics events.json feeds.json state.json
if git diff --cached --quiet; then
  echo "Feeds unchanged"
else
  git commit --quiet -m "Update feeds $(date -u +%Y-%m-%dT%H:%MZ)"
  git push --quiet
  echo "Pushed"
fi
