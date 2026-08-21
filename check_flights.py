"""Daily AMS -> {candidate Indian destinations} price comparison, pushed
to WhatsApp.

Candidates and route/date come from routes.toml's [check_flights] section
(see load_route_config in flightwatch_core.py) -- edit that file to add,
remove, or retarget a candidate; this script has no hardcoded airport
list. See README.md's "Airports considered" table for the full
reasoning behind which candidates are active vs. deliberately dropped
(GOP, KBK, KNU) -- this script only reflects whatever routes.toml
currently says, not the history of how that list got here.

Cheapest-price-only for now. Factoring in ground travel time/cost from
whichever candidate wins to the traveller's actual final destination is
an explicit, tracked v2 TODO -- see README -- not built here; it would
need real distance/time/cost data this script does not have.

Same three secrets as before (SERPAPI_KEY, CALLMEBOT_PHONE,
CALLMEBOT_APIKEY) -- no new secrets needed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum

from flightwatch_core import (
    CheckFailed,
    NoFlightsFoundYet,
    fetch_cheapest_price,
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


class Outcome(Enum):
    PRICE_FOUND = "price_found"
    NO_DATA_YET = "no_data_yet"  # routine, silent -- see NoFlightsFoundYet
    ERROR = "error"  # genuine failure, must still notify


@dataclass(frozen=True)
class CandidateResult:
    line: str
    price: float | None
    outcome: Outcome


def _check_one(arrival_id: str, label: str) -> CandidateResult:
    """One candidate's outcome. Never raises -- every exception
    fetch_cheapest_price can produce is caught HERE, per candidate, so one
    candidate's failure never hides another candidate's real result.
    """
    try:
        itinerary = fetch_cheapest_price(
            departure_id=DEPARTURE_ID,
            arrival_id=arrival_id,
            outbound_date=OUTBOUND_DATE,
            currency=CURRENCY,
        )
    except NoFlightsFoundYet:
        return CandidateResult(
            line=f"{label} ({arrival_id}): no data yet", price=None, outcome=Outcome.NO_DATA_YET
        )
    except CheckFailed as exc:
        return CandidateResult(
            line=f"{label} ({arrival_id}): error -- {exc}", price=None, outcome=Outcome.ERROR
        )

    flights = itinerary["flights"]
    airlines = ", ".join(dict.fromkeys(leg["airline"] for leg in flights))
    stop_count = len(flights) - 1
    stops_label = "direct" if stop_count == 0 else f"{stop_count} stop(s)"
    hours, minutes = divmod(itinerary["total_duration"], 60)
    price = itinerary["price"]
    line = (
        f"{label} ({arrival_id}): {CURRENCY} {price} -- {airlines} -- "
        f"{stops_label} -- {hours}h{minutes:02d}m"
    )
    return CandidateResult(line=line, price=price, outcome=Outcome.PRICE_FOUND)


def build_comparison(results: list[CandidateResult]) -> str:
    """Cheapest-first; results with no price (no-data-yet or error) sort
    last, in their original order relative to each other."""
    ordered = sorted(
        results, key=lambda r: (r.price is None, r.price if r.price is not None else 0.0)
    )
    return (
        f"Flight watch from {DEPARTURE_ID} on {OUTBOUND_DATE}\n"
        + "\n".join(result.line for result in ordered)
    )


def main() -> int:
    results = [_check_one(arrival_id, label) for arrival_id, label in CANDIDATES]

    all_no_data_yet = all(result.outcome is Outcome.NO_DATA_YET for result in results)
    if all_no_data_yet:
        # Deliberately silent on WhatsApp -- every candidate having
        # nothing published yet is the routine outcome for a far-future
        # date, not worth a daily ping. A genuine ERROR outcome is NOT
        # covered by this branch -- the all() above is only true when
        # every single result is NO_DATA_YET, never when one is ERROR.
        print("(no WhatsApp sent, all candidates have no data yet)", file=sys.stderr)
        for result in results:
            print(result.line, file=sys.stderr)
        return 0

    message = build_comparison(results)
    print(message)
    send_whatsapp(message)

    # A genuine ERROR outcome on ANY candidate must make the whole run
    # exit 1 -- matches the pre-merge check_price.py's own discipline
    # (exit 1 only for a real failure, never for the routine no-data
    # case) even though this script covers several candidates at once.
    if any(result.outcome is Outcome.ERROR for result in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
