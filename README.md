# boombox-calendar

An iCalendar feed of upcoming events at [The Boombox Miami](https://shotgun.live/en/venues/the-boombox-miami),
rebuilt from Shotgun every 6 hours by a cron job on a home server and served from GitHub Pages.

**Feed:** `https://thedieselpunk.github.io/boombox-calendar/boombox.ics`
**Landing page:** https://thedieselpunk.github.io/boombox-calendar/

## How it works

1. `boombox_ics.py` loads the venue page to find upcoming event slugs (and the promoter's genre chips).
2. Each event page embeds a `schema.org/MusicEvent` JSON-LD block with real start/end times, address,
   lineup and ticket tiers — that's what goes into each `VEVENT`.
3. `UID` is the Shotgun slug, so a re-run updates or removes events rather than duplicating them.
4. `update.sh` (cron, every 6 h) commits `boombox.ics` only when it changed. If any fetch fails the script
   exits non-zero and the previous feed stays published (a partial feed would make Google delete the
   missing events).

## Why not GitHub Actions?

Shotgun returns `429 Too Many Requests` instantly to GitHub's runner IPs (a datacenter-IP block, not
rate limiting). The workflow in `.github/workflows/` is kept for manual runs only; the scheduled build
has to come from a residential IP.

## Server setup (Ubuntu, `ubuntu` user)

```bash
git clone git@github.com:TheDieselPunk/boombox-calendar.git ~/boombox-calendar
cd ~/boombox-calendar
git config user.name  "boombox-cron"
git config user.email "boombox-cron@users.noreply.github.com"
# push auth: a write-enabled deploy key scoped to this repo
ssh-keygen -t ed25519 -N "" -f ~/.ssh/boombox_deploy -C boombox-cron
git config core.sshCommand "ssh -i ~/.ssh/boombox_deploy -o IdentitiesOnly=yes"
#   -> add ~/.ssh/boombox_deploy.pub as a deploy key with write access
#      (gh repo deploy-key add --allow-write, or repo Settings -> Deploy keys)
chmod +x update.sh && ./update.sh          # smoke test
( crontab -l 2>/dev/null; echo '17 */6 * * * /home/ubuntu/boombox-calendar/update.sh >> /home/ubuntu/boombox-calendar/update.log 2>&1' ) | crontab -
```

## Running locally

```bash
python boombox_ics.py                       # -> boombox.ics
python boombox_ics.py --venue <slug> --name "Some Venue" --out other.ics
```

No dependencies beyond the standard library.

## Notes

- Google Calendar polls subscribed URLs on its own schedule, typically every 12–24 hours.
- Shotgun rate-limits bare clients; the script sends a normal browser header set and retries on 429.
