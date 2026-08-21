"""Tests for check_flights.py -- stop-category bucketing, per-category
line formatting (including the departure/arrival time-of-day "+1"
day-rollover suffix), and the generalized silence rule: stay silent on
WhatsApp only when there is truly nothing to report for ANY candidate in
EITHER category, never when even one candidate has a real finding or a
real error (and a real error must still exit 1).

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
        },
        {
            "airline": "Oman Air",
            "departure_airport": {"time": "2027-07-17 16:30"},
            "arrival_airport": {"time": "2027-07-18 06:30"},
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


class TestFormatCategoryLine:
    def test_direct_line_has_no_transit_segment(self) -> None:
        line = cf._format_category_line("Delhi", "DEL", _DIRECT_ITINERARY)

        assert line == (
            "Delhi (DEL): EUR 480 -- KLM -- dep 09:15 -> arr 21:30 -- 12h15m total"
        )
        assert "transit" not in line

    def test_one_stop_line_includes_transit_and_day_rollover(self) -> None:
        line = cf._format_category_line("Varanasi", "VNS", _ONE_STOP_ITINERARY)

        assert line == (
            "Varanasi (VNS): EUR 356 -- Oman Air -- dep 10:15 -> arr 06:30+1 -- "
            "transit 1h30m -- 15h15m total"
        )

    def test_deduplicates_repeated_airline_across_segments(self) -> None:
        line = cf._format_category_line("Varanasi", "VNS", _ONE_STOP_ITINERARY)
        assert line.count("Oman Air") == 1


class TestCheckOne:
    def test_price_found_in_both_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cf, "fetch_all_itineraries", lambda **_: [_DIRECT_ITINERARY, _ONE_STOP_ITINERARY]
        )

        outcome = cf._check_one("DEL", "Delhi")

        assert outcome.direct_line is not None and "EUR 480" in outcome.direct_line
        assert outcome.one_stop_line is not None and "EUR 356" in outcome.one_stop_line
        assert outcome.error_line is None

    def test_no_flights_found_yet_gives_all_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("No flights found for AMS->DXN on 2027-07-17")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)

        outcome = cf._check_one("DXN", "Noida (Jewar)")

        assert outcome == cf.CandidateOutcome(direct_line=None, one_stop_line=None, error_line=None)

    def test_only_two_stop_options_gives_all_none_same_as_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "fetch_all_itineraries", lambda **_: [_TWO_STOP_ITINERARY])

        outcome = cf._check_one("DEL", "Delhi")

        assert outcome.direct_line is None
        assert outcome.one_stop_line is None
        assert outcome.error_line is None

    def test_genuine_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.CheckFailed("SerpApi returned HTTP 500: boom")

        monkeypatch.setattr(cf, "fetch_all_itineraries", _raise)

        outcome = cf._check_one("VNS", "Varanasi")

        assert outcome.direct_line is None
        assert outcome.one_stop_line is None
        assert outcome.error_line is not None and "boom" in outcome.error_line


class TestBuildMessage:
    def test_sections_omitted_when_empty(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_line="Delhi (DEL): ...", one_stop_line=None, error_line=None
            )
        ]

        message = cf._build_message(outcomes)

        assert "DIRECT" in message
        assert "1 STOP" not in message
        assert "ERRORS" not in message

    def test_all_three_sections_appear_when_populated(self) -> None:
        outcomes = [
            cf.CandidateOutcome(
                direct_line="Delhi (DEL): direct-line", one_stop_line=None, error_line=None
            ),
            cf.CandidateOutcome(
                direct_line=None, one_stop_line="Varanasi (VNS): one-stop-line", error_line=None
            ),
            cf.CandidateOutcome(
                direct_line=None, one_stop_line=None, error_line="Lucknow (LKO): error -- boom"
            ),
        ]

        message = cf._build_message(outcomes)

        assert "DIRECT" in message and "direct-line" in message
        assert "1 STOP" in message and "one-stop-line" in message
        assert "ERRORS" in message and "error -- boom" in message


class TestMainSilenceRule:
    def test_all_candidates_no_data_yet_stays_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert "EUR 480" in sent[0]
        assert "DIRECT" in sent[0]
        assert "1 STOP" not in sent[0]  # nobody else had a one-stop result either

    def test_one_candidate_erroring_still_notifies_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact case the module docstring warns about: an error
        outcome must never be masked by another candidate's routine
        no-data outcome, and must make the whole run exit 1."""
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

        def _fake_fetch(*, arrival_id: str, **_: object) -> list[dict[str, object]]:
            return [{**_DIRECT_ITINERARY, "price": float(len(arrival_id) * 100)}]

        monkeypatch.setattr(cf, "fetch_all_itineraries", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert len(sent) == 1
        for arrival_id, label in cf.CANDIDATES:
            assert f"{label} ({arrival_id})" in sent[0]
