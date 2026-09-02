"""Tests for check_flights.py -- stop-category bucketing, per-category
row construction (including the departure/arrival time-of-day "+1"
day-rollover suffix), the generic monospace table formatter, the
whatsapp-vs-email channel selection, and the generalized silence rule:
stay silent only when there is truly nothing to report for ANY
candidate in EITHER category, never when even one candidate has a real
finding or a real error (and a real error must still exit 1).

Candidates come from routes.toml's [check_flights] section at import
time (DEL, BOM, VNS, LKO, DXN as of this writing, each with its own
one_stop eligibility flag) -- these tests exercise the logic generically
via check_flights.CANDIDATES rather than hardcoding which airports are
configured, so they stay correct if routes.toml changes.
"""

from __future__ import annotations

import importlib

import pytest

import check_flights as cf
import flightwatch_core as core

# Captured before any test can monkeypatch it, so fixture teardown below
# always has the one true path to reload check_flights back against,
# regardless of monkeypatch's own teardown ordering.
_REAL_ROUTES_CONFIG_PATH = core._ROUTES_CONFIG_PATH

_DIRECT_ITINERARY = {
    "price": 480,
    "flights": [
        {
            "airline": "KLM",
            "departure_airport": {"time": "2027-07-17 09:15"},
            "arrival_airport": {"time": "2027-07-17 21:30"},
            "extensions": ["Below average legroom (29 in)", "Checked baggage for a fee"],
        }
    ],
    "total_duration": 735,
}

_ONE_STOP_ITINERARY = {
    "price": 356,
    "flights": [
        {
            "airline": "Oman Air",
            "departure_airport": {"time": "2027-07-17 10:15"},
            "arrival_airport": {"time": "2027-07-17 15:00"},
            "duration": 180,
            "extensions": ["Checked baggage for a fee"],
        },
        {
            "airline": "Oman Air",
            "departure_airport": {"time": "2027-07-17 16:30"},
            "arrival_airport": {"time": "2027-07-18 06:30"},
            "duration": 645,
            "extensions": ["Average legroom (31 in)", "1 free checked bag"],
        },
    ],
    "layovers": [{"duration": 90, "name": "Muscat", "id": "MCT"}],
    "total_duration": 915,
}

_TWO_STOP_ITINERARY = {
    "price": 200,  # deliberately cheapest of all -- must still be ignored
    "flights": [{"airline": "X"}, {"airline": "X"}, {"airline": "X"}],
    "total_duration": 1000,
}


class TestItineraryScore:
    def test_direct_itinerary_has_no_layover_term(self) -> None:
        # _DIRECT_ITINERARY: price=480, total_duration=735 (12h15m), no layovers
        expected = 480 + (735 / 60) * cf._TIME_VALUE_PER_HOUR
        assert cf._itinerary_score(_DIRECT_ITINERARY) == pytest.approx(expected)

    def test_one_stop_itinerary_counts_layover_twice(self) -> None:
        # _ONE_STOP_ITINERARY: price=356, total_duration=915 (15h15m),
        # layover=90min (1h30m) -- counted once inside total_duration,
        # once again as its own penalty term.
        expected = (
            356
            + (915 / 60) * cf._TIME_VALUE_PER_HOUR
            + (90 / 60) * cf._LAYOVER_PENALTY_PER_HOUR
        )
        assert cf._itinerary_score(_ONE_STOP_ITINERARY) == pytest.approx(expected)


class TestBestByStopCategory:
    def test_picks_best_direct_and_best_one_stop_independently(self) -> None:
        pricier_direct = {**_DIRECT_ITINERARY, "price": 900}
        itineraries = [pricier_direct, _DIRECT_ITINERARY, _ONE_STOP_ITINERARY]

        best = cf._best_by_stop_category(itineraries)

        assert best[cf._DIRECT]["price"] == 480
        assert best[cf._ONE_STOP]["price"] == 356

    def test_ignores_two_or_more_stops_entirely(self) -> None:
        best = cf._best_by_stop_category([_TWO_STOP_ITINERARY, _ONE_STOP_ITINERARY])

        assert cf._DIRECT not in best
        assert best[cf._ONE_STOP]["price"] == 356  # not the cheaper 200 two-stop one

    def test_category_absent_when_no_itinerary_qualifies(self) -> None:
        best = cf._best_by_stop_category([_ONE_STOP_ITINERARY])

        assert cf._DIRECT not in best
        assert cf._ONE_STOP in best

    def test_cheaper_but_much_longer_and_layover_heavy_loses_to_faster_pricier(self) -> None:
        """The actual behavior change: "best" is no longer "cheapest."
        A cheap itinerary with a huge layover (mirrors real BOM data
        seen live this session: price=860, total=33h30m, layover
        23h10m) should lose to a pricier-but-much-faster option."""
        cheap_but_slow = {
            "price": 300,
            "flights": [{"airline": "X"}, {"airline": "X"}],
            "layovers": [{"duration": 1200, "name": "Somewhere", "id": "XXX"}],  # 20h layover
            "total_duration": 2000,  # ~33.3h
        }
        pricier_but_fast = {
            "price": 500,
            "flights": [{"airline": "Y"}, {"airline": "Y"}],
            "layovers": [{"duration": 60, "name": "Elsewhere", "id": "YYY"}],  # 1h layover
            "total_duration": 600,  # 10h
        }

        best = cf._best_by_stop_category([cheap_but_slow, pricier_but_fast])

        assert best[cf._ONE_STOP]["price"] == 500


class TestTimeOfDay:
    def test_same_day_returns_bare_time(self) -> None:
        assert cf._time_of_day("2027-07-17 10:15", "2027-07-17 15:00") == "15:00"

    def test_next_day_gets_plus_one_suffix(self) -> None:
        assert cf._time_of_day("2027-07-17 10:15", "2027-07-18 06:30") == "06:30+1"


