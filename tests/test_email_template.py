"""Tests for email_template.py -- deliberately standalone, no
check_flights import: this module only knows plain dicts, so tests
build those directly rather than going through CategoryRow."""

from __future__ import annotations

import email_template as et

_DIRECT_OPTION = {
    "airline": "KLM",
    "from": "AMS",
    "to": "DEL",
    "to_label": "Delhi",
    "price": "480",
    "price_value": 480,
    "dep": "09:15",
    "arr": "21:30",
    "arr_next_day": False,
    "total": "12h15m",
    "total_minutes": 735,
    "stops": [],
}

_ONE_STOP_OPTION = {
    "airline": "Oman Air",
    "from": "AMS",
    "to": "VNS",
    "to_label": "Varanasi",
    "price": "356",
    "price_value": 356,
    "dep": "10:15",
    "arr": "06:30",
    "arr_next_day": True,
    "total": "15h15m",
    "total_minutes": 915,
    "stops": [{"via": "Muscat International Airport", "transit": "1h30m"}],
}

_FORBIDDEN_SUBSTRINGS = ["display:flex", "display: flex", "<img", "<script", "position:"]


class TestRenderStructure:
    def test_direct_only_shows_direct_section_not_one_stop(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "DIRECT" in html
        assert "ONE STOP" not in html

    def test_one_stop_only_shows_one_stop_section_not_direct(self) -> None:
        html = et.render(
            options=[_ONE_STOP_OPTION],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "ONE STOP" in html
        assert "DIRECT" not in html

    def test_mixed_shows_both_sections(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION, _ONE_STOP_OPTION],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "DIRECT" in html
        assert "ONE STOP" in html

    def test_empty_recommendation_omits_the_panel(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "Check fares" not in html  # the CTA only exists inside the panel

    def test_non_empty_recommendation_renders_the_panel_and_cta(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION],
            recommendation="KLM to Delhi at EUR 480 -- cheapest overall.",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "KLM to Delhi at EUR 480" in html
        assert "Check fares" in html

    def test_cheapest_badge_on_the_exact_right_price_only(self) -> None:
        pricier_direct = {**_DIRECT_OPTION, "price": "999", "price_value": 999}
        html = et.render(
            options=[pricier_direct, _ONE_STOP_OPTION],  # 356 is the real cheapest
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert html.count("CHEAPEST") == 1

    def test_errors_render_when_present_and_are_absent_when_not(self) -> None:
        with_errors = et.render(
            options=[],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
            errors=["Lucknow (LKO): error -- boom"],
        )
        assert "error -- boom" in with_errors

        without_errors = et.render(
            options=[_DIRECT_OPTION],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "Errors" not in without_errors


class TestEscaping:
    def test_airline_markup_is_neutralized_not_rendered_live(self) -> None:
        hostile = {**_DIRECT_OPTION, "airline": "<script>alert(1)</script>"}
        html = et.render(
            options=[hostile],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_stopover_name_markup_is_neutralized(self) -> None:
        hostile = {
            **_ONE_STOP_OPTION,
            "stops": [{"via": "<b>Evil</b> Airport", "transit": "1h30m"}],
        }
        html = et.render(
            options=[hostile],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert "<b>Evil</b>" not in html
        assert "&lt;b&gt;Evil&lt;/b&gt;" in html


class TestEmailSafeConstraints:
    def test_no_email_unsafe_constructs(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION, _ONE_STOP_OPTION],
            recommendation="KLM to Delhi at EUR 480 -- cheapest overall.",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
            errors=["some error"],
        )
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in html, f"found forbidden construct: {forbidden!r}"

    def test_preheader_is_first_visible_content_in_body(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION],
            recommendation="KLM to Delhi at EUR 480 -- cheapest overall.",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        body_start = html.index("<body")
        preheader_pos = html.index("mso-hide:all")
        wrapper_table_pos = html.index('class="email-wrapper"')
        assert body_start < preheader_pos < wrapper_table_pos

    def test_has_color_scheme_meta_and_lang_attribute(self) -> None:
        html = et.render(
            options=[_DIRECT_OPTION],
            recommendation="",
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
        )
        assert '<html lang="en">' in html
        assert '<meta name="color-scheme" content="light dark">' in html

    def test_stays_under_100kb_for_a_realistic_dataset(self) -> None:
        many_options = [
            {**_DIRECT_OPTION, "to_label": f"City{i}", "price_value": 400 + i}
            for i in range(5)
        ] + [
            {**_ONE_STOP_OPTION, "to_label": f"City{i}", "price_value": 300 + i}
            for i in range(5)
        ]
        html = et.render(
            options=many_options,
            recommendation=(
                "KLM to Delhi at EUR 480 -- cheapest overall and 3h quicker than any one-stop."
            ),
            currency="EUR",
            departure_label="Amsterdam",
            checked_on="2 Sep 2026",
            errors=["one error line for good measure"],
        )
        assert len(html.encode("utf-8")) < 100_000
