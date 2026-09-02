# flight-price-watch

One daily check, pushing a notification: compares the best AMS round-
trip option (price, time, and layover combined — not cheapest alone)
to several candidate Indian destination airports, out on a fixed date
and back a fixed number of weeks later. Notification channel picks
itself based on where the script runs — see "Notification channel"
below.

## How it works

1. GitHub Actions runs `check_flights.py` on a daily cron (`0 6 * * *`
   UTC, `.github/workflows/daily-check.yml`), or on demand via the
   Actions tab's "Run workflow" button.
2. For each candidate airport in `routes.toml`'s `[check_flights]`
   section, it queries [SerpApi's Google Flights API](https://serpapi.com/google-flights-api)
   **once** for every round-trip fare from AMS on the configured
   outbound/return dates, then locally splits the results into the
   best-scored **direct** option and (for hub candidates only — see
   below) the best-scored **1-stop (max)** option — one search per
   candidate either way, never two, to stay inside the monthly search
   budget.

   Round trip still means **one** SerpApi call, not two — confirmed
   live (2026-09-02) against SerpApi's actual behavior rather than
   trusting their docs, which imply the first call's price is
   outbound-only. It isn't: when both `outbound_date` and
   `return_date` are set, the very first response already carries the
   real round-trip total per itinerary. SerpApi also supports a
   second, `departure_token`-based call to let you swap which return
   flight pairs with a given outbound — this script never makes that
   call, since it only ever wants the one best-scored pairing, not a
   choice.

   "Best" is not "cheapest": `_itinerary_score` in `check_flights.py`
   combines price with a time-value weight on total duration
   (`_TIME_VALUE_PER_HOUR`, EUR/hour) plus an EXTRA weight on layover
   time specifically (`_LAYOVER_PENALTY_PER_HOUR`) — a long layover is
   penalized twice (once as part of total duration, once again on its
   own), because sitting in an airport is worse than the same time
   spent flying. Both are simple, transparent constants (currently 15
   EUR/hour each), not fitted to any real preference data — adjust
   them directly in the source if a real pick ever looks wrong. Lower
   score wins; the table still shows the itinerary's real price, so
   you can always see what "best" traded off against "cheapest."
3. It builds a report with two sections — `DIRECT` and `1 STOP (max)`
   — each a monospace table listing every candidate that has an option
   in that category (best-scored per airport, not sorted across
   airports), with airline, departure/arrival local time, transit
   (layover) time, and total duration. A candidate with nothing in a
   category is simply omitted from that section, e.g.:

   ```
   Flight watch from AMS on 2027-07-22, back 2027-08-29 (EUR) -- 2 adults + 1 child

   DIRECT
   From             To            Price  Airline  Dep    Arr      Total
   ---------------  ------------  -----  -------  -----  -------  ------
   Amsterdam (AMS)  Delhi (DEL)     480  KLM      09:15  21:30    12h15m

   Delhi (DEL): Baggage -- Checked baggage for a fee

   1 STOP (max)
   From             To            Via                           Price  Airline        Dep    Arr      Transit  Total
   ---------------  ------------  ----------------------------  -----  -------------  -----  -------  -------  ------
   Amsterdam (AMS)  Mumbai (BOM)  Hamad International Airport    1618  Qatar Airways  10:15  06:30+1  1h30m    15h15m

   Mumbai (BOM) via Hamad International Airport (DOH): AMS->DOH 5h50m, layover 1h30m, DOH->BOM 4h00m -- Baggage: not specified
   ```

   Every candidate row is followed by its own detail line: for 1-STOP
   that's the stopover's name/code plus the AMS-to-stopover leg
   duration, the layover, and the stopover-to-destination leg duration
   (all three sum to the table's own Total column); for DIRECT it's
   just the baggage note.

   **1-STOP is only computed for hub candidates** (`one_stop = true`
   in `routes.toml`, currently DEL and BOM) — "1 stop" here means
   "reach a major hub via one layover," not "search a one-layover
   itinerary all the way into a near-destination airport." LKO is
   DIRECT-only by design: even if SerpApi returns a real one-stop
   option for it, it's deliberately discarded, never shown. See
   "Airports considered" below for which candidates are hub-eligible.

   Two caveats on what SerpApi's Google Flights data actually
   contains, both checked directly against their docs rather than
   assumed:
   - **No self-transfer field**: nothing distinguishes a self-transfer
     (separate tickets, re-check-in required) from a protected
     through-connection, so the 1-STOP breakdown doesn't claim to show
     it.
   - **Baggage is free text, not a structured field**: there's no bag
     count or fee amount anywhere, only whatever sentence Google
     Flights itself shows per flight leg (e.g. `"Checked baggage for a
     fee"`, `"1 free checked bag"`) — surfaced as-is, filtered to lines
     mentioning "bag". When a 1-STOP itinerary's two legs disagree
     (different airline/fare per leg — a real, common case, exactly
     the "surprise at booking" scenario this exists to catch), both
     are shown, labeled by leg. `"not specified"` means no baggage-
     related text was found at all, not that baggage is free.

   Sent via **email or WhatsApp depending on where the script runs**
   (see "Notification channel" below) — but only if at least one
   candidate has a real finding (in either category) or a real error.
   If every single candidate has nothing to report, **no notification
   is sent** — only logged in the run's own output. That's the expected
   daily outcome for a while (see "Why today's run might fail" below),
   not a failure, and pinging you every day with "still nothing" would
   be noise.
4. A genuine error on even one candidate (a real SerpApi error, a
   malformed response, a send failure) still sends the notification (in
   an `ERRORS` section) and makes the whole run exit 1 — a silent
   failure on a daily job is worse than a noisy one, just not for the
   routine "not published yet" case above.

**Budget**: 3 candidates × 1 search/day ≈ 90 searches/month, well
inside SerpApi's free-tier 250/month cap (~160/month spare). Round
trip (see below) costs no extra searches — the same one call per
candidate already returns the full round-trip price.

## Notification channel

CallMeBot's free WhatsApp API blocks GitHub Actions' shared runner IP
ranges (confirmed by testing: the same key/phone works instantly from
a home network, but every GitHub Actions attempt fails identically
regardless of trigger type or timing — not a rate limit, a standing
block on that class of IP). So the channel is picked automatically:

- **Running in GitHub Actions** (`GITHUB_ACTIONS=true`, set
  automatically in every job) → **email**, via Gmail SMTP. A real
  visually designed HTML email (`email_template.py`) — grouped by stop
  count, a cheapest badge, a recommendation panel — built for email
  clients specifically (nested tables, inlined styles, no JS/images/
  web fonts), not the same monospace table WhatsApp gets.
- **Running anywhere else** (e.g. your own machine, on your home
  network) → **WhatsApp**, via CallMeBot — this already works fine
  from a residential IP.
- `FLIGHT_NOTIFY_CHANNEL=email` or `FLIGHT_NOTIFY_CHANNEL=whatsapp`
  overrides the automatic pick either way, for testing one channel
  from the "wrong" environment.

Both channels' credentials are always configured (see "One-time
setup") regardless of which one a given run actually uses — cheap to
keep both ready since nothing about the WhatsApp path needed to change.

**Fallback**: if the chosen channel's send itself fails (a real send
error — CallMeBot down, a Gmail SMTP hiccup — not a per-candidate
SerpApi error, which is handled separately via the `ERRORS` section),
the run automatically retries once through the OTHER channel before
giving up. Both message forms are always built regardless of channel,
so this costs nothing extra. If the fallback also fails, the run exits
1 with a clear log line for each attempt — no notification goes out
that day, but the failure is visible in the Action's own log rather
than an unhandled crash.

## Delivery consistency — the external cron dispatch

GitHub Actions' `schedule:` trigger has gotten unreliable in 2026, not
just "a few minutes late" as the workflow's own comment used to assume.
Actual run timestamps show minutes-level jitter through 2026-08-26,
then multi-hour delays (up to ~12h) from 2026-08-27 onward — the daily
06:00 UTC check landing mid-afternoon or later instead. This lines up
with real incidents on GitHub's own status page around 2026-08-26, and
other people running scheduled Actions workflows report the same
worsening pattern through 2026 — this isn't specific to this repo's
setup, it's a platform-level `schedule:` reliability problem right now.

The fix doesn't touch `schedule:` itself (GitHub gives no way to fix
its own queue). Instead, an external free cron service —
[cron-job.org](https://cron-job.org) or similar — calls GitHub's REST
API directly to fire the workflow's existing `workflow_dispatch`
trigger, which runs immediately rather than sitting in the same
degraded `schedule:` queue.

Setup:

1. Create a GitHub PAT at
   [github.com/settings/tokens](https://github.com/settings/tokens) —
   **fine-grained**, scoped to just this repo, with **Actions: read and
   write** permission. Nothing broader.
2. Enter that PAT into cron-job.org's own UI, in the job's request
   headers — never into this repo, and never as a GitHub Actions
   secret. GitHub secrets exist for code running *inside* Actions;
   this PAT is used by cron-job.org's servers, which need it directly,
   not by anything in this repo.
3. Configure the cron-job.org job:
   - **URL**: `POST https://api.github.com/repos/sshukla154/flight-price-watch/actions/workflows/daily-check.yml/dispatches`
   - **Header**: `Authorization: Bearer <PAT>`
   - **Header**: `Accept: application/vnd.github+json`
   - **Body**: `{"ref":"master"}`
   - **Schedule**: daily, 06:00 UTC

The workflow's own `schedule: "0 6 * * *"` cron stays in place,
unchanged, as a backup trigger — if cron-job.org itself has an outage,
the run still fires eventually through GitHub's own (degraded but not
dead) queue.

One visible side effect: the "official" daily run now shows up in `gh
run list` as `event: workflow_dispatch`, not `event: schedule`. That's
just which trigger fired first each day, not a sign anything is
broken — `schedule` would only show up if cron-job.org's own call
failed to land before GitHub's queue got to it.

## Configuration — `routes.toml`

The **one file to edit** to change the route, date, or candidate
airports — no Python edit needed. Quick reference:

| Want to change | Field | Example |
|---|---|---|
| From location (departure airport) | `departure_id` | `"AMS"` |
| To location(s) (destination airports compared) | `candidates` | add/remove `{ id = "...", label = "...", one_stop = true/false }` entries |
| Outbound date | `outbound_date` | `"2027-07-22"` |
| Trip length (return date) | `return_after_days` | `38` — return date is always computed from `outbound_date`, never stored separately, so it can't drift out of sync |
| Passengers | `adults` / `children` | `2` / `1` — headcount only, SerpApi has no age field (a 7-year-old is just `children = 1`) |

Edit the value(s), commit, push — the next scheduled run (or a manual
`workflow_dispatch`) picks it up automatically.

```toml
[check_flights]
departure_id = "AMS"
departure_label = "Amsterdam"
outbound_date = "2027-07-22"
return_after_days = 38
currency = "EUR"
adults = 2
children = 1
candidates = [
    { id = "DEL", label = "Delhi", one_stop = true },
    { id = "BOM", label = "Mumbai", one_stop = true },
    { id = "LKO", label = "Lucknow", one_stop = false },
]

[check_flights.filters]
preferred_airlines = []
required_airlines = []
excluded_layover_regions = []
required_layover_regions = []

[check_flights.filters.layover_regions]
middle_east = ["MCT", "DXB", "AUH", "DOH", "AMM", "CAI", "JED", "RUH", "BAH", "KWI"]
europe_uk   = ["LHR", "LGW", "AMS", "CDG", "FRA", "MUC", "IST"]
```

`one_stop = true` means "also compute/show a 1-STOP result for this
candidate" — reserved for major hubs where a one-layover itinerary is
a meaningful comparison. `false` means DIRECT-only, always, even if
SerpApi genuinely has a one-stop option for that candidate.

`[check_flights.filters]` is opt-in itinerary filtering, absent/empty
by default — all four lists empty means nothing about scoring or
selection changes. `preferred_airlines` is the only SOFT one: a match
on any leg knocks `_AIRLINE_PREFERENCE_BONUS` (25, roughly half a
layover-hour penalty) off that itinerary's score, enough to break a
close tie, never enough to exclude anything or override a genuinely
better option on price/time/layover. `required_airlines` is a HARD
allow-list, opt-in (empty = off): an itinerary with no leg matching
falls out of contention — but if the allow-list matches nothing at all
for a candidate, that candidate doesn't go blank, it falls back to the
best available option from the pool that survived region filtering,
with `_format_row_detail` appending "(no itinerary matched required
filters, showing best available)" so the row is never mistaken for a
real match. `excluded_layover_regions` is HARD with no fallback: a
one-stop itinerary whose layover lands in an excluded region is
dropped outright, and if that empties the pool for a candidate, that
category just produces no row that day, same as any other "nothing to
report" case. `required_layover_regions` is also HARD with no
fallback, unlike `required_airlines` — an allow-list that matches
nothing simply leaves that category empty rather than widening back
to an unfiltered pool, since `_passes_region_filters` runs before (and
feeds into) the airline-fallback logic rather than getting a
fallback path of its own. `layover_regions` is the hand-maintained
IATA-code-to-region lookup both filters key against — deliberately
incomplete, extend it the same way `candidates` gets extended, by hand
as new layover airports show up in real results. An unclassified
layover (an airport not yet in `layover_regions`) always passes both
region filters — a config gap should never silently drop an itinerary
nobody told this script to exclude.