class TestRowFromItinerary:
    def test_direct_row_has_no_transit_or_stop_detail(self) -> None:
        row = cf._row_from_itinerary("Delhi", "DEL", _DIRECT_ITINERARY)

        assert row == cf.CategoryRow(
            airport="DEL",
            label="Delhi",
            price="480",
            airline="KLM",
            departure="09:15",
            arrival="21:30",
            total="12h15m",
            total_minutes=735,
            transit=None,
            baggage="Checked baggage for a fee",  # legroom extension filtered out
        )
        assert row.stop_id is None
        assert row.stop_name is None
        assert row.leg1_duration is None
        assert row.leg2_duration is None

    def test_one_stop_row_includes_transit_and_day_rollover(self) -> None:
        row = cf._row_from_itinerary("Varanasi", "VNS", _ONE_STOP_ITINERARY)

        assert row == cf.CategoryRow(
            airport="VNS",
            label="Varanasi",
            price="356",
            airline="Oman Air",
            departure="10:15",
            arrival="06:30+1",
            total="15h15m",
            total_minutes=915,
            transit="1h30m",
            baggage="AMS-MCT: Checked baggage for a fee; MCT-VNS: 1 free checked bag",
            stop_id="MCT",
            stop_name="Muscat",
            leg1_duration="3h00m",
            leg2_duration="10h45m",
        )

    def test_deduplicates_repeated_airline_across_segments(self) -> None:
        row = cf._row_from_itinerary("Varanasi", "VNS", _ONE_STOP_ITINERARY)
        assert row.airline == "Oman Air"  # not "Oman Air, Oman Air"


class TestBaggageTextForLeg:
    def test_matches_bag_related_extension_case_insensitively(self) -> None:
        flight = {"extensions": ["Wi-Fi for a fee", "1 free Checked Bag"]}
        assert cf._baggage_text_for_leg(flight) == "1 free Checked Bag"

    def test_joins_multiple_bag_related_extensions(self) -> None:
        flight = {"extensions": ["Carry-on bag included", "1 checked bag for a fee"]}
        assert cf._baggage_text_for_leg(flight) == "Carry-on bag included, 1 checked bag for a fee"

    def test_not_specified_when_no_extensions_mention_bags(self) -> None:
        flight = {"extensions": ["Below average legroom (29 in)", "Wi-Fi for a fee"]}
        assert cf._baggage_text_for_leg(flight) == "not specified"

    def test_not_specified_when_extensions_missing_entirely(self) -> None:
        assert cf._baggage_text_for_leg({}) == "not specified"


class TestNamed:
    def test_wraps_code_in_parens_after_label(self) -> None:
        assert cf._named("Amsterdam", "AMS") == "Amsterdam (AMS)"


class TestFormatRowDetail:
    def test_direct_row_shows_label_airport_and_baggage_only(self) -> None:
        row = cf._row_from_itinerary("Delhi", "DEL", _DIRECT_ITINERARY)

        line = cf._format_row_detail(row)

        assert line == "Delhi (DEL): Baggage -- Checked baggage for a fee"

    def test_one_stop_row_includes_leg_breakdown_and_baggage(self) -> None:
        row = cf._row_from_itinerary("Varanasi", "VNS", _ONE_STOP_ITINERARY)

        line = cf._format_row_detail(row)

        assert line == (
            f"Varanasi (VNS) via Muscat (MCT): {cf.DEPARTURE_ID}->MCT 3h00m, "
            "layover 1h30m, MCT->VNS 10h45m -- Baggage: "
            "AMS-MCT: Checked baggage for a fee; MCT-VNS: 1 free checked bag"
        )


class TestPassengerSummary:
    def test_single_adult_no_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "ADULTS", 1)
        monkeypatch.setattr(cf, "CHILDREN", 0)
        assert cf._passenger_summary() == "1 adult"

    def test_multiple_adults_no_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "ADULTS", 2)
        monkeypatch.setattr(cf, "CHILDREN", 0)
        assert cf._passenger_summary() == "2 adults"

    def test_adults_and_one_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "ADULTS", 2)
        monkeypatch.setattr(cf, "CHILDREN", 1)
        assert cf._passenger_summary() == "2 adults + 1 child"

    def test_adults_and_multiple_children(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "ADULTS", 2)
        monkeypatch.setattr(cf, "CHILDREN", 3)
        assert cf._passenger_summary() == "2 adults + 3 children"


class TestFormatTable:
    def test_columns_align_to_the_longest_of_header_or_any_cell(self) -> None:
        table = cf._format_table(
            ["Airport", "Price", "Airline"],
            [["DEL", "356", "Oman Air"], ["VNS", "9999", "KLM"]],
        )
        lines = table.split("\n")

        # "Price" header (5 chars) vs widest cell "9999" (4 chars) -> column
        # width is 5, driven by the header, not the cell. "Airline" header
        # (7 chars) vs widest cell "Oman Air" (8 chars) -> column width 8,
        # driven by the cell this time.
        assert lines[0] == "  ".join(
            ["Airport".ljust(7), "Price".rjust(5), "Airline".ljust(8)]
        ).rstrip()
        assert lines[1] == "  ".join(["-" * 7, "-" * 5, "-" * 8])
        assert lines[2] == "  ".join(
            ["DEL".ljust(7), "356".rjust(5), "Oman Air".ljust(8)]
        ).rstrip()
        assert lines[3] == "  ".join(
            ["VNS".ljust(7), "9999".rjust(5), "KLM".ljust(8)]
        ).rstrip()

    def test_price_column_right_aligned_others_left_aligned(self) -> None:
        table = cf._format_table(["Airport", "Price"], [["DEL", "9"], ["VNS", "1000"]])
        lines = table.split("\n")

        assert lines[2] == "  ".join(["DEL".ljust(7), "9".rjust(5)]).rstrip()
        assert lines[3] == "  ".join(["VNS".ljust(7), "1000".rjust(5)]).rstrip()

    def test_empty_rows_still_produces_header_and_separator(self) -> None:
        table = cf._format_table(["Airport", "Price"], [])
        assert table == "Airport  Price\n-------  -----"

    def test_price_column_found_by_name_not_fixed_index(self) -> None:
        """Price is no longer always column index 1 (From/To/Via can
        precede it) -- this proves the right-alignment follows the
        "Price" header wherever it actually lands."""
        table = cf._format_table(
            ["From", "To", "Via", "Price"],
            [["AMS", "DEL", "WAW", "9"], ["AMS", "BOM", "KWI", "1000"]],
        )
        lines = table.split("\n")

        assert lines[2] == "  ".join(
            ["AMS".ljust(4), "DEL".ljust(2), "WAW".ljust(3), "9".rjust(5)]
        ).rstrip()
        assert lines[3] == "  ".join(
            ["AMS".ljust(4), "BOM".ljust(2), "KWI".ljust(3), "1000".rjust(5)]
        ).rstrip()


