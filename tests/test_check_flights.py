"""Tests for check_flights.py -- per-candidate outcome mapping,
cheapest-first comparison ordering, and the one genuinely non-obvious
rule in this repo: stay silent on WhatsApp only when EVERY candidate has
no data yet, never when even one has a real price or a real error (and
a real error must still exit 1, even if other candidates are fine).

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


class TestCheckOne:
    def test_price_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        itinerary = {
            "price": 407,
            "flights": [{"airline": "IndiGo"}, {"airline": "IndiGo"}],
            "total_duration": 990,
        }
        monkeypatch.setattr(cf, "fetch_cheapest_price", lambda **_: itinerary)

        result = cf._check_one("DEL", "Delhi")

        assert result.outcome is cf.Outcome.PRICE_FOUND
        assert result.price == 407
        assert "Delhi (DEL)" in result.line
        assert "EUR 407" in result.line

    def test_no_flights_found_yet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("No flights found for AMS->DXN on 2027-07-17")

        monkeypatch.setattr(cf, "fetch_cheapest_price", _raise)

        result = cf._check_one("DXN", "Noida (Jewar)")

        assert result.outcome is cf.Outcome.NO_DATA_YET
        assert result.price is None
        assert "Noida (Jewar) (DXN): no data yet" == result.line

    def test_genuine_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.CheckFailed("SerpApi returned HTTP 500: boom")

        monkeypatch.setattr(cf, "fetch_cheapest_price", _raise)

        result = cf._check_one("VNS", "Varanasi")

        assert result.outcome is cf.Outcome.ERROR
        assert result.price is None
        assert "error -- " in result.line
        assert "boom" in result.line


class TestBuildComparison:
    def test_cheapest_sorts_first(self) -> None:
        results = [
            cf.CandidateResult(
                line="Delhi (DEL): EUR 600 ...", price=600, outcome=cf.Outcome.PRICE_FOUND
            ),
            cf.CandidateResult(
                line="Varanasi (VNS): EUR 450 ...", price=450, outcome=cf.Outcome.PRICE_FOUND
            ),
        ]

        message = cf.build_comparison(results)

        # VNS's cheaper 450 line must appear before DEL's 600 line.
        assert message.index("EUR 450") < message.index("EUR 600")

    def test_no_price_results_sort_last(self) -> None:
        results = [
            cf.CandidateResult(
                line="Noida (Jewar) (DXN): no data yet", price=None, outcome=cf.Outcome.NO_DATA_YET
            ),
            cf.CandidateResult(
                line="Delhi (DEL): EUR 407 ...", price=407, outcome=cf.Outcome.PRICE_FOUND
            ),
        ]

        message = cf.build_comparison(results)

        assert message.index("EUR 407") < message.index("no data yet")

    def test_header_names_origin_and_date(self) -> None:
        message = cf.build_comparison([])
        expected_header = f"Flight watch from {cf.DEPARTURE_ID} on {cf.OUTBOUND_DATE}"
        assert message.startswith(expected_header)


class TestMainSilenceRule:
    def test_all_candidates_no_data_yet_stays_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_: object) -> None:
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_cheapest_price", _raise)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert sent == []

    def test_one_candidate_priced_still_notifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = {"n": 0}
        priced_id = cf.CANDIDATES[0][0]

        def _fake_fetch(*, arrival_id: str, **_: object) -> dict[str, object]:
            call_count["n"] += 1
            if arrival_id == priced_id:
                return {
                    "price": 407,
                    "flights": [{"airline": "IndiGo"}],
                    "total_duration": 990,
                }
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_cheapest_price", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert len(sent) == 1
        assert "EUR 407" in sent[0]
        assert "no data yet" in sent[0]  # every other candidate's outcome still visible
        assert call_count["n"] == len(cf.CANDIDATES)  # every candidate was actually queried

    def test_one_candidate_erroring_still_notifies_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact case the module docstring warns about: an ERROR
        outcome must never be masked by another candidate's routine
        NO_DATA_YET outcome, and must make the whole run exit 1."""
        erroring_id = cf.CANDIDATES[0][0]

        def _fake_fetch(*, arrival_id: str, **_: object) -> dict[str, object]:
            if arrival_id == erroring_id:
                raise core.CheckFailed("SerpApi returned HTTP 500: boom")
            raise core.NoFlightsFoundYet("no data yet")

        monkeypatch.setattr(cf, "fetch_cheapest_price", _fake_fetch)
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

        def _fake_fetch(*, arrival_id: str, **_: object) -> dict[str, object]:
            return {
                "price": float(len(arrival_id) * 100),  # any distinct, deterministic value
                "flights": [{"airline": "Test Air"}],
                "total_duration": 600,
            }

        monkeypatch.setattr(cf, "fetch_cheapest_price", _fake_fetch)
        sent: list[str] = []
        monkeypatch.setattr(cf, "send_whatsapp", sent.append)

        exit_code = cf.main()

        assert exit_code == 0
        assert len(sent) == 1
        for arrival_id, label in cf.CANDIDATES:
            assert f"{label} ({arrival_id})" in sent[0]
