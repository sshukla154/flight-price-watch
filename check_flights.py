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

Each category's row is the best-SCORED itinerary, not the cheapest --
see _itinerary_score, which weighs price against total travel time and
layover time. Factoring in ground travel time/cost from whichever
candidate wins to the traveller's actual final destination is a
separate, still-unbuilt v2 TODO -- see README -- it would need real
distance/time/cost data this script does not have.

Notification channel is picked automatically by _notify_channel():
GitHub Actions (which sets GITHUB_ACTIONS=true in every job) always
gets email, since CallMeBot's free WhatsApp API blocks GitHub's shared
runner IP ranges -- confirmed by testing the same key/phone from a home
network, where it works fine. Running locally (GITHUB_ACTIONS unset)
defaults to WhatsApp. FLIGHT_NOTIFY_CHANNEL overrides either way, for
testing. See README's "Notification channel" section.

If the chosen (primary) channel's send itself fails -- a real
CheckFailed from send_whatsapp/send_email, not a per-candidate SerpApi
error -- main() falls back to the OTHER channel once before giving up.
Both credentials are always configured in every environment (see
README), so this costs nothing to attempt. If the fallback also fails,
main() returns 1 cleanly rather than letting the exception crash the
process uncaught.
"""

from __future__ import annotations

import html
import os
import sys
from dataclasses import dataclass, replace
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
ADULTS = int(os.environ.get("FLIGHT_ADULTS") or _CONFIG["adults"])
CHILDREN = int(os.environ.get("FLIGHT_CHILDREN") or _CONFIG["children"])

CANDIDATES = tuple((c["id"], c["label"], c["one_stop"]) for c in _CONFIG["candidates"])

_DIRECT = 0
_ONE_STOP = 1


@dataclass(frozen=True)
class CategoryRow:
    airport: str
    label: str
    price: str
    airline: str
    departure: str
    arrival: str
    total: str
    transit: str | None
    baggage: str
    stop_id: str | None = None
    stop_name: str | None = None
    leg1_duration: str | None = None
    leg2_duration: str | None = None
    filtered_fallback: bool = False


@dataclass(frozen=True)
class CandidateOutcome:
    direct_row: CategoryRow | None
    one_stop_row: CategoryRow | None
    error_line: str | None


@dataclass(frozen=True)
class Filters:
    """One candidate's active filter configuration, built from
    routes.toml's [check_flights.filters] section -- see the
    module-level _FILTERS loading/validation below for where these
    values come from."""

    preferred_airlines: tuple[str, ...]
    required_airlines: tuple[str, ...]
    excluded_layover_regions: tuple[str, ...]
    required_layover_regions: tuple[str, ...]
    layover_regions: dict[str, list[str]]


def _validate_filter_lists(filters: dict[str, Any]) -> None:
    """Each of the four filter keys must be an actual list -- catches
    e.g. `preferred_airlines = "KLM"`, a valid TOML string that would
    otherwise iterate per-character instead of failing loudly."""
    for key in (
        "preferred_airlines",
        "required_airlines",
        "excluded_layover_regions",
        "required_layover_regions",
    ):
        value = filters.get(key, [])
        if not isinstance(value, list):
            raise CheckFailed(
                f"check_flights.filters.{key} must be a list, got {type(value).__name__}"
            )


def _validate_region_keys(regions: tuple[str, ...], region_map: dict[str, list[str]]) -> None:
    """Every region name used in excluded_layover_regions/
    required_layover_regions must be an actual key in layover_regions --
    catches a "middel_east"-style typo before it can silently match
    nothing forever."""
    for region in regions:
        if region not in region_map:
            raise CheckFailed(
                f"check_flights.filters region '{region}' is not a key in layover_regions"
            )


# `.get()` defaults here, unlike every `_CONFIG[...]` read above (which
# fail loud on a typo, per load_route_config's own stated "fail loud"
# philosophy) -- because departure_id/candidates/etc. are core trip
# parameters that must always be set, while filters are a genuinely
# optional, backward-compatible, opt-in feature layered on top: absent
# or empty, nothing about this script's behaviour changes.
_FILTERS = _CONFIG.get("filters", {})
_validate_filter_lists(_FILTERS)

_LAYOVER_REGIONS: dict[str, list[str]] = _FILTERS.get("layover_regions", {})
_PREFERRED_AIRLINES = tuple(_FILTERS.get("preferred_airlines", []))
_REQUIRED_AIRLINES = tuple(_FILTERS.get("required_airlines", []))
_EXCLUDED_LAYOVER_REGIONS = tuple(_FILTERS.get("excluded_layover_regions", []))
_REQUIRED_LAYOVER_REGIONS = tuple(_FILTERS.get("required_layover_regions", []))

_validate_region_keys(_EXCLUDED_LAYOVER_REGIONS, _LAYOVER_REGIONS)
_validate_region_keys(_REQUIRED_LAYOVER_REGIONS, _LAYOVER_REGIONS)

_TIME_VALUE_PER_HOUR = 15  # EUR/hour equivalent weight on total travel time
_LAYOVER_PENALTY_PER_HOUR = 15  # EUR/hour EXTRA weight specifically on layover time
_AIRLINE_PREFERENCE_BONUS = 25  # EUR-equivalent score discount for a preferred-airline match --
# roughly half a layover-hour penalty: enough to break a close tie, not enough to override a
# clearly better option on price/time/layover alone


def _itinerary_matches_any_airline(itinerary: dict[str, Any], airlines: tuple[str, ...]) -> bool:
    """True if ANY leg's airline case-insensitive-SUBSTRING-matches ANY
    entry in `airlines`. Substring, not exact match, because SerpApi
    returns full descriptive names like "KLM Royal Dutch Airlines" --
    exact match would silently never fire on a bare "KLM" config
    entry. Shared by both the required-airlines hard filter and the
    preferred-airlines score bonus so the matching rule can't drift
    between the two."""
    return any(
        airline.casefold() in leg["airline"].casefold()
        for leg in itinerary["flights"]
        for airline in airlines
    )


def _itinerary_score(itinerary: dict[str, Any], preferred_airlines: tuple[str, ...] = ()) -> float:
    """Lower is better. price + a time-value-weighted total duration +
    an EXTRA penalty on layover time specifically. Layover minutes are
    deliberately counted TWICE -- once already inside total_duration,
    once again here -- because sitting in an airport is worse than the
    same time spent flying, and this is the one place that distinction
    matters. Both weights are simple, transparent EUR-per-hour
    constants, not fitted to any real preference data -- adjust them
    if a real pick ever looks wrong.

    `preferred_airlines` is a SOFT preference only: a flat score
    discount applied ONCE per itinerary (not once per leg) when any
    leg matches -- it can break a close tie, it never excludes an
    itinerary the way required_airlines does."""
    layover_minutes = sum(layover["duration"] for layover in itinerary.get("layovers") or [])
    score = (
        itinerary["price"]
        + (itinerary["total_duration"] / 60) * _TIME_VALUE_PER_HOUR
        + (layover_minutes / 60) * _LAYOVER_PENALTY_PER_HOUR
    )
    if preferred_airlines and _itinerary_matches_any_airline(itinerary, preferred_airlines):
        score -= _AIRLINE_PREFERENCE_BONUS
    return score


def _classify_layover_region(layover_id: str, region_map: dict[str, list[str]]) -> str | None:
    """Which configured region `layover_id` belongs to, or None if it
    isn't in ANY region's list yet -- a legitimate, expected result
    (layover_regions is deliberately incomplete, extended by hand as
    new layovers show up), not an error. Uppercase-normalizes
    `layover_id` first since it comes from SerpApi's own data, not
    something this codebase controls the casing of."""
    normalized = layover_id.upper()
    for region, airports in region_map.items():
        if normalized in airports:
            return region
    return None


def _passes_region_filters(
    itinerary: dict[str, Any],
    required_regions: tuple[str, ...],
    excluded_regions: tuple[str, ...],
    region_map: dict[str, list[str]],
) -> bool:
    """Region filtering is meaningless for a direct itinerary (no
    layovers) -- always passes. For a one-stop itinerary, an
    UNCLASSIFIED layover (not yet in region_map) also always passes
    both checks -- a config gap should never silently drop an
    itinerary nobody told this script to exclude. Empty
    required_regions is a no-op (nothing to require)."""
    layovers = itinerary.get("layovers") or []
    if not layovers:
        return True
    region = _classify_layover_region(layovers[0]["id"], region_map)
    if region is None:
        return True
    if region in excluded_regions:
        return False
    return not (required_regions and region not in required_regions)


def _passes_airline_required_filter(
    itinerary: dict[str, Any], required_airlines: tuple[str, ...]
) -> bool:
    """Empty `required_airlines` is a no-op (always passes) -- this is
    a hard allow-list, not a preference: an itinerary with no matching
    leg is excluded outright, not merely scored worse."""
    if not required_airlines:
        return True
    return _itinerary_matches_any_airline(itinerary, required_airlines)


def _best_by_stop_category(
    itineraries: list[dict[str, Any]],
    preferred_airlines: tuple[str, ...] = (),
) -> dict[int, dict[str, Any]]:
    """Best-scored itinerary per stop count (see _itinerary_score --
    price, total time, and layover time combined, not price alone),
    keeping only 0 (direct) and 1 (one-stop) -- anything with 2+ stops
    is out of scope and dropped here, not carried forward for a caller
    to filter out again."""
    best: dict[int, dict[str, Any]] = {}
    for itinerary in itineraries:
        stop_count = len(itinerary["flights"]) - 1
        if stop_count not in (_DIRECT, _ONE_STOP):
            continue
        current_best = best.get(stop_count)
        if current_best is None or _itinerary_score(
            itinerary, preferred_airlines
        ) < _itinerary_score(current_best, preferred_airlines):
            best[stop_count] = itinerary
    return best


def _filtered_best_by_stop_category(
    itineraries: list[dict[str, Any]], filters: Filters
) -> tuple[dict[int, dict[str, Any]], bool]:
    """Thin wrapper around _best_by_stop_category (itself unchanged
    except for now threading preferred_airlines through) -- region and
    required-airline filtering happen here, BEFORE bucketing, so the
    existing min-score-per-bucket loop stays oblivious to filtering.

    `used_fallback` means a required_airlines allow-list was set but
    matched nothing in the region-filtered pool, so the returned
    buckets come from that region-filtered pool instead -- the best
    AVAILABLE option, not a blank result."""
    region_ok = [
        it
        for it in itineraries
        if _passes_region_filters(
            it,
            filters.required_layover_regions,
            filters.excluded_layover_regions,
            filters.layover_regions,
        )
    ]
    required_ok = [
        it for it in region_ok if _passes_airline_required_filter(it, filters.required_airlines)
    ]
    pool, used_fallback = (
        (required_ok, False) if required_ok else (region_ok, bool(filters.required_airlines))
    )
    return _best_by_stop_category(pool, filters.preferred_airlines), used_fallback


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


def _baggage_text_for_leg(flight: dict[str, Any]) -> str:
    """Free-text baggage info for one flight leg -- SerpApi/Google
    Flights has no structured bag-count or fee field anywhere, only
    whatever sentence shows up in that leg's own `extensions` (mixed
    in with unrelated text like legroom). "not specified" is returned
    explicitly rather than an empty string, so "nothing useful found"
    is never mistaken for "nothing to report"."""
    bag_lines = [e for e in (flight.get("extensions") or []) if "bag" in e.lower()]
    return ", ".join(bag_lines) if bag_lines else "not specified"


def _row_from_itinerary(label: str, arrival_id: str, itinerary: dict[str, Any]) -> CategoryRow:
    flights = itinerary["flights"]
    airlines = ", ".join(dict.fromkeys(leg["airline"] for leg in flights))
    departure_timestamp = flights[0]["departure_airport"]["time"]
    arrival_timestamp = flights[-1]["arrival_airport"]["time"]
    departure_time = _time_of_day(departure_timestamp, departure_timestamp)
    arrival_time = _time_of_day(departure_timestamp, arrival_timestamp)
    hours, minutes = divmod(itinerary["total_duration"], 60)

    transit: str | None = None
    stop_id: str | None = None
    stop_name: str | None = None
    leg1_duration: str | None = None
    leg2_duration: str | None = None
    layovers = itinerary.get("layovers") or []
    if layovers:
        layover_minutes = sum(layover["duration"] for layover in layovers)
        transit_hours, transit_minutes = divmod(layover_minutes, 60)
        transit = f"{transit_hours}h{transit_minutes:02d}m"

        stop = layovers[0]
        stop_id = stop["id"]
        stop_name = stop["name"]
        leg1_hours, leg1_minutes = divmod(flights[0]["duration"], 60)
        leg1_duration = f"{leg1_hours}h{leg1_minutes:02d}m"
        leg2_hours, leg2_minutes = divmod(flights[1]["duration"], 60)
        leg2_duration = f"{leg2_hours}h{leg2_minutes:02d}m"

    if len(flights) == 1:
        baggage = _baggage_text_for_leg(flights[0])
    else:
        leg1_baggage = _baggage_text_for_leg(flights[0])
        leg2_baggage = _baggage_text_for_leg(flights[1])
        baggage = (
            leg1_baggage
            if leg1_baggage == leg2_baggage
            else f"{DEPARTURE_ID}-{stop_id}: {leg1_baggage}; {stop_id}-{arrival_id}: {leg2_baggage}"
        )

    return CategoryRow(
        airport=arrival_id,
        label=label,
        price=str(itinerary["price"]),
        airline=airlines,
        departure=departure_time,
        arrival=arrival_time,
        total=f"{hours}h{minutes:02d}m",
        transit=transit,
        baggage=baggage,
        stop_id=stop_id,
        stop_name=stop_name,
        leg1_duration=leg1_duration,
        leg2_duration=leg2_duration,
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


def _format_row_detail(row: CategoryRow) -> str:
    """Readable per-candidate detail line -- always includes baggage;
    a one-stop row (row.stop_id is not None) also gets the per-leg
    breakdown, reusing row.transit (already computed) rather than
    recalculating the layover duration a second time. A row.filtered_fallback
    row appends a note that it's the best AVAILABLE option, not one that
    actually matched a configured required_airlines allow-list."""
    if row.stop_id is None:
        detail = f"{row.label} ({row.airport}): Baggage -- {row.baggage}"
    else:
        detail = (
            f"{row.label} ({row.airport}) via {row.stop_name} ({row.stop_id}): "
            f"{DEPARTURE_ID}->{row.stop_id} {row.leg1_duration}, layover {row.transit}, "
            f"{row.stop_id}->{row.airport} {row.leg2_duration} -- Baggage: {row.baggage}"
        )
    if row.filtered_fallback:
        detail += " -- (no itinerary matched required filters, showing best available)"
    return detail


def _check_one(arrival_id: str, label: str, one_stop_eligible: bool) -> CandidateOutcome:
    """One candidate's outcome across both categories. Never raises --
    every exception fetch_all_itineraries can produce is caught HERE,
    per candidate, so one candidate's failure never hides another
    candidate's real result.

    one_stop_eligible=False means a one-stop bucket is deliberately
    discarded even if SerpApi returned one -- reaching a
    near-destination airport (VNS/LKO/DXN) via one layover isn't a
    comparison this trip cares about, only DIRECT is shown for those."""
    try:
        itineraries = fetch_all_itineraries(
            departure_id=DEPARTURE_ID,
            arrival_id=arrival_id,
            outbound_date=OUTBOUND_DATE,
            currency=CURRENCY,
            adults=ADULTS,
            children=CHILDREN,
        )
    except NoFlightsFoundYet:
        return CandidateOutcome(direct_row=None, one_stop_row=None, error_line=None)
    except CheckFailed as exc:
        return CandidateOutcome(
            direct_row=None,
            one_stop_row=None,
            error_line=f"{label} ({arrival_id}): error -- {exc}",
        )

    filters = Filters(
        preferred_airlines=_PREFERRED_AIRLINES,
        required_airlines=_REQUIRED_AIRLINES,
        excluded_layover_regions=_EXCLUDED_LAYOVER_REGIONS,
        required_layover_regions=_REQUIRED_LAYOVER_REGIONS,
        layover_regions=_LAYOVER_REGIONS,
    )
    best, used_fallback = _filtered_best_by_stop_category(itineraries, filters)
    direct_row = (
        _row_from_itinerary(label, arrival_id, best[_DIRECT]) if _DIRECT in best else None
    )
    if direct_row is not None and used_fallback:
        direct_row = replace(direct_row, filtered_fallback=True)
    one_stop_row = (
        _row_from_itinerary(label, arrival_id, best[_ONE_STOP])
        if one_stop_eligible and _ONE_STOP in best
        else None
    )
    if one_stop_row is not None and used_fallback:
        one_stop_row = replace(one_stop_row, filtered_fallback=True)
    # best may legitimately be empty (every itinerary SerpApi returned had
    # 2+ stops, or region/airline filtering emptied the pool) -- that's the
    # same "nothing useful to report" situation as NoFlightsFoundYet from
    # this script's own point of view, handled identically by leaving both
    # rows None.
    return CandidateOutcome(direct_row=direct_row, one_stop_row=one_stop_row, error_line=None)


def _notify_channel() -> str:
    """"email" on GitHub Actions (GITHUB_ACTIONS=true, set automatically
    in every job -- CallMeBot blocks that IP range), "whatsapp"
    otherwise (a home network works fine). FLIGHT_NOTIFY_CHANNEL
    overrides either way, for testing."""
    return os.environ.get("FLIGHT_NOTIFY_CHANNEL") or (
        "email" if os.environ.get("GITHUB_ACTIONS") == "true" else "whatsapp"
    )


def _passenger_summary() -> str:
    """"2 adults + 1 child" (or just "1 adult") -- always shown in the
    report so the price's passenger count is unambiguous regardless of
    whether SerpApi returns a total-for-party or a per-adult figure."""
    parts = [f"{ADULTS} adult{'s' if ADULTS != 1 else ''}"]
    if CHILDREN:
        parts.append(f"{CHILDREN} child{'ren' if CHILDREN != 1 else ''}")
    return " + ".join(parts)


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

    top_line = (
        f"Flight watch from {DEPARTURE_ID} on {OUTBOUND_DATE} ({CURRENCY}) -- "
        f"{_passenger_summary()}"
    )

    tables: list[tuple[str, str]] = []
    if direct_rows:
        table = _format_table(
            ["Airport", "Price", "Airline", "Dep", "Arr", "Total"],
            [
                [r.airport, r.price, r.airline, r.departure, r.arrival, r.total]
                for r in direct_rows
            ],
        )
        detail = "\n".join(_format_row_detail(r) for r in direct_rows)
        tables.append(("DIRECT", f"{table}\n\n{detail}"))
    if one_stop_rows:
        table = _format_table(
            ["Airport", "Price", "Airline", "Dep", "Arr", "Transit", "Total"],
            [
                [r.airport, r.price, r.airline, r.departure, r.arrival, r.transit or "", r.total]
                for r in one_stop_rows
            ],
        )
        detail = "\n".join(_format_row_detail(r) for r in one_stop_rows)
        tables.append(("1 STOP (max)", f"{table}\n\n{detail}"))

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


def _send_via(channel: str, whatsapp_message: str, email_subject: str, email_html: str) -> None:
    """Print+send for one channel -- shared by main()'s primary attempt
    and its fallback attempt so both go through identical logic."""
    if channel == "whatsapp":
        print(whatsapp_message)
        send_whatsapp(whatsapp_message)
    else:
        print(email_subject)
        print(email_html)
        send_email(email_subject, email_html)


def main() -> int:
    channel = _notify_channel()
    if channel not in ("whatsapp", "email"):
        print(f"Unknown FLIGHT_NOTIFY_CHANNEL '{channel}'", file=sys.stderr)
        return 1

    outcomes = [
        _check_one(arrival_id, label, one_stop_eligible)
        for arrival_id, label, one_stop_eligible in CANDIDATES
    ]

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

    # Both forms are built unconditionally -- cheap string formatting,
    # no extra API calls either way -- so a failed primary send can
    # fall back to the other channel without rebuilding anything.
    whatsapp_message = _build_whatsapp_message(outcomes)
    email_subject, email_html = _build_email_body(outcomes)

    primary = channel
    fallback = "email" if primary == "whatsapp" else "whatsapp"
    try:
        _send_via(primary, whatsapp_message, email_subject, email_html)
    except CheckFailed as primary_exc:
        print(
            f"{primary} notification failed ({primary_exc}) -- falling back to {fallback}",
            file=sys.stderr,
        )
        try:
            _send_via(fallback, whatsapp_message, email_subject, email_html)
        except CheckFailed as fallback_exc:
            print(
                f"{fallback} fallback ALSO failed ({fallback_exc}) -- no notification delivered",
                file=sys.stderr,
            )
            return 1

    # A genuine error on ANY candidate must make the whole run exit 1 --
    # matches this repo's own established discipline (exit 1 only for a
    # real failure, never for the routine no-data case).
    if any(o.error_line is not None for o in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