class TestCheckOne:
    def test_price_found_in_both_categories_when_one_stop_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", lambda **_: [_DIRECT_ITINERARY, _ONE_STOP_ITINERARY]
        )

        outcome = cf._check_one("DEL", "Delhi", True)

        assert outcome.direct_row is not None and outcome.direct_row.price == "480"
        assert outcome.one_stop_row is not None and outcome.one_stop_row.price == "356"
        assert outcome.error_line is None

    def test_one_stop_bucket_dropped_when_not_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual bug being fixed: VNS/LKO/DXN should never show a
        1-STOP result, even when SerpApi genuinely has one -- only
        DIRECT is meaningful for a near-destination airport."""
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", lambda **_: [_DIRECT_ITINERARY, _ONE_STOP_ITINERARY]
        )

        outcome = cf._check_one("VNS", "Varanasi", False)

        assert outcome.direct_row is not None and outcome.direct_row.price == "480"
        assert outcome.one_stop_row is None
        assert outcome.error_line is None

    def test_no_flights_found_yet_gives_all_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("No flights found for AMS->DXN on 2027-07-17")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)

        outcome = cf._check_one("DXN", "Noida (Jewar)", False)

        assert outcome == cf.CandidateOutcome(direct_row=None, one_stop_row=None, error_line=None)

    def test_only_two_stop_options_gives_all_none_same_as_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "fetch_all_itineraries", lambda **_: [_TWO_STOP_ITINERARY])

        outcome = cf._check_one("DEL", "Delhi", True)

        assert outcome.direct_row is None
        assert outcome.one_stop_row is None
        assert outcome.error_line is None

    def test_genuine_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.CheckFailed("SerpApi returned HTTP 500: boom")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)

        outcome = cf._check_one("VNS", "Varanasi", False)

        assert outcome.direct_row is None
        assert outcome.one_stop_row is None
        assert outcome.error_line is not None and "boom" in outcome.error_line


class TestNotifyChannel:
    def test_defaults_to_whatsapp_locally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("FLIGHT_NOTIFY_CHANNEL", raising=False)
        assert cf._notify_channel() == "whatsapp"

    def test_defaults_to_email_on_github_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("FLIGHT_NOTIFY_CHANNEL", raising=False)
        assert cf._notify_channel() == "email"

    def test_override_wins_over_github_actions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("FLIGHT_NOTIFY_CHANNEL", "whatsapp")
        assert cf._notify_channel() == "whatsapp"

    def test_override_wins_locally_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setenv("FLIGHT_NOTIFY_CHANNEL", "email")
        assert cf._notify_channel() == "email"


class TestBuildWhatsappMessage:
    def test_sections_omitted_when_empty(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_row=cf.CategoryRow(
                    airport="DEL",
                    label="Delhi",
                    price="480",
                    airline="KLM",
                    departure="09:15",
                    arrival="21:30",
                    total="12h15m",
                    total_minutes=735,
                    transit=None,
                    baggage="Checked baggage for a fee",
                ),
                one_stop_row=None,
                error_line=None,
            )
        ]

        message = cf._build_whatsapp_message(outcomes)

        assert "DIRECT" in message
        assert "```" in message  # the direct table is fenced
        assert "1 STOP" not in message
        assert "ERRORS" not in message

    def test_currency_appears_once_in_header_not_per_row(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_row=cf.CategoryRow(
                    airport="DEL",
                    label="Delhi",
                    price="480",
                    airline="KLM",
                    departure="09:15",
                    arrival="21:30",
                    total="12h15m",
                    total_minutes=735,
                    transit=None,
                    baggage="Checked baggage for a fee",
                ),
                one_stop_row=None,
                error_line=None,
            )
        ]

        message = cf._build_whatsapp_message(outcomes)

        assert message.count(cf.CURRENCY) == 1
        assert "480" in message

    def test_direct_row_baggage_line_appears(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_row=cf.CategoryRow(
                    airport="DEL",
                    label="Delhi",
                    price="480",
                    airline="KLM",
                    departure="09:15",
                    arrival="21:30",
                    total="12h15m",
                    total_minutes=735,
                    transit=None,
                    baggage="Checked baggage for a fee",
                ),
                one_stop_row=None,
                error_line=None,
            )
        ]

        message = cf._build_whatsapp_message(outcomes)

        assert "Delhi (DEL): Baggage -- Checked baggage for a fee" in message

    def test_all_three_sections_appear_when_populated(self) -> None:
        direct_row = cf.CategoryRow(
            airport="DEL",
            label="Delhi",
            price="480",
            airline="KLM",
            departure="09:15",
            arrival="21:30",
            total="12h15m",
            total_minutes=735,
            transit=None,
            baggage="Checked baggage for a fee",
        )
        one_stop_row = cf.CategoryRow(
            airport="VNS",
            label="Varanasi",
            price="364",
            airline="IndiGo",
            departure="20:35",
            arrival="20:00+1",
            total="19h55m",
            total_minutes=1195,
            transit="8h00m",
            baggage="1 free checked bag",
            stop_id="DXB",
            stop_name="Dubai",
            leg1_duration="3h00m",
            leg2_duration="8h45m",
        )
        outcomes = [
            cf.CandidateOutcome(direct_row=direct_row, one_stop_row=None, error_line=None),
            cf.CandidateOutcome(direct_row=None, one_stop_row=one_stop_row, error_line=None),
            cf.CandidateOutcome(
                direct_row=None, one_stop_row=None, error_line="Lucknow (LKO): error -- boom"
            ),
        ]

        message = cf._build_whatsapp_message(outcomes)

        assert "DIRECT" in message and "DEL" in message and "480" in message
        assert "1 STOP" in message and "VNS" in message and "364" in message
        assert "ERRORS" in message and "error -- boom" in message
        # the per-candidate leg breakdown rides along inside the same
        # fenced section, not just the summary table
        assert "Varanasi (VNS) via Dubai (DXB)" in message
        assert f"{cf.DEPARTURE_ID}->DXB 3h00m" in message
        assert "DXB->VNS 8h45m" in message
        # baggage line appears for BOTH categories, not just 1-STOP
        assert "Delhi (DEL): Baggage -- Checked baggage for a fee" in message
        assert "Baggage: 1 free checked bag" in message

    def test_direct_table_has_from_to_columns_one_stop_table_adds_via(self) -> None:
        direct_row = cf.CategoryRow(
            airport="DEL",
            label="Delhi",
            price="480",
            airline="KLM",
            departure="09:15",
            arrival="21:30",
            total="12h15m",
            total_minutes=735,
            transit=None,
            baggage="Checked baggage for a fee",
        )
        one_stop_row = cf.CategoryRow(
            airport="VNS",
            label="Varanasi",
            price="364",
            airline="IndiGo",
            departure="20:35",
            arrival="20:00+1",
            total="19h55m",
            total_minutes=1195,
            transit="8h00m",
            baggage="1 free checked bag",
            stop_id="DXB",
            stop_name="Dubai",
            leg1_duration="3h00m",
            leg2_duration="8h45m",
        )
        outcomes = [
            cf.CandidateOutcome(direct_row=direct_row, one_stop_row=None, error_line=None),
            cf.CandidateOutcome(direct_row=None, one_stop_row=one_stop_row, error_line=None),
        ]

        message = cf._build_whatsapp_message(outcomes)
        lines = message.split("\n")
        from_cell = f"{cf.DEPARTURE_LABEL} ({cf.DEPARTURE_ID})"

        direct_header = next(line for line in lines if line.startswith("From"))
        assert direct_header.split() == ["From", "To", "Price", "Airline", "Dep", "Arr", "Total"]
        direct_data_row = next(line for line in lines if line.startswith(from_cell))
        assert "Delhi (DEL)" in direct_data_row

        via_header = next(line for line in lines if line.startswith("From") and "Via" in line)
        assert via_header.split() == [
            "From",
            "To",
            "Via",
            "Price",
            "Airline",
            "Dep",
            "Arr",
            "Transit",
            "Total",
        ]
        via_data_row = next(
            line for line in lines if line.startswith(from_cell) and "Varanasi" in line
        )
        assert "Varanasi (VNS)" in via_data_row
        assert "Dubai (DXB)" in via_data_row
        # "To" must come before "Via" in the row -- proves column order, not just presence
        assert via_data_row.index("Varanasi (VNS)") < via_data_row.index("Dubai (DXB)")


class TestBuildEmailBody:
    def test_subject_and_html_pre_wrapping_no_fences(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_row=cf.CategoryRow(
                    airport="DEL",
                    label="Delhi",
                    price="480",
                    airline="KLM",
                    departure="09:15",
                    arrival="21:30",
                    total="12h15m",
                    total_minutes=735,
                    transit=None,
                    baggage="Checked baggage for a fee",
                ),
                one_stop_row=None,
                error_line=None,
            )
        ]

        subject, html_body = cf._build_email_body(outcomes)

        assert subject == (
            f"Flight watch: {cf.DEPARTURE_ID} on {cf.OUTBOUND_DATE}, back {cf.RETURN_DATE}"
        )
        assert html_body.startswith("<!DOCTYPE html>")
        assert "```" not in html_body  # no whatsapp fences in the email path
        assert "DIRECT" in html_body
        assert "480" in html_body
        assert "KLM" in html_body

    def test_one_stop_detail_line_appears_in_body(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_row=None,
                one_stop_row=cf.CategoryRow(
                    airport="VNS",
                    label="Varanasi",
                    price="364",
                    airline="IndiGo",
                    departure="20:35",
                    arrival="20:00+1",
                    total="19h55m",
                    total_minutes=1195,
                    transit="8h00m",
                    baggage="1 free checked bag",
                    stop_id="DXB",
                    stop_name="Dubai",
                    leg1_duration="3h00m",
                    leg2_duration="8h45m",
                ),
                error_line=None,
            )
        ]

        _subject, html_body = cf._build_email_body(outcomes)

        assert "ONE STOP" in html_body
        assert "DIRECT" not in html_body  # no direct rows this time
        assert "via Dubai" in html_body
        assert "8h00m transit" in html_body
        assert "IndiGo" in html_body
        assert "364" in html_body

    def test_html_escapes_unsafe_characters(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_row=None,
                one_stop_row=None,
                error_line="Delhi (DEL): error -- <script>&boom</script>",
            )
        ]

        _subject, html_body = cf._build_email_body(outcomes)

        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body
        assert "&amp;boom" in html_body


class TestAllCategoryRows:
    def test_flattens_direct_and_one_stop_across_multiple_outcomes(self) -> None:
        direct_row = cf._row_from_itinerary("Delhi", "DEL", _DIRECT_ITINERARY)
        one_stop_row = cf._row_from_itinerary("Varanasi", "VNS", _ONE_STOP_ITINERARY)
        outcomes = [
            cf.CandidateOutcome(direct_row=direct_row, one_stop_row=None, error_line=None),
            cf.CandidateOutcome(direct_row=None, one_stop_row=one_stop_row, error_line=None),
            cf.CandidateOutcome(direct_row=None, one_stop_row=None, error_line="boom"),
        ]

        rows = cf._all_category_rows(outcomes)

        assert rows == [(direct_row, False), (one_stop_row, True)]

    def test_empty_outcomes_gives_empty_list(self) -> None:
        assert cf._all_category_rows([]) == []


class TestEmailOptions:
    def test_direct_row_maps_to_empty_stops_and_clean_arrival(self) -> None:
        row = cf._row_from_itinerary("Delhi", "DEL", _DIRECT_ITINERARY)
        outcomes = [cf.CandidateOutcome(direct_row=row, one_stop_row=None, error_line=None)]

        options = cf._email_options(outcomes)

        assert len(options) == 1
        option = options[0]
        assert option["stops"] == []
        assert option["arr"] == "21:30"
        assert option["arr_next_day"] is False
        assert option["price_value"] == 480
        assert option["total_minutes"] == 735

    def test_one_stop_row_splits_next_day_suffix_and_builds_stops(self) -> None:
        row = cf._row_from_itinerary("Varanasi", "VNS", _ONE_STOP_ITINERARY)
        outcomes = [cf.CandidateOutcome(direct_row=None, one_stop_row=row, error_line=None)]

        options = cf._email_options(outcomes)

        option = options[0]
        assert option["arr"] == "06:30"  # "+1" suffix stripped
        assert option["arr_next_day"] is True
        assert option["stops"] == [{"via": "Muscat", "transit": "1h30m"}]

    def test_multiple_airlines_joined_with_plus_not_comma(self) -> None:
        itinerary = {
            **_ONE_STOP_ITINERARY,
            "flights": [
                {**_ONE_STOP_ITINERARY["flights"][0], "airline": "Qatar Airways"},
                {**_ONE_STOP_ITINERARY["flights"][1], "airline": "IndiGo"},
            ],
        }
        row = cf._row_from_itinerary("Varanasi", "VNS", itinerary)
        outcomes = [cf.CandidateOutcome(direct_row=None, one_stop_row=row, error_line=None)]

        options = cf._email_options(outcomes)

        assert options[0]["airline"] == "Qatar Airways + IndiGo"

    def test_float_formatted_price_string_does_not_crash(self) -> None:
        """Real bug caught during implementation: an older test fixture
        produces a price like "300.0" (str(float(...))) -- price_value
        must tolerate that, not just a clean integer string."""
        row = cf._row_from_itinerary("Delhi", "DEL", {**_DIRECT_ITINERARY, "price": 300.0})
        outcomes = [cf.CandidateOutcome(direct_row=row, one_stop_row=None, error_line=None)]

        options = cf._email_options(outcomes)

        assert options[0]["price_value"] == 300


class TestRecommendation:
    def _option(self, **overrides: object) -> dict[str, object]:
        base = {
            "airline": "KLM",
            "to_label": "Delhi",
            "price": "480",
            "price_value": 480,
            "total_minutes": 600,
            "stops": [],
        }
        base.update(overrides)
        return base

    def test_empty_options_returns_empty_string(self) -> None:
        assert cf._recommendation([], "EUR") == ""

    def test_direct_cheapest_with_at_least_hour_faster_gets_comparative_clause(self) -> None:
        cheapest_direct = self._option(total_minutes=600)  # 10h
        slower_one_stop = self._option(
            price_value=900, total_minutes=780, stops=[{"via": "X", "transit": "1h"}]
        )  # 13h -- 3h slower

        text = cf._recommendation([cheapest_direct, slower_one_stop], "EUR")

        assert text == (
            "KLM to Delhi at EUR 480 -- cheapest overall and 3h quicker than any one-stop."
        )

    def test_direct_cheapest_with_sub_hour_gap_has_no_comparative_clause(self) -> None:
        cheapest_direct = self._option(total_minutes=600)
        barely_slower_one_stop = self._option(
            price_value=900, total_minutes=630, stops=[{"via": "X", "transit": "1h"}]
        )  # only 30 minutes slower -- must not claim "0h quicker"

        text = cf._recommendation([cheapest_direct, barely_slower_one_stop], "EUR")

        assert text == "KLM to Delhi at EUR 480 -- cheapest overall."
        assert "quicker" not in text

    def test_direct_cheapest_with_no_one_stop_to_compare_has_no_comparative_clause(self) -> None:
        only_direct = self._option()

        text = cf._recommendation([only_direct], "EUR")

        assert text == "KLM to Delhi at EUR 480 -- cheapest overall."

    def test_one_stop_is_cheapest_has_no_comparative_clause(self) -> None:
        cheapest_one_stop = self._option(stops=[{"via": "X", "transit": "1h"}])
        pricier_direct = self._option(price_value=900)

        text = cf._recommendation([cheapest_one_stop, pricier_direct], "EUR")

        assert text == "KLM to Delhi at EUR 480 -- cheapest overall."

    def test_tie_break_is_encounter_order(self) -> None:
        first = self._option(to_label="Delhi")
        tied_second = self._option(to_label="Mumbai")  # same price_value=480

        text = cf._recommendation([first, tied_second], "EUR")

        assert "Delhi" in text
        assert "Mumbai" not in text


class TestMainSilenceRule:
    """All these pin GITHUB_ACTIONS unset (-> whatsapp channel) unless
    a test says otherwise -- the silence/notify/exit-code rules
    themselves don't depend on which channel is active, so most tests
    exercise the already-established whatsapp path and one dedicated
    test below proves the email path gets picked and used correctly
    too."""

    def test_all_candidates_no_data_yet_stays_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert sent == []

    def test_one_candidate_direct_only_still_notifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        priced_id = cf.CANDIDATES[0][0]

        def _fake_fetch(*, arrival_id: str, **_: object) -> list[dict[str, object]]:
            if arrival_id == priced_id:
                return [_DIRECT_ITINERARY]
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert len(sent) == 1
        assert "480" in sent[0]
        assert "DIRECT" in sent[0]
        assert "1 STOP" not in sent[0]  # nobody else had a one-stop result either

    def test_one_candidate_erroring_still_notifies_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact case the module docstring warns about: an error
        outcome must never be masked by another candidate's routine
        no-data outcome, and must make the whole run exit 1."""
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        erroring_id = cf.CANDIDATES[0][0]

        def _fake_fetch(*, arrival_id: str, **_: object) -> list[dict[str, object]]:
            if arrival_id == erroring_id:
                raise core.CheckFailed("SerpApi returned HTTP 500: boom")
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 1
        assert len(sent) == 1
        assert "boom" in sent[0]

    def test_multiple_candidates_priced_all_appear_in_one_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the comparison genuinely covers every configured
        candidate in ONE message, not just the first/last one -- real at
        the actual 4-candidate scale, not just 2."""
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

        def _fake_fetch(*, arrival_id: str, **_: object) -> list[dict[str, object]]:
            return [{**_DIRECT_ITINERARY, "price": float(len(arrival_id) * 100)}]

        monkeypatch.setattr(cf, "fetch_all_itineraries", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert len(sent) == 1
        for arrival_id, _label, _one_stop in cf.CANDIDATES:
            assert arrival_id in sent[0]

    def test_github_actions_env_routes_to_email_instead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        priced_id = cf.CANDIDATES[0][0]

        def _fake_fetch(*, arrival_id: str, **_: object) -> list[dict[str, object]]:
            if arrival_id == priced_id:
                return [_DIRECT_ITINERARY]
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _fake_fetch)
        whatsapp_sent: list[str] = []
        email_sent: list[tuple[str, str]] = []
        monkeypatch.setattr(cf, "send_whatsapp", whatsapp_sent.append)
        monkeypatch.setattr(
            cf, "send_email", lambda subject, body: email_sent.append((subject, body))
        )

        exit_code = cf.main()

        assert exit_code == 0
        assert whatsapp_sent == []
        assert len(email_sent) == 1
        subject, body = email_sent[0]
        assert subject == (
            f"Flight watch: {cf.DEPARTURE_ID} on {cf.OUTBOUND_DATE}, back {cf.RETURN_DATE}"
        )
        assert "480" in body

    def test_invalid_notify_channel_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLIGHT_NOTIFY_CHANNEL", "carrier-pigeon")
        called = []
        monkeypatch.setattr(cf, "fetch_all_itineraries", lambda **_: called.append(1))

        exit_code = cf.main()

        assert exit_code == 1
        assert called == []  # fails before ever querying SerpApi


class TestMainFallback:
    """If the chosen channel's send itself fails, main() falls back to
    the other channel once rather than crashing with an uncaught
    exception -- traced live this session against a real CallMeBot
    403."""

    def _priced_fetch(self, priced_id: str):
        def _fake_fetch(*, arrival_id: str, **_: object) -> list[dict[str, object]]:
            if arrival_id == priced_id:
                return [_DIRECT_ITINERARY]
            raise core.NoFlightsFoundYet("no data yet")

        return _fake_fetch

    def test_primary_success_never_attempts_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # -> whatsapp primary
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", self._priced_fetch(cf.CANDIDATES[0][0])
        )
        whatsapp_sent: list[str] = []
        email_sent: list[tuple[str, str]] = []
        monkeypatch.setattr(cf, "send_whatsapp", whatsapp_sent.append)
        monkeypatch.setattr(
            cf, "send_email", lambda subject, body: email_sent.append((subject, body))
        )

        exit_code = cf.main()

        assert exit_code == 0
        assert len(whatsapp_sent) == 1
        assert email_sent == []  # fallback never touched

    def test_primary_failure_falls_back_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # -> whatsapp primary
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", self._priced_fetch(cf.CANDIDATES[0][0])
        )

        def _failing_whatsapp(_message: str) -> None:
            raise core.CheckFailed("CallMeBot returned HTTP 403: forbidden")

        email_sent: list[tuple[str, str]] = []
        monkeypatch.setattr(cf, "send_whatsapp", _failing_whatsapp)
        monkeypatch.setattr(
            cf, "send_email", lambda subject, body: email_sent.append((subject, body))
        )

        exit_code = cf.main()

        assert exit_code == 0  # no per-candidate error_line, fallback delivered fine
        assert len(email_sent) == 1
        assert "480" in email_sent[0][1]

    def test_both_channels_failing_exits_one_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", self._priced_fetch(cf.CANDIDATES[0][0])
        )

        def _failing_whatsapp(_message: str) -> None:
            raise core.CheckFailed("CallMeBot returned HTTP 403: forbidden")

        def _failing_email(_subject: str, _body: str) -> None:
            raise core.CheckFailed("Gmail SMTP send failed: connection refused")

        monkeypatch.setattr(cf, "send_whatsapp", _failing_whatsapp)
        monkeypatch.setattr(cf, "send_email", _failing_email)

        # main() must return 1 cleanly -- if either CheckFailed escaped
        # uncaught, this call itself would raise and fail the test.
        exit_code = cf.main()

        assert exit_code == 1

    def test_fallback_direction_is_whatsapp_when_email_is_primary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")  # -> email primary
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", self._priced_fetch(cf.CANDIDATES[0][0])
        )

        def _failing_email(_subject: str, _body: str) -> None:
            raise core.CheckFailed("Gmail SMTP send failed: auth error")

        whatsapp_sent: list[str] = []
        monkeypatch.setattr(cf, "send_email", _failing_email)
        monkeypatch.setattr(cf, "send_whatsapp", whatsapp_sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert len(whatsapp_sent) == 1
        assert "480" in whatsapp_sent[0]


# Region map used by every filter test below -- deliberately smaller than
# routes.toml's real one so each test's intent is obvious from its own
# entries, not from having to cross-reference the committed config file.
_REGION_MAP = {
    "middle_east": ["MCT", "DXB"],
    "europe_uk": ["LHR", "AMS"],
}


class TestClassifyLayoverRegion:
    def test_known_iata_returns_correct_region(self) -> None:
        assert cf._classify_layover_region("MCT", _REGION_MAP) == "middle_east"

    def test_lowercase_input_still_matches_uppercase_normalized(self) -> None:
        assert cf._classify_layover_region("mct", _REGION_MAP) == "middle_east"

    def test_unknown_iata_returns_none(self) -> None:
        assert cf._classify_layover_region("XXX", _REGION_MAP) is None


class TestPassesRegionFilters:
    def test_direct_itinerary_always_passes_regardless_of_filters(self) -> None:
        assert cf._passes_region_filters(
            _DIRECT_ITINERARY,
            required_regions=("europe_uk",),
            excluded_regions=("middle_east",),
            region_map=_REGION_MAP,
        )

    def test_excluded_region_layover_is_dropped(self) -> None:
        assert not cf._passes_region_filters(
            _ONE_STOP_ITINERARY,  # layover MCT -> middle_east
            required_regions=(),
            excluded_regions=("middle_east",),
            region_map=_REGION_MAP,
        )

    def test_required_region_allow_list_keeps_only_matches(self) -> None:
        assert cf._passes_region_filters(
            _ONE_STOP_ITINERARY,  # layover MCT -> middle_east
            required_regions=("middle_east",),
            excluded_regions=(),
            region_map=_REGION_MAP,
        )
        assert not cf._passes_region_filters(
            _ONE_STOP_ITINERARY,
            required_regions=("europe_uk",),
            excluded_regions=(),
            region_map=_REGION_MAP,
        )

    def test_unknown_region_layover_passes_both_filters(self) -> None:
        unclassified = {
            **_ONE_STOP_ITINERARY,
            "layovers": [{"duration": 90, "name": "Somewhere", "id": "ZZZ"}],
        }
        assert cf._passes_region_filters(
            unclassified,
            required_regions=("middle_east",),
            excluded_regions=("europe_uk",),
            region_map=_REGION_MAP,
        )

    def test_both_filters_empty_is_a_no_op(self) -> None:
        """Regression guard: pre-feature behaviour (no region filtering
        at all) must still hold when both region filter lists are
        empty, even for a one-stop itinerary with a classified
        layover."""
        assert cf._passes_region_filters(
            _ONE_STOP_ITINERARY, required_regions=(), excluded_regions=(), region_map=_REGION_MAP
        )


class TestPassesAirlineRequiredFilter:
    def test_empty_required_airlines_always_passes(self) -> None:
        assert cf._passes_airline_required_filter(_DIRECT_ITINERARY, ())

    def test_substring_match_against_full_airline_name(self) -> None:
        itinerary = {
            **_DIRECT_ITINERARY,
            "flights": [{**_DIRECT_ITINERARY["flights"][0], "airline": "KLM Royal Dutch Airlines"}],
        }
        assert cf._passes_airline_required_filter(itinerary, ("KLM",))

    def test_match_on_second_leg_not_just_first(self) -> None:
        itinerary = {
            **_ONE_STOP_ITINERARY,
            "flights": [
                {**_ONE_STOP_ITINERARY["flights"][0], "airline": "Oman Air"},
                {**_ONE_STOP_ITINERARY["flights"][1], "airline": "KLM Royal Dutch Airlines"},
            ],
        }
        assert cf._passes_airline_required_filter(itinerary, ("KLM",))

    def test_no_match_returns_false(self) -> None:
        assert not cf._passes_airline_required_filter(_DIRECT_ITINERARY, ("Emirates",))


class TestItineraryScorePreferredAirlineBonus:
    def test_empty_preferred_airlines_matches_prefeature_score(self) -> None:
        """Regression guard: _itinerary_score's original two-term
        formula (price + time-value-weighted duration + layover
        penalty) must be byte-identical when no preferred airlines are
        configured -- this IS the pre-feature score, just reached via
        the new optional parameter's default."""
        expected = 480 + (735 / 60) * cf._TIME_VALUE_PER_HOUR
        assert cf._itinerary_score(
            _DIRECT_ITINERARY, preferred_airlines=()
        ) == pytest.approx(expected)

    def test_one_matching_leg_subtracts_bonus_exactly_once(self) -> None:
        expected = 480 + (735 / 60) * cf._TIME_VALUE_PER_HOUR - cf._AIRLINE_PREFERENCE_BONUS
        assert cf._itinerary_score(
            _DIRECT_ITINERARY, preferred_airlines=("KLM",)
        ) == pytest.approx(expected)

    def test_match_on_both_legs_still_subtracts_bonus_only_once(self) -> None:
        # _ONE_STOP_ITINERARY: both flights are "Oman Air" -- matching on
        # every leg must still only apply the bonus ONCE per itinerary.
        expected = (
            356
            + (915 / 60) * cf._TIME_VALUE_PER_HOUR
            + (90 / 60) * cf._LAYOVER_PENALTY_PER_HOUR
            - cf._AIRLINE_PREFERENCE_BONUS
        )
        assert cf._itinerary_score(
            _ONE_STOP_ITINERARY, preferred_airlines=("Oman Air",)
        ) == pytest.approx(expected)


