-- 001_initial_schema.sql
-- Initial operational (OLTP) schema for the CareMetrics source system.
--
-- The migration runner applies each file inside a single transaction,
-- so this file intentionally contains no BEGIN/COMMIT.
--
-- Conventions:
--   * UUID primary keys. The uuidv7stgres 18) is time-ordered,
--     which keeps B-tree inserts append-mostly. The seeder supplies its own explicit IDs.
--   * Status-like columns are text types: easier to evolve,
--     and they replicate to BigQuery as plain strings.
--   * All timestamps are timestamptz. updated_at is maintained by a trigger on UPDATE;
--     on INSERT the caller may set it explicitly (the seeder backfills history this way).

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------

create function set_updated_at() returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

create table locations (
    id          uuid        primary key default uuidv7(),
    name        text        not null,
    city        text        not null,
    state       text        not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),

    constraint locations_name_key         unique (name),
    constraint locations_name_not_blank   check (btrim(name) <> ''),
    constraint locations_city_not_blank   check (btrim(city) <> ''),
    constraint locations_state_format     check (state ~ '^[A-Z]{2}$'),
    constraint locations_timestamps_order check (updated_at >= created_at)
);

create table payers (
    id          uuid        primary key default uuidv7(),
    name        text        not null,
    payer_type  text        not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),

    constraint payers_name_key         unique (name),
    constraint payers_name_not_blank   check (btrim(name) <> ''),
    constraint payers_payer_type_check check (
        payer_type in ('commercial', 'medicare', 'medicaid', 'other_government')
    ),
    constraint payers_timestamps_order check (updated_at >= created_at)
);

create table providers (
    id           uuid        primary key default uuidv7(),
    first_name   text        not null,
    last_name    text        not null,
    specialty    text        not null,
    location_id  uuid        not null references locations (id),
    active       boolean     not null default true,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),

    constraint providers_first_name_not_blank check (btrim(first_name) <> ''),
    constraint providers_last_name_not_blank  check (btrim(last_name) <> ''),
    constraint providers_specialty_not_blank  check (btrim(specialty) <> ''),
    constraint providers_timestamps_order     check (updated_at >= created_at)
);

create table patients (
    id             uuid        primary key default uuidv7(),
    first_name     text        not null,
    last_name      text        not null,
    date_of_birth  date        not null,
    gender         text        not null,
    state          text        not null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint patients_first_name_not_blank check (btrim(first_name) <> ''),
    constraint patients_last_name_not_blank  check (btrim(last_name) <> ''),
    constraint patients_gender_check         check (gender in ('female', 'male', 'other','unknown')),
    constraint patients_state_format         check (state ~ '^[A-Z]{2}$'),
    -- A patient cannot be born after their record was created.
    -- "at time zone 'UTC'" makes the cast immutable (a bare ::date depends on the sessionTimeZone).
    constraint patients_dob_range check (
        date_of_birth >= date '1900-01-01'
        and date_of_birth <= (created_at at time zone 'UTC')::date
    ),
    constraint patients_timestamps_order check (updated_at >= created_at)
);

-- ---------------------------------------------------------------------------
-- Transactional data
-- ---------------------------------------------------------------------------

create table appointments (
    id                uuid        primary key default uuidv7(),
    patient_id        uuid        not null references patients (id),
    provider_id       uuid        not null references providers (id),
    location_id       uuid        not null references locations (id),
    scheduled_at      timestamptz not null,
    status            text        not null default 'scheduled',
    appointment_type  text        not null,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),

    constraint appointments_status_check check (
        status in ('scheduled', 'completed', 'cancelled', 'no_show')
    ),
    constraint appointments_appointment_type_check check (
        appointment_type in (
            'new_patient', 'follow_up', 'annual_wellness', 'consultation',
            'procedure', 'urgent', 'telehealth'
        )
    ),
    constraint appointments_timestamps_order check (updated_at >= created_at),

    -- Target for the composite foreign key on encounters (see below).
    -- Redundant with the primary key for uniqueness; it exists so that
    -- encounters can declare "same patient/provider/location as my appointment".
    constraint appointments_id_parties_key unique (id, patient_id, provider_id, location_id)
);

create table encounters (
    id              uuid        primary key default uuidv7(),
    appointment_id  uuid        not null,
    patient_id      uuid        not null,
    provider_id     uuid        not null,
    location_id     uuid        not null,
    started_at      timestamptz not null,
    completed_at    timestamptz,             -- null while the encounter is in progress
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    constraint encounters_appointment_id_key unique (appointment_id),

    -- One FK enforces both "the appointment exists" and "the patient, provider and
    -- location match the appointment". Patient/provider/location existence follows
    -- transitively from the appointment's own foreign keys.
    constraint encounters_appointment_fkey
        foreign key (appointment_id, patient_id, provider_id, location_id)
        references appointments (id, patient_id, provider_id, location_id),

    constraint encounters_completed_after_started check (
        completed_at is null or completed_at > started_at
    ),
    constraint encounters_timestamps_order check (updated_at >= created_at),

    -- Target for the composite foreign key on claims.
    constraint encounters_id_parties_key unique (id, patient_id, provider_id)
);

create table claims (
    id             uuid          primary key default uuidv7(),
    encounter_id   uuid          not null,
    patient_id     uuid          not null,
    provider_id    uuid          not null,
    payer_id       uuid          not null references payers (id),
    submitted_at   timestamptz,               -- null until the claim leaves 'pending'
    status         text          not null default 'pending',
    amount_billed  numeric(12, 2) not null,
    amount_paid    numeric(12, 2) not null default 0,
    created_at     timestamptz   not null default now(),
    updated_at     timestamptz   not null default now(),

    -- One claim per encounter (no resubmission/correction modelling in this phase).
    constraint claims_encounter_id_key unique (encounter_id),

    -- Claim's patient/provider must match its encounter.
    constraint claims_encounter_fkey
        foreign key (encounter_id, patient_id, provider_id)
        references encounters (id, patient_id, provider_id),

    constraint claims_status_check check (
        status in ('pending', 'submitted', 'accepted', 'denied', 'paid')
    ),
    -- A claim has a submission time exactly when it is past 'pending'.
    constraint claims_submitted_at_matches_status check (
        (submitted_at is null) = (status = 'pending')
    ),
    constraint claims_submitted_after_created check (
        submitted_at is null or submitted_at >= created_at
    ),
    constraint claims_amount_billed_positive check (amount_billed > 0),
    constraint claims_amount_paid_range check (
        amount_paid >= 0 and amount_paid <= amount_billed
    ),
    -- Money moves only on paid claims; pending/submitted/accepted/denied carry zero payment.
    constraint claims_amount_paid_matches_status check (
        (amount_paid > 0) = (status = 'paid')
    ),
    constraint claims_timestamps_order check (updated_at >= created_at)
);

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------

create trigger locations_set_updated_at    before update on locations    for each row execute function set_updated_at();
create trigger payers_set_updated_at       before update on payers       for each row execute function set_updated_at();
create trigger providers_set_updated_at    before update on providers    for each row execute function set_updated_at();
create trigger patients_set_updated_at     before update on patients     for each row execute function set_updated_at();
create trigger appointments_set_updated_at before update on appointments for each row execute function set_updated_at();
create trigger encounters_set_updated_at   before update on encounters   for each row execute function set_updated_at();
create trigger claims_set_updated_at       before update on claims       for each row execute function set_updated_at();