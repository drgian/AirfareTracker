# Changelog

## Unreleased

- Test copy (flightfare.io/dev/) is private: its API only accepts the emails in `PRIVATE_TO` and answers
  everyone else with "This test site is private."; signed-out visitors see sign-in only; search engines are
  told to skip /dev/ (robots.txt plus a noindex tag). The live site doesn't set `PRIVATE_TO`, so it's unaffected.

## 1.1.0 (2026-09-15)

- My trips: an overview of all active trips (latest price, change, lowest seen, status) with Edit, Archive and
  Delete on each card; opens by default when you have more than one trip.
- Roles: basic (own trips), researcher (plus Analysis), admin (everything); admins set roles in the Admin tab.
- Analysis tab for researchers and admins: price by day of week, time of day, and time before departure, each vs.
  the route's own average, as dots with ±1σ whiskers (hover shows ±1 standard error too); how often and how much
  prices move; per-route ranges. Real checks only (gap fill-ins excluded); all-routes view shows routes, not trip names.
- Watchdog on the AWS server (`deploy/watchdog.py`, every 5 minutes): emails when the Windows price checker
  goes offline for 10+ minutes, when it's back, and when an 8 AM / 6 PM check saves no prices.

## 1.0.1 (2026-09-15)

- Fix: the word "null" appeared under the header for anyone without archived trips.

## 1.0.0 (2026-09-15)

First versioned release.

- Accounts: sign up by email link then choose a password; sign in with email and password; forgot password;
  Continue with Google; emailed sign-in links as a backup.
- Welcome step asks for a home airport, which pre-fills "From" when adding trips; changeable in account settings.
- Trips: searchable airport list (4,000+ airports, including region and alternate names), two-month date range
  picker with drag selection, cheapest or fastest flights, or specific flight numbers.
- First price appears within a minute or two of adding a trip; then checked every morning and evening.
- Validation: unknown airports and malformed flight numbers are rejected; flights Delta doesn't offer are flagged.
- Editing: renames save directly; changing dates, airports, flights or cabin offers "replace" or "keep both".
- Archive (stops checking, keeps history), restore, delete; trips archive themselves after departure.
- Email alerts on price changes and below-target prices, from alerts@flightfare.io.
- Admin tab: usage stats, page views, recent activity, feedback, user details, grant or remove admin.
- Feedback button on every page.
- Test copy at flightfare.io/dev/ with its own API and database.
