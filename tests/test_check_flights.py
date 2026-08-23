"""Tests for check_flights.py -- stop-category bucketing, per-category
row construction (including the departure/arrival time-of-day "+1"
day-rollover suffix), the generic monospace table formatter, the
whatsapp-vs-email channel selection, and the generalized silence rule:
stay silent only when there is truly nothing to report for ANY
candidate in EITHER category, never when even one candidate has a real
finding or a real error (and a real error must still exit 1).

Candidates come from routes.toml's [check_flights] section at import
time (DEL, VNS, LKO, DXN as of this writing) -- these tests exercise the
logic generically via check_flights.CANDIDATES rather than hardcoding
which four airports are configured, so they stay correct if routes.toml
changes.
"""

from __future__ import annotations

import pytest

import check_flights as cf
import flightwatch_core as core

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


class TestBestByStopCategory:
    def test_picks_cheapest_direct_and_cheapest_one_stop_independently(self) -> None:
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


class TestCheckOne:
    def test_price_found_in_both_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", lambda **_: [_DIRECT_ITINERARY, _ONE_STOP_ITINERARY]
        )

        outcome = cf._check_one("DEL", "Delhi")

        assert outcome.direct_row is not None and outcome.direct_row.price == "480"
        assert outcome.one_stop_row is not None and outcome.one_stop_row.price == "356"
        assert outcome.error_line is None

    def test_no_flights_found_yet_gives_all_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("No flights found for AMS->DXN on 2027-07-17")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)

        outcome = cf._check_one("DXN", "Noida (Jewar)")

        assert outcome == cf.CandidateOutcome(direct_row=None, one_stop_row=None, error_line=None)

    def test_only_two_stop_options_gives_all_none_same_as_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "fetch_all_itineraries", lambda **_: [_TWO_STOP_ITINERARY])

        outcome = cf._check_one("DEL", "Delhi")

        assert outcome.direct_row is None
        assert outcome.one_stop_row is None
        assert outcome.error_line is None

    def test_genuine_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.CheckFailed("SerpApi returned HTTP 500: boom")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)

        outcome = cf._check_one("VNS", "Varanasi")

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
                    transit=None,
                    baggage="Checked baggage for a fee",
                ),
                one_stop_row=None,
                error_line=None,
            )
        ]

        subject, html_body = cf._build_email_body(outcomes)

        assert subject == f"Flight watch: {cf.DEPARTURE_ID} on {cf.OUTBOUND_DATE}"
        assert html_body.startswith('<pre style="font-family: monospace">')
        assert html_body.endswith("</pre>")
        assert "```" not in html_body  # no whatsapp fences in the email path
        assert "DIRECT" in html_body
        assert "480" in html_body
        # baggage line appears under DIRECT too, not just 1-STOP
        assert "Delhi (DEL): Baggage -- Checked baggage for a fee" in html_body

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

        assert "Varanasi (VNS) via Dubai (DXB)" in html_body
        assert "Baggage: 1 free checked bag" in html_body

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
        for arrival_id, _label in cf.CANDIDATES:
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
        assert subject == f"Flight watch: {cf.DEPARTURE_ID} on {cf.OUTBOUND_DATE}"
        assert "480" in body

    def test_invalid_notify_channel_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLIGHT_NOTIFY_CHANNEL", "carrier-pigeon")
        called = []
        monkeypatch.setattr(cf, "fetch_all_itineraries", lambda **_: called.append(1))

        exit_code = cf.main()

        assert exit_code == 1
        assert called == []  # fails before ever querying SerpApi
