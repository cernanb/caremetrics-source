"""Unit tests for the simulator's pure decision logic. No database needed.

    python -m unittest tests.test_simulate_logic -v
"""

import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from caremetrics.seed.appointments import CANCEL_RATE, NO_SHOW_RATE_DEFAULT
from caremetrics.seed.config import SeedSettings
from caremetrics.seed.payers import PROFILES
from caremetrics.simulate.appointments import decide
from caremetrics.simulate.bookings import Visit
from caremetrics.simulate.claims import STATUS_RANK, timeline
from caremetrics.simulate.core import Window, daily_count, entity_rng, local_days, times_on

SETTINGS = SeedSettings(random_seed=42, anchor_date=date(2026, 10, 1))
UTC = timezone.utc
BOOKED = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
SLOT = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)


def _appointment(key: str, appointment_type: str = "follow_up", payer_type: str | None = "commercial"):
    return SimpleNamespace(
        id=uuid.uuid5(uuid.NAMESPACE_URL, key), payer_type=payer_type, appointment_type=appointment_type,
        created_at=BOOKED, scheduled_at=SLOT, specialty="Family Medicine",
    )


class EntityRngTest(unittest.TestCase):
    def test_same_record_and_purpose_give_the_same_stream(self):
        a = entity_rng(SETTINGS, "purpose", "record").random()
        b = entity_rng(SETTINGS, "purpose", "record").random()
        self.assertEqual(a, b)

    def test_purpose_separates_streams(self):
        a = entity_rng(SETTINGS, "one", "record").random()
        b = entity_rng(SETTINGS, "two", "record").random()
        self.assertNotEqual(a, b)


class DecideTest(unittest.TestCase):
    def test_outcome_is_deterministic(self):
        appointment = _appointment("deterministic")
        self.assertEqual(decide(SETTINGS, appointment), decide(SETTINGS, appointment))

    def test_rates_match_the_seed(self):
        outcomes = [decide(SETTINGS, _appointment(f"rate-{i}")).status for i in range(20_000)]
        self.assertAlmostEqual(outcomes.count("no_show") / len(outcomes), NO_SHOW_RATE_DEFAULT, delta=0.01)
        self.assertAlmostEqual(outcomes.count("cancelled") / len(outcomes), CANCEL_RATE, delta=0.01)

    def test_telehealth_has_fewer_no_shows(self):
        def no_shows(appointment_type):
            return sum(decide(SETTINGS, _appointment(f"tele-{i}", appointment_type)).status == "no_show"
                       for i in range(10_000))
        self.assertLess(no_shows("telehealth"), no_shows("follow_up"))

    def test_outcome_times_are_consistent(self):
        for i in range(2_000):
            appointment = _appointment(f"times-{i}")
            outcome = decide(SETTINGS, appointment)
            if outcome.status == "completed":
                self.assertGreaterEqual(outcome.visit_start, appointment.created_at)
                self.assertGreater(outcome.visit_end, outcome.visit_start)
                self.assertGreater(outcome.happens_at, outcome.visit_end)
                self.assertLess(abs(outcome.visit_start - appointment.scheduled_at), timedelta(hours=1))
            elif outcome.status == "cancelled":
                self.assertTrue(appointment.created_at <= outcome.happens_at <= appointment.scheduled_at)
            else:
                self.assertTrue(appointment.scheduled_at + timedelta(minutes=20)
                                <= outcome.happens_at <= appointment.scheduled_at + timedelta(minutes=180))


class ClaimTimelineTest(unittest.TestCase):
    BILLED = Decimal("200.00")

    def _paths(self, count: int = 500):
        for i in range(count):
            payer = PROFILES[i % len(PROFILES)]
            yield timeline(SETTINGS, f"claim-{i}", BOOKED, None, payer)

    def test_status_never_moves_backwards_over_time(self):
        moments = [BOOKED + timedelta(days=d) for d in range(0, 120, 2)]
        for path in self._paths():
            ranks = [STATUS_RANK[path.status_at(m)] for m in moments]
            self.assertEqual(ranks, sorted(ranks))

    def test_only_paid_claims_carry_money_and_never_more_than_billed(self):
        for path in self._paths():
            for days in (0, 5, 30, 90):
                status = path.status_at(BOOKED + timedelta(days=days))
                paid = path.amount_paid(status, self.BILLED)
                if status == "paid":
                    self.assertTrue(Decimal("0") < paid <= self.BILLED)
                else:
                    self.assertEqual(paid, Decimal("0.00"))

    def test_accepted_claims_are_never_denied(self):
        for path in self._paths():
            for days in range(0, 120, 3):
                self.assertNotEqual(path.status_at(BOOKED + timedelta(days=days), can_deny=False), "denied")

    def test_existing_submission_time_is_kept(self):
        submitted = BOOKED + timedelta(days=2)
        path = timeline(SETTINGS, "claim-x", BOOKED, submitted, PROFILES[0])
        self.assertEqual(path.submitted_at, submitted)


class VisitKeptTest(unittest.TestCase):
    def _visit(self, fate: str, fate_at: datetime | None) -> Visit:
        return Visit(uuid.uuid4(), "Family Medicine", "follow_up", SLOT, BOOKED, fate, fate_at)

    def test_not_kept_before_it_was_booked(self):
        self.assertFalse(self._visit("completed", None).kept_at(BOOKED - timedelta(minutes=1)))

    def test_completed_and_still_scheduled_visits_are_kept(self):
        self.assertTrue(self._visit("completed", None).kept_at(BOOKED + timedelta(days=1)))
        self.assertTrue(self._visit("scheduled", None).kept_at(BOOKED + timedelta(days=1)))

    def test_cancellation_counts_only_once_it_has_happened(self):
        cancelled_at = BOOKED + timedelta(days=5)
        visit = self._visit("cancelled", cancelled_at)
        self.assertTrue(visit.kept_at(cancelled_at - timedelta(seconds=1)))
        self.assertFalse(visit.kept_at(cancelled_at))

    def test_cancellation_before_this_run_is_never_kept(self):
        self.assertFalse(self._visit("no_show", None).kept_at(BOOKED + timedelta(days=1)))


class CalendarHelpersTest(unittest.TestCase):
    def test_local_days_cover_the_window(self):
        from zoneinfo import ZoneInfo
        denver = ZoneInfo("America/Denver")
        window = Window(datetime(2026, 10, 1, 0, 0, tzinfo=UTC), datetime(2026, 10, 3, 12, 0, tzinfo=UTC))
        # 2026-10-01 00:00 UTC is still September 30 in Denver.
        self.assertEqual(local_days(window, denver),
                         [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)])

    def test_times_on_are_sorted_and_on_the_requested_day(self):
        from zoneinfo import ZoneInfo
        denver = ZoneInfo("America/Denver")
        times = times_on(entity_rng(SETTINGS, "t", "d"), date(2026, 10, 2), denver, {9: 1.0, 14: 1.0}, 50)
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(t.astimezone(denver).date() == date(2026, 10, 2) for t in times))

    def test_daily_count_is_never_negative_and_centred_on_the_mean(self):
        counts = [daily_count(entity_rng(SETTINGS, "count", i), 5.5) for i in range(5_000)]
        self.assertGreaterEqual(min(counts), 0)
        self.assertAlmostEqual(sum(counts) / len(counts), 5.5, delta=0.15)


if __name__ == "__main__":
    unittest.main()
