"""Tests for flightwatch_core.py -- the shared SerpApi/CallMeBot logic and
routes.toml loader every driver script builds on.

Uses requests_mock rather than any real network call -- CI must never
spend real SerpApi/CallMeBot quota just to prove the logic is correct.
"""

from __future__ import annotations

import pytest
import requests_mock

import flightwatch_core as core


@pytest.fixture(autouse=True)
def _serpapi_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fetch_cheapest_price call reads SERPAPI_KEY via _env() --
    give every test in this file a value so that's never the failure
    reason being tested, unless a test explicitly deletes it."""
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    monkeypatch.setenv("CALLMEBOT_PHONE", "+310000000")
    monkeypatch.setenv("CALLMEBOT_APIKEY", "test-callmebot-key")


class TestLoadRouteConfig:
    def test_reads_the_real_committed_check_price_section(self) -> None:
        config = core.load_route_config("check_price")
        assert config == {
            "departure_id": "AMS",
            "arrival_id": "DEL",
            "outbound_date": "2027-07-17",
            "currency": "EUR",
        }

    def test_reads_the_real_committed_check_gorakhpur_section(self) -> None:
        config = core.load_route_config("check_gorakhpur")
        assert config["departure_id"] == "AMS"
        assert config["candidates"] == [
            {"id": "GOP", "label": "Gorakhpur"},
            {"id": "KBK", "label": "Kushinagar"},
        ]

    def test_missing_section_raises_check_failed(self) -> None:
        with pytest.raises(core.CheckFailed, match=r"no \[nonexistent\] section"):
            core.load_route_config("nonexistent")

    def test_missing_file_raises_check_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        missing_path = tmp_path / "does-not-exist.toml"  # type: ignore[operator]
        monkeypatch.setattr(core, "_ROUTES_CONFIG_PATH", missing_path)
        with pytest.raises(core.CheckFailed, match="routes.toml not found"):
            core.load_route_config("check_price")


_SUCCESS_RESPONSE_TWO_OFFERS = {
    "search_metadata": {"status": "Success"},
    "best_flights": [
        {
            "price": 600,
            "flights": [{"airline": "KLM"}],
            "total_duration": 540,
        }
    ],
    "other_flights": [
        {
            "price": 450,
            "flights": [{"airline": "Air France"}, {"airline": "Air France"}],
            "total_duration": 720,
        }
    ],
}

_SUCCESS_RESPONSE_EMPTY = {
    "search_metadata": {"status": "Success"},
    "best_flights": [],
    "other_flights": [],
}


class TestFetchCheapestPrice:
    def test_picks_the_cheapest_across_best_and_other_flights(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            itinerary = core.fetch_cheapest_price(
                departure_id="AMS", arrival_id="DEL", outbound_date="2027-07-17", currency="EUR"
            )
        # 450 (other_flights) is cheaper than 600 (best_flights) -- proves
        # this doesn't just trust best_flights[0].
        assert itinerary["price"] == 450

    def test_empty_result_raises_no_flights_found_yet(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_EMPTY)
            with pytest.raises(core.NoFlightsFoundYet):
                core.fetch_cheapest_price(
                    departure_id="AMS",
                    arrival_id="DEL",
                    outbound_date="2027-07-17",
                    currency="EUR",
                )

    def test_non_200_raises_check_failed_not_no_flights_found_yet(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, status_code=500, text="internal error")
            with pytest.raises(core.CheckFailed) as exc_info:
                core.fetch_cheapest_price(
                    departure_id="AMS",
                    arrival_id="DEL",
                    outbound_date="2027-07-17",
                    currency="EUR",
                )
        # Specifically NOT the NoFlightsFoundYet subclass -- a real HTTP
        # error must never be silently swallowed the way an empty
        # itinerary list is.
        assert not isinstance(exc_info.value, core.NoFlightsFoundYet)

    def test_non_success_status_raises_check_failed(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json={"search_metadata": {"status": "Error"}})
            with pytest.raises(core.CheckFailed, match="did not succeed"):
                core.fetch_cheapest_price(
                    departure_id="AMS",
                    arrival_id="DEL",
                    outbound_date="2027-07-17",
                    currency="EUR",
                )

    def test_missing_serpapi_key_raises_check_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SERPAPI_KEY", raising=False)
        with pytest.raises(core.CheckFailed, match="SERPAPI_KEY is not set"):
            core.fetch_cheapest_price(
                departure_id="AMS", arrival_id="DEL", outbound_date="2027-07-17", currency="EUR"
            )


class TestSendWhatsapp:
    def test_success_does_not_raise(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.CALLMEBOT_URL, status_code=200, text="Message sent")
            core.send_whatsapp("hello")  # must not raise

    def test_non_200_raises_check_failed(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.CALLMEBOT_URL, status_code=400, text="bad phone number")
            with pytest.raises(core.CheckFailed, match="CallMeBot returned HTTP 400"):
                core.send_whatsapp("hello")
