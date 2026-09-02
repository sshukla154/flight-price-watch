"""One-time throwaway diagnostic -- NOT part of the real codebase.
Dumps the raw SerpApi round-trip response shape (both the outbound
call and the departure_token follow-up call) so the real feature can
be built against verified ground truth instead of ambiguous docs.
Deleted once its job is done."""

import json
import os

import requests

SERPAPI_KEY = os.environ["SERPAPI_KEY"]

step1 = requests.get(
    "https://serpapi.com/search",
    params={
        "engine": "google_flights",
        "departure_id": "AMS",
        "arrival_id": "DEL",
        "outbound_date": "2026-10-15",
        "return_date": "2026-11-19",
        "type": "1",
        "currency": "EUR",
        "adults": 2,
        "children": 1,
        "hl": "en",
        "api_key": SERPAPI_KEY,
    },
    timeout=30,
)
data1 = step1.json()
print("=== STEP 1 (outbound) status ===")
print(data1.get("search_metadata", {}).get("status"))

itineraries = [*data1.get("best_flights", []), *data1.get("other_flights", [])]
print(f"=== STEP 1: {len(itineraries)} outbound itineraries ===")
first = itineraries[0] if itineraries else None
print("=== STEP 1: first itinerary (full) ===")
print(json.dumps(first, indent=2))

token = first.get("departure_token") if first else None
print(f"=== departure_token present: {token is not None} ===")

if token:
    step2 = requests.get(
        "https://serpapi.com/search",
        params={
            "engine": "google_flights",
            "departure_id": "AMS",
            "arrival_id": "DEL",
            "outbound_date": "2026-10-15",
            "return_date": "2026-11-19",
            "type": "1",
            "currency": "EUR",
            "adults": 2,
            "children": 1,
            "hl": "en",
            "departure_token": token,
            "api_key": SERPAPI_KEY,
        },
        timeout=30,
    )
    data2 = step2.json()
    print("=== STEP 2 (return) status ===")
    print(data2.get("search_metadata", {}).get("status"))
    return_itineraries = [*data2.get("best_flights", []), *data2.get("other_flights", [])]
    print(f"=== STEP 2: {len(return_itineraries)} return itineraries ===")
    print("=== STEP 2: first return itinerary (full) ===")
    print(json.dumps(return_itineraries[0] if return_itineraries else None, indent=2))
    print("=== STEP 2: full top-level response keys ===")
    print(list(data2.keys()))
