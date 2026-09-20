"""Build an iCalendar (.ics) feed of upcoming events at a Shotgun venue.

Google Calendar (and anything else) can subscribe to the published file.

Discovery: Shotgun's /en/venues/<slug> page is really the venue's *own organizer
profile* - promoters who rent the room publish under their own profile and only
set the location to the venue, so those shows never appear there. The city
listing (/en/cities/<city>?page=N) does list everything, with the venue name on
each card, so that is the primary source; the venue page is unioned in as a
backstop. Every candidate is then confirmed against the schema.org MusicEvent
JSON-LD on its event page (location name / street address), which also supplies
the real start/end times, lineup and ticket tiers. Genre chips only exist on the
listing cards, so they are picked up there.

Usage:
    python boombox_ics.py                                  # -> boombox.ics
    python boombox_ics.py --city miami --match "club space" --venue-slug club-space --out space.ics

Exit status is non-zero if anything fails to fetch, so a scheduled run leaves
the previous feed in place rather than publishing a partial one (a subscribed
calendar would otherwise delete the missing events).
"""

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request

CITY_SLUG = "miami"
VENUE_SLUG = "the-boombox-miami"        # the venue's own organizer page
VENUE_MATCH = "boombox"                 # case-insensitive substring on venue name
VENUE_ADDRESS = "4447 southwest 75th"   # second confirmation signal
OUT_FILE = "boombox.ics"
FEED_NAME = "The Boombox Miami"
TIMEZONE_ID = "America/New_York"
PRODID = "-//TheDieselPunk//boombox-calendar//EN"
MAX_CITY_PAGES = 25
POLITE_DELAY = 1.0

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

CARD_RE = re.compile(r'<a data-slot="tracked-link" href="(/en/events/[^"?#]+)"[^>]*>(.*?)</a>', re.S)
VENUE_RE = re.compile(r'<div class="text-muted-foreground[^"]*whitespace-nowrap">([^<]*)</div>')
BADGE_RE = re.compile(r'rounded-full border[^"]*"[^>]*>([^<]{2,40})</div>')
JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
PAST_RE = re.compile(r"PAST EVENTS|Past events")

if hasattr(sys.stderr, "reconfigure"):  # Windows consoles default to cp1252
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(msg, file=sys.stderr)


def fetch(url, retries=3):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
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


# --------------------------------------------------------------------------- #
# Shotgun discovery
# --------------------------------------------------------------------------- #

def card_info(block):
    """(venue text, [genre, ...]) from one listing card."""
    venue = VENUE_RE.search(block)
    # Chips include the date/price; genre chips are the rest (skip '+N' overflow chips).
    badges = [clean(b) for b in BADGE_RE.findall(block)]
    genres = [b.lower() for b in badges if b and not b.startswith("+") and not re.search(r"\d", b)]
    return (clean(venue.group(1)) if venue else "", genres)


def city_cards(city_slug):
    """Crawl the paginated city listing -> {event_path: (venue_text, genres)}."""
    out = {}
    for page in range(MAX_CITY_PAGES):
        url = f"https://shotgun.live/en/cities/{city_slug}" + (f"?page={page}" if page else "")
        found = CARD_RE.findall(fetch(url))
        if page == 0 and not found:
            raise RuntimeError("city listing has no event cards - page layout changed?")
        new = 0
        for href, block in found:
            if href not in out:
                out[href] = card_info(block)
                new += 1
        if not new:
            break
        time.sleep(POLITE_DELAY)
    return out


def venue_cards(venue_slug):
    """The venue's own organizer page (self-published events only) -> same shape."""
    markup = fetch(f"https://shotgun.live/en/venues/{venue_slug}")
    cut = PAST_RE.search(markup)
    if cut:
        markup = markup[: cut.start()]
    else:
        log("  warning: no 'Past events' marker on venue page; relying on end-date filter")
    return {href: card_info(block) for href, block in CARD_RE.findall(markup)}


def event_details(path):
    """Parse the schema.org MusicEvent block from an event page."""
    markup = fetch("https://shotgun.live" + path)
    for blob in JSONLD_RE.findall(markup):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if data.get("@type") in ("MusicEvent", "Event"):
            return data
    raise RuntimeError(f"no Event JSON-LD on {path}")


def at_venue(data, match, address):
    loc = data.get("location") or {}
    where = " ".join([loc.get("name", ""), (loc.get("address") or {}).get("streetAddress", "")]).lower()
    return match in where or address in where


# --------------------------------------------------------------------------- #
# iCalendar
# --------------------------------------------------------------------------- #

def parse_iso(stamp):
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def ics_dt(value):
    return value.strftime("%Y%m%dT%H%M%SZ")


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


