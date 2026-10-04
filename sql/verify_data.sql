-- verify_data.sql
-- Read-only checks of the seeded data: the cross-table rules the schema cannot
-- express as constraints, plus dataset-shape sanity checks.
--
-- Each check is a query returning the number of violating rows (0 = pass).
-- Runs in a READ ONLY transaction, so it is safe against any environment (local, Neon).
-- Exits non-zero if any check fails, so it can gate CI or a reseed.
--
-- Usage:
--   docker compose exec -T postgres psql -U caremetrics -d caremetrics < sql/verify_data.sql

\set ON_ERROR_STOP on
\set QUIET on

begin read only;

do $$
declare
    checks text[] := array[
        -- ---------------------------------------------------------------
        -- Appointments
        -- ---------------------------------------------------------------
        ['appointment location is the provider''s clinic', $q$
            select count(*) from appointments a join providers p on p.id = a.provider_id
            where a.location_id <> p.location_id $q$],

        ['appointment not before its clinic went live', $q$
            select count(*) from appointments a join locations l on l.id = a.location_id
            where a.scheduled_at < l.created_at or a.created_at < l.created_at $q$],

        ['appointment booked after patient registered and provider hired', $q$
            select count(*) from appointments a
            join patients pt on pt.id = a.patient_id
            join providers p on p.id = a.provider_id
            where a.created_at < pt.created_at or a.created_at < p.created_at $q$],

        ['appointment booked no later than its scheduled time', $q$
            select count(*) from appointments where created_at > scheduled_at $q$],

        ['no appointment scheduled after the provider left', $q$
            select count(*) from appointments a join providers p on p.id = a.provider_id
            where not p.active and a.scheduled_at >= p.updated_at $q$],

        ['completed / no_show only marked after the visit time', $q$
            select count(*) from appointments
            where status in ('completed', 'no_show') and updated_at < scheduled_at $q$],

        ['pediatrics only sees patients under 18', $q$
            select count(*) from appointments a
            join providers p on p.id = a.provider_id
            join patients pt on pt.id = a.patient_id
            where p.specialty = 'Pediatrics'
              and extract(year from age((a.scheduled_at at time zone 'America/Denver')::date,
                                        pt.date_of_birth)) >= 18 $q$],

        ['OB/GYN only sees female patients', $q$
            select count(*) from appointments a
            join providers p on p.id = a.provider_id
            join patients pt on pt.id = a.patient_id
            where p.specialty = 'Obstetrics and Gynecology' and pt.gender <> 'female' $q$],

        -- ---------------------------------------------------------------
        -- Encounters
        -- ---------------------------------------------------------------
        ['every completed appointment has an encounter', $q$
            select count(*) from appointments a
            where a.status = 'completed'
              and not exists (select 1 from encounters e where e.appointment_id = a.id) $q$],

        ['encounters only for completed appointments', $q$
            select count(*) from encounters e join appointments a on a.id = e.appointment_id
            where a.status <> 'completed' $q$],

        ['encounter starts after booking and within 1h of scheduled time', $q$
            select count(*) from encounters e join appointments a on a.id = e.appointment_id
            where e.started_at < a.created_at
               or abs(extract(epoch from e.started_at - a.scheduled_at)) > 3600 $q$],

        ['encounter completes before appointment checkout', $q$
            select count(*) from encounters e join appointments a on a.id = e.appointment_id
            where e.completed_at > a.updated_at $q$],

        -- ---------------------------------------------------------------
        -- Claims
        -- ---------------------------------------------------------------
        ['claims only for completed encounters', $q$
            select count(*) from claims c join encounters e on e.id = c.encounter_id
            where e.completed_at is null $q$],

        ['claim created and submitted after the encounter completed', $q$
            select count(*) from claims c join encounters e on e.id = c.encounter_id
            where c.created_at < e.completed_at or c.submitted_at < e.completed_at $q$],

        ['claim not created before the payer was contracted', $q$
            select count(*) from claims c join payers py on py.id = c.payer_id
            where c.created_at < py.created_at $q$],

        ['paid claims recover a plausible share of billed (20-90%)', $q$
            select count(*) from claims
            where status = 'paid' and amount_paid / amount_billed not between 0.20 and 0.90 $q$],

        -- ---------------------------------------------------------------
        -- Whole dataset
        -- ---------------------------------------------------------------
        ['no timestamps in the future', $q$
            select (select count(*) from patients     where created_at > now() or updated_at > now())
                 + (select count(*) from providers    where created_at > now() or updated_at > now())
                 + (select count(*) from appointments where created_at > now() or updated_at > now())
                 + (select count(*) from encounters   where updated_at > now() or completed_at > now())
                 + (select count(*) from claims       where updated_at > now() or submitted_at > now()) $q$],

        ['row counts near targets (+/-10%)', $q$
            select (select count(*) not between     4 and      6 from locations)::int
                 + (select count(*) not between     9 and     11 from payers)::int
                 + (select count(*) not between    45 and     55 from providers)::int
                 + (select count(*) not between  9000 and  11000 from patients)::int
                 + (select count(*) not between 67500 and  82500 from appointments)::int
                 + (select count(*) not between 45000 and  55000 from encounters)::int
                 + (select count(*) not between 40500 and  49500 from claims)::int $q$]
    ];
    violations bigint;
    failures int := 0;
begin
    for i in 1 .. array_length(checks, 1) loop
        execute checks[i][2] into violations;
        if violations = 0 then
            raise notice 'ok    %', checks[i][1];
        else
            raise warning 'FAIL  % (% violating rows)', checks[i][1], violations;
            failures := failures + 1;
        end if;
    end loop;

    if failures > 0 then
        raise exception '% of % data checks failed', failures, array_length(checks, 1);
    end if;
    raise notice 'All % data checks passed.', array_length(checks, 1);
end
$$;

rollback;

\set QUIET off
\echo
\echo Row counts:
select t.table_name, t.row_count
from (
    select 1 as ord, 'locations' as table_name, count(*) as row_count from locations
    union all select 2, 'payers',       count(*) from payers
    union all select 3, 'providers',    count(*) from providers
    union all select 4, 'patients',     count(*) from patients
    union all select 5, 'appointments', count(*) from appointments
    union all select 6, 'encounters',   count(*) from encounters
    union all select 7, 'claims',       count(*) from claims
) t
order by t.ord;