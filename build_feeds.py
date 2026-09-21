"""Miami venue calendars: one crawl, one .ics feed per venue.

Sources
  * Shotgun  - the paginated Miami city listing (/en/cities/miami?page=N). For
               events at tracked venues the event page's schema.org MusicEvent
               JSON-LD is fetched too: real start/end, address, lineup, ticket
               tiers. Genre chips only exist on listing cards. A venue's own
               /en/venues/<slug> page is just its organizer profile (promoter-run
               shows don't appear there), so it's only used as a backstop.
  * Edmtrain - metro-wide coverage (location id 87) including the rooms that
               don't sell on Shotgun (Space, Floyd, Kemistry, ...). Date only:
               no start time, no genres. The official API is used automatically
               when EDMTRAIN_CLIENT_KEY is set (free personal keys at
               https://edmtrain.com/developer-api); it may carry start times.

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

def title_key(title):
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())[:12]


def merge(shotgun, edmtrain):
    """Fold Edmtrain rows into Shotgun events that are clearly the same night."""
    by_key = {}
    for ev in shotgun:
        by_key.setdefault((ev["date"], title_key(ev["title"])), ev)
    by_venue_date = {}
    for ev in shotgun:
        if ev["venue_slug"]:
            by_venue_date.setdefault((ev["venue_slug"], ev["date"]), []).append(ev)

    merged = list(shotgun)
    for ev in edmtrain:
        target = by_key.get((ev["date"], title_key(ev["title"])))
        if not target and ev["venue_slug"]:
            same_night = by_venue_date.get((ev["venue_slug"], ev["date"]), [])
            if len(same_night) == 1:
                target = same_night[0]
            elif same_night:
                names = {a.lower() for a in ev["artists"]}
                overlap = [s for s in same_night
                           if names & {a.lower() for a in s["artists"]} or
                           any(a.lower() in s["title"].lower() for a in ev["artists"])]
                target = overlap[0] if len(overlap) == 1 else None
        if target:
            target["source"] = "shotgun+edmtrain"
            target["artists"] = target["artists"] or ev["artists"]
            target["ages"] = target["ages"] or ev["ages"]
            target["edmtrain_url"] = ev["url"]
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
    uid = f"{ident}@shotgun.live" if kind == "shotgun" else f"edmtrain-{ident}@miami-calendars"
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
    if ev["time_estimated"]:
        desc.append(f"Times are an estimate from {venue['name']}'s usual hours - the listing has no "
                    "start time yet. Check the link.")
    elif not ev["start"]:
        desc.append("Start time not listed - check the link.")
    desc.append(ev["url"])
    if ev.get("edmtrain_url") and ev["edmtrain_url"] != ev["url"]:
        desc.append(ev["edmtrain_url"])

    lines += [
        f"SUMMARY:{ics_text(ev['title'])}",
        f"LOCATION:{ics_text(location)}",
        "DESCRIPTION:" + ics_text("\n".join(desc)),
        f"URL:{ev['url']}",
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

    events = merge(shotgun_events(venues), edmtrain_events(venues))
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
        keep = [e for e in events if e["date"] >= cutoff]
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