def build_vevent(path, genres, data):
    slug = path.rsplit("/", 1)[-1]
    start = parse_iso(data["startDate"])
    end = parse_iso(data["endDate"]) if data.get("endDate") else start + dt.timedelta(hours=5)
    if end <= start:
        end = start + dt.timedelta(hours=5)

    # Stable DTSTAMP so unchanged events produce byte-identical output between runs.
    stamps = [o.get("validFrom") for o in data.get("offers") or [] if o.get("validFrom")]
    dtstamp = parse_iso(min(stamps)) if stamps else start

    loc = data.get("location") or {}
    addr = (loc.get("address") or {}).get("streetAddress", "")
    location = ", ".join(x for x in [loc.get("name", ""), addr] if x)

    performers = [p.get("name") for p in data.get("performer") or [] if p.get("name")]
    organizer = (data.get("organizer") or {}).get("name", "")
    desc_lines = []
    if data.get("description"):
        desc_lines.append(clean(data["description"]))
    if performers:
        desc_lines.append("Lineup: " + ", ".join(performers))
    if genres:
        desc_lines.append("Tags: " + ", ".join(genres))
    if organizer and VENUE_MATCH not in organizer.lower():
        desc_lines.append("Presented by: " + organizer)
    tickets = describe_offers(data.get("offers"))
    if tickets:
        desc_lines.append("Tickets: " + tickets)
    url = data.get("url") or "https://shotgun.live" + path
    desc_lines.append(url)

    status = "CANCELLED" if "Cancelled" in (data.get("eventStatus") or "") else "CONFIRMED"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{slug}@shotgun.live",
        f"DTSTAMP:{ics_dt(dtstamp)}",
        f"DTSTART:{ics_dt(start)}",
        f"DTEND:{ics_dt(end)}",
        f"SUMMARY:{ics_text(clean(data.get('name')))}",
        f"LOCATION:{ics_text(location)}",
        "DESCRIPTION:" + ics_text("\n".join(desc_lines)),
        f"URL:{url}",
        f"STATUS:{status}",
    ]
    if genres:
        lines.append("CATEGORIES:" + ",".join(ics_text(g) for g in genres))
    if loc.get("geo"):
        lines.append(f"GEO:{loc['geo'].get('latitude')};{loc['geo'].get('longitude')}")
    lines.append("END:VEVENT")
    return lines, start, end


def build_calendar(feed_name, vevents):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_text(feed_name)}",
        f"X-WR-TIMEZONE:{TIMEZONE_ID}",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    for ev in vevents:
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    physical = []
    for line in lines:
        physical.extend(fold(line))
    return "\r\n".join(physical) + "\r\n"


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Shotgun venue -> .ics feed")
    ap.add_argument("--city", default=CITY_SLUG, help="shotgun.live/en/cities/<slug>")
    ap.add_argument("--match", default=VENUE_MATCH, help="substring of the venue name (case-insensitive)")
    ap.add_argument("--address", default=VENUE_ADDRESS, help="substring of the street address (backup match)")
    ap.add_argument("--venue-slug", default=VENUE_SLUG, help="shotgun.live/en/venues/<slug>, unioned in")
    ap.add_argument("--name", default=FEED_NAME, help="calendar display name")
    ap.add_argument("--out", default=OUT_FILE)
    args = ap.parse_args()
    match, address = args.match.lower(), args.address.lower()

    log(f"Crawling city listing for {args.city}")
    city = city_cards(args.city)
    candidates = {p: info for p, info in city.items() if match in info[0].lower()}
    log(f"  {len(city)} events listed, {len(candidates)} at venue")

    log(f"Fetching venue page for {args.venue_slug}")
    own = venue_cards(args.venue_slug)
    extra = [p for p in own if p not in candidates]
    for p in extra:
        candidates[p] = own[p]
    log(f"  {len(own)} upcoming on venue page, {len(extra)} not in city listing")

    now = dt.datetime.now(dt.timezone.utc)
    built = []
    for path, (_venue_text, genres) in candidates.items():
        time.sleep(POLITE_DELAY)
        data = event_details(path)
        if not at_venue(data, match, address):
            loc = (data.get("location") or {}).get("name", "?")
            log(f"  skip (location is {loc!r}): {path}")
            continue
        ev, start, end = build_vevent(path, genres, data)
        if end < now - dt.timedelta(days=1):
            log(f"  skip (already ended): {path}")
            continue
        log(f"  {start.astimezone().strftime('%a %b %d %I:%M %p')}  {clean(data.get('name'))}")
        built.append((start, path, ev))

    built.sort(key=lambda t: (t[0], t[1]))  # deterministic order -> byte-identical reruns
    text = build_calendar(args.name, [ev for _, _, ev in built])
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    log(f"Wrote {len(built)} event(s) to {args.out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # any failure -> non-zero, keep the previous feed
        log(f"FAILED: {exc!r}")
        sys.exit(1)
