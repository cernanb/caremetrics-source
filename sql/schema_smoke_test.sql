-- schema_smoke_test.sql
-- Proves the schema rejects invalid data, before any real data is seeded.
--
-- Runs entirely inside one transaction that is rolled back at the end, so it is
-- safe to run against any environment (local, Neon) at any time and leaves no rows.
--
-- Each negative test runs one statement that must fail with a specific named
-- constraint. A wrong constraint, a different error, or unexpected success aborts the run.
--
-- Usage:
--   docker compose exec -T postgres psql -U caremetrics -d caremetrics < sql/schema_smoke_test.sql

\set ON_ERROR_STOP on
\set QUIET on

begin;

-- Session-local helper; disappears with the rollback.
create function pg_temp.expect_violation(test_name text, stmt text, expected_constraint text)
returns void
language plpgsql
as $$
declare
    actual_constraint text;
begin
    begin
        execute stmt;
    exception
        -- SQLSTATE class 23: check, unique, foreign key, not-null violations.
        -- The inner BEGIN/EXCEPTION block is a subtransaction, so the failed
        -- statement is undone and the outer transaction stays usable.
        when integrity_constraint_violation then
            get stacked diagnostics actual_constraint = constraint_name;
            if actual_constraint is distinct from expected_constraint then
                raise exception 'FAIL %: expected violation of %, got %',
                    test_name, expected_constraint, actual_constraint;
            end if;
            raise notice 'ok    %', test_name;
            return;
    end;
    raise exception 'FAIL %: statement succeeded but should have violated %',
        test_name, expected_constraint;
end;
$$;

-- ---------------------------------------------------------------------------
-- Valid fixtures (fixed UUIDs so the tests below can reference them)
-- ---------------------------------------------------------------------------

insert into locations (id, name, city, state, created_at, updated_at) values
    ('10000000-0000-0000-0000-000000000001', 'Smoke Test Clinic A', 'Austin', 'TX', '2024-01-01 00:00+00', '2024-01-01 00:00+00'),
    ('10000000-0000-0000-0000-000000000002', 'Smoke Test Clinic B', 'Dallas', 'TX', '2024-01-01 00:00+00', '2024-01-01 00:00+00');

insert into payers (id, name, payer_type) values
    ('20000000-0000-0000-0000-000000000001', 'Smoke Test Health Plan', 'commercial');

insert into providers (id, first_name, last_name, specialty, location_id) values
    ('30000000-0000-0000-0000-000000000001', 'Test', 'Provider', 'Family Medicine', '10000000-0000-0000-0000-000000000001');

insert into patients (id, first_name, last_name, date_of_birth, gender, state) values
    ('40000000-0000-0000-0000-000000000001', 'Test', 'PatientOne', '1980-05-17', 'female', 'TX'),
    ('40000000-0000-0000-0000-000000000002', 'Test', 'PatientTwo', '1975-11-02', 'male',   'TX');

insert into appointments (id, patient_id, provider_id, location_id, scheduled_at, status, appointment_type, created_at, updated_at) values
    ('50000000-0000-0000-0000-000000000001',
     '40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
     '2025-03-10 15:00+00', 'completed', 'follow_up', '2025-03-01 12:00+00', '2025-03-10 16:00+00'),
    -- Second appointment with no encounter. Unique constraints are checked as each row
    -- is inserted, foreign keys only afterwards, so mismatch tests against an appointment
    -- that already has an encounter would hit encounters_appointment_id_key first.
    ('50000000-0000-0000-0000-000000000002',
     '40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
     '2025-04-14 15:00+00', 'completed', 'follow_up', '2025-04-01 12:00+00', '2025-04-14 16:00+00');

insert into encounters (id, appointment_id, patient_id, provider_id, location_id, started_at, completed_at, created_at, updated_at) values
    ('60000000-0000-0000-0000-000000000001', '50000000-0000-0000-0000-000000000001',
     '40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001',
     '2025-03-10 15:05+00', '2025-03-10 15:35+00', '2025-03-10 15:05+00', '2025-03-10 15:35+00');

-- ---------------------------------------------------------------------------
-- Reference tables
-- ---------------------------------------------------------------------------

