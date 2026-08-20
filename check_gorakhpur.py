"""Daily AMS -> {Gorakhpur-area airports} price comparison, pushed to WhatsApp.

Independent of check_price.py's AMS->DEL check -- separate route,
separate WhatsApp message, separate workflow/cron. They share
flightwatch_core.py's SerpApi/CallMeBot logic only.

Origin/date/currency/candidates come from routes.toml's [check_gorakhpur]
table -- edit that file, not this one, to change them.

Candidates (confirmed with the user -- see README): GOP (Gorakhpur
itself) and KBK (Kushinagar, ~55km away, newer airport with some
international connectivity). VNS/LKO/PAT (200km+) deliberately excluded.

Cheapest-price-only for now. Factoring in ground travel time/cost from
each candidate airport to Gorakhpur itself (mirroring flight-agent's own
D7 formula) is an explicit, tracked v2 TODO -- see README -- not built
here; it would need real distance/time/cost data this script does not
have.

Same three secrets as check_price.py (SERPAPI_KEY, CALLMEBOT_PHONE,
CALLMEBOT_APIKEY) -- no new secrets needed, same SerpApi account, same
WhatsApp number.
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

_config = load_route_config("check_gorakhpur")

DEPARTURE_ID = os.environ.get("FLIGHT_DEPARTURE_ID") or _config["departure_id"]
OUTBOUND_DATE = os.environ.get("FLIGHT_OUTBOUND_DATE") or _config["outbound_date"]
CURRENCY = os.environ.get("FLIGHT_CURRENCY") or _config["currency"]
"""Same override convention as check_price.py's own env vars -- the daily
cron never sets these, so production behaviour is driven by routes.toml,
not a Python literal; overridable for testing with a near-term date.
Deliberately no FLIGHT_ARRIVAL_ID override here -- the candidate list
below is this script's whole point, not a single overridable
destination."""

CANDIDATES = tuple((candidate["id"], candidate["label"]) for candidate in _config["candidates"])


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
        f"Gorakhpur-area watch from {DEPARTURE_ID} on {OUTBOUND_DATE}\n"
        + "\n".join(result.line for result in ordered)
    )


def main() -> int:
    results = [_check_one(arrival_id, label) for arrival_id, label in CANDIDATES]

    all_no_data_yet = all(result.outcome is Outcome.NO_DATA_YET for result in results)
    if all_no_data_yet:
        # Deliberately silent on WhatsApp -- matches check_price.py's own
        # philosophy: every candidate having nothing published yet is the
        # routine outcome for a far-future date, not worth a daily ping.
        # A genuine ERROR outcome is NOT covered by this branch -- see the
        # `all()` above, which is only true when every single result is
        # NO_DATA_YET, never when one of them is ERROR.
        print("(no WhatsApp sent, all candidates have no data yet)", file=sys.stderr)
        for result in results:
            print(result.line, file=sys.stderr)
        return 0

    message = build_comparison(results)
    print(message)
    send_whatsapp(message)
    # Matches check_price.py's own exit-code discipline: 1 only when a
    # GENUINE failure occurred (at least one candidate is ERROR), never
    # for the routine "found a price"/"no data yet" outcomes -- so the
    # Actions UI shows red exactly when something is actually broken.
    any_error = any(result.outcome is Outcome.ERROR for result in results)
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