`FLIGHT_DEPARTURE_ID` / `FLIGHT_OUTBOUND_DATE` / `FLIGHT_RETURN_AFTER_DAYS` /
`FLIGHT_CURRENCY` / `FLIGHT_ADULTS` / `FLIGHT_CHILDREN` environment
variables still override these for one-off diagnostic runs
(e.g. `workflow_dispatch` with a near-term test date) — precedence is
env var > `routes.toml`. There's no per-run override for the candidate
list itself; edit `routes.toml` and commit for that.

### Airports considered (reference — so a future change doesn't start from scratch)

| Code | Name | Status | 1-STOP eligible | Why |
|---|---|---|---|---|
| DEL | Delhi | Active | Yes | Major established international hub |
| BOM | Mumbai | Active | Yes | Major established international hub — already proven to have real SerpApi/Google Flights coverage, having shown up as a real layover city in earlier live tests before being added as its own candidate |
| LKO | Lucknow | Active | No | Real international connectivity (Chaudhary Charan Singh Int'l), but DIRECT-only — a one-layover itinerary all the way into a near-destination airport isn't the "1 stop" comparison this trip wants |
| VNS | Varanasi | Dropped | -- | Removed 2026-09-02 by explicit choice — the user narrowed the candidate list down to DEL/BOM/LKO |
| DXN | Noida (Jewar) | Dropped | -- | Removed 2026-09-02, same choice — was brand new (commercial ops began 2026-06-15), Zurich Airport International-operated, real ambition (12M pax capacity, meant to complement DEL), but not part of the current candidate list |
| GOP | Gorakhpur | Dropped | -- | Small regional airport, likely poor international coverage even though geographically closest to the traveller's actual destination |
| KBK | Kushinagar | Dropped | -- | ~55km from Gorakhpur, has some international ambition, but **live-tested and confirmed zero Google Flights coverage** even on a near-term date (2026-10-15) — not a schedule-horizon issue, a real coverage gap |
| KNU | Kanpur (Chakeri) | Dropped | -- | Small, domestic-focused — never live-tested, but unpromising for the same reason as GOP |

## Why today's run might fail (or partially fail)

2027-07-22 (or its round-trip pairing 2027-08-29) is far enough out
that airline schedules/fares may not be loaded into Google Flights yet
for the outbound leg, the return leg, or both — typically published
330-360 days before departure, and that window is right around now for
these dates. Either leg missing means the whole round-trip pairing
comes back empty, handled the same as any other "nothing to report"
case (see `NoFlightsFoundYet` in `flightwatch_core.py`) — the daily job
will just start succeeding on its own once real data loads for both
legs; no action needed.

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

### 2. CallMeBot (WhatsApp sending — used only for local runs)

1. Add `+34 694 26 48 06` to your phone contacts.
2. From your own WhatsApp, message that contact: `I allow callmebot to
   send me messages`.
3. Within ~2 minutes you'll get a reply containing your API key. If not,
   try again after 24h (documented rate limit on their side).

### 3. Gmail (email sending — used only for GitHub Actions runs)

1. Enable 2-Step Verification on the Gmail account, if not already on
   (required before Google will issue an App Password):
   [myaccount.google.com/security](https://myaccount.google.com/security).
2. Generate an App Password at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   (pick "Mail" and any device name) — copy the 16-character password
   shown.
3. That's your `GMAIL_APP_PASSWORD`; the Gmail address itself is
   `GMAIL_ADDRESS`. This account sends the email to itself — no
   separate recipient to configure.

### 4. Set the GitHub Actions secrets

Run these yourself with your real values — nobody else should ever see
them, and they should never be typed into chat, a commit, or any file in
this repo:

```bash
gh secret set SERPAPI_KEY --repo <your-username>/flight-price-watch
gh secret set CALLMEBOT_PHONE --repo <your-username>/flight-price-watch
gh secret set CALLMEBOT_APIKEY --repo <your-username>/flight-price-watch
gh secret set GMAIL_ADDRESS --repo <your-username>/flight-price-watch
gh secret set GMAIL_APP_PASSWORD --repo <your-username>/flight-price-watch
```

(Each command prompts for the value interactively, or pipe it in —
either way it never appears in your shell history if you paste at the
prompt rather than passing it as a CLI argument.)

### 5. Test before trusting the daily cron

```bash
gh workflow run daily-check.yml --repo <your-username>/flight-price-watch
gh run watch --repo <your-username>/flight-price-watch
```

Confirm a real email actually arrives before walking away and trusting
the schedule — GitHub Actions always uses the email channel (see
"Notification channel" above).

## Running tests / lint locally

```bash
python -m venv .venv
.venv/Scripts/activate   # or: source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check .
```

Every test mocks SerpApi/CallMeBot/Gmail via `requests_mock` and
`unittest.mock` — no real network call, no quota spent, ever, by the
test suite. `.github/workflows/ci.yml` runs the same two commands on
every push/PR, separate from the daily product-automation workflow
above; it needs none of the product secrets, since nothing it runs
ever makes a real network call.

## Testing locally against the real APIs (optional)

```bash
cp .env.example .env   # fill in real values, never commit this file
export $(cat .env | xargs)   # or use a tool like direnv
python check_flights.py
```

`GITHUB_ACTIONS` won't be set in a local shell, so this picks the
WhatsApp channel automatically — set `FLIGHT_NOTIFY_CHANNEL=email` in
`.env` first if you want to test the email path from your own machine
instead.

## Possible v2 (not built, deliberately)

- **Multiple dates / date range** — SerpApi's Google Flights API has
  no calendar/cheapest-dates feature (verified live, 2026-08-21):
  `outbound_date` accepts exactly one fixed date per call, always. A
  range means one SerpApi call per date, multiplied by however many
  candidates are active — budget math matters here (today's 3
  candidates × 1 date ≈ 90/month; a ±3-day range would jump to
  ~630/month, well past the 250 free cap) — needs either fewer
  candidates, a lower check frequency, or scoping the range to one
  candidate at a time.
- A committed price-history log (trends visible beyond WhatsApp chat
  history).
- Rotating through more origins (`flight-agent`'s own 10-airport list
  near Nieuwegein) — no real decision rule exists for this yet.
- **Ground-travel-aware "best" scoring** (explicitly requested by the
  user, partially done): `_itinerary_score` already factors in flight
  price, total travel time, and layover time (see "How it works"
  above) — what's still missing is the GROUND leg: actual travel
  time/cost from whichever candidate airport wins to the traveller's
  real final destination, mirroring `flight-agent`'s own D7 formula
  (`total_journey_score = adjusted_score + ground_cost_component + ground_time_component`)
  — needs real distance/time/cost data per candidate, which doesn't
  exist anywhere in this repo yet.
