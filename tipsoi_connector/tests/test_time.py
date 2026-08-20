# -*- coding: utf-8 -*-
"""Timestamp rules, tested as pure functions.

Every rule here was read out of the two upstream implementations, and every one of them
fails *silently* when it is wrong -- an attendance record six hours out looks like a
record, not like an error. That makes these the cheapest tests in the suite and the ones
most worth having.

Covered:

* the Device Portal hands back naive wall-clock strings in the application timezone, and
  parses the window parameters in that same zone -- so a request built in UTC asks for the
  wrong window;
* HRM works in epoch milliseconds, and formats its day times at a *fixed* +06:00 offset
  compiled into the API rather than a configurable or named timezone, so it has no DST;
* `"-"` is a real value in the HRM day payload, not a parse failure;
* and an exit earlier in the day than the entry means the shift crossed midnight.
"""

from datetime import date, datetime, time

from odoo.tests import TransactionCase, tagged

from ..models import tipsoi_time as tt


@tagged("post_install", "-at_install")
class TestTipsoiTime(TransactionCase):

    # -- Device Portal: naive local wall time -------------------------------------------

    def test_dhaka_wall_time_becomes_utc(self):
        self.assertEqual(
            tt.dp_to_utc("2026-08-01 09:15:00", "Asia/Dhaka"),
            datetime(2026, 8, 1, 3, 15))

    def test_utc_backend_timezone_leaves_the_value_alone(self):
        self.assertEqual(
            tt.dp_to_utc("2026-08-01 09:15:00", "UTC"),
            datetime(2026, 8, 1, 9, 15))

    def test_a_dst_zone_resolves_rather_than_raising(self):
        """Asia/Dhaka has no DST, but the field is configurable.

        A punch that lands in a repeated hour is still a real punch, so ambiguity is
        resolved rather than raised -- otherwise one punch would fail a whole page.
        """
        ambiguous = tt.dp_to_utc("2026-10-25 02:30:00", "Europe/Berlin")
        self.assertIsInstance(ambiguous, datetime)

    def test_an_unparseable_timestamp_is_none_not_an_exception(self):
        self.assertIsNone(tt.dp_to_utc("not a timestamp", "Asia/Dhaka"))
        self.assertIsNone(tt.dp_to_utc("", "Asia/Dhaka"))
        self.assertIsNone(tt.dp_to_utc(None, "Asia/Dhaka"))

    def test_an_explicit_offset_is_honoured_over_the_backend_timezone(self):
        """A self-describing string means what it says, whatever the backend is set to."""
        self.assertEqual(
            tt.dp_to_utc("2026-08-01T09:15:00+02:00", "Asia/Dhaka"),
            datetime(2026, 8, 1, 7, 15))

    def test_window_parameters_are_local_wall_time_not_utc(self):
        """`start`/`end` are parsed in the API server's own application timezone.

        Sending UTC would shift the requested window by the offset -- six hours, by
        default -- which is why this is asserted rather than assumed.
        """
        param = tt.utc_to_dp_param(datetime(2026, 8, 1, 3, 15), "Asia/Dhaka")
        self.assertEqual(param, "2026-08-01 09:15:00")
        self.assertNotEqual(param, "2026-08-01 03:15:00")

    def test_window_parameter_round_trips(self):
        moment = datetime(2026, 8, 1, 3, 15)
        self.assertEqual(
            tt.dp_to_utc(tt.utc_to_dp_param(moment, "Asia/Dhaka"), "Asia/Dhaka"),
            moment)

    def test_unknown_timezone_falls_back_to_the_device_portal_default(self):
        """Falling back to UTC would move every punch by six hours."""
        self.assertEqual(str(tt.to_tz("Not/AZone")), "Asia/Dhaka")
        self.assertEqual(str(tt.to_tz(None)), "Asia/Dhaka")

    # -- HRM: epoch milliseconds ---------------------------------------------------------

    def test_millis_round_trip(self):
        moment = datetime(2026, 8, 1, 3, 15, 30)
        self.assertEqual(tt.millis_to_utc(tt.utc_to_millis(moment)), moment)

    def test_millis_accepts_a_string_because_json_keys_are_strings(self):
        millis = tt.utc_to_millis(datetime(2026, 8, 1))
        self.assertEqual(tt.millis_to_utc(str(millis)), datetime(2026, 8, 1))

    def test_empty_and_zero_millis_are_none(self):
        for value in (0, None, "", "nonsense", -1):
            self.assertIsNone(tt.millis_to_utc(value), value)

    def test_day_keys_are_read_at_the_fixed_offset(self):
        """The key's UTC instant is the previous evening.

        Start-of-day at +06:00 on 2026-08-01 is 2026-07-31 18:00 UTC, so reading the key
        as UTC would land a day early for every row in the payload.
        """
        key = tt.date_to_hrm_millis(date(2026, 8, 1))
        self.assertEqual(tt.millis_to_utc(key), datetime(2026, 7, 31, 18, 0))
        self.assertEqual(tt.hrm_day_to_date(key), date(2026, 8, 1))

    def test_end_of_day_key_is_still_the_same_local_date(self):
        key = tt.date_to_hrm_millis(date(2026, 8, 1), end_of_day=True)
        self.assertEqual(tt.hrm_day_to_date(key), date(2026, 8, 1))
        self.assertGreater(key, tt.date_to_hrm_millis(date(2026, 8, 1)))

    # -- HRM: the display strings --------------------------------------------------------

    def test_twelve_hour_display_strings_parse(self):
        self.assertEqual(tt.parse_hrm_time("09:15 AM"), time(9, 15))
        self.assertEqual(tt.parse_hrm_time("05:30 PM"), time(17, 30))
        self.assertEqual(tt.parse_hrm_time("12:00 AM"), time(0, 0))
        self.assertEqual(tt.parse_hrm_time("12:00 PM"), time(12, 0))

    def test_twenty_four_hour_strings_also_parse(self):
        """Tolerant on purpose: this is a display string and could be reformatted."""
        self.assertEqual(tt.parse_hrm_time("18:45"), time(18, 45))
        self.assertEqual(tt.parse_hrm_time("18:45:30"), time(18, 45, 30))

    def test_the_dash_sentinel_means_no_punch_not_a_failure(self):
        """The API writes a literal "-" when there is no punch to report."""
        self.assertIsNone(tt.parse_hrm_time("-"))
        self.assertIsNone(tt.parse_hrm_time(""))
        self.assertIsNone(tt.parse_hrm_time(None))

    # -- HRM: building the span ----------------------------------------------------------

    def test_a_normal_day_becomes_a_utc_span(self):
        key = tt.date_to_hrm_millis(date(2026, 8, 1))
        check_in, check_out = tt.hrm_day_span(key, "09:05 AM", "06:30 PM")
        self.assertEqual(check_in, datetime(2026, 8, 1, 3, 5))
        self.assertEqual(check_out, datetime(2026, 8, 1, 12, 30))

    def test_an_overnight_day_crosses_midnight_instead_of_going_negative(self):
        """The exit string is earlier in the day than the entry.

        Without moving the exit to the following date the span is negative, and
        `hr.attendance` refuses it outright -- so this rule is what makes a night shift
        importable at all.
        """
        key = tt.date_to_hrm_millis(date(2026, 8, 1))
        check_in, check_out = tt.hrm_day_span(key, "10:00 PM", "06:00 AM")
        self.assertEqual(check_in, datetime(2026, 8, 1, 16, 0))
        self.assertEqual(check_out, datetime(2026, 8, 2, 0, 0))
        self.assertGreater(check_out, check_in)

    def test_a_missing_exit_yields_a_check_in_and_nothing_else(self):
        key = tt.date_to_hrm_millis(date(2026, 8, 1))
        check_in, check_out = tt.hrm_day_span(key, "09:05 AM", "-")
        self.assertEqual(check_in, datetime(2026, 8, 1, 3, 5))
        self.assertIsNone(check_out)

    def test_a_missing_entry_yields_neither(self):
        key = tt.date_to_hrm_millis(date(2026, 8, 1))
        self.assertEqual(tt.hrm_day_span(key, "-", "06:30 PM"), (None, None))

    def test_the_offset_is_a_constant_because_it_is_one_upstream(self):
        """Exposing it as a setting would invite someone to shift every day row."""
        self.assertEqual(tt.HRM_UTC_OFFSET.total_seconds(), 6 * 3600)
