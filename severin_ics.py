#!/usr/bin/env python3
"""Convert the LVR-Severin-Schule "Termine" table into an iCalendar feed."""

import argparse
import hashlib
import html
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from icalendar import Calendar, Event
from lxml import html as lxml_html

URL = "https://www.lvr.de/severin-schule/aktuelles/termine/"
OUTPUT = Path("severin-schule.ics")

# The page has two <script type="application/json" data-x-ref="data"> tags:
# one in <head> (site config) and one inside the table figure (the calendar).
TABLE_XPATH = (
    '//figure[contains(concat(" ", normalize-space(@class), " "), " table ")]'
    '//script[@type="application/json"][@data-x-ref="data"]'
)
HEADING_XPATH = '//h2[contains(@class, "module-table__heading")]'

HOLIDAY_EMOJI = "☀️"  # prefixed to events containing "schulfrei"

log = logging.getLogger("severin_ics")

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4,5})?")
DAY_RANGE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\.\s*[-–]\s*(?:[A-Za-zäöüÄÖÜ]+[.,]\s*)?(\d{1,2})\.(\d{1,2})\.(\d{4,5})?"
)


def fetch(url: str) -> bytes:
    resp = requests.get(
        url, headers={"User-Agent": "Mozilla/5.0 (severin-ics-feed)"}, timeout=30
    )
    resp.raise_for_status()
    # Return raw bytes: lxml honours <meta charset>, whereas requests would
    # guess ISO-8859-1 when the Content-Type header has no charset.
    return resp.content


def extract_table(page: bytes) -> tuple[list[dict], int]:
    """Return (rows, school_year_start)."""
    tree = lxml_html.fromstring(page)
    scripts = tree.xpath(TABLE_XPATH)
    if not scripts:
        raise RuntimeError("Calendar <script> tag not found; page layout changed?")
    rows = json.loads(scripts[0].text_content())["data"]

    heading = " ".join(h.text_content() for h in tree.xpath(HEADING_XPATH))
    m = re.search(r"(\d{4})\s*/\s*(\d{2,4})", heading)
    if not m:
        raise RuntimeError(f"School year not found in heading: {heading!r}")
    return rows, int(m.group(1))


def make_date(day: int, month: int, year_raw: str | None, sy: int) -> date:
    """Build a date, correcting missing/typo'd years using the school year.

    Valid window: 1 Aug <sy> .. 15 Sep <sy+1>.
    """
    lo, hi = date(sy, 8, 1), date(sy + 1, 9, 15)
    if year_raw and len(year_raw) == 4:
        try:
            d = date(int(year_raw), month, day)
            if lo <= d <= hi:
                return d
        except ValueError:
            pass
    d = date(sy if month >= 8 else sy + 1, month, day)
    log.warning("Inferred year for %02d.%02d. (given %r) -> %s", day, month, year_raw, d)
    return d


def parse_dates(text: str, sy: int) -> tuple[date, date] | None:
    """Return inclusive (start, end) or None if there is no concrete date."""
    m = DAY_RANGE_RE.search(text)  # "16.-18.06.2027", "02. – Freitag, 06.11.2026"
    if m:
        d1, d2, mon, yr = int(m[1]), int(m[2]), int(m[3]), m[4]
        return make_date(d1, mon, yr, sy), make_date(d2, mon, yr, sy)

    found = DATE_RE.findall(text)
    if not found:
        return None
    # Year missing on the start of a range ("30.03. bis 11.04.2027") -> use last year.
    last_year = next((y for *_, y in reversed(found) if y), None)
    dates = [make_date(int(d), int(mo), y or last_year, sy) for d, mo, y in found]
    start, end = dates[0], dates[-1]
    if end < start:
        log.warning("End before start in %r; using start date only", text)
        end = start
    return start, end


def clean_blocks(cell: str) -> list[str]:
    """Split an HTML cell into text blocks (separated by blank lines)."""
    s = re.sub(r"<br\s*/?>", "\n", cell, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    blocks, cur = [], []
    for ln in lines:
        if ln:
            cur.append(ln)
        elif cur:
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def summarize(block: str) -> str:
    lines = block.split("\n")
    out = lines[0]
    for ln in lines[1:]:
        out += (" " if out.endswith(":") else ": ") + ln
    return out


def build_calendar(rows: list[dict], sy: int) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//LVR-Severin-Schule Termine//severin-ics//DE")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", f"LVR-Severin-Schule {sy}/{sy + 1}")
    cal.add("x-wr-timezone", "Europe/Berlin")
    cal.add("refresh-interval;value=duration", "P7D")
    cal.add("x-published-ttl", "P7D")
    # Constant stamp so unchanged data gives an identical file (no noisy commits).
    stamp = datetime(sy, 8, 1, tzinfo=timezone.utc)

    seen = set()
    for row in rows:
        cells = row["cells"]
        if len(cells) < 2:
            continue
        date_text = " ".join(clean_blocks(cells[0]["value"]))
        log.debug("[%s] IN   date=%r text=%r", row.get("id"), date_text, cells[1]["value"])
        span = parse_dates(date_text, sy)
        if not span:
            log.debug("[%s] SKIP no concrete date", row.get("id"))
            continue
        start, end = span
        for block in clean_blocks(cells[1]["value"]):
            summary = summarize(block)
            if "schulfrei" in block.casefold():
                summary = f"{HOLIDAY_EMOJI} {summary}"
            log.debug(
                "[%s] OUT  %s .. %s (inclusive) summary=%r block=%r",
                row.get("id"), start, end, summary, block,
            )
            uid = hashlib.sha1(f"{start}|{end}|{summary}".encode()).hexdigest()
            if uid in seen:
                log.debug("[%s] SKIP duplicate event", row.get("id"))
                continue
            seen.add(uid)
            ev = Event()
            ev.add("uid", f"{uid}@severin-schule.lvr.de")
            ev.add("dtstamp", stamp)
            ev.add("summary", summary)
            ev.add("dtstart", start)
            ev.add("dtend", end + timedelta(days=1))  # all-day DTEND is exclusive
            ev.add("transp", "TRANSPARENT")
            ev.add("url", URL)
            if "\n" in block:
                ev.add("description", block)
            cal.add_component(ev)
    return cal


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("html_file", nargs="?", type=Path, help="parse a saved page instead of fetching")
    ap.add_argument("--debug", action="store_true", help="trace how each input row maps to events (stderr)")
    ap.add_argument("--dump-json", action="store_true", help="pretty-print the extracted table JSON to stdout and exit")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    page = args.html_file.read_bytes() if args.html_file else fetch(URL)
    rows, sy = extract_table(page)
    if args.dump_json:
        print(json.dumps({"school_year": sy, "rows": rows}, indent=2, ensure_ascii=False))
        return 0
    cal = build_calendar(rows, sy)
    n = len(cal.subcomponents)
    if n == 0:
        raise RuntimeError("No events parsed; refusing to overwrite the feed")
    OUTPUT.write_bytes(cal.to_ical())
    log.info("Wrote %d events to %s", n, OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
