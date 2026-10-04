-- 003_simulator_schema.sql
-- State for the change simulator (python -m caremetrics.simulate).
--
-- This is not operational data. It lives in its own schema so that:
--   * Airbyte, configured for schema "public", never replicates it
--   * airbyte_reader has no privileges on it (its grants cover "public" only)
--   * it is obvious to any reader that these tables belong to the tooling, not the clinic
--
-- Applied in a transaction by the migration runner.

create schema simulator;

comment on schema simulator is
    'State of the change simulator (caremetrics.simulate). Not operational data; not replicated.';

-- One row per completed simulator run: the window of simulated time it covered.
-- A run inserts its row in the same transaction as its data changes, so a window is
-- recorded exactly when its changes are committed.
create table simulator.runs (
    id               bigint      generated always as identity primary key,
    simulated_from   timestamptz not null,
    simulated_until  timestamptz not null,
    -- Transaction start time of the run. Equals the updated_at that the trigger
    -- stamped on every row the run changed, which makes a run's changes easy to find.
    run_at           timestamptz not null default now(),
    counts           jsonb       not null default '{}'::jsonb,

    constraint runs_window_order check (simulated_until > simulated_from),
    -- No two runs may cover overlapping simulated time (no event processed twice).
    constraint runs_no_overlap exclude using gist (
        tstzrange(simulated_from, simulated_until) with &&
    )
);

-- Generator-only patient attributes from the seed (patients.py), made durable so
-- that every run sees the same values. Bootstrapped on the first run; patients
-- registered by the simulator get a row when they are created.
create table simulator.patient_profiles (
    patient_id        uuid             primary key references public.patients (id),
    home_location_id  uuid             not null references public.locations (id),
    payer_id          uuid             references public.payers (id),  -- null = uninsured / self-pay
    utilization       double precision not null,

    constraint patient_profiles_utilization_positive check (utilization > 0)
);