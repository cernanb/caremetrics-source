-- 004_claim_event_dates.sql
-- When each claim was adjudicated (accepted or denied) and paid.
--
-- The seeder and simulator already compute these moments to decide a claim's status,
-- but until now they were not stored. Downstream analytics need them for payer
-- turnaround (days to decision, days to payment); updated_at cannot stand in for them,
-- because for simulator changes it is the time the row was written.
--
-- Existing claims have no dates and could not satisfy the new constraints, so this
-- migration refuses to run while claims has any rows. Reset the database first:
-- see "Resetting for a schema change" in docs/ingestion.md.
--
-- Applied in a transaction by the migration runner, so this file contains no BEGIN/COMMIT.

do $$
begin
    if exists (select 1 from claims) then
        raise exception 'claims must be empty before 004_claim_event_dates.sql: existing claims have no adjudication or payment dates'
            using hint = 'Reset the database first; see "Resetting for a schema change" in docs/ingestion.md.';
    end if;
end
$$;

alter table claims
    add column adjudicated_at timestamptz,   -- payer decision: accepted or denied
    add column paid_at        timestamptz,   -- payment posted

    -- A claim has a decision date exactly when it is past 'submitted'
    -- (same pattern as claims_submitted_at_matches_status).
    add constraint claims_adjudicated_at_matches_status check (
        (adjudicated_at is null) = (status in ('pending', 'submitted'))
    ),
    -- A claim has a payment date exactly when it is 'paid'.
    add constraint claims_paid_at_matches_status check (
        (paid_at is null) = (status <> 'paid')
    ),
    -- Events happen in order: submitted <= adjudicated <= paid.
    add constraint claims_adjudicated_after_submitted check (
        adjudicated_at is null or adjudicated_at >= submitted_at
    ),
    add constraint claims_paid_after_adjudicated check (
        paid_at is null or paid_at >= adjudicated_at
    );
