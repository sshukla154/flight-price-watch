# flight-price-watch

Two independent daily checks, both pushing to WhatsApp:

1. **`check_price.py`** — one fixed route, AMS → DEL, 2027-07-17.
2. **`check_gorakhpur.py`** — AMS against two candidate destination
   airports near Gorakhpur (GOP, KBK), comparing which is cheaper.

They share `flightwatch_core.py`'s SerpApi/CallMeBot logic but are
otherwise unrelated — separate schedules, separate WhatsApp messages,
separate failure handling. Nothing fancier than that in v1 — no history
log (see "Possible v2" below).

## How `check_price.py` works

1. GitHub Actions runs it on a daily cron (`0 6 * * *` UTC, `daily-check.yml`),
   or on demand via the Actions tab's "Run workflow" button.
2. It queries [SerpApi's Google Flights API](https://serpapi.com/google-flights-api)
   for the cheapest one-way AMS→DEL fare on 2027-07-17.
3. If a real price was found, it sends one WhatsApp message with the
   result via [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/).
4. If Google Flights simply has no fare data for 2027-07-17 yet (see
   "Why today's run might fail" below), **no WhatsApp message is sent** —
   only logged in the Action's own run output. That's the expected daily
   outcome for a while, not a failure, and pinging you every day with
   "still nothing" would be noise.
5. Any OTHER failure (a real SerpApi error, a malformed response, a
   CallMeBot send failure) still sends a WhatsApp message saying so — a
   silent failure on a daily job is worse than a noisy one, just not for
   the routine "not published yet" case above.

## How `check_gorakhpur.py` works

Same mechanics as above (`gorakhpur-check.yml`, cron `15 6 * * *` UTC —
a few minutes offset from `daily-check.yml` just so the two don't hit
SerpApi at the exact same instant), but it queries **both** candidate
airports every run — GOP (Gorakhpur itself) and KBK (Kushinagar, ~55km
away) — and sends **one combined message** comparing them, cheapest
first. VNS/Lucknow/Patna (200km+ away) are deliberately excluded from
this pass. Stays silent only when **both** candidates have no data yet;
notifies the moment either one has a real price, or a real error, so a
genuine failure on one candidate is never masked by the other's routine
"no data yet."

**Budget**: `check_price.py` uses ~30 searches/month, `check_gorakhpur.py`
uses ~60/month (2 candidates × 1/day) — ~90/month combined, well inside
SerpApi's free-tier 250/month cap.

## Why today's run might fail

2027-07-17 is far enough out that airline schedules/fares may not be
loaded into Google Flights yet — typically published 330-360 days
before departure, and that window is right around now for this date.
The daily job will just start succeeding on its own once real data
loads; no action needed. If you want to confirm the pipeline itself
works before then, trigger it manually with a near-term date:

```bash
gh workflow run daily-check.yml --repo <your-username>/flight-price-watch -f outbound_date=2026-10-15
```

The same question applies separately to `check_gorakhpur.py` — GOP/KBK
are much smaller airports than DEL, so their real-world SerpApi/Google
Flights coverage isn't something to assume works just because DEL's
does. Test it the same way, with the same near-term-date override:

```bash
gh workflow run gorakhpur-check.yml --repo <your-username>/flight-price-watch -f outbound_date=2026-10-15
```

## One-time setup

### 1. SerpApi (the data source)

1. Sign up at [serpapi.com](https://serpapi.com) — the **Free** plan is
   $0/month, 250 searches/month. One run/day here uses ~30/month.
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

Both workflows read the same three secrets — nothing extra to set up for
`check_gorakhpur.py`.

### 4. Test before trusting the daily crons

```bash
gh workflow run daily-check.yml --repo <your-username>/flight-price-watch
gh workflow run gorakhpur-check.yml --repo <your-username>/flight-price-watch
gh run watch --repo <your-username>/flight-price-watch
```

Confirm a WhatsApp message actually arrives for each before walking away
and trusting the schedules.

## Testing locally (optional)

```bash
cp .env.example .env   # fill in real values, never commit this file
pip install -r requirements.txt
export $(cat .env | xargs)   # or use a tool like direnv
python check_price.py
python check_gorakhpur.py
```

## Possible v2 (not built, deliberately)

- A committed price-history log (CSV/JSON), so trends are visible beyond
  WhatsApp chat history.
- Rotating through multiple origins (flight-agent's own 10-airport list
  near Nieuwegein) — deferred because there's no real decision rule for
  this yet, and building one speculatively would be infrastructure for a
  policy that doesn't exist.
- **Ground-travel-aware "best" scoring for `check_gorakhpur.py`**
  (explicitly requested by the user, tracked here rather than forgotten):
  today "best" means cheapest price only. A real v2 would factor in
  actual ground travel time/cost from GOP/KBK to Gorakhpur itself,
  mirroring `flight-agent`'s own D7 formula
  (`total_journey_score = adjusted_score + ground_cost_component + ground_time_component`)
  — needs real distance/time/cost data per candidate airport, which
  doesn't exist anywhere in this repo yet.
