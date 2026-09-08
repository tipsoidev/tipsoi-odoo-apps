# -*- coding: utf-8 -*-
"""The Device Portal's day-wise view, derived in Odoo from paired attendance.

The Device Portal has no day endpoint, so a client on that backend never sees the
day-wise attendance a Tipsoi app client gets for free. This model rolls up what pairing
already wrote. Three properties carry the design and each is pinned here:

* it **reads** `hr.attendance` and never writes it. Pairing is already that writer, and a
  second one would collide on the overlap constraint `hr.attendance` enforces itself;
* an absence is the one row here that is not evidence of anything -- it is asserted from
  a working calendar the Device Portal never sends, so it stays off until an administrator
  says the calendars are right;
* a day belongs to the local date of its *check-in*, which is what keeps an overnight
  shift whole instead of splitting it at midnight.
"""

from datetime import date, datetime, timedelta

from psycopg2 import IntegrityError

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import TipsoiCase

#: A Monday, in the past so no future-dated-attendance rule can interfere.
MONDAY = date(2025, 9, 1)
SATURDAY = date(2025, 9, 6)

#: Asia/Dhaka is +06:00 with no DST, which is the backend default and the client's own
#: offset. Local 08:00 is 02:00 UTC, and every time in this file is written that way
#: rather than computed, so a timezone regression shows up as a failing assertion rather
#: than as two wrong numbers agreeing with each other.
def utc(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute) - timedelta(hours=6)


