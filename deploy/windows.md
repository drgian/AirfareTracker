# Windows price-checker PC

Folder: `C:\Users\<user>\flight-tracker` containing `tracker.py`, `config.json` (see
`tracker-config.example.json`), the AWS key file named in `config.json`, and a Python 3.12 venv
with `tracker/requirements.txt` installed. Google Chrome must be installed (the tracker starts it itself).

Scheduled tasks (run only when the user is signed in, because Chrome needs the desktop):

| Task | Trigger | Action (working directory = the folder) |
|---|---|---|
| FlightTracker | Daily 8:00 AM and 6:00 PM | `cmd /c ""venv\Scripts\python.exe" tracker.py >> tracker.log 2>&1"` |
| FlightTracker-Worker | At logon, plus every 5 minutes (ignored if already running) | `cmd /c ""venv\Scripts\python.exe" -u tracker.py --worker >> worker.log 2>&1"` |

Other modes: `tracker.py --dry-run` (search Delta, save nothing) and `tracker.py --publish`
(rebuild the legacy dashboard from the database).

Keep sleep disabled on AC power (`powercfg /change standby-timeout-ac 0`).

To restart the worker after deploying a new `tracker.py`, stop the worker's `python.exe` processes and any
`ssh.exe` tunnel to `localhost:5432`, then `Start-ScheduledTask FlightTracker-Worker`.
