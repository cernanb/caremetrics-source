# Ingestion: Neon Postgres to BigQuery with Airbyte

Phase 2 of the CareMetrics pipeline replicates the operational database into BigQuery, where dbt picks it up.

```
Neon Postgres 18 (direct endpoint)
  role: airbyte_reader (SELECT only)
        |
        |  Airbyte OSS (abctl, local)
        |  cursor-based incremental on updated_at
        v
BigQuery  care-metrics-510606
  caremetrics_raw   typed, deduplicated tables (read by dbt)
  airbyte_internal  Airbyte's intermediate raw tables (not for direct use)
```

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Airbyte deployment | Self-hosted OSS via `abctl` on the developer machine | Free, and gives Dagster (Phase 4) a local Airbyte to orchestrate. Airbyte Cloud would work with the same connection settings. |
| Change capture | Cursor-based incremental on `updated_at` | Every table has a trigger-maintained `updated_at`, the large tables index it, and the application model has no hard deletes. CDC would need logical replication and a replication slot on Neon, which retains WAL while Airbyte is not running. |
| Neon endpoint | Direct (unpooled) | Neon's pooler runs PgBouncer in transaction mode, which is meant for many short-lived connections, not long-running sync reads. |
| BigQuery location | `US` multi-region | Cannot be changed after creation; every dbt dataset joined with it must share the location. |
| BigQuery project | Dedicated project `care-metrics-510606` | Isolates billing and limits the service account's project-level permissions to this project's data. |

## Source: Neon

Airbyte connects as a dedicated read-only role, created with plain SQL.
Do not create it in the Neon console: console-created roles become members of `neon_superuser` and can write to every table.

```sql
create role airbyte_reader with login password '<generated, stored in a password manager>';
grant usage on schema public to airbyte_reader;
grant select on all tables in schema public to airbyte_reader;
alter default privileges in schema public grant select on tables to airbyte_reader;
```

The default privileges line covers tables added by future migrations.

Verify the role is read-only:

```sql
select has_table_privilege('airbyte_reader', 'public.claims', 'select') as can_read,   -- true
       has_table_privilege('airbyte_reader', 'public.claims', 'insert') as can_write;  -- false

select r.rolname from pg_roles r                                                       -- no rows
where pg_has_role('airbyte_reader', r.oid, 'member') and r.rolname <> 'airbyte_reader';
```

## Destination: BigQuery

| Resource | Value |
|---|---|
| Project | `care-metrics-510606` (billing linked; the BigQuery sandbox does not support the `MERGE` statements that deduplication needs) |
| Dataset | `caremetrics_raw`, location `US` |
| Service account | `airbyte-loader@care-metrics-510606.iam.gserviceaccount.com` |
| Project roles | `roles/bigquery.dataEditor` (write tables, create the `airbyte_internal` dataset), `roles/bigquery.jobUser` (run load and merge jobs) |
| Key | JSON key stored at `~/.config/caremetrics/airbyte-loader.json` (mode `600`), outside the repository |

## Airbyte

### Running Airbyte locally

```bash
brew tap airbytehq/tap
brew trust --formula airbytehq/tap/abctl   # newer Homebrew requires trusting third-party formulae
brew install abctl                          # v0.30.4 at the time of writing

abctl local install                         # first run takes several minutes; UI at http://localhost:8000
abctl local credentials                     # UI login
abctl local uninstall                       # stop; keeps configuration by default
```

Give Docker Desktop at least 12 GB of memory; `abctl` runs a Kubernetes cluster inside Docker and recommends 8 GB for Airbyte alone.

### Source `caremetrics-neon` (Postgres connector)

| Field | Value |
|---|---|
| Host | Neon direct hostname (no `-pooler`) |
| Port | `5432` |
| Database | Neon database name |
| Schemas | `public` |
| User | `airbyte_reader` |
| SSL mode | `require` |
| Update method | Scan Changes with User Defined Cursor |

### Destination `caremetrics-bigquery`

| Field | Value |
|---|---|
| Project ID | `care-metrics-510606` |
| Dataset location | `US` |
| Default dataset | `caremetrics_raw` |
| Loading method | Standard inserts (no GCS staging at this data volume) |
| Service account key | Contents of the JSON key (`pbcopy < ~/.config/caremetrics/airbyte-loader.json`, then clear the clipboard) |
| Raw table dataset | Default, `airbyte_internal` |

