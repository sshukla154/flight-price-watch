"""Tests for check_gorakhpur.py -- per-candidate outcome mapping,
cheapest-first comparison ordering, and the one genuinely non-obvious
rule in this repo: stay silent on WhatsApp only when EVERY candidate has
no data yet, never when even one has a real price or a real error.
"""

from __future__ import annotations

import pytest

import check_gorakhpur as gp
import flightwatch_core as core


class TestCheckOne:
    def test_price_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        itinerary = {
            "price": 407,
            "flights": [{"airline": "IndiGo"}, {"airline": "IndiGo"}],
            "total_duration": 990,
        }
        monkeypatch.setattr(gp, "fetch_cheapest_price", lambda **_: itinerary)

        result = gp._check_one("GOP", "Gorakhpur")

        assert result.outcome is gp.Outcome.PRICE_FOUND
        assert result.price == 407
        assert "Gorakhpur (GOP)" in result.line
        assert "EUR 407" in result.line

    def test_no_flights_found_yet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("No flights found for AMS->KBK on 2027-07-17")

        monkeypatch.setattr(gp, "fetch_cheapest_price", _raise)

        result = gp._check_one("KBK", "Kushinagar")

        assert result.outcome is gp.Outcome.NO_DATA_YET
        assert result.price is None
        assert "Kushinagar (KBK): no data yet" == result.line

    def test_genuine_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.CheckFailed("SerpApi returned HTTP 500: boom")

        monkeypatch.setattr(gp, "fetch_cheapest_price", _raise)

        result = gp._check_one("GOP", "Gorakhpur")

        assert result.outcome is gp.Outcome.ERROR
        assert result.price is None
        assert "error -- " in result.line
        assert "boom" in result.line


class TestBuildComparison:
    def test_cheapest_sorts_first(self) -> None:
        results = [
            gp.CandidateResult(
                line="Gorakhpur (GOP): EUR 600 ...", price=600, outcome=gp.Outcome.PRICE_FOUND
            ),
            gp.CandidateResult(
                line="Kushinagar (KBK): EUR 450 ...", price=450, outcome=gp.Outcome.PRICE_FOUND
            ),
        ]

        message = gp.build_comparison(results)

        # KBK's cheaper 450 line must appear before GOP's 600 line.
        assert message.index("EUR 450") < message.index("EUR 600")

    def test_no_price_results_sort_last(self) -> None:
        results = [
            gp.CandidateResult(
                line="Kushinagar (KBK): no data yet", price=None, outcome=gp.Outcome.NO_DATA_YET
            ),
            gp.CandidateResult(
                line="Gorakhpur (GOP): EUR 407 ...", price=407, outcome=gp.Outcome.PRICE_FOUND
            ),
        ]

        message = gp.build_comparison(results)

        assert message.index("EUR 407") < message.index("no data yet")

    def test_header_names_origin_and_date(self) -> None:
        message = gp.build_comparison([])
        expected_header = f"Gorakhpur-area watch from {gp.DEPARTURE_ID} on {gp.OUTBOUND_DATE}"
        assert message.startswith(expected_header)


class TestMainSilenceRule:
    def test_all_candidates_no_data_yet_stays_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(gp, "fetch_cheapest_price", _raise)
        sent: list[str] = []
        monkeypatch.setattr(gp, "send_whatsapp", sent.append)

        exit_code = gp.main()

        assert exit_code == 0
        assert sent == []

    def test_one_candidate_priced_still_notifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = {"n": 0}

        def _fake_fetch(*, arrival_id: str, **_: object) -> dict[str, object]:
            call_count["n"] += 1
            if arrival_id == "GOP":
                return {
                    "price": 407,
                    "flights": [{"airline": "IndiGo"}],
                    "total_duration": 990,
                }
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(gp, "fetch_cheapest_price", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(gp, "send_whatsapp", sent.append)

        exit_code = gp.main()

        assert exit_code == 0
        assert len(sent) == 1
        assert "EUR 407" in sent[0]
        assert "no data yet" in sent[0]  # KBK's outcome still visible in the combined message
        assert call_count["n"] == len(gp.CANDIDATES)  # every candidate was actually queried

    def test_one_candidate_erroring_still_notifies_even_if_other_has_no_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact case the module docstring warns about: an ERROR
        outcome must never be masked by another candidate's routine
        NO_DATA_YET outcome."""

        def _fake_fetch(*, arrival_id: str, **_: object) -> dict[str, object]:
            if arrival_id == "GOP":
                raise core.CheckFailed("SerpApi returned HTTP 500: boom")
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(gp, "fetch_cheapest_price", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(gp, "send_whatsapp", sent.append)

        exit_code = gp.main()

        # A genuine ERROR outcome must make the whole run exit 1 -- even
        # though the OTHER candidate merely had no data yet -- matching
        # check_price.py's own exit-code discipline (1 only for a real
        # failure, never for the routine no-data-yet case).
        assert exit_code == 1
        assert len(sent) == 1
        assert "boom" in sent[0]
