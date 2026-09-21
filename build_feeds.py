"""Miami venue calendars: one crawl, one .ics feed per venue.

Sources
  * Shotgun  - the paginated Miami city listing (/en/cities/miami?page=N). For
               events at tracked venues the event page's schema.org MusicEvent
               JSON-LD is fetched too: real start/end, address, lineup, ticket
               tiers. Genre chips only exist on listing cards. A venue's own
               /en/venues/<slug> page is just its organizer profile (promoter-run
               shows don't appear there), so it's only used as a backstop.
  * Dice     - each tracked venue's Dice profile (venues.json "dice"). Space,
               Floyd, The Ground, Factory Town and Kemistry sell here; the page's
               embedded JSON has real start and end times and sold-out status.
  * Tablelist- a venue's own box office where it has one (venues.json
               "tablelist", e.g. ZeyZey): house-promoted shows with real times.
  * Edmtrain - metro-wide coverage (location id 87), the backstop for anything
               the other two miss (e.g. Domicile). Date only: no start time, no
               genres. The official API is used automatically when
               EDMTRAIN_CLIENT_KEY is set (free keys at edmtrain.com/developer-api).

Same-night events at a tracked venue are merged with priority Shotgun > Dice >
Tablelist > Edmtrain; the winner keeps its times, the others contribute lineup
and links.

Outputs (all in the repo root)
  <slug>.ics    one feed per venue in venues.json. Shotgun-detailed events carry
                real times; date-only events get the venue's typical hours
                (venues.json "hours") flagged as estimates
  events.json   every event from both sources, normalized, tracked or not -
                what whats_on.py (and the miami-plans skill) read
  feeds.json    manifest the landing page renders
  state.json    per-event content hash / SEQUENCE / last-modified, so feeds
                carry proper change stamps and stay byte-identical otherwise

Usage:
    python build_feeds.py             # build everything
    python build_feeds.py --only boombox

Exit status is non-zero if a source fails, so a scheduled run leaves the
previous feeds in place rather than publishing partial ones.
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
EASTERN = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

CITY_SLUG = "miami"
EDMTRAIN_LOCATION_ID = 87
PRODID = "-//TheDieselPunk//miami-calendars//EN"
MAX_CITY_PAGES = 25
POLITE_DELAY = 1.0
DEFAULT_DURATION = dt.timedelta(hours=5)
STATE_FILE = "state.json"  # uid -> {hash, seq, modified}; drives DTSTAMP/LAST-MODIFIED/SEQUENCE

# Shotgun 429s bare clients; this header set has been reliable.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Shotgun listing cards / event pages
CARD_RE = re.compile(r'<a data-slot="tracked-link" href="(/en/events/[^"?#]+)"[^>]*>(.*?)</a>', re.S)
CARD_TITLE_RE = re.compile(r'<p class="line-clamp-2[^"]*">([^<]*)</p>')
CARD_VENUE_RE = re.compile(r'<div class="text-muted-foreground[^"]*whitespace-nowrap">([^<]*)</div>')
CARD_START_RE = re.compile(r'<time dateTime="([^"]+)">')
CARD_PRICE_RE = re.compile(r"<span>(Free|\$[\d.,]+[^<]*)</span>")
BADGE_RE = re.compile(r'rounded-full border[^"]*"[^>]*>([^<]{2,40})</div>')
JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
PAST_RE = re.compile(r"PAST EVENTS|Past events")

# Edmtrain front-end fragment
EDM_CONTAINER_RE = re.compile(
    r'<div class="eventContainer[^"]*"([^>]*)>(.*?)'
    r'(?=<div class="eventContainer|<div class="dateSepContainer|\Z)',
    re.S,
)
EDM_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
EDM_ADDRESS_RE = re.compile(r'itemprop="address" content="([^"]*)"')
EDM_URL_RE = re.compile(r'itemprop="url" href="([^"]*)"')
EDM_AGES_RE = re.compile(r"</span>\s*(1[89]\+|21\+)")
EDM_ARTIST_RE = re.compile(r"class='artist\d+'>([^<]*)<")

if hasattr(sys.stderr, "reconfigure"):  # Windows consoles default to cp1252
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(msg, file=sys.stderr)


def fetch(url, headers=None, retries=3):
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503) and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                log(f"  {exc.code} from {url}; retrying in {wait}s")
                time.sleep(wait)
                continue
            raise


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def parse_iso(stamp):
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def local_date(utc_dt):
    return utc_dt.astimezone(EASTERN).date().isoformat()


def load_json(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


def venue_for(venues, text):
    """Tracked venue config whose match list hits this venue name, else None."""
    low = (text or "").lower()
    for v in venues:
        if any(m in low for m in v["match"]):
            return v
    return None


def new_event(**kw):
    ev = {
        "id": "", "source": "", "title": "", "artists": [], "venue": "", "venue_slug": None,
        "date": "", "start": None, "end": None, "time_estimated": False, "address": "", "ages": "",
        "price": "", "genres": [], "genres_inferred": False, "organizer": "", "description": "", "url": "",
    }
    ev.update(kw)
    return ev


# --------------------------------------------------------------------------- #
# Typical hours (used until a source publishes the real time)
# --------------------------------------------------------------------------- #

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def learn_hours(events):
    """Median start clock time and duration per venue slug, from timed Shotgun events.

    A fallback for venues without an explicit "hours" entry in venues.json.
    """
    samples = {}
    for ev in events:
        if ev["venue_slug"] and ev["start"] and ev["end"] and not ev["time_estimated"]:
            s = parse_iso(ev["start"]).astimezone(EASTERN)
            e = parse_iso(ev["end"])
            samples.setdefault(ev["venue_slug"], []).append((s.hour * 60 + s.minute, (e - parse_iso(ev["start"])).seconds // 60))
    learned = {}
    for slug, rows in samples.items():
        if len(rows) < 2:
            continue
        starts = sorted(r[0] for r in rows)
        durs = sorted(r[1] for r in rows)
        start, dur = starts[len(starts) // 2], durs[len(durs) // 2]
        learned[slug] = {"default": [f"{start // 60:02d}:{start % 60:02d}", None], "_duration_min": dur}
    return learned


def typical_window(venue, learned, date_iso):
    """(start, end) UTC for an event on date_iso with no published time, or None."""
    day = dt.date.fromisoformat(date_iso)
    hours = venue.get("hours") or learned.get(venue["slug"])
    if not hours:
        return None
    spec = hours.get(DAYS[day.weekday()]) or hours.get("default")
    if not spec:
        return None
    sh, sm = (int(x) for x in spec[0].split(":"))
    start = dt.datetime(day.year, day.month, day.day, sh, sm, tzinfo=EASTERN)
    if spec[1]:
        eh, em = (int(x) for x in spec[1].split(":"))
        end = dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=EASTERN)
        if end <= start:
            end += dt.timedelta(days=1)
    else:
        end = start + dt.timedelta(minutes=hours.get("_duration_min", 300))
    return start.astimezone(UTC), end.astimezone(UTC)


def fill_typical_hours(events, venues, learned):
    by_slug = {v["slug"]: v for v in venues}
    n = 0
    for ev in events:
        if ev["start"] or not ev["venue_slug"]:
            continue
        window = typical_window(by_slug[ev["venue_slug"]], learned, ev["date"])
        if window:
            ev["start"], ev["end"] = window[0].isoformat(), window[1].isoformat()
            ev["time_estimated"] = True
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Shotgun
# --------------------------------------------------------------------------- #

def card_info(block):
    title = CARD_TITLE_RE.search(block)
    venue = CARD_VENUE_RE.search(block)
    start = CARD_START_RE.search(block)
    price = CARD_PRICE_RE.search(block)
    # Chips include the date/price; genre chips are the rest (skip '+N' overflow chips).
    badges = [clean(b) for b in BADGE_RE.findall(block)]
    genres = [b.lower() for b in badges if b and not b.startswith("+") and not re.search(r"\d", b)]
    return {
        "title": clean(title.group(1)) if title else "",
        "venue": clean(venue.group(1)) if venue else "",
        "start": start.group(1) if start else None,
        "price": clean(price.group(1)) if price else "",
        "genres": genres,
    }


def shotgun_city_cards(city_slug):
    """Crawl the paginated city listing -> {event_path: card}."""
    out = {}
    for page in range(MAX_CITY_PAGES):
        url = f"https://shotgun.live/en/cities/{city_slug}" + (f"?page={page}" if page else "")
        found = CARD_RE.findall(fetch(url))
        if page == 0 and not found:
            raise RuntimeError("Shotgun city listing has no event cards - page layout changed?")
        new = 0
        for href, block in found:
            if href not in out:
                out[href] = card_info(block)
                new += 1
        if not new:
            break
        time.sleep(POLITE_DELAY)
    return out


def shotgun_venue_cards(venue_slug):
    """A venue's own organizer page (self-published events only) -> same shape."""
    markup = fetch(f"https://shotgun.live/en/venues/{venue_slug}")
    cut = PAST_RE.search(markup)
    if cut:
        markup = markup[: cut.start()]
    else:
        log(f"  warning: no 'Past events' marker on {venue_slug}; relying on end-date filter")
    return {href: card_info(block) for href, block in CARD_RE.findall(markup)}


