"""Shared SerpApi + CallMeBot + Gmail logic used by every check_*.py
driver script.

Kept intentionally thin -- one HTTP call out to SerpApi's Google Flights
API, one HTTP call out to CallMeBot's WhatsApp API, one SMTP call out to
Gmail, and the shared exception taxonomy that lets every driver script
distinguish "a routine no-data-yet result" from "a genuine failure worth
notifying about." Which notification channel actually gets used for a
given run is a driver script's own decision (see check_flights.py's
_notify_channel) -- this module just exposes both primitives.

Nothing here reads the FLIGHT_* env-var overrides or picks candidate
airports -- that's each driver script's own job (check_price.py: one
fixed route; check_gorakhpur.py: a small hardcoded candidate list). This
module only knows how to run ONE query and send ONE notification; it has
no opinion about how many times a driver calls it per run.
"""

from __future__ import annotations

import os
import smtplib
import tomllib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests

SERPAPI_URL = "https://serpapi.com/search"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465

_ROUTES_CONFIG_PATH = Path(__file__).resolve().parent / "routes.toml"


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


def load_route_config(section: str) -> dict[str, Any]:
    """One `[section]` table from `routes.toml` -- the non-secret route/
    date/candidate values a driver script needs. A config typo (missing
    file, missing section) raises loudly here rather than surfacing later
    as a confusing KeyError deep inside a driver script, or worse, a
    silently-None value.

    Every value here is still overridable by a driver script's own
    `FLIGHT_*` env var (checked env > `routes.toml`, same precedence as
    before this file existed) -- this function only supplies the
    fallback, it never reads the env vars itself.
    """
    if not _ROUTES_CONFIG_PATH.is_file():
        raise CheckFailed(f"routes.toml not found at {_ROUTES_CONFIG_PATH}")

    with _ROUTES_CONFIG_PATH.open("rb") as fh:
        data = tomllib.load(fh)

    if section not in data:
        raise CheckFailed(f"routes.toml has no [{section}] section")

    return data[section]


def fetch_all_itineraries(
    *,
    departure_id: str,
    arrival_id: str,
    outbound_date: str,
    return_date: str,
    currency: str,
    adults: int = 1,
    children: int = 0,
) -> list[dict[str, Any]]:
    """One SerpApi Google Flights query; returns every itinerary from
    BOTH `best_flights` and `other_flights` -- Google Flights' own "best"
    curation optimizes for a blend of price/duration/stops, not strictly
    lowest price, so trusting `best_flights` alone can miss a cheaper (or
    direct, or otherwise notable) option sitting in `other_flights`.

    Round trip (type=1) -- `price` on each itinerary is already the
    real round-trip total, not an outbound-only figure: verified live
    2026-09-02 against a real account by comparing this call's price
    against the departure_token follow-up call's price for the same
    itinerary (identical). That follow-up call only exists to let a
    caller swap which return flight pairs with a chosen outbound --
    irrelevant here, so it's never made; this stays ONE SerpApi call
    per candidate, same as the one-way search it replaced.

    Returns the full, unsorted list -- minus any itinerary with no
    resolved price (see the filter below) -- so a caller can bucket by
    stop count, pick a cheapest-per-category, or whatever else it needs,
    trusting every entry has a usable price without checking itself.
    """
    response = requests.get(
        SERPAPI_URL,
        params={
            "engine": "google_flights",
            "departure_id": departure_id,
            "arrival_id": arrival_id,
            "outbound_date": outbound_date,
            "return_date": return_date,
            "type": "1",  # round trip
            "currency": currency,
            "adults": adults,
            "children": children,
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

    # Google Flights occasionally shows an option with no resolved fare
    # ("price unavailable") -- confirmed live 2026-08-25 when this crashed
    # a caller with a bare KeyError. Filtered out HERE, at the actual data
    # boundary, so every caller (fetch_cheapest_price's min(), or a
    # driver script's own scoring) can trust every itinerary it sees has
    # a usable price, without re-checking for it themselves.
    itineraries = [
        itinerary
        for itinerary in [*data.get("best_flights", []), *data.get("other_flights", [])]
        if "price" in itinerary
    ]
    if not itineraries:
        raise NoFlightsFoundYet(
            f"No flights found for {departure_id}->{arrival_id} on {outbound_date}"
        )

    return itineraries


def fetch_cheapest_price(
    *,
    departure_id: str,
    arrival_id: str,
    outbound_date: str,
    return_date: str,
    currency: str,
    adults: int = 1,
    children: int = 0,
) -> dict[str, Any]:
    """The single cheapest itinerary, regardless of stop count -- a thin
    wrapper over `fetch_all_itineraries` kept for callers (and existing
    tests) that only care about "the cheapest option," not the full list.
    """
    itineraries = fetch_all_itineraries(
        departure_id=departure_id,
        arrival_id=arrival_id,
        outbound_date=outbound_date,
        return_date=return_date,
        currency=currency,
        adults=adults,
        children=children,
    )
    return min(itineraries, key=lambda itinerary: itinerary["price"])


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


def send_email(subject: str, html_body: str) -> None:
    """Sends `html_body` as an HTML email from/to GMAIL_ADDRESS (sends
    to itself -- no separate recipient config needed). Any SMTP failure
    (auth, connection, etc.) is re-raised as CheckFailed so callers
    treat it exactly like a CallMeBot failure -- same taxonomy, same
    exit-1-on-genuine-error handling."""
    address = _env("GMAIL_ADDRESS")
    message = MIMEText(html_body, "html")
    message["Subject"] = subject
    message["From"] = address
    message["To"] = address

    try:
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as smtp:
            smtp.login(address, _env("GMAIL_APP_PASSWORD"))
            smtp.sendmail(address, [address], message.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        raise CheckFailed(f"Gmail SMTP send failed: {exc}") from exc
