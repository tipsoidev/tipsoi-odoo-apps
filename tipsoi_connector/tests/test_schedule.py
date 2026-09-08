# -*- coding: utf-8 -*-
"""Working-calendar arithmetic, tested without a database.

`tipsoi_schedule` reads `resource.calendar.attendance`'s raw columns rather than calling
Odoo's own interval helper, because that helper's shape moved between the series this
module ships on. Reading the columns directly means reproducing two rules Odoo owns --
`dayofweek` is a string with Monday at zero, and a two-week calendar alternates on the
ordinal-week parity -- and getting either wrong shifts somebody's whole roster by a day
or a week without raising anything. That is what these pin.
"""

from datetime import date

from odoo.tests import TransactionCase, tagged

from ..models import tipsoi_schedule as sched


def slot(dayofweek, hour_from, hour_to, date_from=None, date_to=None,
         week_type=False, is_lunch=False):
    return sched.Slot(str(dayofweek), hour_from, hour_to, date_from, date_to,
                      week_type, is_lunch)


#: 2026-09-07 is a Monday, so the week that follows it is unambiguous to read.
MONDAY = date(2026, 9, 7)
TUESDAY = date(2026, 9, 8)
SATURDAY = date(2026, 9, 12)
SUNDAY = date(2026, 9, 13)


@tagged("post_install", "-at_install")
class TestDayOfWeek(TransactionCase):
    """`dayofweek` is a string, Monday is "0", and `date.weekday()` agrees."""

    def test_a_monday_slot_applies_on_monday(self):
        self.assertEqual(sched.slots_for_day([slot(0, 9.0, 17.0)], MONDAY),
                         [(9.0, 17.0)])

    def test_a_monday_slot_does_not_apply_on_tuesday(self):
        self.assertEqual(sched.slots_for_day([slot(0, 9.0, 17.0)], TUESDAY), [])

    def test_sunday_is_six_not_zero(self):
        """The off-by-one that would move every roster a day if `dayofweek` were read
        as Python's `isoweekday`."""
        self.assertEqual(sched.slots_for_day([slot(6, 9.0, 17.0)], SUNDAY),
                         [(9.0, 17.0)])
        self.assertEqual(sched.slots_for_day([slot(6, 9.0, 17.0)], MONDAY), [])

    def test_a_working_week_that_is_not_odoo_default(self):
        """Sunday to Thursday, which is the working week where this connector deploys.

        Pinned because Odoo's stock calendar is Monday to Friday, and inheriting it is
        exactly the failure `generate_absences` defaults to off for.
        """
        slots = [slot(6, 8.0, 17.0)] + [slot(d, 8.0, 17.0) for d in range(0, 4)]
        self.assertTrue(sched.slots_for_day(slots, SUNDAY))
        self.assertFalse(sched.slots_for_day(slots, SATURDAY))


@tagged("post_install", "-at_install")
class TestExpectedHours(TransactionCase):

    def test_a_plain_day(self):
        self.assertEqual(sched.expected_hours([slot(0, 9.0, 17.0)], MONDAY), 8.0)

    def test_a_non_working_day_is_zero_not_none(self):
        """Zero is what the caller compares against, and None would raise there."""
        self.assertEqual(sched.expected_hours([slot(0, 9.0, 17.0)], TUESDAY), 0.0)

    def test_no_calendar_at_all_is_zero(self):
        self.assertEqual(sched.expected_hours([], MONDAY), 0.0)

    def test_a_split_shift_adds_up(self):
        slots = [slot(0, 8.0, 12.0), slot(0, 13.0, 17.0)]
        self.assertEqual(sched.expected_hours(slots, MONDAY), 8.0)

    def test_a_lunch_row_is_not_owed_time(self):
        """`day_period == "lunch"` marks an unpaid break, so counting it would inflate
        every expected day by an hour."""
        slots = [slot(0, 8.0, 17.0), slot(0, 12.0, 13.0, is_lunch=True)]
        self.assertEqual(sched.expected_hours(slots, MONDAY), 9.0)

    def test_overlapping_rows_are_merged_not_summed(self):
        """Two rows overlapping is a data-entry mistake. Summing them would make the day
        longer than any shift the person could actually work."""
        slots = [slot(0, 8.0, 13.0), slot(0, 12.0, 17.0)]
        self.assertEqual(sched.expected_hours(slots, MONDAY), 9.0)

    def test_a_backwards_row_is_ignored_rather_than_subtracted(self):
        slots = [slot(0, 9.0, 17.0), slot(0, 15.0, 10.0)]
        self.assertEqual(sched.expected_hours(slots, MONDAY), 8.0)