do $$
begin
    perform pg_temp.expect_violation(
        'location state must be two uppercase letters',
        $sql$ insert into locations (name, city, state) values ('Bad State Clinic', 'Austin', 'Tx') $sql$,
        'locations_state_format');

    perform pg_temp.expect_violation(
        'location names are unique',
        $sql$ insert into locations (name, city, state) values ('Smoke Test Clinic A', 'Houston', 'TX') $sql$,
        'locations_name_key');

    perform pg_temp.expect_violation(
        'payer_type must be a known value',
        $sql$ insert into payers (name, payer_type) values ('Bad Type Plan', 'self_pay') $sql$,
        'payers_payer_type_check');

    perform pg_temp.expect_violation(
        'provider must belong to an existing location',
        $sql$ insert into providers (first_name, last_name, specialty, location_id)
              values ('No', 'Location', 'Cardiology', gen_random_uuid()) $sql$,
        'providers_location_id_fkey');

    perform pg_temp.expect_violation(
        'patient cannot be born after record creation',
        $sql$ insert into patients (first_name, last_name, date_of_birth, gender, state, created_at, updated_at)
              values ('Future', 'Baby', '2030-01-01', 'female', 'TX', '2026-01-01 00:00+00', '2026-01-01 00:00+00') $sql$,
        'patients_dob_range');

    perform pg_temp.expect_violation(
        'patient gender must be a known value',
        $sql$ insert into patients (first_name, last_name, date_of_birth, gender, state)
              values ('Bad', 'Gender', '1990-01-01', 'M', 'TX') $sql$,
        'patients_gender_check');
end
$$;

-- ---------------------------------------------------------------------------
-- Appointments and encounters
-- ---------------------------------------------------------------------------

do $$
begin
    perform pg_temp.expect_violation(
        'appointment status must be a known value',
        $sql$ insert into appointments (patient_id, provider_id, location_id, scheduled_at, status, appointment_type)
              values ('40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001',
                      '10000000-0000-0000-0000-000000000001', now(), 'done', 'follow_up') $sql$,
        'appointments_status_check');

    perform pg_temp.expect_violation(
        'appointment_type must be a known value',
        $sql$ insert into appointments (patient_id, provider_id, location_id, scheduled_at, status, appointment_type)
              values ('40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001',
                      '10000000-0000-0000-0000-000000000001', now(), 'scheduled', 'checkup') $sql$,
        'appointments_appointment_type_check');

    perform pg_temp.expect_violation(
        'updated_at cannot precede created_at',
        $sql$ insert into appointments (patient_id, provider_id, location_id, scheduled_at, appointment_type, created_at, updated_at)
              values ('40000000-0000-0000-0000-000000000001', '30000000-0000-0000-0000-000000000001',
                      '10000000-0000-0000-0000-000000000001', now(), 'follow_up',
                      '2025-06-02 00:00+00', '2025-06-01 00:00+00') $sql$,
        'appointments_timestamps_order');

    perform pg_temp.expect_violation(
        'encounter patient must match its appointment',
        $sql$ insert into encounters (appointment_id, patient_id, provider_id, location_id, started_at)
              values ('50000000-0000-0000-0000-000000000002', '40000000-0000-0000-0000-000000000002',
                      '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', now()) $sql$,
        'encounters_appointment_fkey');

    perform pg_temp.expect_violation(
        'encounter location must match its appointment',
        $sql$ insert into encounters (appointment_id, patient_id, provider_id, location_id, started_at)
              values ('50000000-0000-0000-0000-000000000002', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000002', now()) $sql$,
        'encounters_appointment_fkey');

    perform pg_temp.expect_violation(
        'at most one encounter per appointment',
        $sql$ insert into encounters (appointment_id, patient_id, provider_id, location_id, started_at)
              values ('50000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', now()) $sql$,
        'encounters_appointment_id_key');

    perform pg_temp.expect_violation(
        'encounter cannot complete before it starts',
        $sql$ update encounters set completed_at = started_at - interval '1 minute'
              where id = '60000000-0000-0000-0000-000000000001' $sql$,
        'encounters_completed_after_started');

    perform pg_temp.expect_violation(
        'appointment with an encounter cannot be deleted',
        $sql$ delete from appointments where id = '50000000-0000-0000-0000-000000000001' $sql$,
        'encounters_appointment_fkey');
end
$$;

-- ---------------------------------------------------------------------------
-- Claims
-- ---------------------------------------------------------------------------

do $$
begin
    perform pg_temp.expect_violation(
        'claim patient must match its encounter',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, status, amount_billed, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000002',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      'pending', 185.00, '2025-03-10 15:35+00', '2025-03-10 15:35+00') $sql$,
        'claims_encounter_fkey');

    perform pg_temp.expect_violation(
        'pending claim cannot have submitted_at',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, status, amount_billed, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', 'pending', 185.00, '2025-03-10 15:35+00', '2025-03-11 09:00+00') $sql$,
        'claims_submitted_at_matches_status');

    perform pg_temp.expect_violation(
        'submitted claim requires submitted_at',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, status, amount_billed, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      'submitted', 185.00, '2025-03-10 15:35+00', '2025-03-10 15:35+00') $sql$,
        'claims_submitted_at_matches_status');

    perform pg_temp.expect_violation(
        'claim cannot be submitted before it was created',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, status, amount_billed, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-09 09:00+00', 'submitted', 185.00, '2025-03-10 15:35+00', '2025-03-10 15:35+00') $sql$,
        'claims_submitted_after_created');

    perform pg_temp.expect_violation(
        'amount_billed must be positive',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, status, amount_billed, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      'pending', 0, '2025-03-10 15:35+00', '2025-03-10 15:35+00') $sql$,
        'claims_amount_billed_positive');

    perform pg_temp.expect_violation(
        'paid claim must have a positive payment',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', '2025-04-01 00:00+00', 'paid', 185.00, 0, '2025-03-10 15:35+00', '2025-04-01 00:00+00') $sql$,
        'claims_amount_paid_matches_status');

    perform pg_temp.expect_violation(
        'denied claim cannot carry a payment',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', null, 'denied', 185.00, 50.00, '2025-03-10 15:35+00', '2025-04-01 00:00+00') $sql$,
        'claims_amount_paid_matches_status');

    perform pg_temp.expect_violation(
        'amount_paid cannot exceed amount_billed',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', '2025-04-01 00:00+00', 'paid', 185.00, 200.00, '2025-03-10 15:35+00', '2025-04-01 00:00+00') $sql$,
        'claims_amount_paid_range');

    -- Adjudication and payment dates (migration 004). Dates: submitted 03-11, decided 03-25, paid 04-01.

    perform pg_temp.expect_violation(
        'accepted claim requires adjudicated_at',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', null, null, 'accepted', 185.00, 0, '2025-03-10 15:35+00', '2025-03-25 00:00+00') $sql$,
        'claims_adjudicated_at_matches_status');

    perform pg_temp.expect_violation(
        'submitted claim cannot have adjudicated_at',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', null, 'submitted', 185.00, 0, '2025-03-10 15:35+00', '2025-03-25 00:00+00') $sql$,
        'claims_adjudicated_at_matches_status');

    perform pg_temp.expect_violation(
        'paid claim requires paid_at',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', null, 'paid', 185.00, 142.50, '2025-03-10 15:35+00', '2025-04-01 00:00+00') $sql$,
        'claims_paid_at_matches_status');

    perform pg_temp.expect_violation(
        'denied claim cannot have paid_at',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', '2025-04-01 00:00+00', 'denied', 185.00, 0, '2025-03-10 15:35+00', '2025-04-01 00:00+00') $sql$,
        'claims_paid_at_matches_status');

    perform pg_temp.expect_violation(
        'claim cannot be adjudicated before it was submitted',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-10 20:00+00', null, 'denied', 185.00, 0, '2025-03-10 15:35+00', '2025-03-11 09:00+00') $sql$,
        'claims_adjudicated_after_submitted');

    perform pg_temp.expect_violation(
        'claim cannot be paid before it was adjudicated',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      '2025-03-11 09:00+00', '2025-03-25 00:00+00', '2025-03-20 00:00+00', 'paid', 185.00, 142.50, '2025-03-10 15:35+00', '2025-04-01 00:00+00') $sql$,
        'claims_paid_after_adjudicated');
