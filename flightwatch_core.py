"""Shared SerpApi + CallMeBot logic used by every check_*.py driver script.

Kept intentionally thin -- one HTTP call out to SerpApi's Google Flights
API, one HTTP call out to CallMeBot's WhatsApp API, and the shared
exception taxonomy that lets every driver script distinguish "a routine
no-data-yet result" from "a genuine failure worth pinging WhatsApp about."

Nothing here reads the FLIGHT_* env-var overrides or picks candidate
airports -- that's each driver script's own job (check_price.py: one
fixed route; check_gorakhpur.py: a small hardcoded candidate list). This
module only knows how to run ONE query and send ONE WhatsApp message; it
has no opinion about how many times a driver calls it per run.
"""

from __future__ import annotations

import os
from typing import Any

import requests

SERPAPI_URL = "https://serpapi.com/search"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


class CheckFailed(Exception):
    """Raised for a genuine failure -- a caller catches this and sends a
    WhatsApp message about it. Duplicating a try/except around every
    possible failure point would be worse than one shared catch per
    driver script's main()."""


class NoFlightsFoundYet(CheckFailed):
    """A `Success` SerpApi response with zero itineraries -- for a
    far-future date this is the routine, expected outcome for weeks/months
    until Google Flights loads real fare data, not a broken query.
    Deliberately a SEPARATE exception from the base `CheckFailed` so a
    caller can treat it as logged-only instead of triggering a daily
    WhatsApp ping that would just say "still nothing" over and over."""


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CheckFailed(f"{name} is not set in the environment")
    return value


def fetch_cheapest_price(
    *, departure_id: str, arrival_id: str, outbound_date: str, currency: str
) -> dict[str, Any]:
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
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "type": "2",  # one-way
            "currency": currency,
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
        raise NoFlightsFoundYet(
            f"No flights found for {departure_id}->{arrival_id} on {outbound_date}"
        )

    return min(candidates, key=lambda itinerary: itinerary["price"])


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
