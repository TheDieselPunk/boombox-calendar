# boombox-calendar

An iCalendar feed of upcoming events at [The Boombox Miami](https://shotgun.live/en/venues/the-boombox-miami),
rebuilt from Shotgun every 6 hours by GitHub Actions and served from GitHub Pages.

**Feed:** `https://thedieselpunk.github.io/boombox-calendar/boombox.ics`
**Landing page:** https://thedieselpunk.github.io/boombox-calendar/

## How it works

1. `boombox_ics.py` loads the venue page to find upcoming event slugs (and the promoter's genre chips).
2. Each event page embeds a `schema.org/MusicEvent` JSON-LD block with real start/end times, address,
   lineup and ticket tiers — that's what goes into each `VEVENT`.
3. `UID` is the Shotgun slug, so a re-run updates or removes events rather than duplicating them.
4. The workflow commits `boombox.ics` only when it changed. If any fetch fails the script exits non-zero
   and the previous feed stays published (a partial feed would make Google delete the missing events).

## Running locally

```bash
python boombox_ics.py                       # -> boombox.ics
python boombox_ics.py --venue <slug> --name "Some Venue" --out other.ics
```

No dependencies beyond the standard library.

## Notes

- Google Calendar polls subscribed URLs on its own schedule, typically every 12–24 hours.
- GitHub disables scheduled workflows after 60 days with no repository activity. The bot's commits count,
  and Boombox posts often enough that this shouldn't bite; if the feed ever goes stale, re-enable the
  workflow from the Actions tab.
- Shotgun rate-limits bare clients; the script sends a normal browser header set and retries on 429.
