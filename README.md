# flight-price-watch

One daily check, pushing to WhatsApp: compares the cheapest AMS one-way
fare to several candidate Indian destination airports, on a fixed date.

## How it works

1. GitHub Actions runs `check_flights.py` on a daily cron (`0 6 * * *`
   UTC, `.github/workflows/daily-check.yml`), or on demand via the
   Actions tab's "Run workflow" button.
2. For each candidate airport in `routes.toml`'s `[check_flights]`
   section, it queries [SerpApi's Google Flights API](https://serpapi.com/google-flights-api)
   for the cheapest one-way fare from AMS on the configured date.
3. It builds **one combined WhatsApp message** comparing every
   candidate, cheapest first, and sends it via
   [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/)
   — but only if at least one candidate has a real price or a real
   error. If every single candidate has no fare data yet, **no WhatsApp
   message is sent** — only logged in the Action's own run output.
   That's the expected daily outcome for a while (see "Why today's run
   might fail" below), not a failure, and pinging you every day with
   "still nothing" would be noise.
4. A genuine error on even one candidate (a real SerpApi error, a
   malformed response, a CallMeBot send failure) still sends the
   combined message and makes the whole run exit 1 — a silent failure
   on a daily job is worse than a noisy one, just not for the routine
   "not published yet" case above.

**Budget**: 4 candidates × 1 search/day ≈ 120 searches/month, well
inside SerpApi's free-tier 250/month cap (~130/month spare).

## Configuration — `routes.toml`

The **one file to edit** to change the route, date, or candidate
airports — no Python edit needed:

```toml
[check_flights]
departure_id = "AMS"
outbound_date = "2027-07-17"
currency = "EUR"
candidates = [
    { id = "DEL", label = "Delhi" },
    { id = "VNS", label = "Varanasi" },
    { id = "LKO", label = "Lucknow" },
    { id = "DXN", label = "Noida (Jewar)" },
]
```

`FLIGHT_DEPARTURE_ID` / `FLIGHT_OUTBOUND_DATE` / `FLIGHT_CURRENCY`
environment variables still override these for one-off diagnostic runs
(e.g. `workflow_dispatch` with a near-term test date) — precedence is
env var > `routes.toml`. There's no per-run override for the candidate
list itself; edit `routes.toml` and commit for that.

### Airports considered (reference — so a future change doesn't start from scratch)

| Code | Name | Status | Why |
|---|---|---|---|
| DEL | Delhi | Active | Major established international hub |
| VNS | Varanasi | Active | Real international connectivity |
| LKO | Lucknow | Active | Real international connectivity (Chaudhary Charan Singh Int'l) |
| DXN | Noida (Jewar) | Active | Brand new (commercial ops began 2026-06-15), Zurich Airport International-operated, real ambition (12M pax capacity, meant to complement DEL) — but SerpApi/Google Flights coverage for it is genuinely unverified, being so new |
| GOP | Gorakhpur | Dropped | Small regional airport, likely poor international coverage even though geographically closest to the traveller's actual destination |
| KBK | Kushinagar | Dropped | ~55km from Gorakhpur, has some international ambition, but **live-tested and confirmed zero Google Flights coverage** even on a near-term date (2026-10-15) — not a schedule-horizon issue, a real coverage gap |
| KNU | Kanpur (Chakeri) | Dropped | Small, domestic-focused — never live-tested, but unpromising for the same reason as GOP |

## Why today's run might fail (or partially fail)

2027-07-17 is far enough out that airline schedules/fares may not be
loaded into Google Flights yet for some or all candidates — typically
published 330-360 days before departure, and that window is right
around now for this date. The daily job will just start succeeding on
its own once real data loads; no action needed.

**DXN specifically** is a second, independent reason a candidate might
show "no data yet" regardless of date — it only started commercial
operations 2026-06-15, so Google Flights may simply not have indexed it
yet at all.

Test any of this directly with a near-term override date instead of
waiting:

```bash
gh workflow run daily-check.yml --repo <your-username>/flight-price-watch -f outbound_date=2026-10-15
```

## One-time setup

### 1. SerpApi (the data source)

1. Sign up at [serpapi.com](https://serpapi.com) — the **Free** plan is
   $0/month, 250 searches/month.
2. Copy your API key from the dashboard.

### 2. CallMeBot (WhatsApp sending)

1. Add `+34 694 26 48 06` to your phone contacts.
2. From your own WhatsApp, message that contact: `I allow callmebot to
   send me messages`.
3. Within ~2 minutes you'll get a reply containing your API key. If not,
   try again after 24h (documented rate limit on their side).

### 3. Set the three GitHub Actions secrets

Run these yourself with your real values — nobody else should ever see
them, and they should never be typed into chat, a commit, or any file in
this repo:

```bash
gh secret set SERPAPI_KEY --repo <your-username>/flight-price-watch
gh secret set CALLMEBOT_PHONE --repo <your-username>/flight-price-watch
gh secret set CALLMEBOT_APIKEY --repo <your-username>/flight-price-watch
```

(Each command prompts for the value interactively, or pipe it in —
either way it never appears in your shell history if you paste at the
prompt rather than passing it as a CLI argument.)

### 4. Test before trusting the daily cron

```bash
gh workflow run daily-check.yml --repo <your-username>/flight-price-watch
gh run watch --repo <your-username>/flight-price-watch
```

Confirm a WhatsApp message actually arrives before walking away and
trusting the schedule.

## Running tests / lint locally

```bash
python -m venv .venv
.venv/Scripts/activate   # or: source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check .
```

Every test mocks SerpApi/CallMeBot via `requests_mock` — no real network
call, no quota spent, ever, by the test suite. `.github/workflows/ci.yml`
runs the same two commands on every push/PR, separate from the daily
product-automation workflow above; it needs none of the three product
secrets, since nothing it runs ever makes a real network call.

## Testing locally against the real APIs (optional)

```bash
cp .env.example .env   # fill in real values, never commit this file
export $(cat .env | xargs)   # or use a tool like direnv
python check_flights.py
```

## Possible v2 (not built, deliberately)

- A committed price-history log (trends visible beyond WhatsApp chat
  history).
- Rotating through more origins (`flight-agent`'s own 10-airport list
  near Nieuwegein) — no real decision rule exists for this yet.
- **Ground-travel-aware "best" scoring** (explicitly requested by the
  user, tracked here rather than forgotten): today "best" means cheapest
  price only. A real v2 would factor in actual ground travel time/cost
  from whichever candidate airport wins to the traveller's real final
  destination, mirroring `flight-agent`'s own D7 formula
  (`total_journey_score = adjusted_score + ground_cost_component + ground_time_component`)
  — needs real distance/time/cost data per candidate, which doesn't
  exist anywhere in this repo yet.