def shotgun_event_details(path):
    """The schema.org MusicEvent block from an event page."""
    markup = fetch("https://shotgun.live" + path)
    for blob in JSONLD_RE.findall(markup):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if data.get("@type") in ("MusicEvent", "Event"):
            return data
    raise RuntimeError(f"no Event JSON-LD on {path}")


def describe_offers(offers):
    bits = []
    for o in offers or []:
        name = o.get("name") or "Ticket"
        price = o.get("price")
        cur = o.get("priceCurrency", "USD")
        if price in (None, "", 0, "0"):
            cost = "Free"
        elif cur == "USD" and isinstance(price, (int, float)):
            cost = f"${price:g}"
        else:
            cost = f"{price} {cur}"
        sold_out = "SoldOut" in (o.get("availability") or "")
        bits.append(f"{name} {cost}" + (" (sold out)" if sold_out else ""))
    return ", ".join(bits)


def shotgun_events(venues):
    """All Shotgun Miami events; tracked-venue ones enriched from their event pages."""
    log(f"Shotgun: crawling city listing for {CITY_SLUG}")
    cards = shotgun_city_cards(CITY_SLUG)
    log(f"  {len(cards)} events listed")

    for v in venues:
        if v.get("shotgun_page"):
            extra = {p: c for p, c in shotgun_venue_cards(v["shotgun_page"]).items() if p not in cards}
            if extra:
                log(f"  +{len(extra)} from {v['shotgun_page']} page not in city listing")
            cards.update(extra)
            time.sleep(POLITE_DELAY)

    events = []
    for path, card in cards.items():
        slug = path.rsplit("/", 1)[-1]
        url = "https://shotgun.live" + path
        tracked = venue_for(venues, card["venue"])
        start = parse_iso(card["start"]) if card["start"] else None
        ev = new_event(
            id=f"shotgun:{slug}", source="shotgun", title=card["title"], venue=card["venue"],
            venue_slug=tracked["slug"] if tracked else None,
            date=local_date(start) if start else "", start=start.isoformat() if start else None,
            price=card["price"], genres=card["genres"], url=url,
        )
        if tracked:
            time.sleep(POLITE_DELAY)
            data = shotgun_event_details(path)
            loc = data.get("location") or {}
            addr = (loc.get("address") or {}).get("streetAddress", "")
            where = f"{loc.get('name', '')} {addr}".lower()
            confirmed = any(m in where for m in tracked["match"]) or (
                tracked.get("address") and tracked["address"] in where)
            if not confirmed:
                log(f"  {slug}: card says {card['venue']!r} but page says {loc.get('name')!r}; untracking")
                ev["venue_slug"] = None
            s = parse_iso(data["startDate"])
            e = parse_iso(data["endDate"]) if data.get("endDate") else s + DEFAULT_DURATION
            if e <= s:
                e = s + DEFAULT_DURATION
            stamps = [o.get("validFrom") for o in data.get("offers") or [] if o.get("validFrom")]
            ev.update(
                title=clean(data.get("name")) or ev["title"],
                artists=[p.get("name") for p in data.get("performer") or [] if p.get("name")],
                venue=loc.get("name") or ev["venue"], address=addr,
                date=local_date(s), start=s.isoformat(), end=e.isoformat(),
                organizer=(data.get("organizer") or {}).get("name", ""),
                description=clean(data.get("description")),
                tickets=describe_offers(data.get("offers")),
                cancelled="Cancelled" in (data.get("eventStatus") or ""),
                geo=loc.get("geo"), created=min(stamps) if stamps else None,
                url=data.get("url") or url,
            )
        events.append(ev)
    return events


