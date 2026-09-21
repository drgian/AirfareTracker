# Changelog

## 1.7.1 (2026-09-21)

- Fix: on a phone the buttons at the bottom of the Add trip and Study a route dialogs could sit off screen and
  couldn't be scrolled to. Tall dialogs now scroll, and the buttons stay pinned in view.

## 1.7.0 (2026-09-21)

- Research tab for researchers and admins: pick a route and we price that same trip every week out to six months,
  every day, recording the price, how far ahead it was booked, the day of the week, and the price of oil (WTI, from
  the St. Louis Fed). Delta, American and United; three routes per person, unlimited for admins.
- The day's searches are spread over about 19 hours with random gaps, and only run when nobody is waiting on an
  instant check, so research never delays a real trip.
- Chrome fixes from the Linux work: it is pinned to X11 (on a Wayland desktop it would silently refuse to open its
  debugging port) and stale profile locks are cleared before each run.

## 1.6.0 (2026-09-20)

- Admin: click a user's email to unfold the flights they're tracking underneath — airline, route, dates, cabin,
  latest and lowest price, checks and status. The arrow beside it still opens the full account page.

## 1.5.0 (2026-09-20)

- Southwest Airlines, alongside Delta, American and United. Its cabins are Basic, Choice, Choice Preferred and
  Choice Extra. Southwest prices each direction separately, so a tracked round trip is the chosen outbound plus
  the cheapest return.
- Real airline logos on the trip form, trip heading, My trips cards and Archived.

## 1.4.0 (2026-09-20)

- Admin can disable or remove an account. Disabling blocks every way in (emailed link, password, Google), signs them
  out everywhere and stops their trips being priced or emailed, while keeping everything so it can be undone.
  Removing deletes the account and its trips after you type the email to confirm; price history stays, because it
  belongs to the route and other people may be tracking it. Neither can be done to your own account.
- Groundwork for Sign in with Apple (inactive until the Services ID is configured).

## 1.3.0 (2026-09-19)

- One price summary email a day instead of one per change: every trip that moved is listed with where it started and
  where it ended up. Target alerts stay immediate and now fire only when a price crosses below the target, rather
  than on every check while it sits below.

## 1.2.0 (2026-09-15)

- American Airlines and United Airlines fares alongside Delta. Pick the airline on a trip and the cabin list becomes
  that airline's own cabins: Delta (Main Basic/Classic/Extra, Comfort+, First), American (Basic Economy, Main Cabin,
  Main Cabin Extra, Business), United (Basic Economy, United Economy, Economy Plus, Business). A trip is now
  route + dates + airline + cabin; existing trips stay Delta with their history intact.
- Prices still come straight from each airline's own site: American's results page carries its fare table, United's
  results page is linked to directly and streams its fares. Each search gets its own tab and they are paced apart.
- Airline badges (our own initials mark in each airline's colours) on the trip heading, My trips cards and Archived.

## 1.1.2 (2026-09-15)

- Fix: Delete on a My trips card did nothing. The card's "don't open the trip" click handler ran first and
  cancelled the delete. Delete from a trip tab and from Archived were unaffected. Covered by the live test now.

## 1.1.1 (2026-09-15)

- Smoother price charts: when two checks are under 6 hours apart, the chart plots only the later one, so the line
  doesn't kink sharply. The lowest and highest prices always stay on the chart; the history table lists every check.
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
