"""Daily AMS -> {candidate Indian destinations} price comparison, pushed
as two sections: DIRECT and 1-STOP (max).

Candidates and route/date come from routes.toml's [check_flights] section
(see load_route_config in flightwatch_core.py) -- edit that file to add,
remove, or retarget a candidate; this script has no hardcoded airport
list. See README.md's "Airports considered" table for the full
reasoning behind which candidates are active vs. deliberately dropped
(GOP, KBK, KNU) -- this script only reflects whatever routes.toml
currently says, not the history of how that list got here.

Each candidate gets exactly ONE SerpApi call (`fetch_all_itineraries`),
never two -- splitting the result into a direct bucket and a one-stop
bucket happens locally, so this doesn't double the ~120/month quota
this repo already runs close to. Itineraries with 2+ stops are silently
out of scope (the user only asked for direct and "indirect, max 1
stop"). If an airport has no option in a given category, that airport
is simply omitted from that section -- never an explicit "not found"
line.

Cheapest-price-only for now (per category). Factoring in ground travel
time/cost from whichever candidate wins to the traveller's actual final
destination is an explicit, tracked v2 TODO -- see README -- not built
here; it would need real distance/time/cost data this script does not
have.

Notification channel is picked automatically by _notify_channel():
GitHub Actions (which sets GITHUB_ACTIONS=true in every job) always
gets email, since CallMeBot's free WhatsApp API blocks GitHub's shared
runner IP ranges -- confirmed by testing the same key/phone from a home
network, where it works fine. Running locally (GITHUB_ACTIONS unset)
defaults to WhatsApp. FLIGHT_NOTIFY_CHANNEL overrides either way, for
testing. See README's "Notification channel" section.
"""

from __future__ import annotations

import html
import os
import sys
from dataclasses import dataclass
from typing import Any

from flightwatch_core import (
    CheckFailed,
    NoFlightsFoundYet,
    fetch_all_itineraries,
    load_route_config,
    send_email,
    send_whatsapp,
)

_CONFIG = load_route_config("check_flights")

# `or` rather than dict.get's own default arg -- a workflow_dispatch input
# left blank still SETS the env var (to ""), it doesn't omit it, so the
# fallback has to treat "" the same as unset. The daily cron never sets
# this env var, so production behaviour always comes from routes.toml.
DEPARTURE_ID = os.environ.get("FLIGHT_DEPARTURE_ID") or _CONFIG["departure_id"]
OUTBOUND_DATE = os.environ.get("FLIGHT_OUTBOUND_DATE") or _CONFIG["outbound_date"]
CURRENCY = os.environ.get("FLIGHT_CURRENCY") or _CONFIG["currency"]

CANDIDATES = tuple((c["id"], c["label"]) for c in _CONFIG["candidates"])

_DIRECT = 0
_ONE_STOP = 1


@dataclass(frozen=True)
class CategoryRow:
    airport: str
    price: str
    airline: str
    departure: str
    arrival: str
    total: str
    transit: str | None


@dataclass(frozen=True)
class CandidateOutcome:
    direct_row: CategoryRow | None
    one_stop_row: CategoryRow | None
    error_line: str | None


