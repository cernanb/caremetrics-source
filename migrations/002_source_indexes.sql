-- 002_source_indexes.sql
-- Secondary indexes for the source system's own access paths.
--
-- Postgres indexes primary keys and unique constraints automatically, but NOT the
-- referencing side of a foreign key. Each index below exists for a named reason:
--   (fk)      supports a foreign key: joins, and the lookup Postgres performs on
--             DELETE/UPDATE of the referenced row
--   (app)     an operational query the clinic application would run
--   (sync)    updated_at cursor for Airbyte incremental sync:
--             WHERE updated_at > :cursor ORDER BY updated_at
--
-- Foreign keys already covered by an existing index are not repeated:
-- encounters -> appointments uses encounters_appointment_id_key, and
-- claims -> encounters uses claims_encounter_id_key (both lead with the FK column).
--
-- Applied in a transaction by the migration runner, so these are plain CREATE INDEX.
-- On a large live table you would use CREATE INDEX CONCURRENTLY instead, which cannot
-- run inside a transaction and would need its own non-transactional migration.

-- providers -------------------------------------------------------------------
create index providers_location_id_idx
    on providers (location_id);                                    -- (fk) clinic roster

-- patients --------------------------------------------------------------------
create index patients_last_name_first_name_idx
    on patients (last_name, first_name);                           -- (app) front-desk patient search
create index patients_updated_at_idx
    on patients (updated_at);                                      -- (sync)

-- appointments ----------------------------------------------------------------
create index appointments_patient_id_scheduled_at_idx
    on appointments (patient_id, scheduled_at);                    -- (fk) + (app) patient visit history
create index appointments_provider_id_scheduled_at_idx
    on appointments (provider_id, scheduled_at);                   -- (fk) + (app) provider daily schedule
create index appointments_location_id_scheduled_at_idx
    on appointments (location_id, scheduled_at);                   -- (fk) + (app) clinic daily schedule
create index appointments_updated_at_idx
    on appointments (updated_at);                                  -- (sync)

-- encounters ------------------------------------------------------------------
create index encounters_patient_id_started_at_idx
    on encounters (patient_id, started_at);                        -- (app) patient chart timeline
create index encounters_provider_id_started_at_idx
    on encounters (provider_id, started_at);                       -- (app) provider's charts to sign
create index encounters_updated_at_idx
    on encounters (updated_at);                                    -- (sync)

-- claims ----------------------------------------------------------------------
create index claims_patient_id_idx
    on claims (patient_id);                                        -- (app) patient billing history
create index claims_payer_id_status_idx
    on claims (payer_id, status);                                  -- (fk) + (app) payer A/R by status
create index claims_open_submitted_at_idx
    on claims (submitted_at)
    where status in ('pending', 'submitted', 'accepted');          -- (app) billing work queue: unresolved claims by age
create index claims_updated_at_idx
    on claims (updated_at);                                        -- (sync)
