"""Renders the Gmail channel's HTML body -- a real, visually designed
email instead of a monospace <pre> dump, built for email clients, not
browsers: nested <table role="presentation"> layout only, every style
inlined except one mobile media query, no JS/external CSS/web fonts/
images. See flight-price-watch's own planning notes for the full
constraint list this file has to satisfy.

Deliberately knows nothing about CategoryRow/CandidateOutcome/SerpApi
-- check_flights.py maps its own data into the plain dicts this module
accepts, so this file stays testable and reusable on its own. Every
string value is html.escape()'d here, at the point of insertion, since
all of it ultimately traces back to external (SerpApi) data -- this is
the one place that boundary is enforced.
"""

from __future__ import annotations

from html import escape
from typing import Any

_PAGE_BG = "#efece7"
_CARD_BG = "#ffffff"
_INK = "#1f2320"
_MUTED = "#7a7f77"
_HAIRLINE = "#d8d3cb"
_ACCENT = "#12564b"
_BADGE_BG = "#e3efe9"

_UI_FONT = "Helvetica, Arial, sans-serif"
_SERIF_FONT = "Georgia, 'Times New Roman', serif"


def _spacer(height: int) -> str:
    return (
        f'<tr><td height="{height}" '
        f'style="height:{height}px;mso-line-height-rule:exactly;'
        f'line-height:{height}px;font-size:0;">&nbsp;</td></tr>'
    )


def _meta_line(option: dict[str, Any]) -> str:
    """Already safe to insert as-is -- `stop['via']` is escaped here,
    not by the caller, since the rest of the line is trusted literal
    text; wrapping this return value in escape() again would corrupt
    the entity into a double-escaped mess."""
    if option["stops"]:
        stop = option["stops"][0]
        meta = f"{option['total']} · via {escape(stop['via'])}, {stop['transit']} transit"
    else:
        meta = f"{option['total']} · nonstop"
    departure_hour = int(option["dep"].split(":")[0])
    if option["arr_next_day"] and departure_hour >= 18:
        meta += " · overnight"
    return meta


def _option_card(option: dict[str, Any], currency: str, is_cheapest: bool) -> str:
    price_color = _ACCENT if is_cheapest else _INK
    badge = (
        f'<span style="display:inline-block;background:{_BADGE_BG};color:{_ACCENT};'
        f'font-family:{_UI_FONT};font-size:11px;font-weight:bold;letter-spacing:0.04em;'
        f'padding:2px 8px;border-radius:10px;margin-left:8px;'
        f'mso-line-height-rule:exactly;line-height:16px;">CHEAPEST</span>'
        if is_cheapest
        else ""
    )
    arrival_suffix = "+1" if option["arr_next_day"] else ""
    return f"""
<tr><td style="padding:16px 18px;background:{_CARD_BG};border:1px solid {_HAIRLINE};
border-radius:10px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td style="font-family:{_UI_FONT};font-size:14px;color:{_INK};
mso-line-height-rule:exactly;line-height:20px;" width="70%">
{escape(option['airline'])}{badge}
</td>
<td align="right" class="price-text" style="font-family:{_SERIF_FONT};font-size:18px;
color:{price_color};mso-line-height-rule:exactly;line-height:20px;" width="30%">
{escape(currency)} {option['price']}
</td>
</tr>
{_spacer(6)}
<tr>
<td colspan="2" style="font-family:{_UI_FONT};font-size:14px;color:{_INK};
mso-line-height-rule:exactly;line-height:18px;">
{escape(option['dep'])} {escape(option['from'])} &mdash;
{escape(option['arr'])}{arrival_suffix} {escape(option['to_label'])}
</td>
</tr>
{_spacer(4)}
<tr>
<td colspan="2" style="font-family:{_UI_FONT};font-size:12px;color:{_MUTED};
mso-line-height-rule:exactly;line-height:16px;">
{_meta_line(option)}
</td>
</tr>
</table>
</td></tr>"""


def _section(title: str, options: list[dict[str, Any]], currency: str, cheapest_price: int) -> str:
    if not options:
        return ""
    ordered = sorted(options, key=lambda o: o["price_value"])
    cards = [_option_card(o, currency, o["price_value"] == cheapest_price) for o in ordered]
    rows = ""
    for i, card in enumerate(cards):
        rows += card
        if i != len(cards) - 1:
            rows += _spacer(10)
    option_word = "option" if len(options) == 1 else "options"
    return f"""
<tr><td style="font-family:{_UI_FONT};font-size:12px;font-weight:bold;letter-spacing:0.06em;
color:{_MUTED};mso-line-height-rule:exactly;line-height:16px;">
{escape(title)}&nbsp;&mdash;&nbsp;<span style="font-weight:normal;">{len(options)}
{option_word}</span>
</td></tr>
{_spacer(8)}
<tr><td>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
{rows}
</table>
</td></tr>
{_spacer(20)}"""