def _best_by_stop_category(
    itineraries: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Cheapest itinerary per stop count, keeping only 0 (direct) and 1
    (one-stop) -- anything with 2+ stops is out of scope and dropped
    here, not carried forward for a caller to filter out again."""
    best: dict[int, dict[str, Any]] = {}
    for itinerary in itineraries:
        stop_count = len(itinerary["flights"]) - 1
        if stop_count not in (_DIRECT, _ONE_STOP):
            continue
        current_best = best.get(stop_count)
        if current_best is None or itinerary["price"] < current_best["price"]:
            best[stop_count] = itinerary
    return best


def _time_of_day(departure_timestamp: str, timestamp: str) -> str:
    """`timestamp`'s bare HH:MM, with a `+1` suffix if its date differs
    from `departure_timestamp`'s date. Both are SerpApi's own
    "YYYY-MM-DD HH:MM" strings, already local to their respective
    airport -- no timezone conversion needed. `+1` (not an exact day
    count) is a safe simplification: nothing in scope here (direct or
    one-stop, AMS<->India) spans more than one day end-to-end.
    """
    departure_date, _ = departure_timestamp.split(" ")
    date, time_of_day = timestamp.split(" ")
    return f"{time_of_day}+1" if date != departure_date else time_of_day


def _row_from_itinerary(arrival_id: str, itinerary: dict[str, Any]) -> CategoryRow:
    flights = itinerary["flights"]
    airlines = ", ".join(dict.fromkeys(leg["airline"] for leg in flights))
    departure_timestamp = flights[0]["departure_airport"]["time"]
    arrival_timestamp = flights[-1]["arrival_airport"]["time"]
    departure_time = _time_of_day(departure_timestamp, departure_timestamp)
    arrival_time = _time_of_day(departure_timestamp, arrival_timestamp)
    hours, minutes = divmod(itinerary["total_duration"], 60)

    transit: str | None = None
    layovers = itinerary.get("layovers") or []
    if layovers:
        layover_minutes = sum(layover["duration"] for layover in layovers)
        transit_hours, transit_minutes = divmod(layover_minutes, 60)
        transit = f"{transit_hours}h{transit_minutes:02d}m"

    return CategoryRow(
        airport=arrival_id,
        price=str(itinerary["price"]),
        airline=airlines,
        departure=departure_time,
        arrival=arrival_time,
        total=f"{hours}h{minutes:02d}m",
        transit=transit,
    )


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Aligned monospace table -- the only way WhatsApp shows something
    table-like, since it has no real markdown table rendering. Column
    widths are computed from the actual content (header or any cell,
    whichever is longest), never hardcoded, so this stays correct
    however long an airline name or a total-duration string turns out
    to be. Price (column 1) is right-aligned; everything else is left-
    aligned. Caller wraps the result in a ``` fence."""
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def _format_row(cells: list[str]) -> str:
        parts = [
            cell.rjust(widths[i]) if i == 1 else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ]
        return "  ".join(parts).rstrip()

    lines = [_format_row(headers), _format_row(["-" * w for w in widths])]
    lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def _check_one(arrival_id: str, label: str) -> CandidateOutcome:
    """One candidate's outcome across both categories. Never raises --
    every exception fetch_all_itineraries can produce is caught HERE,
    per candidate, so one candidate's failure never hides another
    candidate's real result."""
    try:
        itineraries = fetch_all_itineraries(
            departure_id=DEPARTURE_ID,
            arrival_id=arrival_id,
            outbound_date=OUTBOUND_DATE,
            currency=CURRENCY,
        )
    except NoFlightsFoundYet:
        return CandidateOutcome(direct_row=None, one_stop_row=None, error_line=None)
    except CheckFailed as exc:
        return CandidateOutcome(
            direct_row=None,
            one_stop_row=None,
            error_line=f"{label} ({arrival_id}): error -- {exc}",
        )

    best = _best_by_stop_category(itineraries)
    direct_row = _row_from_itinerary(arrival_id, best[_DIRECT]) if _DIRECT in best else None
    one_stop_row = _row_from_itinerary(arrival_id, best[_ONE_STOP]) if _ONE_STOP in best else None
    # best may legitimately be empty (every itinerary SerpApi returned had
    # 2+ stops) -- that's the same "nothing useful to report" situation as
    # NoFlightsFoundYet from this script's own point of view, handled
    # identically by leaving both rows None.
    return CandidateOutcome(direct_row=direct_row, one_stop_row=one_stop_row, error_line=None)


def _notify_channel() -> str:
    """"email" on GitHub Actions (GITHUB_ACTIONS=true, set automatically
    in every job -- CallMeBot blocks that IP range), "whatsapp"
    otherwise (a home network works fine). FLIGHT_NOTIFY_CHANNEL
    overrides either way, for testing."""
    return os.environ.get("FLIGHT_NOTIFY_CHANNEL") or (
        "email" if os.environ.get("GITHUB_ACTIONS") == "true" else "whatsapp"
    )


def _build_sections(
    outcomes: list[CandidateOutcome],
) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Channel-neutral report content: the top summary line, a list of
    (section header, table text) pairs -- table text has no fence/HTML
    wrapping, that's each channel's own job -- and the plain error
    lines (errors are never tabular)."""
    direct_rows = [o.direct_row for o in outcomes if o.direct_row is not None]
    one_stop_rows = [o.one_stop_row for o in outcomes if o.one_stop_row is not None]
    error_lines = [o.error_line for o in outcomes if o.error_line is not None]

    top_line = f"Flight watch from {DEPARTURE_ID} on {OUTBOUND_DATE} ({CURRENCY})"

    tables: list[tuple[str, str]] = []
    if direct_rows:
        table = _format_table(
            ["Airport", "Price", "Airline", "Dep", "Arr", "Total"],
            [
                [r.airport, r.price, r.airline, r.departure, r.arrival, r.total]
                for r in direct_rows
            ],
        )
        tables.append(("DIRECT", table))
    if one_stop_rows:
        table = _format_table(
            ["Airport", "Price", "Airline", "Dep", "Arr", "Transit", "Total"],
            [
                [r.airport, r.price, r.airline, r.departure, r.arrival, r.transit or "", r.total]
                for r in one_stop_rows
            ],
        )
        tables.append(("1 STOP (max)", table))

    return top_line, tables, error_lines


def _build_whatsapp_message(outcomes: list[CandidateOutcome]) -> str:
    """WhatsApp has no real markdown table support -- each table is
    wrapped in a ``` fence, the only way to force a monospace font
    there."""
    top_line, tables, error_lines = _build_sections(outcomes)
    sections = [top_line]
    for header, table in tables:
        sections.append(f"\n{header}\n```\n{table}\n```")
    if error_lines:
        sections.append("\nERRORS\n" + "\n".join(error_lines))
    return "\n".join(sections)


def _build_email_body(outcomes: list[CandidateOutcome]) -> tuple[str, str]:
    """Gmail's plain-text view uses a proportional font that would
    misalign the raw table, so this sends HTML with a <pre> block
    instead -- no ``` fences needed, <pre> already forces monospace.
    Returns (subject, html_body)."""
    top_line, tables, error_lines = _build_sections(outcomes)
    sections = [top_line]
    for header, table in tables:
        sections.append(f"\n{header}\n{table}")
    if error_lines:
        sections.append("\nERRORS\n" + "\n".join(error_lines))
    text = "\n".join(sections)

    subject = f"Flight watch: {DEPARTURE_ID} on {OUTBOUND_DATE}"
    html_body = f'<pre style="font-family: monospace">{html.escape(text)}</pre>'
    return subject, html_body


def main() -> int:
    channel = _notify_channel()
    if channel not in ("whatsapp", "email"):
        print(f"Unknown FLIGHT_NOTIFY_CHANNEL '{channel}'", file=sys.stderr)
        return 1

    outcomes = [_check_one(arrival_id, label) for arrival_id, label in CANDIDATES]

    has_any_finding = any(
        o.direct_row is not None or o.one_stop_row is not None or o.error_line is not None
        for o in outcomes
    )
    if not has_any_finding:
        # Deliberately silent -- every candidate having nothing useful
        # to report (no data yet, or only 2+-stop options) is the
        # routine outcome for a far-future date, not worth a daily
        # ping. A genuine error is NOT covered by this branch -- it's
        # exactly one of the three fields has_any_finding checks.
        print("(nothing to report for any candidate, no notification sent)", file=sys.stderr)
        return 0

    if channel == "whatsapp":
        message = _build_whatsapp_message(outcomes)
        print(message)
        send_whatsapp(message)
    else:
        subject, html_body = _build_email_body(outcomes)
        print(subject)
        print(html_body)
        send_email(subject, html_body)

    # A genuine error on ANY candidate must make the whole run exit 1 --
    # matches this repo's own established discipline (exit 1 only for a
    # real failure, never for the routine no-data case).
    if any(o.error_line is not None for o in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
