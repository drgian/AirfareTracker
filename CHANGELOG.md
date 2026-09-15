# Changelog

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
