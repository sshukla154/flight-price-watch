"""Daily AMS -> DEL flight-price check, pushed to WhatsApp.

Fixed route/date for this v1 (deliberately -- see README for why):
AMS -> DEL, 2027-07-17, one-way, economy default.

Data source: SerpApi's Google Flights API (https://serpapi.com/google-flights-api).
Free tier is 250 searches/month; one run/day here uses ~30/month.

Notification: CallMeBot's free personal WhatsApp API
(https://www.callmebot.com/blog/free-api-whatsapp-messages/) -- one HTTP
GET per message, no business account.

Reads three secrets from the environment (GitHub Actions repo secrets in
CI, or your own shell env for a local test run) -- never hardcoded, never
committed:
    SERPAPI_KEY        -- from serpapi.com's dashboard
    CALLMEBOT_PHONE     -- your WhatsApp number, e.g. +311234567890
    CALLMEBOT_APIKEY    -- the key CallMeBot's bot sent you after the
                           one-time WhatsApp activation handshake

On ANY failure (SerpApi error, zero flights found, CallMeBot non-200),
this still tries to send a WhatsApp message saying so -- a silently
failed daily job is worse than a noisy one.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import requests

DEPARTURE_ID = "AMS"
ARRIVAL_ID = "DEL"
OUTBOUND_DATE = "2027-07-17"
CURRENCY = "EUR"

SERPAPI_URL = "https://serpapi.com/search"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


class CheckFailed(Exception):
    """Raised for any expected failure mode -- caught once in main() so the
    WhatsApp-on-failure path always runs, instead of duplicating a
    try/except around every possible failure point."""


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CheckFailed(f"{name} is not set in the environment")
    return value


def fetch_cheapest_price() -> dict[str, Any]:
    """One SerpApi Google Flights query; returns the cheapest itinerary
    across BOTH `best_flights` and `other_flights` -- Google Flights' own
    "best" curation optimizes for a blend of price/duration/stops, not
    strictly lowest price, so trusting best_flights[0] alone can miss a
    cheaper option sitting in other_flights.
    """
    response = requests.get(
        SERPAPI_URL,
        params={
            "engine": "google_flights",
            "departure_id": DEPARTURE_ID,
            "arrival_id": ARRIVAL_ID,
            "outbound_date": OUTBOUND_DATE,
            "type": "2",  # one-way
            "currency": CURRENCY,
            "hl": "en",
            "api_key": _env("SERPAPI_KEY"),
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise CheckFailed(f"SerpApi returned HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    if data.get("search_metadata", {}).get("status") != "Success":
        raise CheckFailed(f"SerpApi search did not succeed: {data.get('search_metadata')}")

    candidates = [*data.get("best_flights", []), *data.get("other_flights", [])]
    if not candidates:
        raise CheckFailed(f"No flights found for {DEPARTURE_ID}->{ARRIVAL_ID} on {OUTBOUND_DATE}")

    return min(candidates, key=lambda itinerary: itinerary["price"])


def format_message(itinerary: dict[str, Any]) -> str:
    flights = itinerary["flights"]
    airlines = ", ".join(dict.fromkeys(leg["airline"] for leg in flights))
    stop_count = len(flights) - 1
    stops_label = "direct" if stop_count == 0 else f"{stop_count} stop(s)"
    hours, minutes = divmod(itinerary["total_duration"], 60)

    return (
        f"Flight watch {DEPARTURE_ID}->{ARRIVAL_ID} on {OUTBOUND_DATE}\n"
        f"{CURRENCY} {itinerary['price']} -- {airlines} -- {stops_label} -- "
        f"{hours}h{minutes:02d}m total"
    )


def send_whatsapp(message: str) -> None:
    response = requests.get(
        CALLMEBOT_URL,
        params={
            "phone": _env("CALLMEBOT_PHONE"),
            "text": message,
            "apikey": _env("CALLMEBOT_APIKEY"),
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise CheckFailed(
            f"CallMeBot returned HTTP {response.status_code}: {response.text[:300]}"
        )


def main() -> int:
    try:
        itinerary = fetch_cheapest_price()
        message = format_message(itinerary)
    except CheckFailed as exc:
        failure_message = (
            f"Flight watch {DEPARTURE_ID}->{ARRIVAL_ID} on {OUTBOUND_DATE}: "
            f"check FAILED -- {exc}"
        )
        print(failure_message, file=sys.stderr)
        send_whatsapp(failure_message)
        return 1

    print(message)
    send_whatsapp(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
