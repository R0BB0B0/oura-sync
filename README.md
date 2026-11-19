# Oura → SQLite

Pulls your own Oura Ring data from the Oura API v2 into a local SQLite file (`oura.db`) and exports it to CSV.

## Setup

1. Register an app at <https://cloud.ouraring.com/oauth/applications> with redirect URI
   `http://localhost:8080/callback`.
2. Create `.env` next to the script:
   ```
   OURA_CLIENT_ID=your-client-id
   OURA_CLIENT_SECRET=your-client-secret
   ```
3. Install dependencies (Python 3.9+):
   ```
   pip install -r requirements.txt
   ```

## Usage

```
python oura_sync.py auth      # opens a browser; catches the callback on localhost:8080
python oura_sync.py sync      # first run backfills everything, later runs are incremental
python oura_sync.py export    # writes exports/<table>.csv
```

Options:

- `sync --since 2023-01-01` – start the backfill from a given date instead of the earliest available.
- `sync --full` – ignore saved progress and re-pull everything (upserts, so no duplicates).
- `export --out DIR` – choose the CSV folder.
- `--db PATH` (before the command) – use a different SQLite file.

## How it works

- **Auth:** OAuth2 authorisation code flow with a `state` check. Tokens go in `.oura_tokens.json` (git-ignored).
  Access tokens are refreshed automatically before they expire or on a 401. Oura refresh tokens are
  single use, so the new token pair is written to disk (atomically) straight after every refresh.
  If a refresh is ever rejected, run `auth` again.
- **Endpoints:** `daily_sleep`, `sleep`, `daily_readiness`, `daily_activity`, `daily_stress`,
  `daily_spo2`, `workout`, `enhanced_tag` and `heartrate`, following `next_token` pagination.
- **Backfill:** date-based endpoints are walked in one-year windows from 2015-01-01 (before Oura existed),
  so whatever is the earliest data on your account gets picked up. Heartrate is dense, so it is
  backfilled in 30-day windows starting from the earliest day any other endpoint returned.
  Progress is saved after each window, so an interrupted backfill resumes where it stopped.
- **Incremental:** each endpoint remembers how far it has synced (`sync_state` table). Later runs
  re-fetch the last 7 days (24 hours for heartrate), because Oura revises recent scores.
- **Storage:** each endpoint has its own table holding the record `id`, `day` and the full JSON in `data`
  (query with SQLite's `json_extract`). `heartrate` is stored as `timestamp, source, bpm`.
- **Export:** nested fields are flattened into dotted column names (e.g. `contributors.deep_sleep`);
  lists such as 5-minute HRV series are written as JSON strings.
- **Errors:** 429/5xx responses are retried with backoff. An endpoint that returns 403 (missing scope or
  no Oura membership) is skipped with a message rather than stopping the run.

Records deleted on Oura's side (e.g. a removed tag) are not deleted locally.