def _recommendation_panel(recommendation: str, book_url: str) -> str:
    if not recommendation:
        return ""
    return f"""
<tr><td style="background:{_ACCENT};border-radius:10px;padding:18px 20px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="font-family:{_UI_FONT};font-size:14px;color:#ffffff;
mso-line-height-rule:exactly;line-height:20px;">
{escape(recommendation)}
</td></tr>
{_spacer(14)}
<tr><td>
<table role="presentation" cellpadding="0" cellspacing="0" border="0">
<tr><td bgcolor="{_CARD_BG}" style="border-radius:6px;">
<a href="{escape(book_url)}" target="_blank" style="display:inline-block;
font-family:{_UI_FONT};font-size:13px;font-weight:bold;color:{_ACCENT};
padding:10px 18px;mso-line-height-rule:exactly;line-height:16px;
text-decoration:none;">Check fares</a>
</td></tr>
</table>
</td></tr>
</table>
</td></tr>
{_spacer(20)}"""


def _errors_block(errors: list[str]) -> str:
    if not errors:
        return ""
    lines = "<br>".join(escape(line) for line in errors)
    return f"""
<tr><td style="background:#fdf1ea;border:1px solid #e8c9ab;border-radius:8px;
padding:14px 16px;font-family:{_UI_FONT};font-size:12px;color:{_INK};
mso-line-height-rule:exactly;line-height:18px;">
<strong>Errors</strong><br>{lines}
</td></tr>
{_spacer(20)}"""


def render(
    *,
    options: list[dict[str, Any]],
    recommendation: str,
    currency: str,
    departure_label: str,
    checked_on: str,
    errors: list[str] | None = None,
    book_url: str = "https://www.google.com/travel/flights",
) -> str:
    """Full HTML email document (DOCTYPE through /html) -- `options` is
    a flat list of plain dicts (see check_flights.py's _email_options
    for the exact shape); this function only groups/sorts/renders, it
    never talks to CategoryRow or SerpApi directly. `errors` get a
    small block of their own -- not in the original visual spec, but
    this project's "a silent failure on a daily job is worse than a
    noisy one" rule means they can't just be dropped when the old
    plain-text report already surfaced them. No literal "unsubscribe"
    link (the spec's footer mentions one) -- there's no subscription
    to unsubscribe from, this is a single-recipient personal
    automation, not a marketing send.
    """
    errors = errors or []
    title = f"{departure_label} flight watch"
    cheapest_price = min((o["price_value"] for o in options), default=None)

    direct = [o for o in options if not o["stops"]]
    one_stop = [o for o in options if o["stops"]]
    sections = _section("DIRECT", direct, currency, cheapest_price) + _section(
        "ONE STOP", one_stop, currency, cheapest_price
    )

    preheader_source = recommendation or (errors[0] if errors else "No new fares to report today.")
    preheader = escape(preheader_source)[:85]

    body = f"""<span style="display:none;font-size:1px;color:{_PAGE_BG};
mso-line-height-rule:exactly;line-height:1px;
max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{preheader}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
bgcolor="{_PAGE_BG}">
<tr><td align="center" class="outer-pad" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
class="email-wrapper">
<tr><td style="font-family:{_SERIF_FONT};font-size:22px;color:{_INK};
mso-line-height-rule:exactly;line-height:28px;">
{escape(title)}
</td></tr>
{_spacer(20)}
{_recommendation_panel(recommendation, book_url)}
{sections}
{_errors_block(errors)}
<tr><td style="font-family:{_UI_FONT};font-size:11px;color:{_MUTED};
mso-line-height-rule:exactly;line-height:16px;border-top:1px solid {_HAIRLINE};
padding-top:14px;">
All times local. Prices per person, economy, checked as of {escape(checked_on)}
and subject to change.
</td></tr>
</table>
</td></tr>
</table>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>{escape(title)}</title>
<style>
@media only screen and (max-width:620px) {{
  .email-wrapper {{ width: 100% !important; }}
  .outer-pad {{ padding-left: 12px !important; padding-right: 12px !important; }}
  .price-text {{ font-size: 15px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background:{_PAGE_BG};">
{body}
</body>
</html>"""
