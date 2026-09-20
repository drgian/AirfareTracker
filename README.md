# FlightFare

Tracks Delta Air Lines, American Airlines, United Airlines and Southwest Airlines fares for trips people choose and emails them when prices change.
Live at **https://flightfare.io** · test copy at **https://flightfare.io/dev/**

FlightFare is an independent service and is not affiliated with Delta Air Lines, American Airlines or United Airlines.

## How it fits together

| Piece | Where it runs | Code |
|---|---|---|
| Website | GitHub Pages (`gh-pages` branch), served at flightfare.io | `site/` |
| API (sign-in, trips, admin) | AWS EC2 `3.20.7.57`, behind Caddy at api.flightfare.io | `api/` |
| Database | PostgreSQL on the same EC2 server (`flighttracker`; test copy `flighttracker_dev`) | `deploy/schema.sql` |
| Price checker | Windows PC on a home connection (the airlines block cloud IPs) | `tracker/` |
| Email | Resend, sending as alerts@flightfare.io | — |
| Watchdog | AWS server, systemd timer every 5 min; emails when the price checker needs attention | `deploy/watchdog.py` |

The price checker runs two ways on the Windows PC:
- **Scheduled** (Task Scheduler "FlightTracker", 8 AM and 6 PM Eastern): checks every active trip.
- **Worker** (Task Scheduler "FlightTracker-Worker", always on): prices newly added trips within a minute or two.

Both reach the database through an SSH tunnel and drive a normal Chrome window, so the PC must stay
on and signed in. See `deploy/windows.md`.

## Releasing a change

1. Build it on **dev**: deploy the API to `/opt/flightfare/api-dev`, publish `site/index.html` to `dev/index.html`.
2. Check it at flightfare.io/dev/.
3. Promote: migrate the live database if needed, deploy the API to `/opt/flightfare/api`, deploy the
   tracker to the Windows PC if it changed, and publish `site/index.html` to `index.html`.
4. Run `tests/e2e_live.py`.
5. Bump `VERSION` and the `VERSION` constant of each component you changed (`site/index.html`, `api/api.py`,
   `tracker/tracker.py`; each reports the release it last changed in),
   add a `CHANGELOG.md` entry, commit, and tag `vX.Y.Z`.

Versions follow semantic versioning: patch for fixes, minor for new features, major for breaking changes.

## Secrets

None are stored here. They live in `/etc/flightfare/*.env` on the server and in `config.json` on the
Windows PC; see the `*.example` files in `deploy/`.
