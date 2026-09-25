# PostgreSQL setup and migration

## Local development

1. Start a PostgreSQL database and create a database/user for this project.
2. Create and activate a Python virtual environment, then install dependencies:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Set `DATABASE_URL` to the PostgreSQL connection string. Do not commit the
   connection string or put it in source code.
4. Initialize the schema and import `data.json` once:

   ```sh
   python migrate_to_postgres.py
   ```

5. Start the app with `python server.py` or `./start.command`.

When `DATABASE_URL` is not set, local development continues to use `data.json`
for backwards compatibility. This mode is not suitable for a multi-instance
production deployment.

## Tables

`db_schema.sql` is the canonical initial schema. `migrate_to_postgres.py` runs
it safely on every invocation, then imports the current JSON data only when
the database contains no stores or ads. The migration is therefore safe to
run at service startup and will not overwrite populated PostgreSQL data.

The current UI state remains compatible: stores map to `stores`, the current
single ad maps to `ads`, a `default` campaign and its selected store map to
`campaigns` and `campaign_stores`, design/history settings map to the `global`
row in `store_settings`, and event records map to `ad_events`.

The old `history` values are retained as legacy/demo history in JSONB; they
are not treated as measured events. `data.json` remains in the repository as
the local fallback and migration source. Back it up before migrating.

## Render

The Blueprint declares a Render Postgres database and injects its connection
string as `DATABASE_URL`. The start command runs the idempotent migration
before the web server. Render's environment variable contains the secret; do
not copy the connection string into this repository.

The Blueprint currently selects free plans in Singapore for a no-cost trial.
Render's free Postgres instances expire after 30 days and are not suitable for
production. Before production, select paid web and Postgres plans and configure
backups and recovery according to the service's retention requirements.
Uploaded images still use the existing `uploads/` filesystem in this release
and are not moved by this database migration.
