#!/usr/bin/env bash
# Cron entry point. Runs on a residential-IP host (Shotgun blocks datacenter
# IPs, so GitHub Actions can't do this). Rebuilds the feed and pushes it only
# when it changed; GitHub Pages picks up the commit automatically.
#
#   17 */6 * * *  /home/ubuntu/boombox-calendar/update.sh >> /home/ubuntu/boombox-calendar/update.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git pull --rebase --autostash --quiet
python3 boombox_ics.py          # non-zero exit leaves the previous feed in place
git add boombox.ics
if git diff --cached --quiet; then
  echo "Feed unchanged"
else
  git commit --quiet -m "Update feed $(date -u +%Y-%m-%dT%H:%MZ)"
  git push --quiet
  echo "Pushed"
fi