@tagged("post_install", "-at_install")
class DaySummaryCase(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.calendar = self.env["resource.calendar"].create({
            "name": "Sun-Thu 08:00-17:00",
            "company_id": self.backend.company_id.id,
            "tz": "Asia/Dhaka",
            "attendance_ids": [(5, 0, 0)] + [
                (0, 0, {"name": "day %s" % d, "dayofweek": str(d),
                        "hour_from": 8.0, "hour_to": 17.0})
                # Sunday (6) through Thursday (3): the working week where this deploys,
                # deliberately not Odoo's stock Monday-to-Friday.
                for d in (6, 0, 1, 2, 3)
            ],
        })
        self.employee = self._employee("E-001", backend=self.backend, name="Rahim")
        self.employee.resource_calendar_id = self.calendar

    # -- fixtures -----------------------------------------------------------------------

    def _attendance(self, check_in, check_out=None, employee=None):
        return self.env["hr.attendance"].create({
            "employee_id": (employee or self.employee).id,
            "check_in": check_in,
            "check_out": check_out,
        })

    def _punch(self, when, direction="in", employee=None, uid=None):
        employee = employee or self.employee
        return self.env["tipsoi.punch.log"].create({
            "backend_id": self.backend.id,
            "tipsoi_log_id": uid or ("u%s" % when.isoformat()),
            "employee_id": employee.id,
            "person_identifier": employee.tipsoi_identifier,
            "punch_time_utc": when,
            "direction": direction,
            "state": "paired",
        })

    def _build(self, first=MONDAY, last=None):
        last = last or first
        window_from = utc(first, 0)
        window_to = utc(last, 23, 59)
        with self._run(self.backend, "days") as run:
            self.env["tipsoi.day.summary"]._build(
                self.backend, run, window_from, window_to)
        return run

    def _days(self):
        return self.env["tipsoi.day.summary"].search(
            [("backend_id", "=", self.backend.id)])

    def _day(self, day=MONDAY):
        return self.env["tipsoi.day.summary"].search([
            ("backend_id", "=", self.backend.id),
            ("employee_id", "=", self.employee.id),
            ("day_date", "=", day)])


@tagged("post_install", "-at_install")
class TestRollup(DaySummaryCase):

    def test_a_worked_day_becomes_one_present_row(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        day = self._day()
        self.assertEqual(len(day), 1)
        self.assertEqual(day.day_type, "present")
        self.assertEqual(day.check_in_utc, utc(MONDAY, 8))
        self.assertEqual(day.check_out_utc, utc(MONDAY, 17))
        self.assertAlmostEqual(day.worked_hours, 9.0, places=2)

    def test_the_day_is_the_local_date_not_the_utc_one(self):
        """The client is at +06:00. A shift starting 08:00 local is 02:00 UTC, and both
        land on the same day -- but an evening shift does not, which is the next test."""
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        self.assertEqual(self._day().day_date, MONDAY)

    def test_two_pairs_in_a_day_give_worked_under_on_site(self):
        """A lunch break: on site spans it, worked excludes it. The Tipsoi app's own day
        feed reports only first-in and last-out, so it cannot make this distinction."""
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 12))
        self._attendance(utc(MONDAY, 13), utc(MONDAY, 17))
        self._build()
        day = self._day()
        self.assertEqual(day.attendance_count, 2)
        self.assertAlmostEqual(day.worked_hours, 8.0, places=2)
        self.assertAlmostEqual(day.span_hours, 9.0, places=2)

    def test_a_day_with_a_gap_is_flagged_as_having_a_break(self):
        """Stored rather than computed in the view: the older series express a view
        condition as a domain, and a domain cannot subtract one field from another."""
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 12))
        self._attendance(utc(MONDAY, 13), utc(MONDAY, 17))
        self._build()
        self.assertTrue(self._day().has_break)

    def test_a_single_unbroken_pair_has_no_break(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        day = self._day()
        self.assertFalse(day.has_break)
        self.assertAlmostEqual(day.span_hours, day.worked_hours, places=2)

    def test_the_row_links_the_attendance_it_summarises(self):
        first = self._attendance(utc(MONDAY, 8), utc(MONDAY, 12))
        second = self._attendance(utc(MONDAY, 13), utc(MONDAY, 17))
        self._build()
        self.assertEqual(set(self._day().attendance_ids.ids),
                         {first.id, second.id})

    def test_punches_are_counted_alongside_the_pairs(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._punch(utc(MONDAY, 8), "in")
        self._punch(utc(MONDAY, 17), "out")
        self._build()
        self.assertEqual(self._day().punch_count, 2)

    def test_expected_hours_come_from_the_calendar(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        self.assertAlmostEqual(self._day().expected_hours, 9.0, places=2)


@tagged("post_install", "-at_install")
class TestOvernightShifts(DaySummaryCase):
    """The rule `max_shift_hours` protects during pairing, carried into the day view."""

    def test_a_shift_that_crosses_midnight_belongs_to_the_day_it_started(self):
        tuesday = MONDAY + timedelta(days=1)
        self._attendance(utc(MONDAY, 22), utc(tuesday, 6))
        self._build(MONDAY, tuesday)
        day = self._day(MONDAY)
        self.assertEqual(day.day_type, "present")
        self.assertAlmostEqual(day.worked_hours, 8.0, places=2)

    def test_the_following_day_does_not_also_claim_it(self):
        """Bucketing by check-*out* as well would double-count the hours.

        Tuesday gets a shift of its own so that the row exists and can be inspected --
        otherwise there is nothing to assert against.
        """
        tuesday = MONDAY + timedelta(days=1)
        overnight = self._attendance(utc(MONDAY, 22), utc(tuesday, 6))
        own = self._attendance(utc(tuesday, 9), utc(tuesday, 17))
        self._build(MONDAY, tuesday)
        day = self._day(tuesday)
        self.assertEqual(day.attendance_ids.ids, own.ids)
        self.assertNotIn(overnight, day.attendance_ids)
        self.assertAlmostEqual(day.worked_hours, 8.0, places=2)

    def test_a_shift_started_before_the_window_is_not_pulled_in(self):
        """It belongs to its own start day, which is outside the range asked for.

        A second shift inside the window gives Monday a row to exist at all -- without
        one this passes because nothing was built, which would prove nothing.
        """
        sunday = MONDAY - timedelta(days=1)
        crossing = self._attendance(utc(sunday, 22), utc(MONDAY, 6))
        inside = self._attendance(utc(MONDAY, 9), utc(MONDAY, 17))
        self._build(MONDAY, MONDAY)
        day = self._day(MONDAY)
        self.assertTrue(day, "Monday should have a row from the shift that started on it")
        self.assertEqual(day.attendance_ids.ids, inside.ids)
        self.assertNotIn(crossing, day.attendance_ids)
        self.assertEqual(day.check_in_utc, utc(MONDAY, 9))
        self.assertAlmostEqual(day.worked_hours, 8.0, places=2)


@tagged("post_install", "-at_install")
class TestIncompleteDays(DaySummaryCase):

    def test_an_entry_with_no_exit_is_incomplete_not_absent(self):
        """The distinction the client cares about: somebody who is at work right now must
        not be reported absent."""
        self._punch(utc(MONDAY, 8), "in")
        self._build()
        day = self._day()
        self.assertEqual(day.day_type, "partial")
        self.assertEqual(day.punch_count, 1)
        self.assertTrue(day.state_reason)

    def test_an_incomplete_day_reports_no_hours_rather_than_a_guess(self):
        self._punch(utc(MONDAY, 8), "in")
        self._build()
        self.assertAlmostEqual(self._day().worked_hours, 0.0, places=2)

    def test_an_open_attendance_is_incomplete(self):
        self._attendance(utc(MONDAY, 8))
        self._build()
        self.assertEqual(self._day().day_type, "partial")


@tagged("post_install", "-at_install")
class TestAbsencesAreOptIn(DaySummaryCase):
    """The switch that keeps an inherited working calendar from making false claims."""

    def setUp(self):
        super().setUp()
        # A punch a week earlier, so the absence floor is not what is under test here.
        self._punch(utc(MONDAY - timedelta(days=7), 8), "in", uid="floor")

    def test_no_absence_row_is_written_while_the_switch_is_off(self):
        self.assertFalse(self.backend.generate_absences)
        self._build()
        self.assertFalse(self._days())

    def test_an_absence_appears_once_the_switch_is_on(self):
        self.backend.generate_absences = True
        self._build()
        day = self._day()
        self.assertEqual(day.day_type, "absent")
        self.assertAlmostEqual(day.expected_hours, 9.0, places=2)

    def test_a_non_working_day_is_never_an_absence(self):
        """Saturday is not in this calendar, so there is nothing to be absent from."""
        self.backend.generate_absences = True
        self._build(SATURDAY, SATURDAY)
        self.assertFalse(self._day(SATURDAY))

    def test_an_employee_with_no_working_hours_is_never_absent(self):
        """No schedule means no working day, and no working day means no claim.

        The calendar is emptied rather than removed. Detaching it outright is not a state
        every supported series can hold -- on 15 `resource.resource.calendar_id` is NOT
        NULL, so writing False there is a database error rather than a test of anything --
        and a calendar with no hours reaches the identical branch: `_calendar_of` returns
        an empty slot list either way, and `_should_mark_absent` stops on `not slots`.
        """
        self.backend.generate_absences = True
        self.calendar.attendance_ids.unlink()
        self._build()
        self.assertFalse(self._day())

    def test_no_slots_means_no_absence_whatever_produced_them(self):
        """The guard itself, asserted directly rather than through a fixture.

        This is the branch that the missing-calendar case would have exercised, and it is
        reachable on every series because it takes the slot list as an argument.
        """
        self.backend.generate_absences = True
        bounds = (MONDAY - timedelta(days=30), None)
        Summary = self.env["tipsoi.day.summary"]
        self.assertFalse(Summary._should_mark_absent(
            self.backend, MONDAY, bounds, [], False))
        self.assertTrue(
            Summary._should_mark_absent(
                self.backend, MONDAY, bounds,
                self._slots_from(self.calendar), False),
            "the control: a real calendar on a working day does justify an absence")

    def _slots_from(self, calendar):
        return self.env["tipsoi.day.summary"]._calendar_of(self.employee, self.backend)[0]

    def test_switching_absences_off_again_removes_the_rows_it_made(self):
        """They were a claim, and the claim has been withdrawn. Leaving them would be a
        stale assertion that nothing would ever correct."""
        self.backend.generate_absences = True
        self._build()
        self.assertTrue(self._day())
        self.backend.generate_absences = False
        self._build()
        self.assertFalse(self._day())


@tagged("post_install", "-at_install")
class TestAbsenceFloor(DaySummaryCase):
    """Before somebody's first punch they were not absent, they were not yet enrolled."""

    def setUp(self):
        super().setUp()
        self.backend.generate_absences = True

    def test_a_day_before_the_first_ever_punch_is_not_an_absence(self):
        self._punch(utc(MONDAY + timedelta(days=1), 8), "in")
        self._build()
        self.assertFalse(self._day(MONDAY))

    def test_a_day_after_the_first_ever_punch_is_an_absence(self):
        self._punch(utc(MONDAY - timedelta(days=1), 8), "in")
        self._build()
        self.assertEqual(self._day(MONDAY).day_type, "absent")

    def test_an_employee_who_has_never_punched_is_never_absent(self):
        """A new hire enrolled in Odoo but not yet on a device would otherwise acquire a
        fortnight of absences on their first sync."""
        self._build()
        self.assertFalse(self._day())


@tagged("post_install", "-at_install")
class TestDepartedEmployees(DaySummaryCase):
    """After somebody leaves they are not absent, they are gone.

    A Device Portal departure archives the employee (`action_tipsoi_depart` writes
    `active = False`). The build keeps archived people in scope on purpose, so their real
    history still rebuilds -- which means without a ceiling a leaver would collect a fresh
    absence every working day, indefinitely, each one a claim they failed to turn up.
    """

    def setUp(self):
        super().setUp()
        self.backend.generate_absences = True
        # Punched a fortnight before the window, so the floor is not what is under test.
        self._punch(utc(MONDAY - timedelta(days=14), 8), "in", uid="floor")

    def _depart(self, on=None):
        """Archive the employee the way a Device Portal departure does.

        The `departure_date` read-back is a precondition, not a subject: `hr` owns that
        field and has a departure wizard around it, so if a series ever stops persisting
        it through a plain write, the ceiling silently falls back to the last punch day
        and these tests would fail somewhere far less informative than here.
        """
        vals = {"active": False}
        if on:
            vals["departure_date"] = on
        self.employee.with_context(tipsoi_syncing=True).write(vals)
        self.assertFalse(self.employee.active)
        if on:
            self.assertEqual(
                self.employee.departure_date, on,
                "hr did not persist departure_date through a plain write on this "
                "series; the absence ceiling needs reworking around that")

    def test_a_still_employed_person_is_absent(self):
        """The control: without it the next two tests could pass for the wrong reason."""
        self._build()
        self.assertEqual(self._day().day_type, "absent")

    def test_a_day_after_the_departure_date_is_not_an_absence(self):
        self._depart(on=MONDAY - timedelta(days=1))
        self._build()
        self.assertFalse(self._day())

    def test_a_day_before_the_departure_date_is_still_an_absence(self):
        """Leaving in June does not rewrite what happened in May."""
        self._depart(on=MONDAY + timedelta(days=30))
        self._build()
        self.assertEqual(self._day().day_type, "absent")

    def test_an_archived_employee_with_no_departure_date_stops_at_their_last_punch(self):
        """Nobody recorded a leaving date, so the last time a device saw them is the best
        evidence available. Guessing later invents exactly the absences this prevents."""
        self._depart()
        self._build()
        self.assertFalse(self._day())

    def test_a_departed_employee_still_gets_the_days_they_actually_worked(self):
        """The ceiling must bound absences only. Real history is evidence and stays."""
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._depart(on=MONDAY - timedelta(days=1))
        self._build()
        day = self._day()
        self.assertEqual(day.day_type, "present")
        self.assertAlmostEqual(day.worked_hours, 9.0, places=2)


@tagged("post_install", "-at_install")
class TestPunctuality(DaySummaryCase):

    def setUp(self):
        super().setUp()
        self.backend.generate_absences = True

    def test_arriving_after_the_calendar_start_is_late(self):
        self._attendance(utc(MONDAY, 8, 25), utc(MONDAY, 17))
        self._build()
        day = self._day()
        self.assertTrue(day.is_late)
        self.assertEqual(day.late_minutes, 25)

    def test_arriving_on_time_is_not_late(self):
        self._attendance(utc(MONDAY, 7, 50), utc(MONDAY, 17))
        self._build()
        self.assertFalse(self._day().is_late)

    def test_leaving_before_the_calendar_end_is_early(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 16, 30))
        self._build()
        day = self._day()
        self.assertTrue(day.is_early)
        self.assertEqual(day.early_minutes, 30)

    def test_lateness_is_not_judged_while_absences_are_off(self):
        """Both are claims about the same unverified calendar, so both wait for the same
        confirmation."""
        self.backend.generate_absences = False
        self._attendance(utc(MONDAY, 10), utc(MONDAY, 17))
        self._build()
        self.assertFalse(self._day().is_late)

    def test_working_a_day_the_calendar_calls_off_is_present_not_late(self):
        """Overtime and weekend callouts are real. Judging them against a shift that does
        not exist that day would flag every one of them."""
        self._attendance(utc(SATURDAY, 10), utc(SATURDAY, 14))
        self._build(SATURDAY, SATURDAY)
        day = self._day(SATURDAY)
        self.assertEqual(day.day_type, "present")
        self.assertAlmostEqual(day.expected_hours, 0.0, places=2)
        self.assertFalse(day.is_late)
        self.assertFalse(day.is_early)

    def test_working_less_than_the_calendar_expects_is_short(self):
        self._attendance(utc(MONDAY, 9), utc(MONDAY, 15))
        self._build()
        day = self._day()
        self.assertTrue(day.is_short)
        self.assertAlmostEqual(day.worked_hours, 6.0, places=2)
        self.assertAlmostEqual(day.expected_hours, 9.0, places=2)

    def test_a_full_day_is_not_short(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        self.assertFalse(self._day().is_short)

    def test_short_hours_is_not_judged_while_absences_are_off(self):
        self.backend.generate_absences = False
        self._attendance(utc(MONDAY, 9), utc(MONDAY, 15))
        self._build()
        self.assertFalse(self._day().is_short)

    def test_an_unfinished_day_is_not_called_short(self):
        """It has not finished being worked, so there is nothing to be short of yet."""
        self._attendance(utc(MONDAY, 8))
        self._build()
        day = self._day()
        self.assertEqual(day.day_type, "partial")
        self.assertFalse(day.is_short)

    def test_lateness_uses_the_calendar_timezone_not_the_device_one(self):
        """`hour_from = 8.0` means 08:00 wherever the calendar says it lives. With the
        calendar an hour ahead of the devices, an 08:00 device arrival is 09:00 there."""
        self.calendar.tz = "Asia/Yangon"        # +06:30, no DST
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        self.assertEqual(self._day().late_minutes, 30)


@tagged("post_install", "-at_install")
class TestIdempotency(DaySummaryCase):
    """A rebuild restates a day; it never duplicates one."""

    def test_building_twice_leaves_one_row(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        self._build()
        self.assertEqual(len(self._day()), 1)

    def test_the_second_build_writes_nothing(self):
        """What keeps a fifteen-minute cron over a quiet office from rewriting the lot."""
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        before = self._day().write_date
        run = self._build()
        self.assertEqual(self._day().write_date, before)
        self.assertEqual(run.updated, 0)
        self.assertEqual(run.created, 0)
        self.assertTrue(run.skipped)

    def test_a_late_punch_restates_the_day_in_place(self):
        """The case the rolling window exists for: an exit arrives after the day was
        already summarised, and the row is corrected rather than joined by a second one."""
        attendance = self._attendance(utc(MONDAY, 8))
        self._build()
        self.assertEqual(self._day().day_type, "partial")

        attendance.check_out = utc(MONDAY, 17)
        run = self._build()
        day = self._day()
        self.assertEqual(len(day), 1)
        self.assertEqual(day.day_type, "present")
        self.assertAlmostEqual(day.worked_hours, 9.0, places=2)
        self.assertEqual(run.updated, 1)

    def test_a_day_whose_attendance_is_deleted_stops_being_reported(self):
        attendance = self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        attendance.unlink()
        self._build()
        self.assertFalse(self._day())


@tagged("post_install", "-at_install")
class TestNeverWritesAttendance(DaySummaryCase):
    """The rule the whole design rests on, asserted directly rather than inferred.

    `tipsoi.day.attendance` stages what the Tipsoi app reported and *creates* attendance
    from it. This model runs the other way. If it ever gained a write path, two writers
    would land on the same employee-day and the loser would surface as a random import
    failure at a customer.
    """

    def test_a_build_creates_no_attendance(self):
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._punch(utc(MONDAY, 8), "in")
        before = self.env["hr.attendance"].search_count([])
        self._build()
        self.assertEqual(self.env["hr.attendance"].search_count([]), before)

    def test_a_build_does_not_edit_the_attendance_it_reads(self):
        attendance = self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        before = attendance.write_date
        self._build()
        self.assertEqual(attendance.write_date, before)

    def test_an_absence_creates_no_attendance_either(self):
        self.backend.generate_absences = True
        self._punch(utc(MONDAY - timedelta(days=1), 8), "in")
        before = self.env["hr.attendance"].search_count([])
        self._build()
        self.assertEqual(self._day().day_type, "absent")
        self.assertEqual(self.env["hr.attendance"].search_count([]), before)


@tagged("post_install", "-at_install")
class TestBackfill(DaySummaryCase):
    """An explicit range rebuilds history the rolling window can never reach."""

    def test_a_range_builds_every_day_in_it(self):
        for offset in range(3):
            day = MONDAY + timedelta(days=offset)
            self._attendance(utc(day, 8), utc(day, 17))
        self._build(MONDAY, MONDAY + timedelta(days=2))
        self.assertEqual(len(self._days()), 3)

    def test_running_the_same_range_twice_adds_nothing(self):
        for offset in range(3):
            day = MONDAY + timedelta(days=offset)
            self._attendance(utc(day, 8), utc(day, 17))
        self._build(MONDAY, MONDAY + timedelta(days=2))
        self._build(MONDAY, MONDAY + timedelta(days=2))
        self.assertEqual(len(self._days()), 3)

    def test_the_backend_window_is_the_last_few_days(self):
        window_from, window_to = self.backend._day_window()
        self.assertEqual((window_to - window_from).days,
                         self.backend.day_window_days)


@tagged("post_install", "-at_install")
class TestScoping(DaySummaryCase):
    """Days are built for this backend's own people, and nobody else's."""

    def test_an_employee_not_linked_to_the_backend_gets_no_rows(self):
        stranger = self._employee(name="Unlinked")
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17), employee=stranger)
        self._build()
        self.assertFalse(self.env["tipsoi.day.summary"].search(
            [("employee_id", "=", stranger.id)]))

    def test_two_employees_get_a_row_each(self):
        other = self._employee("E-002", backend=self.backend, name="Karim")
        other.resource_calendar_id = self.calendar
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._attendance(utc(MONDAY, 9), utc(MONDAY, 18), employee=other)
        self._build()
        self.assertEqual(len(self._days()), 2)

    def test_the_same_employee_and_day_cannot_be_stored_twice(self):
        """The database backstop under the rebuild's own idempotency.

        Run inside a savepoint: an IntegrityError aborts the transaction, and without one
        every later test in this class would fail on a poisoned cursor rather than on
        anything it was written to check.
        """
        self._attendance(utc(MONDAY, 8), utc(MONDAY, 17))
        self._build()
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self.env["tipsoi.day.summary"].create({
                    "backend_id": self.backend.id,
                    "employee_id": self.employee.id,
                    "day_date": MONDAY,
                }).flush_recordset()