class TestFilteredBestByStopCategory:
    def test_required_airline_filter_emptying_pool_falls_back_to_region_filtered(self) -> None:
        filters = cf.Filters(
            preferred_airlines=(),
            required_airlines=("Emirates",),  # matches neither itinerary below
            excluded_layover_regions=(),
            required_layover_regions=(),
            layover_regions=_REGION_MAP,
        )

        best, used_fallback = cf._filtered_best_by_stop_category(
            [_DIRECT_ITINERARY, _ONE_STOP_ITINERARY], filters
        )

        assert used_fallback is True
        assert best[cf._DIRECT]["price"] == 480
        assert best[cf._ONE_STOP]["price"] == 356

    def test_excluded_region_filter_emptying_pool_gives_empty_result_no_fallback(self) -> None:
        filters = cf.Filters(
            preferred_airlines=(),
            required_airlines=(),
            excluded_layover_regions=("middle_east",),
            required_layover_regions=(),
            layover_regions=_REGION_MAP,
        )

        # _ONE_STOP_ITINERARY's only layover (MCT) is in the excluded
        # region, so the pool is empty before required_airlines even
        # gets a chance to run -- and required_airlines is empty here,
        # so there is nothing to fall back to either.
        best, used_fallback = cf._filtered_best_by_stop_category([_ONE_STOP_ITINERARY], filters)

        assert best == {}
        assert used_fallback is False

    def test_combining_excluded_region_and_required_airline_in_one_candidate(self) -> None:
        def _one_stop(price: int, stop_id: str, airline: str) -> dict[str, object]:
            return {
                "price": price,
                "flights": [{"airline": airline}, {"airline": airline}],
                "layovers": [{"duration": 60, "name": "x", "id": stop_id}],
                "total_duration": 600,
            }

        excluded_region = _one_stop(300, "MCT", "British Airways")  # dropped: middle_east
        allowed_region_wrong_airline = _one_stop(310, "LHR", "British Airways")  # dropped: airline
        allowed_region_right_airline = _one_stop(320, "LHR", "KLM Royal Dutch Airlines")  # kept

        filters = cf.Filters(
            preferred_airlines=(),
            required_airlines=("KLM",),
            excluded_layover_regions=("middle_east",),
            required_layover_regions=(),
            layover_regions=_REGION_MAP,
        )

        best, used_fallback = cf._filtered_best_by_stop_category(
            [excluded_region, allowed_region_wrong_airline, allowed_region_right_airline], filters
        )

        assert used_fallback is False  # a real match survived, no fallback needed
        assert best[cf._ONE_STOP]["price"] == 320