@tagged("post_install", "-at_install")
class TestDateBounds(TransactionCase):
    """`date_from` / `date_to` scope a row to part of the year."""

    def test_a_row_that_has_not_started_yet_does_not_apply(self):
        rows = [slot(0, 9.0, 17.0, date_from=date(2026, 10, 1))]
        self.assertEqual(sched.slots_for_day(rows, MONDAY), [])

    def test_a_row_that_has_expired_does_not_apply(self):
        rows = [slot(0, 9.0, 17.0, date_to=date(2026, 1, 1))]
        self.assertEqual(sched.slots_for_day(rows, MONDAY), [])

    def test_a_row_inside_its_bounds_applies(self):
        rows = [slot(0, 9.0, 17.0,
                     date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))]
        self.assertEqual(sched.slots_for_day(rows, MONDAY), [(9.0, 17.0)])


@tagged("post_install", "-at_install")
class TestTwoWeekCalendars(TransactionCase):

    def test_the_two_week_types_alternate_between_adjacent_weeks(self):
        """The property that actually matters, asserted without hardcoding which parity
        a given date lands on -- that is Odoo's business, not this test's."""
        this_week = sched.week_type_of(MONDAY)
        next_week = sched.week_type_of(date(2026, 9, 14))
        self.assertNotEqual(this_week, next_week)
        self.assertEqual(this_week, sched.week_type_of(date(2026, 9, 21)))

    def test_only_the_matching_week_type_applies(self):
        wanted = str(sched.week_type_of(MONDAY))
        other = str(1 - sched.week_type_of(MONDAY))
        rows = [slot(0, 9.0, 17.0, week_type=wanted),
                slot(0, 6.0, 10.0, week_type=other)]
        self.assertEqual(
            sched.slots_for_day(rows, MONDAY, two_weeks_calendar=True), [(9.0, 17.0)])

    def test_week_type_is_ignored_on_a_normal_calendar(self):
        """A single-week calendar can still carry a `week_type` value, and honouring it
        there would silently halve the roster."""
        rows = [slot(0, 9.0, 17.0, week_type="0"), slot(0, 9.0, 17.0, week_type="1")]
        self.assertEqual(
            sched.slots_for_day(rows, MONDAY, two_weeks_calendar=False),
            [(9.0, 17.0), (9.0, 17.0)])


@tagged("post_install", "-at_install")
class TestExpectedSpan(TransactionCase):
    """First start and last end -- what lateness and an early exit are judged against."""

    def test_a_split_shift_spans_the_break(self):
        slots = [slot(0, 8.0, 12.0), slot(0, 13.0, 17.0)]
        self.assertEqual(sched.expected_span(slots, MONDAY), (8.0, 17.0))

    def test_a_non_working_day_has_no_span(self):
        """Which is how "worked on a day the calendar calls off" avoids being lateness."""
        self.assertEqual(sched.expected_span([slot(0, 9.0, 17.0)], TUESDAY),
                         (None, None))


@tagged("post_install", "-at_install")
class TestMinutesFromStart(TransactionCase):

    def test_arriving_after_the_start_is_positive(self):
        from datetime import datetime
        self.assertEqual(
            sched.hours_between(9.0, datetime(2026, 9, 7, 9, 20)), 20)

    def test_arriving_before_the_start_is_negative(self):
        from datetime import datetime
        self.assertEqual(
            sched.hours_between(9.0, datetime(2026, 9, 7, 8, 45)), -15)

    def test_a_half_hour_boundary_reads_as_written(self):
        """`hour_from = 8.5` is 08:30, not 8 hours 50."""
        from datetime import datetime
        self.assertEqual(
            sched.hours_between(8.5, datetime(2026, 9, 7, 8, 30)), 0)