# --------------------------------------------------------------------------- #
# Dice
# --------------------------------------------------------------------------- #

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
DICE_MAX_SPAN = dt.timedelta(days=3)  # belt and braces alongside the is_multi_days_event flag


def dice_events(venues):
    """Upcoming events from each tracked venue's Dice profile (venues.json "dice").

    Dice is where Space / Floyd / The Ground / Factory Town / Kemistry actually
    sell, and its embedded page data carries real start *and* end times plus
    sold-out status - so it outranks Edmtrain (date only) for those rooms.
    """
    events = []
    for v in venues:
        slug = v.get("dice")
        if not slug:
            continue
        time.sleep(POLITE_DELAY)
        markup = fetch(f"https://dice.fm/venue/{slug}")
        m = NEXT_DATA_RE.search(markup)
        if not m:
            raise RuntimeError(f"Dice: no __NEXT_DATA__ on /venue/{slug} - page layout changed?")
        profile = json.loads(m.group(1))["props"]["pageProps"].get("profile") or {}
        n = 0
        for section in profile.get("sections", []):
            for item in section.get("items", []):
                e = item.get("event")
                if not e or not (e.get("dates") or {}).get("event_start_date"):
                    continue
                start = parse_iso(e["dates"]["event_start_date"])
                end = parse_iso(e["dates"]["event_end_date"]) if e["dates"].get("event_end_date") else None
                if e["dates"].get("is_multi_days_event") or (end and end - start > DICE_MAX_SPAN):
                    continue  # passes, donations, season tickets - not a night out
                place = (e.get("venues") or [{}])[0]
                price = (e.get("price") or {}).get("amount_from")
                status = e.get("status") or ""
                events.append(new_event(
                    id=f"dice:{e['id']}", source="dice", title=clean(e.get("name")),
                    venue=place.get("name", "") or v["name"], venue_slug=v["slug"],
                    date=local_date(start), start=start.isoformat(), end=end.isoformat() if end else None,
                    address=place.get("address", ""),
                    price=(f"From ${price / 100:.0f}" if price else "") + (" (sold out)" if status == "sold-out" else ""),
                    url=f"https://dice.fm/event/{e['perm_name']}" if e.get("perm_name") else "",
                    cancelled=status in ("cancelled", "canceled"),
                ))
                n += 1
        log(f"  dice/{slug}: {n} events")
    return events


# --------------------------------------------------------------------------- #
# Tablelist (a venue's own box office, e.g. ZeyZey)
# --------------------------------------------------------------------------- #

def tablelist_events(venues):
    """Upcoming events from a venue's Tablelist page (venues.json "tablelist").

    buy.tablelist.com/v/<slug>/events embeds the list with real start/end
    times. Only what the house itself promotes shows up here - promoter-run
    nights live on Shotgun/Dice - so it's a supplement, not a replacement.
    """
    events = []
    for v in venues:
        slug = v.get("tablelist")
        if not slug:
            continue
        time.sleep(POLITE_DELAY)
        markup = fetch(f"https://buy.tablelist.com/v/{slug}/events")
        m = NEXT_DATA_RE.search(markup)
        if not m:
            raise RuntimeError(f"Tablelist: no __NEXT_DATA__ on /v/{slug}/events - page layout changed?")
        rows = json.loads(m.group(1))["props"]["pageProps"].get("events") or []
        n = 0
        for e in rows:
            if e.get("deleted") or not e.get("dateStart"):
                continue
            start = parse_iso(e["dateStart"])
            end = parse_iso(e["dateEnd"]) if e.get("dateEnd") else None
            events.append(new_event(
                id=f"tablelist:{e['id']}", source="tablelist", title=clean(e.get("name")),
                artists=[p.get("name") for p in e.get("performers") or [] if isinstance(p, dict) and p.get("name")],
                venue=v["name"], venue_slug=v["slug"],
                date=local_date(start), start=start.isoformat(), end=end.isoformat() if end else None,
                description=clean(((e.get("details") or {}).get("description")) or ""),
                url=f"https://buy.tablelist.com/e/{e['id']}",
            ))
            n += 1
        log(f"  tablelist/{slug}: {n} events")
    return events