class TestFormatRowDetailFilteredFallback:
    def test_fallback_annotation_appears_when_filtered_fallback_true(self) -> None:
        row = cf.CategoryRow(
            airport="DEL",
            label="Delhi",
            price="480",
            airline="KLM",
            departure="09:15",
            arrival="21:30",
            total="12h15m",
            total_minutes=735,
            transit=None,
            baggage="Checked baggage for a fee",
            filtered_fallback=True,
        )

        line = cf._format_row_detail(row)

        assert line == (
            "Delhi (DEL): Baggage -- Checked baggage for a fee "
            "-- (no itinerary matched required filters, showing best available)"
        )

    def test_no_fallback_annotation_when_filtered_fallback_false(self) -> None:
        row = cf._row_from_itinerary("Delhi", "DEL", _DIRECT_ITINERARY)

        line = cf._format_row_detail(row)

        assert "(no itinerary matched required filters" not in line


@pytest.fixture
def reload_check_flights():
    """Yields a function that monkeypatches flightwatch_core's routes
    path and reloads check_flights against it -- module-level constants
    like _CONFIG/_FILTERS/_PREFERRED_AIRLINES are computed once, at
    import time, so exercising _validate_filter_lists/_validate_region_keys
    (called only during that module-level setup) means actually
    re-importing the module, not just calling a function directly.

    Always reloads check_flights back against the real routes.toml
    afterward, so later tests in this file see the same module state
    they started with, even when the reload under test raised."""

    def _reload(monkeypatch: pytest.MonkeyPatch, routes_path: object):
        monkeypatch.setattr(core, "_ROUTES_CONFIG_PATH", routes_path)
        return importlib.reload(cf)

    yield _reload

    core._ROUTES_CONFIG_PATH = _REAL_ROUTES_CONFIG_PATH
    importlib.reload(cf)


