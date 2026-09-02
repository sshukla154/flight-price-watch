"""Tests for flightwatch_core.py -- the shared SerpApi/CallMeBot logic and
routes.toml loader every driver script builds on.

Uses requests_mock rather than any real network call -- CI must never
spend real SerpApi/CallMeBot quota just to prove the logic is correct.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    monkeypatch.setenv("GMAIL_ADDRESS", "test@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-app-password")


class TestLoadRouteConfig:
    def test_reads_the_real_committed_check_flights_section(self) -> None:
        config = core.load_route_config("check_flights")
        assert config["departure_id"] == "AMS"
        assert config["outbound_date"] == "2027-07-22"
        assert config["return_after_days"] == 38
        assert config["currency"] == "EUR"
        assert config["adults"] == 2
        assert config["children"] == 1
        assert config["candidates"] == [
            {"id": "DEL", "label": "Delhi", "one_stop": True},
            {"id": "BOM", "label": "Mumbai", "one_stop": True},
            {"id": "LKO", "label": "Lucknow", "one_stop": False},
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
            core.load_route_config("check_flights")


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


class TestFetchAllItineraries:
    def test_returns_every_itinerary_from_both_lists_unfiltered(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            itineraries = core.fetch_all_itineraries(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
            )
        # Both the best_flights entry (600) and the other_flights entry
        # (450) come back, in that order -- nothing picked or dropped
        # (both have a real price; see test_drops_itineraries_with_no_
        # resolved_price below for the one case that IS filtered).
        assert [itinerary["price"] for itinerary in itineraries] == [600, 450]

    def test_empty_result_raises_no_flights_found_yet(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_EMPTY)
            with pytest.raises(core.NoFlightsFoundYet):
                core.fetch_all_itineraries(
                    departure_id="AMS",
                    arrival_id="DEL",
                    outbound_date="2027-07-17",
                    return_date="2027-08-21",
                    currency="EUR",
                )

    def test_non_200_raises_check_failed(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, status_code=500, text="internal error")
            with pytest.raises(core.CheckFailed) as exc_info:
                core.fetch_all_itineraries(
                    departure_id="AMS",
                    arrival_id="DEL",
                    outbound_date="2027-07-17",
                    return_date="2027-08-21",
                    currency="EUR",
                )
        assert not isinstance(exc_info.value, core.NoFlightsFoundYet)

    def test_drops_itineraries_with_no_resolved_price(self) -> None:
        """Real bug, live-verified 2026-08-25: Google Flights occasionally
        shows an option with no resolved fare -- a caller trusting every
        itinerary has "price" crashed with a bare KeyError on real
        production data. Filtering happens HERE so no caller needs its
        own defensive check."""
        response = {
            "search_metadata": {"status": "Success"},
            "best_flights": [{"flights": [{"airline": "KLM"}], "total_duration": 540}],
            "other_flights": [
                {"price": 450, "flights": [{"airline": "Air France"}], "total_duration": 720}
            ],
        }
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=response)
            itineraries = core.fetch_all_itineraries(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
            )
        assert [itinerary["price"] for itinerary in itineraries] == [450]

    def test_no_flights_found_yet_when_every_itinerary_lacks_a_price(self) -> None:
        response = {
            "search_metadata": {"status": "Success"},
            "best_flights": [{"flights": [{"airline": "KLM"}], "total_duration": 540}],
            "other_flights": [],
        }
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=response)
            with pytest.raises(core.NoFlightsFoundYet):
                core.fetch_all_itineraries(
                    departure_id="AMS",
                    arrival_id="DEL",
                    outbound_date="2027-07-17",
                    return_date="2027-08-21",
                    currency="EUR",
                )

    def test_adults_and_children_pass_through_to_the_request(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            core.fetch_all_itineraries(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
                adults=2,
                children=1,
            )
        assert mock.last_request.qs["adults"] == ["2"]
        assert mock.last_request.qs["children"] == ["1"]

    def test_defaults_to_one_adult_zero_children_when_omitted(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            core.fetch_all_itineraries(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
            )
        assert mock.last_request.qs["adults"] == ["1"]
        assert mock.last_request.qs["children"] == ["0"]

    def test_sends_round_trip_type_and_return_date(self) -> None:
        """type=1 (round trip), not type=2 (one-way) -- and return_date
        actually reaches the request, not just accepted as a Python
        param. Real round-trip flow verified live 2026-09-02: the
        first call's price is already the full round-trip total, no
        departure_token follow-up call needed."""
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            core.fetch_all_itineraries(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
            )
        assert mock.last_request.qs["type"] == ["1"]
        assert mock.last_request.qs["return_date"] == ["2027-08-21"]


class TestFetchCheapestPrice:
    def test_picks_the_cheapest_across_best_and_other_flights(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            itinerary = core.fetch_cheapest_price(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
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
                    return_date="2027-08-21",
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
                    return_date="2027-08-21",
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
                    return_date="2027-08-21",
                    currency="EUR",
                )

    def test_missing_serpapi_key_raises_check_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SERPAPI_KEY", raising=False)
        with pytest.raises(core.CheckFailed, match="SERPAPI_KEY is not set"):
            core.fetch_cheapest_price(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
            )

    def test_forwards_adults_and_children_to_fetch_all_itineraries(self) -> None:
        with requests_mock.Mocker() as mock:
            mock.get(core.SERPAPI_URL, json=_SUCCESS_RESPONSE_TWO_OFFERS)
            core.fetch_cheapest_price(
                departure_id="AMS",
                arrival_id="DEL",
                outbound_date="2027-07-17",
                return_date="2027-08-21",
                currency="EUR",
                adults=2,
                children=1,
            )
        assert mock.last_request.qs["adults"] == ["2"]
        assert mock.last_request.qs["children"] == ["1"]


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


class TestSendEmail:
    def test_success_logs_in_and_sends_from_and_to_the_same_address(self) -> None:
        smtp_instance = MagicMock()
        smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
        smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("flightwatch_core.smtplib.SMTP_SSL", return_value=smtp_instance) as ssl_cls:
            core.send_email("subject", "<pre>body</pre>")

        ssl_cls.assert_called_once_with(core.GMAIL_SMTP_HOST, core.GMAIL_SMTP_PORT)
        smtp_instance.login.assert_called_once_with("test@gmail.com", "test-app-password")
        args, _ = smtp_instance.sendmail.call_args
        from_addr, to_addrs, raw_message = args
        assert from_addr == "test@gmail.com"
        assert to_addrs == ["test@gmail.com"]
        assert "subject" in raw_message
        assert "<pre>body</pre>" in raw_message

    def test_smtp_exception_raises_check_failed(self) -> None:
        with patch("flightwatch_core.smtplib.SMTP_SSL", side_effect=OSError("connection refused")):
            with pytest.raises(core.CheckFailed, match="Gmail SMTP send failed"):
                core.send_email("subject", "<pre>body</pre>")