# --------------------------------------------------------------------------- #
# Clock-time helpers (sources that publish "9pm-3am" style ranges)
# --------------------------------------------------------------------------- #

CLOCK_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun",
                                       "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_clock(text):
    """'9pm' / '11:30 pm' -> (hour, minute) in 24h, else None."""
    m = CLOCK_RE.search(text or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
    return (h + 12 if ap == "pm" else h, mi)


def at_local(day, clock):
    return dt.datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=EASTERN)


def end_after(start_local, clock):
    """End datetime for a clock time on the same night as start (rolls past midnight)."""
    end = at_local(start_local.date(), clock)
    if end <= start_local:
        end += dt.timedelta(days=1)
    return end


def default_end(venue, start_local):
    """Closing time from the venue's typical hours for that weekday, else +5h."""
    hours = (venue or {}).get("hours") or {}
    spec = hours.get(DAYS[start_local.weekday()]) or hours.get("default")
    if start_local.hour < 18 and hours.get("day"):  # pool parties, day events
        spec = hours["day"]
    if spec and spec[1]:
        end = end_after(start_local, tuple(int(x) for x in spec[1].split(":")))
        if end - start_local <= dt.timedelta(hours=16):
            return end
    return start_local + DEFAULT_DURATION


def nearest_date(month, day, today=None):
    """Month/day with no year (a site card) -> the occurrence nearest to today."""
    today = today or dt.date.today()
    cands = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cands.append(dt.date(year, month, day))
        except ValueError:
            pass
    return min(cands, key=lambda d: abs((d - today).days))


# --------------------------------------------------------------------------- #
# 19hz.info - curated metro listing: genres, time ranges, ages, ticket links
# --------------------------------------------------------------------------- #

HZ19_CSV = "https://19hz.info/events_Miami.csv"
HZ19_SPLIT_RE = re.compile(r",|\s/\s|\s\+\s|\s&\s|\sb2b\s|\sx\s|:", re.I)


def hz19_events(venues):
    """Rows of 19hz's Miami CSV as events.

    One person's curation, electronic-only, but every row carries genre tags
    and nearly every row a time range - the two things the ticket platforms
    don't give us. Columns (no header): day label, title, genres, venue (city),
    time range, price, ages, organizer, link, second link, Excel serial of the
    start (date + time, local).
    """
    import csv
    import io
    raw = fetch(HZ19_CSV)
    events, n_time = [], 0
    for r in csv.reader(io.StringIO(raw)):
        if len(r) < 11 or not r[10]:
            continue
        try:
            serial = float(r[10])
        except ValueError:
            continue
        when = dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)
        day = when.date()
        venue_txt = re.sub(r"\s*\([^)]*\)\s*$", "", r[3]).strip()
        city = (re.search(r"\(([^)]*)\)\s*$", r[3]) or [None, ""])[1]
        tracked = venue_for(venues, venue_txt)
        clocks = CLOCK_RE.findall(r[4] or "")
        start = end = None
        if clocks:
            start_c = parse_clock(r[4])
            start_l = at_local(day, start_c)
            start = start_l.astimezone(UTC).isoformat()
            if len(clocks) >= 2:
                end_c = parse_clock(r[4][CLOCK_RE.search(r[4]).end():])
                if end_c:
                    end = end_after(start_l, end_c).astimezone(UTC).isoformat()
            if not end:
                end = default_end(tracked, start_l).astimezone(UTC).isoformat()
            n_time += 1
        genres = sorted({g.strip().lower() for g in (r[2] or "").split(",") if g.strip()})
        parts = [clean(x) for x in HZ19_SPLIT_RE.split(r[1]) if len(squash(x)) >= 4]
        events.append(new_event(
            id=f"19hz:{int(serial * 96)}-{squash(r[1])[:16]}", source="19hz", title=clean(r[1]),
            venue=venue_txt, venue_slug=tracked["slug"] if tracked else None,
            date=day.isoformat(), start=start, end=end, address=city if city and city != "Miami" else "",
            ages=(r[6] or "").strip(), price=(r[5] or "").strip(), genres=genres,
            organizer=(r[7] or "").strip(), url=(r[8] or "").strip() or (r[9] or "").strip(),
            match_names=parts,
        ))
    log(f"  {len(events)} rows, {n_time} with a time, {sum(1 for e in events if e['venue_slug'])} at tracked venues")
    return events


# --------------------------------------------------------------------------- #
# ZeyZey's own calendar (Webflow site, tickets via Opendate)
# --------------------------------------------------------------------------- #

ZZ_CARD_RE = re.compile(r'<div class="event-card">(.*?)</div>\s*</div>\s*(?=<div fs-list-element="item"|</div>)', re.S)
ZZ_TEXT_RE = re.compile(r'class="text-block-61-copy[^"]*"[^>]*>([^<]*)<')
ZZ_TITLE_RE = re.compile(r"<h3[^>]*>([^<]*)</h3>")
ZZ_HREF_RE = re.compile(r'href="(/shows/[^"?#]+)"')
ZZ_BUTTON_RE = re.compile(r'class="[^"]*w-button[^"]*"[^>]*>([^<]*)</a>')