### Connection

| Stream | Sync mode | Cursor | Primary key |
|---|---|---|---|
| `patients` | Incremental, Append + Deduped | `updated_at` | `id` |
| `appointments` | Incremental, Append + Deduped | `updated_at` | `id` |
| `encounters` | Incremental, Append + Deduped | `updated_at` | `id` |
| `claims` | Incremental, Append + Deduped | `updated_at` | `id` |
| `locations` | Full Refresh, Overwrite | | |
| `payers` | Full Refresh, Overwrite | | |
| `providers` | Full Refresh, Overwrite | | |
| `schema_migrations` | not synced | | |

| Setting | Value |
|---|---|
| Schedule | Manual (Dagster will trigger syncs in Phase 4) |
| Destination namespace | Destination default (`caremetrics_raw`); "source defined" would create a dataset named `public` |
| Stream prefix | none |
| Schema changes | Propagate field changes only |

## What lands in BigQuery

`caremetrics_raw` holds one table per stream with the source columns plus Airbyte metadata:

| Column | Meaning |
|---|---|
| `_airbyte_raw_id` | Unique ID of the record as extracted |
| `_airbyte_extracted_at` | When Airbyte read the record from Postgres |
| `_airbyte_meta` | Per-record notes, such as type-casting problems |
| `_airbyte_generation_id` | Increments when a stream is refreshed or cleared |

Deduplicated streams keep exactly one row per `id`: the version with the highest `updated_at`.
dbt sources should read `caremetrics_raw`, never `airbyte_internal`.

## Verification

### Initial load

Row counts and uniqueness must match `sql/verify_data.sql` on the source:

```bash
bq query --project_id=care-metrics-510606 --dataset_id=caremetrics_raw --use_legacy_sql=false '
select table_name, row_count, distinct_ids, expected,
       row_count = expected and distinct_ids = row_count as ok
from (
  select "locations" as table_name, count(*) as row_count, count(distinct id) as distinct_ids, 5 as expected from locations
  union all select "payers",       count(*), count(distinct id), 10    from payers
  union all select "providers",    count(*), count(distinct id), 50    from providers
  union all select "patients",     count(*), count(distinct id), 10000 from patients
  union all select "appointments", count(*), count(distinct id), 75000 from appointments
  union all select "encounters",   count(*), count(distinct id), 50603 from encounters
  union all select "claims",       count(*), count(distinct id), 45934 from claims
)
order by expected'
```

Result of the first sync on 2026-10-04: all seven tables `ok`.

### Incremental behavior

Test performed on 2026-10-04: one patient's `state` was updated in Neon, then the connection was synced again.

| Stream | Rows in BigQuery | Rows read by the second sync |
|---|---:|---:|
| `patients` | 10,000 | 2 |
| `appointments` | 75,000 | 1 |
| `encounters` | 50,603 | 62 |
| `claims` | 45,934 | 170 |
| `providers` | 50 | 50 (full refresh) |

The changed patient arrived with its new value and `updated_at`, and no table gained rows.

The few extra rows are expected.
Airbyte re-reads rows whose cursor equals the highest value it saw last time, so rows sharing a timestamp are never missed.
In the seeded data, 170 claims, 62 encounters and 1 appointment share their table's maximum `updated_at` (the seed's anchor instant, `2026-10-01 00:00:00+00`), and those are the rows that were re-read.
Deduplication makes the re-reads harmless.

## Operating notes

* **A cursor only sees changes that move `updated_at` forward.**
  Hard deletes, truncation and a reseed (`python -m caremetrics.seed --reset` restores generated, older timestamps) are invisible to incremental syncs.
  After a reseed, refresh the affected streams in Airbyte (clear and resync) so BigQuery matches the source again.
  Any change simulator must make its changes through `UPDATE`/`INSERT` so the trigger stamps `updated_at`.
* **Rotating the `airbyte_reader` password:** update it in the password manager and in the Airbyte source (save and test).
  Grants are unaffected.
  If the password is reset from the Neon console, re-run the read-only checks above.
* **Rotating the service account key:** create a new key, update the Airbyte destination, then delete the old key with `gcloud iam service-accounts keys delete`.
  Keep exactly one user-managed key.
* **Secrets never enter the repository:** the `airbyte_reader` password lives in a password manager and in Airbyte; the service account key lives in `~/.config/caremetrics/` and in Airbyte.