end
$$;

-- A valid paid claim is accepted...
insert into claims (encounter_id, patient_id, provider_id, payer_id, submitted_at, adjudicated_at, paid_at, status, amount_billed, amount_paid, created_at, updated_at) values
    ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
     '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
     '2025-03-11 09:00+00', '2025-03-25 00:00+00', '2025-04-02 00:00+00', 'paid', 185.00, 142.50, '2025-03-10 15:35+00', '2025-04-02 00:00+00');

-- ...and a second claim for the same encounter is not.
do $$
begin
    perform pg_temp.expect_violation(
        'at most one claim per encounter',
        $sql$ insert into claims (encounter_id, patient_id, provider_id, payer_id, status, amount_billed, created_at, updated_at)
              values ('60000000-0000-0000-0000-000000000001', '40000000-0000-0000-0000-000000000001',
                      '30000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001',
                      'pending', 185.00, '2025-03-10 15:35+00', '2025-03-10 15:35+00') $sql$,
        'claims_encounter_id_key');
end
$$;

-- ---------------------------------------------------------------------------
-- updated_at trigger
-- ---------------------------------------------------------------------------

do $$
declare
    new_updated_at timestamptz;
begin
    update locations set city = 'Round Rock'
    where id = '10000000-0000-0000-0000-000000000001'
    returning updated_at into new_updated_at;

    -- now() is the transaction start time, so this is an exact comparison.
    if new_updated_at is distinct from now() then
        raise exception 'FAIL updated_at trigger: expected %, got %', now(), new_updated_at;
    end if;
    raise notice 'ok    updated_at trigger sets updated_at on update';
end
$$;

do $$ begin raise notice 'All schema smoke tests passed.'; end $$;

rollback;