def zeyzey_calendar_events(venues):
    """Every show on calendar.zeyzeymiami.com (the venue's full list, all promoters).

    Cards carry weekday / day / month / start time / title / a genre label and
    an RSVP / Buy Tickets / Sold Out button; no year (inferred) and no end time
    (venue's typical closing). Configured via venues.json "zeyzey_calendar".
    """
    events = []
    for v in venues:
        base = v.get("zeyzey_calendar")
        if not base:
            continue
        time.sleep(POLITE_DELAY)
        markup = fetch(base)
        seen, n = set(), 0
        for card in ZZ_CARD_RE.findall(markup):
            href = ZZ_HREF_RE.search(card)
            title = ZZ_TITLE_RE.search(card)
            texts = [clean(t) for t in ZZ_TEXT_RE.findall(card)]
            if not href or not title or href.group(1) in seen:
                continue
            seen.add(href.group(1))
            try:
                day_no = int(next(t for t in texts if t.isdigit()))
                month = MONTHS[next(t for t in texts if t.lower()[:3] in MONTHS and not t.isdigit()).lower()[:3]]
            except (StopIteration, KeyError, ValueError):
                continue
            clock = next((parse_clock(t) for t in texts if CLOCK_RE.search(t)), None)
            day = nearest_date(month, day_no)
            start = end = None
            if clock:
                start_l = at_local(day, clock)
                start, end = start_l.astimezone(UTC).isoformat(), default_end(v, start_l).astimezone(UTC).isoformat()
            genre = [t.lower() for t in texts if t and not t.isdigit() and not CLOCK_RE.search(t)
                     and t.lower()[:3] not in MONTHS and t not in ("-",)
                     and t.lower() not in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
            button = ZZ_BUTTON_RE.findall(card)
            status = (button[-1] if button else "").strip().lower()
            events.append(new_event(
                id=f"zeyzey:{href.group(1).rsplit('/', 1)[-1]}", source="venue", title=clean(title.group(1)),
                venue=v["name"], venue_slug=v["slug"], date=day.isoformat(), start=start, end=end,
                genres=[g for g in genre if g][:1], price="Free" if status == "rsvp" else ("(sold out)" if "sold" in status else ""),
                url=base.rstrip("/") + href.group(1), cancelled=False,
            ))
            n += 1
        log(f"  venue calendar {base}: {n} shows")
    return events


# --------------------------------------------------------------------------- #
# Hard Rock Nightlife calendar (DAER Nightclub / Dayclub, Webflow + Tixr)
# --------------------------------------------------------------------------- #

HR_CARD_SPLIT = 'class="event-item w-dyn-item"'
HR_NAME_RE = re.compile(r'class="ticket-name[^"]*"[^>]*>([^<]*)<')
HR_DATE_RE = re.compile(r'class="month[^"]*"[^>]*>([^<]*)<')
HR_TIXR_RE = re.compile(r'href="(https?://(?:www\.)?tixr\.com/e/\d+)"')
GENERIC_TITLE_RE = re.compile(r"^(mon|tues|wednes|thurs|fri|satur|sun)day,?\s+[a-z]+\s+\d{1,2}(st|nd|rd|th)?$", re.I)


def hardrock_events(venues):
    """Cards on hardrocknightlife.com/calendar for the rooms named in
    venues.json "hardrock_calendar" (e.g. ["daer"]). Each card: "Title | Room",
    weekday / month / day / start time, and a Tixr link. No year (inferred), no
    end time (venue's typical closing, with the daytime rule for the Dayclub).
    """
    events = []
    for v in venues:
        cfg = v.get("hardrock_calendar")
        if not cfg:
            continue
        time.sleep(POLITE_DELAY)
        markup = fetch(cfg["url"])
        rooms = [r.lower() for r in cfg.get("rooms", [])]
        n = 0
        for card in markup.split(HR_CARD_SPLIT)[1:]:
            name = HR_NAME_RE.search(card)
            if not name:
                continue
            full = clean(name.group(1))
            title, _, room = full.rpartition(" | ")
            if not room:
                title, room = full, ""
            if rooms and not any(r in room.lower() for r in rooms):
                continue
            generic = bool(GENERIC_TITLE_RE.match(title))
            if generic:
                title = room or v["name"]
            texts = [clean(t) for t in HR_DATE_RE.findall(card)]
            try:
                day_no = int(next(t for t in texts if t.isdigit()))
                month = MONTHS[next(t for t in texts if t.lower()[:3] in MONTHS and not t.isdigit()).lower()[:3]]
            except (StopIteration, KeyError, ValueError):
                continue
            clock = next((parse_clock(t) for t in texts if CLOCK_RE.search(t)), None)
            day = nearest_date(month, day_no)
            start = end = None
            if clock:
                start_l = at_local(day, clock)
                start, end = start_l.astimezone(UTC).isoformat(), default_end(v, start_l).astimezone(UTC).isoformat()
            tixr = HR_TIXR_RE.search(card)
            ident = tixr.group(1).rsplit("/", 1)[-1] if tixr else f"{day.isoformat()}-{squash(full)[:16]}"
            events.append(new_event(
                id=f"hardrock:{ident}", source="venue", title=title or room,
                venue=room or v["name"], venue_slug=v["slug"], date=day.isoformat(), start=start, end=end,
                address=v.get("street", ""), url=tixr.group(1) if tixr else cfg["url"],
                generic_title=generic,
            ))
            n += 1
        log(f"  hard rock calendar: {n} shows for {v['slug']}")
    return events


# --------------------------------------------------------------------------- #
# Edmtrain
# --------------------------------------------------------------------------- #

def edmtrain_events(venues):
    key = os.environ.get("EDMTRAIN_CLIENT_KEY")
    rows = []
    if key:
        log("Edmtrain: official API")
        url = "https://edmtrain.com/api/events?" + urllib.parse.urlencode(
            {"locationIds": EDMTRAIN_LOCATION_ID, "client": key})
        payload = json.loads(fetch(url))
        if not payload.get("success", True):
            raise RuntimeError(f"Edmtrain API error: {payload.get('message')}")
        for e in payload.get("data", []):
            venue = e.get("venue") or {}
            rows.append({
                "id": str(e.get("id")), "date": e.get("date", ""),
                "start_time": e.get("startTime"), "end_time": e.get("endTime"),
                "title": e.get("name") or ", ".join(a["name"] for a in e.get("artistList", [])),
                "artists": [a["name"] for a in e.get("artistList", [])],
                "venue": venue.get("name", ""), "address": venue.get("address", ""),
                "ages": e.get("ages") or "", "url": e.get("link", ""),
            })
    else:
        log("Edmtrain: public front-end endpoint (set EDMTRAIN_CLIENT_KEY for the API)")
        url = "https://edmtrain.com/get-events?" + urllib.parse.urlencode({
            "locationIdArray[]": EDMTRAIN_LOCATION_ID, "includeElectronic": "true",
            "includeOther": "false", "timeZoneId": "America/New_York"})
        headers = dict(BROWSER_HEADERS)
        headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": "https://edmtrain.com/miami-fl"})
        markup = fetch(url, headers)
        seen = set()
        for attr_blob, block in EDM_CONTAINER_RE.findall(markup):
            attrs = dict(EDM_ATTR_RE.findall(attr_blob))
            eid = attrs.get("eventid")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            address = EDM_ADDRESS_RE.search(block)
            url_m = EDM_URL_RE.search(block)
            ages = EDM_AGES_RE.search(block)
            rows.append({
                "id": eid, "date": attrs.get("sorteddate", ""), "start_time": None, "end_time": None,
                "title": clean(attrs.get("titlestr")),
                "artists": [clean(a) for a in EDM_ARTIST_RE.findall(block) if clean(a)],
                "venue": clean(attrs.get("venue")), "address": clean(address.group(1)) if address else "",
                "ages": ages.group(1) if ages else "", "url": clean(url_m.group(1)) if url_m else "",
            })
    log(f"  {len(rows)} events")

    events = []
    for r in rows:
        if not r["date"]:
            continue
        tracked = venue_for(venues, r["venue"])
        start = end = None
        if r.get("start_time"):  # API only; "HH:MM:SS" local
            try:
                s = dt.datetime.fromisoformat(f"{r['date']}T{r['start_time']}").replace(tzinfo=EASTERN)
                start = s.astimezone(UTC).isoformat()
                if r.get("end_time"):
                    e = dt.datetime.fromisoformat(f"{r['date']}T{r['end_time']}").replace(tzinfo=EASTERN)
                    if e <= s:
                        e += dt.timedelta(days=1)
                    end = e.astimezone(UTC).isoformat()
            except ValueError:
                pass
        events.append(new_event(
            id=f"edmtrain:{r['id']}", source="edmtrain", title=r["title"], artists=r["artists"],
            venue=r["venue"], venue_slug=tracked["slug"] if tracked else None, date=r["date"],
            start=start, end=end, address=r["address"], ages=r["ages"], url=r["url"],
        ))
    return events


# --------------------------------------------------------------------------- #
# Merge + tag
# --------------------------------------------------------------------------- #

TRANSLIT = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ð": "d", "þ": "th", "ß": "ss", "æ": "ae", "œ": "oe"})


def squash(text):
    """Lowercase ASCII alphanumerics only, accents stripped: 'Łaszewo' == 'Laszewo', 'SoDown' == 'SO DOWN'."""
    text = unicodedata.normalize("NFKD", (text or "").lower().translate(TRANSLIT))
    return re.sub(r"[^a-z0-9]", "", text)


def title_key(title):
    return squash(title)[:12]


def drop_excluded(events, venues):
    """Remove events whose title matches a venue's "exclude" patterns (e.g. a comedy room)."""
    rules = {v["slug"]: [x.lower() for x in v.get("exclude", [])] for v in venues if v.get("exclude")}
    kept, dropped = [], 0
    for ev in events:
        pats = rules.get(ev["venue_slug"])
        if pats and any(p in ev["title"].lower() for p in pats):
            dropped += 1
            continue
        kept.append(ev)
    if dropped:
        log(f"  {dropped} event(s) dropped by venue exclude rules")
    return kept


def night_key(ev):
    """Same-night bucket: tracked venue slug, else a prefix of the venue name."""
    return (ev["venue_slug"] or squash(ev["venue"])[:8] or None, ev["date"])


def names_of(ev):
    """Normalized names to match on: artists, the title, and any match_names."""
    return {squash(a) for a in ev["artists"] if a} | {squash(ev["title"])} | \
           {squash(n) for n in ev.get("match_names", []) if squash(n)}


def looks_same(a, b):
    """Two events on the same night at the same venue that share a name."""
    if a.get("generic_title") or b.get("generic_title"):  # "Saturday, October 3rd | DAER Nightclub"
        va, vb = squash(a["venue"]), squash(b["venue"])
        return bool(va and vb and (va in vb or vb in va))
    na, nb = names_of(a), names_of(b)
    if na & nb:
        return True
    ta, tb = squash(a["title"]), squash(b["title"])
    return any(len(n) >= 4 and (n in tb) for n in na) or any(len(n) >= 4 and (n in ta) for n in nb)


def merge(primary, secondary):
    """Fold `secondary` rows into `primary` events that are clearly the same night.

    The primary event keeps its identity and, if it has them, its times; the
    secondary contributes what the primary lacks (times, lineup, ages, price),
    its genre tags (unioned) and its link. Called once per source in priority
    order: Shotgun, then Dice, Tablelist, the venue calendar, 19hz, Edmtrain.
    """
    # Source order isn't stable run to run (Edmtrain repeats events across
    # widgets), and which row folds in first decides the lineup - so sort.
    primary = sorted(primary, key=lambda e: (e["date"], e["id"]))
    secondary = sorted(secondary, key=lambda e: (e["date"], e["id"]))
    by_key = {}
    for ev in primary:
        by_key.setdefault((ev["date"], title_key(ev["title"])), ev)
    by_night = {}
    for ev in primary:
        k = night_key(ev)
        if k[0]:
            by_night.setdefault(k, []).append(ev)

    merged = list(primary)
    for ev in secondary:
        target = by_key.get((ev["date"], title_key(ev["title"])))
        if not target:
            same_night = by_night.get(night_key(ev), []) if night_key(ev)[0] else []
            if len(same_night) == 1 and (ev["venue_slug"] or looks_same(ev, same_night[0])):
                target = same_night[0]
            elif same_night:
                overlap = [s for s in same_night if looks_same(ev, s)]
                target = overlap[0] if len(overlap) == 1 else None
        if target:
            target["source"] = "+".join(sorted(set(target["source"].split("+")) | {ev["source"]}))
            if target.get("generic_title") and ev["title"] and not ev.get("generic_title"):
                target["title"], target["generic_title"] = ev["title"], False
            if not target["start"] and ev["start"]:
                target["start"], target["end"], target["time_estimated"] = ev["start"], ev["end"], False
            elif target["start"] and not target["end"] and ev["end"]:
                target["end"] = ev["end"]
            target["artists"] = target["artists"] or ev["artists"]
            target["ages"] = target["ages"] or ev["ages"]
            target["price"] = target["price"] or ev["price"]
            target["genres"] = sorted(set(target["genres"]) | set(ev["genres"]))
            if ev.get("match_names"):
                target.setdefault("match_names", []).extend(ev["match_names"])
            target.setdefault("alt_urls", []).append(ev["url"])
        else:
            merged.append(ev)
    return merged


def apply_genre_hints(events, hints):
    artists, venues = hints.get("artists", {}), hints.get("venues", {})
    for ev in events:
        if ev["genres"]:
            continue
        hay = " ".join([ev["title"]] + ev["artists"]).lower()
        tags = sorted({g for needle, gs in artists.items() if needle in hay for g in gs})
        if tags:
            ev["genres"] = tags
            continue
        vl = ev["venue"].lower()
        tags = sorted({g for needle, gs in venues.items() if needle in vl for g in gs})
        if tags:
            ev["genres"], ev["genres_inferred"] = tags, True
    return events


# --------------------------------------------------------------------------- #
# iCalendar
# --------------------------------------------------------------------------- #

def ics_dt(value):
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics_text(value):
    value = value or ""
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\n", "\\n")


def fold(line):
    """RFC 5545 line folding: max 75 octets per physical line."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74  # continuation lines start with a space
        if len(chunk) + len(b) > limit:
            # Never break between a backslash and the character it escapes.
            if chunk.endswith(b"\\"):
                chunk, carry = chunk[:-1], b"\\"
            else:
                carry = b""
            out.append(chunk.decode("utf-8"))
            chunk = carry + b
        else:
            chunk += b
    out.append(chunk.decode("utf-8"))
    return [out[0]] + [" " + c for c in out[1:]]


def vevent(ev, venue):
    """(uid, body lines) - the content of a VEVENT without its change stamps.

    DTSTAMP / LAST-MODIFIED / SEQUENCE are added by stamp_vevents() from the
    change-tracking state, so they move only when this body actually changes.
    Clients (Google included) use those to decide whether to apply an update
    to an event they already hold; a same-UID rewrite with a stale DTSTAMP and
    no SEQUENCE can be silently ignored.
    """
    kind, ident = ev["id"].split(":", 1)
    # Non-Shotgun UIDs are scoped to the feed: a whole-complex party (Space +
    # The Ground) is listed by Dice under both venues and belongs on both
    # calendars, and each copy needs its own change-tracking entry.
    uid = f"{ident}@shotgun.live" if kind == "shotgun" else f"{kind}-{ident}@{venue['slug']}.miami-calendars"
    lines = []

    if ev["start"]:
        start = parse_iso(ev["start"])
        end = parse_iso(ev["end"]) if ev["end"] else start + DEFAULT_DURATION
        lines += [f"DTSTART:{ics_dt(start)}", f"DTEND:{ics_dt(end)}"]
    else:
        day = dt.date.fromisoformat(ev["date"])
        lines += [
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}",
        ]

    location = ", ".join(x for x in [ev["venue"] or venue["name"], ev["address"]] if x)
    desc = []
    if ev["description"]:
        desc.append(ev["description"])
    if ev["artists"]:
        desc.append("Lineup: " + ", ".join(ev["artists"]))
    if ev["genres"]:
        desc.append(("Tags (guess): " if ev["genres_inferred"] else "Tags: ") + ", ".join(ev["genres"]))
    if ev["organizer"] and not any(m in ev["organizer"].lower() for m in venue["match"]):
        desc.append("Presented by: " + ev["organizer"])
    if ev.get("tickets"):
        desc.append("Tickets: " + ev["tickets"])
    if ev["ages"]:
        desc.append("Ages: " + ev["ages"])
    if ev.get("generic_title"):
        desc.append("No headliner announced yet.")
    if ev["time_estimated"]:
        desc.append(f"Times are an estimate from {venue['name']}'s usual hours - the listing has no "
                    "start time yet. Check the link.")
    elif not ev["start"]:
        desc.append("Start time not listed - check the link.")
    if ev["url"]:
        desc.append(ev["url"])
    for alt in sorted(set(ev.get("alt_urls", []))):
        if alt and alt != ev["url"]:
            desc.append(alt)

    lines += [
        f"SUMMARY:{ics_text(ev['title'])}",
        f"LOCATION:{ics_text(location)}",
        "DESCRIPTION:" + ics_text("\n".join(desc)),
        *([f"URL:{ev['url']}"] if ev["url"] else []),
        "STATUS:" + ("CANCELLED" if ev.get("cancelled") else "CONFIRMED"),
    ]
    if ev["genres"]:
        lines.append("CATEGORIES:" + ",".join(ics_text(g) for g in ev["genres"]))
    if ev.get("geo"):
        lines.append(f"GEO:{ev['geo'].get('latitude')};{ev['geo'].get('longitude')}")
    return uid, lines


def stamp_vevents(built, state, now):
    """Wrap (uid, body) pairs in BEGIN/END with DTSTAMP, LAST-MODIFIED and SEQUENCE.

    `state` maps uid -> {"hash", "seq", "modified"} and is updated in place: a
    new uid starts at SEQUENCE 0; a changed body bumps SEQUENCE and moves the
    stamps to `now`; an unchanged body keeps its previous stamps, so a run that
    changes nothing produces byte-identical files.
    """
    out = []
    for uid, body in built:
        digest = hashlib.sha1("\n".join(body).encode("utf-8")).hexdigest()[:16]
        prev = state.get(uid)
        if prev is None:
            entry = {"hash": digest, "seq": 0, "modified": now}
        elif prev["hash"] != digest:
            entry = {"hash": digest, "seq": prev["seq"] + 1, "modified": now}
        else:
            entry = prev
        state[uid] = entry
        stamp = ics_dt(parse_iso(entry["modified"]))
        out.append(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}",
                    f"SEQUENCE:{entry['seq']}"] + body + ["END:VEVENT"])
    return out


def calendar_text(name, vevents):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_text(name)}", "X-WR-TIMEZONE:America/New_York",
        "X-PUBLISHED-TTL:PT6H", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    for ev in vevents:
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    physical = []
    for line in lines:
        physical.extend(fold(line))
    return "\r\n".join(physical) + "\r\n"


def write(name, text, binary=False):
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Miami venue calendars from Shotgun + Edmtrain")
    ap.add_argument("--only", help="build just this venue slug (others are left untouched)")
    args = ap.parse_args()

    venues = load_json("venues.json")
    hints = load_json("genre_hints.json")
    if args.only:
        venues = [v for v in venues if v["slug"] == args.only] or sys.exit(f"no venue {args.only!r}")

    events = drop_excluded(shotgun_events(venues), venues)
    log("Dice: venue profiles")
    events = merge(events, drop_excluded(dice_events(venues), venues))
    events = merge(events, drop_excluded(tablelist_events(venues), venues))
    log("Venue calendars")
    events = merge(events, drop_excluded(zeyzey_calendar_events(venues), venues))
    events = merge(events, drop_excluded(hardrock_events(venues), venues))
    log("19hz.info")
    events = merge(events, drop_excluded(hz19_events(venues), venues))
    events = merge(events, drop_excluded(edmtrain_events(venues), venues))
    events = apply_genre_hints(events, hints)
    estimated = fill_typical_hours(events, venues, learn_hours(events))
    log(f"  {estimated} event(s) given the venue's typical hours (no published time)")
    events.sort(key=lambda e: (e["date"], e["start"] or "~", e["venue"], e["title"]))

    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=1)).isoformat()
    state_path = os.path.join(HERE, STATE_FILE)
    state = load_json(STATE_FILE) if os.path.exists(state_path) else {}
    now = dt.datetime.now(UTC).replace(microsecond=0).isoformat()
    seen = set()
    feeds = []
    for v in venues:
        mine = [e for e in events if e["venue_slug"] == v["slug"] and e["date"] >= cutoff]
        built = [vevent(e, v) for e in mine]
        seen.update(uid for uid, _ in built)
        write(f"{v['slug']}.ics", calendar_text(v["name"], stamp_vevents(built, state, now)))
        srcs = sorted({s for e in mine for s in e["source"].split("+")})
        est = sum(1 for e in mine if e["time_estimated"])
        feeds.append({"slug": v["slug"], "name": v["name"], "file": f"{v['slug']}.ics",
                      "events": len(mine), "estimated_times": est, "next": mine[0]["date"] if mine else None,
                      "sources": srcs, "hours_note": v.get("hours_note", "")})
        log(f"  {v['slug']:14s} {len(mine):3d} events  ({'+'.join(srcs) or 'none'}; {est} with estimated times)")

    if not args.only:
        state = {uid: state[uid] for uid in sorted(seen)}  # forget events that have dropped off
        keep = [{k: x for k, x in e.items() if k != "match_names"} for e in events if e["date"] >= cutoff]
        write("events.json", json.dumps({"events": keep}, indent=1, ensure_ascii=False) + "\n")
        write("feeds.json", json.dumps(feeds, indent=1) + "\n")
        log(f"Wrote {len(keep)} events to events.json and {len(feeds)} feeds")
    else:
        state = dict(sorted(state.items()))
    changed = sum(1 for uid in seen if state[uid]["modified"] == now)
    write(STATE_FILE, json.dumps(state, indent=1) + "\n")
    log(f"  {changed} event(s) new or changed since last run")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # any failure -> non-zero, keep the previous feeds
        log(f"FAILED: {exc!r}")
        sys.exit(1)
