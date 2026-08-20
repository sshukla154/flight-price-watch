"""Tests for check_price.py's own logic (format_message, main's dispatch).

Never calls the real fetch_cheapest_price/send_whatsapp -- those are
monkeypatched here, since flightwatch_core.py's own test file already
covers the real HTTP behavior. This file is only about check_price.py's
OWN formatting and control flow.
"""

from __future__ import annotations

import pytest

import check_price
import flightwatch_core as core

_ITINERARY_ONE_STOP = {
    "price": 520,
    "flights": [{"airline": "British Airways"}, {"airline": "British Airways"}],
    "total_duration": 820,
}

_ITINERARY_DIRECT = {
    "price": 300,
    "flights": [{"airline": "KLM"}],
    "total_duration": 540,
}


class TestFormatMessage:
    def test_one_stop_itinerary(self) -> None:
        message = check_price.format_message(_ITINERARY_ONE_STOP)
        assert message == (
            "Flight watch AMS->DEL on 2027-07-17\n"
            "EUR 520 -- British Airways -- 1 stop(s) -- 13h40m total"
        )

    def test_direct_itinerary(self) -> None:
        message = check_price.format_message(_ITINERARY_DIRECT)
        assert message == (
            "Flight watch AMS->DEL on 2027-07-17\nEUR 300 -- KLM -- direct -- 9h00m total"
        )

    def test_deduplicates_repeated_airline_across_segments(self) -> None:
        message = check_price.format_message(_ITINERARY_ONE_STOP)
        # "British Airways" appears once, not twice, even though both
        # segments are the same airline.
        assert message.count("British Airways") == 1


class TestMainDispatch:
    def test_price_found_sends_whatsapp_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(check_price, "fetch_cheapest_price", lambda **_: _ITINERARY_DIRECT)
        sent: list[str] = []
        monkeypatch.setattr(check_price, "send_whatsapp", sent.append)

        exit_code = check_price.main()

        assert exit_code == 0
        assert len(sent) == 1
        assert "EUR 300" in sent[0]

    def test_no_flights_found_yet_stays_silent_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise_no_flights(**_: object) -> None:
            raise core.NoFlightsFoundYet("No flights found for AMS->DEL on 2027-07-17")

        monkeypatch.setattr(check_price, "fetch_cheapest_price", _raise_no_flights)
        sent: list[str] = []
        monkeypatch.setattr(check_price, "send_whatsapp", sent.append)

        exit_code = check_price.main()

        assert exit_code == 0
        assert sent == []  # the whole point of NoFlightsFoundYet's special handling

    def test_genuine_failure_sends_whatsapp_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise_check_failed(**_: object) -> None:
            raise core.CheckFailed("SerpApi returned HTTP 500: boom")

        monkeypatch.setattr(check_price, "fetch_cheapest_price", _raise_check_failed)
        sent: list[str] = []
        monkeypatch.setattr(check_price, "send_whatsapp", sent.append)

        exit_code = check_price.main()

        assert exit_code == 1
        assert len(sent) == 1
        assert "boom" in sent[0]
        assert "check FAILED" in sent[0]
