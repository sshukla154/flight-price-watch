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

On a genuine failure (SerpApi HTTP error, malformed response, CallMeBot
non-200), this still tries to send a WhatsApp message saying so -- a
silently failed daily job is worse than a noisy one. The one deliberate
exception is `NoFlightsFoundYet` (see flightwatch_core's own docstring):
for the 2027-07-17 default date, a `Success` response with zero
itineraries is the EXPECTED daily result until Google Flights loads real
fare data for it (schedule-publication-horizon, see README) -- pinging
WhatsApp every single day for weeks with "still nothing" would just be
noise. That case logs to the Action's own run output only.

This is one of two independent daily checks in this repo -- the other is
check_gorakhpur.py, comparing candidate airports near Gorakhpur. They
share flightwatch_core.py's SerpApi/CallMeBot logic but are otherwise
unrelated: this script's route/schedule/secrets are untouched by that one.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from flightwatch_core import CheckFailed, NoFlightsFoundYet, fetch_cheapest_price, send_whatsapp

# `or` rather than dict.get's own default arg -- a workflow_dispatch input
# left blank still SETS the env var (to ""), it doesn't omit it, so the
# fallback has to treat "" the same as unset.
DEPARTURE_ID = os.environ.get("FLIGHT_DEPARTURE_ID") or "AMS"
ARRIVAL_ID = os.environ.get("FLIGHT_ARRIVAL_ID") or "DEL"
OUTBOUND_DATE = os.environ.get("FLIGHT_OUTBOUND_DATE") or "2027-07-17"
CURRENCY = os.environ.get("FLIGHT_CURRENCY") or "EUR"
"""All four default to the fixed v1 route/date -- the daily cron never
sets these env vars, so production behaviour is unchanged. Overridable
for diagnosing exactly this kind of issue: is a `No flights found` result
a genuine schedule-publication-horizon gap for the far-future default
date, or something wrong with the query itself? Point FLIGHT_OUTBOUND_DATE
at a near-term date (e.g. 60 days out) to tell the two apart without
touching the committed default."""


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


def main() -> int:
    try:
        itinerary = fetch_cheapest_price(
            departure_id=DEPARTURE_ID,
            arrival_id=ARRIVAL_ID,
            outbound_date=OUTBOUND_DATE,
            currency=CURRENCY,
        )
        message = format_message(itinerary)
    except NoFlightsFoundYet as exc:
        # Deliberately silent on WhatsApp -- see NoFlightsFoundYet's own
        # docstring. Still visible in the Action run's own log if checked.
        print(f"(no WhatsApp sent, expected outcome) {exc}", file=sys.stderr)
        return 0
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
