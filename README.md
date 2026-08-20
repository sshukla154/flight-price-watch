# flight-price-watch

Checks the price of one flight (AMS → DEL, 2027-07-17) once a day and
sends the result to WhatsApp. Nothing fancier than that in v1 —
no history log, no rotating origins/destinations (deliberately deferred,
see "Possible v2" below).

## How it works

1. GitHub Actions runs `check_price.py` on a daily cron (`0 6 * * *` UTC),
   or on demand via the Actions tab's "Run workflow" button.
2. The script queries [SerpApi's Google Flights API](https://serpapi.com/google-flights-api)
   for the cheapest one-way AMS→DEL fare on 2027-07-17.
3. It sends one WhatsApp message with the result via
   [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/).
4. If anything fails (no flights found, an API error), it still sends a
   WhatsApp message saying so — a silent failure on a daily job is worse
   than a noisy one.

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

### 4. Test it before trusting the daily cron

```bash
gh workflow run daily-check.yml --repo <your-username>/flight-price-watch
gh run watch --repo <your-username>/flight-price-watch
```

Confirm a WhatsApp message actually arrives before walking away and
trusting the schedule.

## Testing locally (optional)

```bash
cp .env.example .env   # fill in real values, never commit this file
pip install -r requirements.txt
export $(cat .env | xargs)   # or use a tool like direnv
python check_price.py
```

## Possible v2 (not built, deliberately)

- A committed price-history log (CSV/JSON), so trends are visible beyond
  WhatsApp chat history.
- Rotating through multiple origins (flight-agent's own 10-airport list
  near Nieuwegein) and/or destinations, with some rule for "which is
  cheapest so far" — deferred because there's no real decision rule for
  this yet, and building one speculatively would be infrastructure for a
  policy that doesn't exist.
