"""Build an iCalendar (.ics) feed of upcoming events at a Shotgun venue.

Google Calendar (and anything else) can subscribe to the published file. Event
details come from the schema.org MusicEvent JSON-LD that Shotgun embeds on each
event page, which carries real start/end times, the address, performers and
ticket tiers. The venue page is only used to discover which events are upcoming
and to pick up the promoter's genre chips (JSON-LD has no genre field).

Usage:
    python boombox_ics.py                                  # -> boombox.ics
    python boombox_ics.py --venue some-other-slug --out other.ics

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

VENUE_SLUG = "the-boombox-miami"
OUT_FILE = "boombox.ics"
FEED_NAME = "The Boombox Miami"
TIMEZONE_ID = "America/New_York"
PRODID = "-//TheDieselPunk//boombox-calendar//EN"

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
# Shotgun
# --------------------------------------------------------------------------- #

def upcoming_events(venue_slug):
    """Return [(event_path, [genre, ...]), ...] for the venue's upcoming events."""
    markup = fetch(f"https://shotgun.live/en/venues/{venue_slug}")
    cut = PAST_RE.search(markup)
    if cut:
        markup = markup[: cut.start()]
    else:
        log("  warning: no 'Past events' marker on venue page; relying on end-date filter")

    out, seen = [], set()
    for href, block in CARD_RE.findall(markup):
        if href in seen:
            continue
        seen.add(href)
        # Chips include the date/price; genre chips are the rest (skip '+N' overflow chips).
        badges = [clean(b) for b in BADGE_RE.findall(block)]
        genres = [b.lower() for b in badges if b and not b.startswith("+") and not re.search(r"\d", b)]
        out.append((href, genres))
    return out


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
    desc_lines = []
    if data.get("description"):
        desc_lines.append(clean(data["description"]))
    if performers:
        desc_lines.append("Lineup: " + ", ".join(performers))
    if genres:
        desc_lines.append("Tags: " + ", ".join(genres))
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
    ap.add_argument("--venue", default=VENUE_SLUG, help="shotgun.live/en/venues/<slug>")
    ap.add_argument("--name", default=FEED_NAME, help="calendar display name")
    ap.add_argument("--out", default=OUT_FILE)
    args = ap.parse_args()

    log(f"Fetching venue page for {args.venue}")
    cards = upcoming_events(args.venue)
    log(f"  {len(cards)} upcoming event(s) listed")

    now = dt.datetime.now(dt.timezone.utc)
    vevents = []
    for path, genres in cards:
        time.sleep(1)  # be polite
        data = event_details(path)
        ev, start, end = build_vevent(path, genres, data)
        if end < now - dt.timedelta(days=1):
            log(f"  skip (already ended): {path}")
            continue
        log(f"  {start.astimezone().strftime('%a %b %d %I:%M %p')}  {clean(data.get('name'))}")
        vevents.append(ev)

    text = build_calendar(args.name, vevents)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    log(f"Wrote {len(vevents)} event(s) to {args.out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # any failure -> non-zero, keep the previous feed
        log(f"FAILED: {exc!r}")
        sys.exit(1)