_MINIMAL_ROUTES_HEADER = """
[check_flights]
departure_id = "AMS"
departure_label = "Amsterdam"
outbound_date = "2027-07-17"
return_after_weeks = 5
currency = "EUR"
adults = 1
children = 0
candidates = [{ id = "DEL", label = "Delhi", one_stop = true }]
"""


class TestFilterConfigValidation:
    """Module-level validation runs at IMPORT time (see check_flights.py's
    _validate_filter_lists/_validate_region_keys calls, right after
    _FILTERS is loaded from routes.toml) -- these tests reload the
    module against a scratch routes.toml in tmp_path to exercise that
    path, following the same monkeypatched-_ROUTES_CONFIG_PATH pattern
    as test_flightwatch_core.py's test_missing_file_raises_check_failed.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "preferred_airlines",
            "required_airlines",
            "excluded_layover_regions",
            "required_layover_regions",
        ],
    )
    def test_non_list_filter_value_raises_check_failed(
        self,
        key: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: object,
        reload_check_flights,
    ) -> None:
        routes_path = tmp_path / "routes.toml"  # type: ignore[operator]
        routes_path.write_text(
            _MINIMAL_ROUTES_HEADER + f'\n[check_flights.filters]\n{key} = "KLM"\n'
        )

        with pytest.raises(core.CheckFailed, match=f"{key} must be a list"):
            reload_check_flights(monkeypatch, routes_path)

    def test_unknown_region_name_raises_check_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object, reload_check_flights
    ) -> None:
        routes_path = tmp_path / "routes.toml"  # type: ignore[operator]
        routes_path.write_text(
            _MINIMAL_ROUTES_HEADER
            + """
