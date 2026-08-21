"""Daily AMS -> {candidate Indian destinations} price comparison, pushed
to WhatsApp as two sections: DIRECT and 1-STOP (max).

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

Same three secrets as before (SERPAPI_KEY, CALLMEBOT_PHONE,
CALLMEBOT_APIKEY) -- no new secrets needed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from flightwatch_core import (
    CheckFailed,
    NoFlightsFoundYet,
    fetch_all_itineraries,
    load_route_config,
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
class CandidateOutcome:
    direct_line: str | None
    one_stop_line: str | None
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


def _format_category_line(label: str, arrival_id: str, itinerary: dict[str, Any]) -> str:
    flights = itinerary["flights"]
    airlines = ", ".join(dict.fromkeys(leg["airline"] for leg in flights))
    departure_timestamp = flights[0]["departure_airport"]["time"]
    arrival_timestamp = flights[-1]["arrival_airport"]["time"]
    departure_time = _time_of_day(departure_timestamp, departure_timestamp)
    arrival_time = _time_of_day(departure_timestamp, arrival_timestamp)
    hours, minutes = divmod(itinerary["total_duration"], 60)

    transit_part = ""
    layovers = itinerary.get("layovers") or []
    if layovers:
        layover_minutes = sum(layover["duration"] for layover in layovers)
        transit_hours, transit_minutes = divmod(layover_minutes, 60)
        transit_part = f" -- transit {transit_hours}h{transit_minutes:02d}m"

    return (
        f"{label} ({arrival_id}): {CURRENCY} {itinerary['price']} -- {airlines} -- "
        f"dep {departure_time} -> arr {arrival_time}{transit_part} -- "
        f"{hours}h{minutes:02d}m total"
    )


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
        return CandidateOutcome(direct_line=None, one_stop_line=None, error_line=None)
    except CheckFailed as exc:
        return CandidateOutcome(
            direct_line=None,
            one_stop_line=None,
            error_line=f"{label} ({arrival_id}): error -- {exc}",
        )

    best = _best_by_stop_category(itineraries)
    direct_line = (
        _format_category_line(label, arrival_id, best[_DIRECT]) if _DIRECT in best else None
    )
    one_stop_line = (
        _format_category_line(label, arrival_id, best[_ONE_STOP]) if _ONE_STOP in best else None
    )
    # best may legitimately be empty (every itinerary SerpApi returned had
    # 2+ stops) -- that's the same "nothing useful to report" situation as
    # NoFlightsFoundYet from this script's own point of view, handled
    # identically by leaving both lines None.
    return CandidateOutcome(direct_line=direct_line, one_stop_line=one_stop_line, error_line=None)


def _build_message(outcomes: list[CandidateOutcome]) -> str:
    direct_lines = [o.direct_line for o in outcomes if o.direct_line is not None]
    one_stop_lines = [o.one_stop_line for o in outcomes if o.one_stop_line is not None]
    error_lines = [o.error_line for o in outcomes if o.error_line is not None]

    sections = [f"Flight watch from {DEPARTURE_ID} on {OUTBOUND_DATE}"]
    if direct_lines:
        sections.append("\nDIRECT\n" + "\n".join(direct_lines))
    if one_stop_lines:
        sections.append("\n1 STOP (max)\n" + "\n".join(one_stop_lines))
    if error_lines:
        sections.append("\nERRORS\n" + "\n".join(error_lines))
    return "\n".join(sections)


def main() -> int:
    outcomes = [_check_one(arrival_id, label) for arrival_id, label in CANDIDATES]

    has_any_finding = any(
        o.direct_line is not None or o.one_stop_line is not None or o.error_line is not None
        for o in outcomes
    )
    if not has_any_finding:
        # Deliberately silent on WhatsApp -- every candidate having
        # nothing useful to report (no data yet, or only 2+-stop options)
        # is the routine outcome for a far-future date, not worth a daily
        # ping. A genuine error is NOT covered by this branch -- it's
        # exactly one of the three fields has_any_finding checks.
        print("(no WhatsApp sent, nothing to report for any candidate)", file=sys.stderr)
        return 0

    message = _build_message(outcomes)
    print(message)
    send_whatsapp(message)

    # A genuine error on ANY candidate must make the whole run exit 1 --
    # matches this repo's own established discipline (exit 1 only for a
    # real failure, never for the routine no-data case).
    if any(o.error_line is not None for o in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
