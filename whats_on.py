"""What's on in Miami - reads events.json written by build_feeds.py.

    python whats_on.py                     # next 7 days, all venues
    python whats_on.py --days 14
    python whats_on.py --date fri          # one night (weekday name or YYYY-MM-DD)
    python whats_on.py --venue floyd       # a tracked venue slug, or any substring of a venue name
    python whats_on.py --tracked           # only the venues that have calendars
    python whats_on.py --genre techno --exclude house
    python whats_on.py --json out.json

events.json is refreshed by the scheduled task every 6 hours; run
`python build_feeds.py` first if you need it fresher than that.
"""

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STALE_AFTER = dt.timedelta(hours=7)


def resolve_date(token):
    try:
        return dt.date.fromisoformat(token)
    except ValueError:
        pass
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    token = token.lower()
    for idx, name in enumerate(names):
        if name.startswith(token) or token.startswith(name[:3]):
            today = dt.date.today()
            return today + dt.timedelta(days=(idx - today.weekday()) % 7)
    raise SystemExit(f"Could not read date: {token!r} (use YYYY-MM-DD or a weekday)")


def local_time(iso):
    if not iso:
        return ""
    t = dt.datetime.fromisoformat(iso).astimezone()
    return t.strftime("%I:%M %p").lstrip("0")


def main():
    ap = argparse.ArgumentParser(description="Miami electronic music listings from events.json")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--date", help="single day: YYYY-MM-DD or a weekday name")
    ap.add_argument("--venue", help="tracked venue slug or substring of a venue name")
    ap.add_argument("--tracked", action="store_true", help="only venues that have calendar feeds")
    ap.add_argument("--genre", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    path = os.path.join(HERE, "events.json")
    age = dt.datetime.now() - dt.datetime.fromtimestamp(os.path.getmtime(path))
    with open(path, encoding="utf-8") as fh:
        events = json.load(fh)["events"]

    if args.date:
        target = resolve_date(args.date).isoformat()
        events = [e for e in events if e["date"] == target]
        window = target
    else:
        today = dt.date.today()
        end = (today + dt.timedelta(days=args.days)).isoformat()
        events = [e for e in events if today.isoformat() <= e["date"] <= end]
        window = f"{today.isoformat()} .. {end}"

    if args.tracked:
        events = [e for e in events if e["venue_slug"]]
    if args.venue:
        v = args.venue.lower()
        events = [e for e in events if e["venue_slug"] == v or v in e["venue"].lower()]
    if args.genre:
        wanted = [g.lower() for g in args.genre]
        events = [e for e in events if any(w in g for g in e["genres"] for w in wanted)]
    if args.exclude:
        unwanted = [g.lower() for g in args.exclude]
        events = [e for e in events if not any(u in g for g in e["genres"] for u in unwanted)]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(events, fh, indent=2, ensure_ascii=False)
        print(f"Wrote {len(events)} events to {args.json}")
        return 0

    stale = f"  !! events.json is {age.total_seconds() / 3600:.0f}h old - run build_feeds.py" if age > STALE_AFTER else ""
    print(f"{len(events)} events  [{window}]{stale}")
    current = None
    for e in events:
        if e["date"] != current:
            current = e["date"]
            print(f"\n=== {dt.date.fromisoformat(current).strftime('%a %b %d')} ===")
        bits = [e["venue"] or "?"]
        if e["start"]:
            bits.append(("~" if e.get("time_estimated") else "") + local_time(e["start"]))
        if e.get("price"):
            bits.append(e["price"])
        if e["ages"]:
            bits.append(e["ages"])
        tags = ""
        if e["genres"]:
            tags = "  [" + ", ".join(g + ("?" if e["genres_inferred"] else "") for g in e["genres"]) + "]"
        star = "*" if e["venue_slug"] else " "
        print(f" {star}{e['title'][:60]}")
        print(f"    {' | '.join(bits)}{tags}  ({e['source']})")
    print("\n'*' = venue has a calendar feed. '~' before a time = estimated from the venue's usual hours. "
          "'?' on a genre = inferred from the venue, not confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