[check_flights.filters]
excluded_layover_regions = ["middel_east"]

[check_flights.filters.layover_regions]
middle_east = ["MCT"]
"""
        )

        with pytest.raises(core.CheckFailed, match="is not a key in layover_regions"):
            reload_check_flights(monkeypatch, routes_path)

    def test_absent_filters_block_loads_with_all_empty_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object, reload_check_flights
    ) -> None:
        routes_path = tmp_path / "routes.toml"  # type: ignore[operator]
        routes_path.write_text(_MINIMAL_ROUTES_HEADER)

        module = reload_check_flights(monkeypatch, routes_path)

        assert module._PREFERRED_AIRLINES == ()
        assert module._REQUIRED_AIRLINES == ()
        assert module._EXCLUDED_LAYOVER_REGIONS == ()
        assert module._REQUIRED_LAYOVER_REGIONS == ()
        assert module._LAYOVER_REGIONS == {}


class TestFullPipelineRegressionNoFilters:
    """The constraint the whole feature depends on: with
    [check_flights.filters] entirely absent from routes.toml, the
    output must be byte-identical to what the pre-feature pipeline --
    _best_by_stop_category and _row_from_itinerary called directly,
    with no Filters/_filtered_best_by_stop_category concept involved at
    all -- would have produced. This is the test that actually protects
    "nothing regresses," not the individual filter-unit tests above."""

    def test_output_matches_prefeature_pipeline_exactly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object, reload_check_flights
    ) -> None:
        routes_path = tmp_path / "routes.toml"  # type: ignore[operator]
        routes_path.write_text(_MINIMAL_ROUTES_HEADER)
        module = reload_check_flights(monkeypatch, routes_path)

        itineraries = [_DIRECT_ITINERARY, _ONE_STOP_ITINERARY]
        monkeypatch.setattr(module, "fetch_all_itineraries", lambda **_: itineraries)

        outcome = module._check_one("DEL", "Delhi", True)

        best = module._best_by_stop_category(itineraries)
        baseline_outcome = module.CandidateOutcome(
            direct_row=module._row_from_itinerary("Delhi", "DEL", best[module._DIRECT]),
            one_stop_row=module._row_from_itinerary("Delhi", "DEL", best[module._ONE_STOP]),
            error_line=None,
        )

        assert outcome.direct_row == baseline_outcome.direct_row
        assert outcome.one_stop_row == baseline_outcome.one_stop_row
        assert module._build_whatsapp_message([outcome]) == module._build_whatsapp_message(
            [baseline_outcome]
        )
