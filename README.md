# Oura → SQLite

Pulls your own Oura Ring data from the Oura API v2 into a local SQLite database and exports it to CSV.
Everything runs and is stored on your own computer.

## Folder layout

```
oura_sync.py        the tool (auth, sync, export)
run_weekly.cmd      runs sync + export; used by the weekly scheduled task
requirements.txt    Python packages
README.md, TERMS.md, PRIVACY.md
.env                OURA_CLIENT_ID / OURA_CLIENT_SECRET          (git-ignored)
.oura_tokens.json   OAuth tokens, rewritten on every refresh       (git-ignored)
data/               (git-ignored)
  oura.db           SQLite database
  exports/*.csv     one CSV per table, rewritten on every export
  sync.log          output of each scheduled run
```

## Setup

1. Register an app at <https://developer.ouraring.com/applications> with redirect URI
   `http://localhost:8080/callback`.
2. Create `.env` next to the script:
   ```
   OURA_CLIENT_ID=your-client-id
   OURA_CLIENT_SECRET=your-client-secret
   ```
3. Install Python 3.9+ and the dependencies:
   ```
   python -m pip install -r requirements.txt
   ```
4. Log in once (opens a browser; the reply is caught on `localhost:8080`):
   ```
   python oura_sync.py auth
   ```

On Windows, if `python` is "not recognised", either re-run the Python installer → **Modify** → tick
**Add Python to environment variables** and open a new terminal, or use the full path instead of `python`:
`& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"`.

## Usage

```
python oura_sync.py sync      # first run backfills everything, later runs are incremental
python oura_sync.py export    # writes data/exports/<table>.csv
```

Or just double-click `run_weekly.cmd`, which does both and logs to `data/sync.log`.

Options:

- `sync --since 2023-01-01` – start the backfill from a given date instead of the earliest available.
- `sync --full` – ignore saved progress and re-pull everything (upserts, so no duplicates).
- `export --out DIR` – write the CSVs somewhere else.
- `--db PATH` (before the command) – use a different SQLite file.

## Weekly schedule (Windows)

A Task Scheduler task called **Oura weekly sync** runs `run_weekly.cmd` every Friday at 9:00. If the PC
was off, it runs at the next log-in. To create it (from this folder, in PowerShell):

```
$a = New-ScheduledTaskAction -Execute "$PWD\run_weekly.cmd" -WorkingDirectory $PWD
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 9am
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName 'Oura weekly sync' -Action $a -Trigger $t -Settings $s
```

To remove it: `Unregister-ScheduledTask -TaskName 'Oura weekly sync' -Confirm:$false`.
Close any CSV that is open in Excel before a run, or that file can't be rewritten.

## How it works

- **Auth:** OAuth2 authorisation code flow with a `state` check. Access tokens are refreshed automatically
  before they expire or on a 401. Oura refresh tokens are single use, so the new token pair is written to
  disk (atomically) straight after every refresh. If a refresh is ever rejected, run `auth` again.
- **Endpoints:** `daily_sleep`, `sleep`, `daily_readiness`, `daily_activity`, `daily_stress`,
  `daily_spo2`, `workout`, `enhanced_tag` and `heartrate`, following `next_token` pagination.
- **Backfill:** date-based endpoints are walked in one-year windows from 2015-01-01 (before Oura existed),
  so the earliest data on your account is picked up. Heartrate is dense, so it is backfilled in 30-day
  windows from the earliest day any other endpoint returned. Progress is saved after each window, so an
  interrupted backfill resumes where it stopped.
- **Incremental:** each endpoint remembers how far it has synced (`sync_state` table). Later runs
  re-fetch the last 7 days (24 hours for heartrate), because Oura revises recent scores.
- **Storage:** each endpoint has its own table holding the record `id`, `day` and the full JSON in `data`
  (query with SQLite's `json_extract`). `heartrate` is stored as `timestamp, source, bpm`.
- **Export:** nested fields are flattened into dotted column names (e.g. `contributors.deep_sleep`);
  lists such as 5-minute HRV series are written as JSON strings.
- **Errors:** 429/5xx responses are retried with backoff. An endpoint that returns 403 (missing scope or
  no Oura membership) is skipped with a message rather than stopping the run.

Records deleted on Oura's side (e.g. a removed tag) are not deleted locally.
