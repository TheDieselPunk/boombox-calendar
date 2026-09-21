# miami-calendars

One subscribable iCalendar feed per Miami venue, rebuilt every 6 hours from the ticket platforms
(Shotgun, Dice, Tablelist, Tixr-backed venue calendars), 19hz.info and Edmtrain by a Windows scheduled
task, and served from GitHub Pages.

**Site:** https://thedieselpunk.github.io/miami-calendars/ — a month/list calendar of every event in
`events.json` (all metro venues, not just the ones with feeds) with filters for venue, genre (`genres.json` puts every source tag into one of the 28 scenes of
[Ishkur's Guide to Electronic Music](https://music.ishkur.com/), in the guide's order, with the scene's
canonical genres kept for reference; require or hide at scene or subgenre level, plus a one-click "skip all
house scenes"), free/RSVP, 18+, day vs night, published-times-only,
sources and text search; filter state lives in the URL hash so a view can be shared. The per-venue
subscribe cards are at the bottom of the same page.
**Feed URLs:** `https://thedieselpunk.github.io/miami-calendars/<slug>.ics` — slugs are in
[`venues.json`](venues.json): `club-space`, `the-ground`, `floyd`, `domicile`, `factory-town`, `kemistry`,
`boombox`, `zeyzey`, `mad-radio`, `daer`, `jolene`, `midline`, and `festivals` — a rule-based feed of
everything *not* at a club (parks, amphitheaters, convention halls, pop-ups) plus festival-branded nights
wherever they are (III Points, Ultra, Art Basel week, Circoloco…).

## How it works

`build_feeds.py` does one crawl per run and writes everything:

1. **Shotgun** — crawls the paginated Miami city listing (`/en/cities/miami?page=N`, ~70 events). For
   events at tracked venues it also fetches the event page's `schema.org/MusicEvent` JSON-LD: real
   start/end, address, lineup, organizer, ticket tiers. Genre chips come from the listing cards.
   A venue's own `/en/venues/<slug>` page is only its *organizer profile* — promoter-run shows never
   appear there — so it's only used as a backstop (`shotgun_page` in `venues.json`).
2. **Dice** — each tracked venue's Dice profile (`dice` in `venues.json`). Space, Floyd, The Ground,
   Factory Town and Kemistry sell here; the page's embedded JSON carries real start *and* end times,
   price-from and sold-out status. Multi-day "pass"/donation items are skipped.
3. **Tablelist** — a venue's own box office where it has one (`tablelist` in `venues.json`; ZeyZey).
   Real start/end times, but only for house-promoted shows.
4. **Venue calendars** — a venue's own website when it lists everything regardless of promoter:
   ZeyZey's Webflow calendar (`zeyzey_calendar`) and Hard Rock Nightlife's calendar for DAER
   (`hardrock_calendar`, Tixr-ticketed). Cards give date + start time; the closing time comes from
   the venue's typical hours (with a `day` rule for pool parties). DAER's placeholder cards
   ("Saturday, October 3rd") adopt the act name from 19hz/Edmtrain when one is announced.
5. **19hz.info** — the curated Miami CSV. One person's electronic-only listing, but every row carries
   **genre tags** and a **time range**, plus ages, price and the ticket link. Used as an enrichment
   layer: it fills genres/times/ages/price on events the platforms already gave us, and adds the few
   it alone lists.
6. **Edmtrain** — metro-wide (location id 87, ~300 events), the last backstop. Date only: no start
   time, no genres. If `EDMTRAIN_CLIENT_KEY` is set the official API is used instead (free personal
   keys at https://edmtrain.com/developer-api).
7. Same-night events at a tracked venue are merged in that priority order: the first source keeps
   the event's identity and times; later ones contribute what's missing (times for date-only rows,
   lineup, ages, price), union their genre tags, and add their link. Name matching is spelling- and
   accent-insensitive ("SO DOWN" = "SoDown", "Łaszewo" = "Laszewo"). `genre_hints.json` tags what's
   still untagged by artist, or by venue as a marked guess.
8. Per venue: `<slug>.ics`. Shotgun-detailed events carry their real times. Date-only events
   (Edmtrain) get the venue's **typical hours** from `venues.json` (`hours`, with optional per-weekday
   overrides such as Club Space's Saturday-into-Sunday-afternoon) and are flagged as estimates in the
   description; the real time replaces the estimate as soon as a source publishes one. Venues
   without an `hours` entry fall back to the median of observed Shotgun times, then to all-day.
   `UID` is the Shotgun slug or Edmtrain id, so re-runs update/remove rather than duplicate.
   `state.json` remembers each event's content hash, so `DTSTAMP`/`LAST-MODIFIED` move and
   `SEQUENCE` increments **only when an event actually changes** — that's what calendar clients
   (Google included) use to decide whether to apply an update to an event they already hold —
   while a run that changes nothing produces byte-identical files and no commit.
9. Also written: `events.json` (every event from both sources, tracked or not — what `whats_on.py`
   reads) and `feeds.json` (manifest the landing page renders).

If a source fails the script exits non-zero and nothing is committed: a partial feed would make
Google delete the missing events.

## Adding a venue

Add an entry to `venues.json` and push (a `rules` entry — regexes on `venue`, `loose_venue` and `title`
— makes a feed by pattern instead; see `festivals`, which also lists matching events that live in another
venue's feed):

```json
{ "slug": "kemistry", "name": "Kemistry", "match": ["kemistry"] }
```

`match` is a list of case-insensitive substrings tested against the venue name as each source
reports it (and, for Shotgun, the event page's location name). Optional: `dice` — the venue's Dice
profile slug (from a `dice.fm/venue/<slug>` URL; gives real times for venues that sell there);
`tablelist` — the venue's slug from a `buy.tablelist.com/v/<slug>` URL, if its own site sells there;
`zeyzey_calendar` / `hardrock_calendar` — the site-specific calendar scrapers (see `build_feeds.py`);
`street` — address to put in LOCATION when a source gives none;
`exclude` — title substrings to drop, e.g. `["the comedy bar"]` for a venue whose profile mixes in a
comedy room; `hours` — typical local
start/end as `{"default": ["23:00", "05:00"], "sat": ["23:00", "13:00"], "day": ["12:00", "18:00"]}`,
used for events whose source has no start time (and, via `day`, as the closing time for daytime
events); `address` (substring of the street address, a second confirmation signal);
`shotgun_page` (the venue's own Shotgun slug, unioned in as a backstop). Check the exact venue
spelling with `python whats_on.py --days 60 --venue <text>`.

## What's on (replaces the old `miami_plans.py`)

```bash
python whats_on.py --date fri --tracked          # tracked venues on Friday
python whats_on.py --days 14 --exclude house      # two weeks, drop house-tagged rows
python whats_on.py --venue floyd                  # one venue
```

Reads `events.json`; the scheduled task refreshes it every 6 h, or run `python build_feeds.py` (~40 s).
`*` marks venues that have a feed; `~` before a time means it's estimated from the venue's usual hours;
`?` on a genre means it was inferred from the venue.

## Why not GitHub Actions?

Shotgun returns `429 Too Many Requests` instantly to GitHub's runner IPs (a datacenter-IP block, not
rate limiting). The workflow in `.github/workflows/` is kept for manual runs only; the scheduled build
has to come from a residential IP.

## Scheduling on Windows (current setup)

The clone lives at `C:\Users\david\OneDrive\Desktop\miami-calendars`. A scheduled task named
**"Miami calendar feeds"** runs `update.py` at 00:17 / 06:17 / 12:17 / 18:17 local time, only while
the user is logged on (so it can use Git Credential Manager for the push), catching up on the next
wake if a run was missed. It is launched with `pythonw.exe` (no console) and starts every child
process with `CREATE_NO_WINDOW`, so nothing flashes on screen. Output goes to `update.log` in the
repo (git-ignored).

Re-create it from PowerShell (registers under the current user):

```powershell
$repo = "C:\Users\david\OneDrive\Desktop\miami-calendars"
$pyw  = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
$action  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$repo\update.py`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(17) -RepetitionInterval (New-TimeSpan -Hours 6)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Miami calendar feeds" -Action $action -Trigger $trigger -Settings $settings -Force
```

Useful: `Start-ScheduledTask "Miami calendar feeds"` to run now, `Get-ScheduledTaskInfo "Miami calendar feeds"`
for last run time/result (0 = success), `Get-Content update.log -Tail 20` for output.

## Alternative: cron on a Linux box

`update.sh` is the same logic for cron (`17 */6 * * *`). It needs a residential IP, a git identity, and
push auth — a write-enabled deploy key scoped to this repo is the cleanest
(`ssh-keygen -t ed25519 -f ~/.ssh/miami_deploy`, then `gh repo deploy-key add --allow-write`, and
`git config core.sshCommand "ssh -i ~/.ssh/miami_deploy -o IdentitiesOnly=yes"`).

## Notes

- Google Calendar polls subscribed URLs on its own schedule, typically every 12–24 hours.
- Shotgun rate-limits bare clients; the script sends a normal browser header set and retries on 429.
- Edmtrain's metro feed includes Fort Lauderdale/Davie and occasionally mis-files a listing.
