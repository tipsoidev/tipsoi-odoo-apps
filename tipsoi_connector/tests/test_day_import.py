# -*- coding: utf-8 -*-
"""The Tipsoi app attendance feed, and the day import.

One paginated call carries identity, the org masters and a per-day grid together, so this
is the whole of this mode's sync. The rules pinned here:

* `from` and `to` are epoch milliseconds, and the attendance map is keyed by start-of-day
  at a *fixed* +06:00 -- reading a key as UTC lands a day early for every row;
* `firstLoggedTime` / `lastLoggedTime` are `hh:mm a` display strings at that same offset,
  with the literal `"-"` when there was no punch;
* the response is flat -- each response puts its rows at the top level under its own
  name, so there is no shared envelope key and no `data` wrapper;
* and the import **updates** the attendance it already created rather than adding a second
  one. A fifteen-minute cron over a three-day rolling window would otherwise duplicate
  every day, every time.
"""

from datetime import date, datetime, timedelta

from odoo import fields
from odoo.tests import tagged

from ..models import tipsoi_time as tt
from .common import Pages, TipsoiCase

DAY_ONE = date(2026, 8, 1)


def day_details(**overrides):
    day = {
        "firstLoggedTime": "09:05 AM",
        "lastLoggedTime": "06:30 PM",
        "totalHour": "9:25",
        "totalHourInMillis": 33900000,
        "overtimeInMinutes": 25,
        "attStatusText": "Present",
        "isPresent": True,
        "isLate": False,
        "isEarly": False,
        "isInadequate": False,
        "isLeave": False,
        "isHoliday": False,
        "isOffday": False,
        "isHalfDay": False,
        "entryManual": False,
        "exitManual": False,
        "leaveAppliedButNotApproved": False,
    }
    day.update(overrides)
    return day


def employee_row(days=None, **overrides):
    row = {
        "employeeId": 9001,
        "employeeName": "Rahim Uddin",
        "employeeOfficeId": "OFF-77A",
        "employeeIdentifier": "E-001",
        "departmentName": "Operations",
        "departmentExternalSyncId": "DEP-1",
        "designationName": "Supervisor",
        "shiftStartTime": "09:00 AM",
        "shiftEndTime": "06:00 PM",
        "status": "Active",
        # JSON object keys are strings, which is why the key is stringified here.
        "attendance": days if days is not None else {
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details()},
    }
    row.update(overrides)
    return row


def report(rows, current=1, last=1):
    """The attendance report -- flat, with no envelope key to unwrap."""
    return {"attendance": rows, "total": len(rows),
            "totalPages": last, "currentPage": current,
            "officeName": "Head office", "officeId": 456}


@tagged("post_install", "-at_install")
class TestHrmAttendanceSync(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm", tipsoi_office_id=456)
        self.Day = self.env["tipsoi.day.attendance"]

    def _sync(self, body=None, window_from=None, window_to=None):
        if body is not None:
            self.transport.route("GET", "attendance", body)
        with self._run(self.backend, "attendance") as run:
            self.Day._sync(self.backend,
                           run,
                           window_from or datetime(2026, 8, 1, 6, 0),
                           window_to or datetime(2026, 8, 3, 6, 0))
        return run

    def _import(self):
        with self._run(self.backend, "day_import") as run:
            self.Day._import_days(self.backend, run)
        return run

    def _days(self):
        return self.Day.search([("backend_id", "=", self.backend.id)])

    def _attendances(self, employee=None):
        employee = employee or self.env["hr.employee"].search(
            [("tipsoi_identifier", "=", "E-001")])
        return self.env["hr.attendance"].search(
            [("employee_id", "=", employee.id)], order="check_in")

    # -- the request -----------------------------------------------------------------------

    def test_the_window_is_sent_as_epoch_millis_with_the_office(self):
        self._sync(report([]))
        params = self.transport.calls[0]["params"]
        self.assertEqual(params["from"], tt.date_to_hrm_millis(date(2026, 8, 1)))
        self.assertEqual(params["to"],
                         tt.date_to_hrm_millis(date(2026, 8, 3), end_of_day=True))
        self.assertEqual(params["officeId"], 456)
        self.assertEqual(params["pageNumber"], 1)

    def test_pagination_follows_current_and_total_pages(self):
        self._sync(Pages([
            report([employee_row()], current=1, last=2),
            report([employee_row(
                employeeIdentifier="E-002", employeeId=9002,
                employeeName="Karim")], current=2, last=2),
        ]))
        self.assertEqual(self.transport.count_for("attendance", "GET"), 2)
        self.assertEqual(len(self._days()), 2)

    def test_this_mode_re_reads_a_window_rather_than_cursoring(self):
        """The rows are derived, so they change after the fact.

        A manual entry approved this morning rewrites yesterday's row; leave applied today
        rewrites last week's. There is no "updated since" to cursor on, so
        `last_attendance_sync` records only *when* the last read happened and must never
        narrow the next one -- narrowing it would skip exactly the changes the window
        exists to catch.

        Pinned by moving the cursor to three very different places and asserting the
        window does not budge, which states "not a cursor" far more strongly than any
        single comparison could.
        """
        self.backend.hrm_window_days = 3
        for cursor in (datetime(2020, 1, 1, 0, 0),     # long past
                       datetime(2030, 1, 1, 0, 0),     # implausibly future
                       False):                          # never read
            self.backend.last_attendance_sync = cursor
            window_from, window_to = self.backend._attendance_window()
            with self.subTest(cursor=cursor):
                self.assertEqual(window_to - window_from, timedelta(days=3),
                                 "the window is a fixed span of days")
                self.assertAlmostEqual(
                    (fields.Datetime.now() - window_to).total_seconds(), 0, delta=120,
                    msg="the window ends now, wherever the cursor sits")
                if cursor:
                    self.assertNotEqual(window_from, cursor)
                    self.assertNotEqual(window_to, cursor)

    def test_the_window_widens_only_with_its_own_setting(self):
        self.backend.hrm_window_days = 7
        window_from, window_to = self.backend._attendance_window()
        self.assertEqual(window_to - window_from, timedelta(days=7))

    def test_a_zero_window_still_reads_a_day(self):
        """A misconfigured zero must not silently read nothing."""
        self.backend.hrm_window_days = 0
        window_from, window_to = self.backend._attendance_window()
        self.assertEqual(window_to - window_from, timedelta(days=1))

    # -- staging ----------------------------------------------------------------------------

    def test_a_day_lands_with_its_key_read_at_the_fixed_offset(self):
        self._sync(report([employee_row()]))
        day = self._days()
        self.assertEqual(len(day), 1)
        self.assertEqual(day.day_date, DAY_ONE)
        # The raw key is kept, because the derived date is what everything else uses and
        # this is the only way to check it.
        self.assertEqual(day.day_epoch, str(tt.date_to_hrm_millis(DAY_ONE)))
        self.assertEqual(day.employee_identifier, "E-001")
        self.assertEqual(day.state, "new")

    def test_the_display_strings_become_a_utc_span(self):
        self._sync(report([employee_row()]))
        day = self._days()
        self.assertEqual(day.first_logged_raw, "09:05 AM")
        self.assertEqual(day.last_logged_raw, "06:30 PM")
        self.assertEqual(day.check_in_utc, datetime(2026, 8, 1, 3, 5))
        self.assertEqual(day.check_out_utc, datetime(2026, 8, 1, 12, 30))

    def test_hours_come_from_the_millis_not_the_display_string(self):
        self._sync(report([employee_row()]))
        day = self._days()
        self.assertEqual(day.total_hour_millis, 33900000)
        self.assertAlmostEqual(day.total_hours, 9.4166, places=3)
        self.assertEqual(day.overtime_minutes, 25)

    def test_the_status_flags_land_on_the_row(self):
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(
                isLate=True, isLeave=True, isHoliday=True, isHalfDay=True,
                entryManual=True, leaveAppliedButNotApproved=True,
                attStatusText="Late"),
        })]))
        day = self._days()
        self.assertTrue(day.is_late)
        self.assertTrue(day.is_leave)
        self.assertTrue(day.is_holiday)
        self.assertTrue(day.is_half_day)
        self.assertTrue(day.entry_manual)
        self.assertTrue(day.leave_pending)
        self.assertEqual(day.att_status_text, "Late")

    def test_the_employee_and_masters_arrive_with_the_days(self):
        run = self._sync(report([employee_row()]))
        employee = self.env["hr.employee"].search([("tipsoi_identifier", "=", "E-001")])
        self.assertEqual(len(employee), 1)
        self.assertEqual(employee.tipsoi_employee_office_id, "OFF-77A")
        self.assertEqual(employee.department_id.name, "Operations")
        self.assertEqual(self._days().employee_id, employee)
        # The run's totals cover employees and day rows together, so the breakdown is
        # recorded to keep the numbers interpretable.
        self.assertIn("day row", run.notes)

    def test_re_reading_the_same_window_updates_one_row(self):
        self._sync(report([employee_row()]))
        first_id = self._days().id
        run = self._sync(report([employee_row()]))
        self.assertEqual(len(self._days()), 1)
        self.assertEqual(self._days().id, first_id)
        self.assertEqual(run.created, 0)

    def test_an_unchanged_re_read_does_not_reopen_an_imported_day(self):
        """Otherwise every re-read of the window re-imports every day in it."""
        self._sync(report([employee_row()]))
        self._import()
        self.assertEqual(self._days().state, "imported")

        self._sync(report([employee_row()]))
        self.assertEqual(self._days().state, "imported")

    # -- import ------------------------------------------------------------------------------

    def test_a_present_day_becomes_an_attendance(self):
        self._sync(report([employee_row()]))
        run = self._import()

        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_in, datetime(2026, 8, 1, 3, 5))
        self.assertEqual(attendance.check_out, datetime(2026, 8, 1, 12, 30))
        self.assertEqual(self._days().state, "imported")
        self.assertEqual(self._days().attendance_id, attendance)
        self.assertEqual(run.created, 1)

    def test_a_day_with_no_exit_time_is_left_unpaired(self):
        """Never written as an open attendance: Odoo allows one per employee, so an
        unclosed day would block every following one."""
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(lastLoggedTime="-")})]))
        self._import()

        day = self._days()
        self.assertEqual(day.state, "unpaired")
        self.assertTrue(day.state_reason)
        self.assertFalse(day.attendance_id)
        self.assertEqual(len(self._attendances()), 0)

    def test_a_day_with_no_entry_time_is_left_unpaired(self):
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(firstLoggedTime="-")})]))
        self._import()
        self.assertEqual(self._days().state, "unpaired")
        self.assertEqual(len(self._attendances()), 0)

    def test_a_day_tipsoi_reports_as_absent_is_skipped(self):
        """This is what keeps leave, holidays and off days out of attendance."""
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(
                isPresent=False, isLeave=True, firstLoggedTime="-",
                lastLoggedTime="-", attStatusText="Leave")})]))
        run = self._import()

        day = self._days()
        self.assertEqual(day.state, "skipped")
        self.assertIn("Leave", day.state_reason)
        self.assertEqual(len(self._attendances()), 0)
        self.assertEqual(run.skipped, 1)

    def test_a_holiday_somebody_actually_worked_is_still_imported(self):
        """Upstream reports them present, and the punch times are real."""
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(
                isHoliday=True, isPresent=True)})]))
        self._import()
        self.assertEqual(self._days().state, "imported")
        self.assertEqual(len(self._attendances()), 1)

    def test_a_missing_present_flag_falls_back_to_the_times(self):
        """`isPresent` can legitimately be absent from the payload altogether.

        Defaulting it to False would let one upstream rename silently skip every day;
        defaulting to True would import leave and holidays as attendance.
        """
        day = day_details()
        day.pop("isPresent")
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day})]))
        self.assertTrue(self._days().is_present)
        self._import()
        self.assertEqual(len(self._attendances()), 1)

    def test_an_overnight_day_is_one_span_crossing_midnight(self):
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(
                firstLoggedTime="10:00 PM", lastLoggedTime="06:00 AM")})]))
        self._import()

        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_in, datetime(2026, 8, 1, 16, 0))
        self.assertEqual(attendance.check_out, datetime(2026, 8, 2, 0, 0))

    def test_a_day_with_no_odoo_employee_is_surfaced(self):
        """Built directly, because the sync itself creates the employee it needs."""
        row = self.Day.create({
            "backend_id": self.backend.id,
            "employee_identifier": "GHOST-9",
            "day_date": DAY_ONE,
            "check_in_utc": datetime(2026, 8, 1, 3, 0),
            "check_out_utc": datetime(2026, 8, 1, 12, 0),
            "is_present": True,
        })
        self._import()
        row.invalidate_cache()
        self.assertEqual(row.state, "unmatched")
        self.assertIn("GHOST-9", row.state_reason)

    # -- the idempotency guarantee -------------------------------------------------------------

    def test_an_approved_manual_entry_updates_the_day_it_belongs_to(self):
        """The case the rolling window exists for, and the one that must not duplicate."""
        self._sync(report([employee_row()]))
        self._import()
        original = self._attendances()
        self.assertEqual(len(original), 1)

        # Somebody approves a corrected exit time; the same day comes back changed.
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(DAY_ONE)): day_details(
                lastLoggedTime="07:30 PM", exitManual=True)})]))
        self.assertEqual(self._days().state, "new", "a changed day is reopened")
        self._import()

        after = self._attendances()
        self.assertEqual(len(self._days()), 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(after.ids, original.ids, "the same record, moved")
        self.assertEqual(after.check_out, datetime(2026, 8, 1, 13, 30))
        self.assertTrue(self._days().exit_manual)

    def test_a_third_cycle_changes_nothing(self):
        for _round in range(3):
            self._sync(report([employee_row()]))
            self._import()
        self.assertEqual(len(self._days()), 1)
        self.assertEqual(len(self._attendances()), 1)
        self.assertFalse(self.Day.search_count([
            ("backend_id", "=", self.backend.id), ("state", "=", "error")]))

    def test_several_days_for_one_employee_stay_separate(self):
        self._sync(report([employee_row(days={
            str(tt.date_to_hrm_millis(date(2026, 8, 1))): day_details(),
            str(tt.date_to_hrm_millis(date(2026, 8, 2))): day_details(),
            str(tt.date_to_hrm_millis(date(2026, 8, 3))): day_details(),
        })]))
        self._import()
        self.assertEqual(len(self._days()), 3)
        self.assertEqual(len(self._attendances()), 3)

    # -- contested days --------------------------------------------------------------------------

    def test_an_overlap_with_a_hand_typed_attendance_is_recorded_not_fatal(self):
        self._sync(report([employee_row()]))
        employee = self.env["hr.employee"].search([("tipsoi_identifier", "=", "E-001")])
        self.env["hr.attendance"].create({
            "employee_id": employee.id,
            "check_in": datetime(2026, 8, 1, 4, 0),
            "check_out": datetime(2026, 8, 1, 14, 0),
        })

        run = self._import()

        self.assertEqual(run.state, "partial")
        self.assertEqual(run.failed, 1)
        day = self._days()
        self.assertEqual(day.state, "error")
        self.assertTrue(day.state_reason)

    def test_a_day_in_error_is_retried_on_the_next_import(self):
        """`error` is in the importable set, so a fixed conflict resolves itself."""
        self._sync(report([employee_row()]))
        employee = self.env["hr.employee"].search([("tipsoi_identifier", "=", "E-001")])
        clash = self.env["hr.attendance"].create({
            "employee_id": employee.id,
            "check_in": datetime(2026, 8, 1, 4, 0),
            "check_out": datetime(2026, 8, 1, 14, 0),
        })
        self._import()
        self.assertEqual(self._days().state, "error")

        clash.unlink()
        self._import()
        self.assertEqual(self._days().state, "imported")


@tagged("post_install", "-at_install")
class TestHrmPunchDetail(TipsoiCase):
    """The per-employee repair path, which is deliberately never a scheduled poll."""

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm", tipsoi_office_id=456)
        self.employee = self._employee(
            "E-001", backend=self.backend, name="Rahim", tipsoi_employee_id=9001)
        self.day = self.env["tipsoi.day.attendance"].create({
            "backend_id": self.backend.id,
            "employee_identifier": "E-001",
            "employee_id": self.employee.id,
            "day_date": DAY_ONE,
            "is_present": True,
            "check_in_utc": datetime(2026, 8, 1, 3, 5),
            "check_out_utc": datetime(2026, 8, 1, 12, 30),
        })

    def _detail_body(self, entries):
        return {"name": "Rahim Uddin", "totalPages": 1,
                "attendances": [{"date": "2026-08-01", "entries": entries}]}

    def test_the_punch_feed_has_explicit_directions_and_epoch_times(self):
        """The detailed punch row beats the Device Portal's on every axis that matters:
        epoch millis instead of naive local strings, and an explicit `entryType`."""
        self.transport.route("GET", "attendance/detailed/both", self._detail_body([
            {"id": 1, "uid": "h1", "attendanceTime": 1785574800000, "entryType": 1,
             "punchType": 1, "deviceIdentifier": "HRM-0001", "location": "Lobby",
             "empIdentifier": "E-001"},
            {"id": 2, "uid": "h2", "attendanceTime": 1785607200000, "entryType": 2,
             "punchType": 2, "deviceIdentifier": "HRM-0001", "empIdentifier": "E-001"},
        ]))
        action = self.day.action_fetch_punch_detail()

        punches = self.env["tipsoi.punch.log"].browse(action["domain"][0][2])
        self.assertEqual(len(punches), 2)
        entry = punches.filtered(lambda p: p.tipsoi_log_id == "h1")
        self.assertEqual(entry.direction, "in")
        self.assertEqual(entry.punch_method, "fingerprint")
        self.assertEqual(entry.punch_time_utc, datetime(2026, 8, 1, 9, 0))
        self.assertEqual(entry.employee_id, self.employee)
        exit_ = punches.filtered(lambda p: p.tipsoi_log_id == "h2")
        self.assertEqual(exit_.direction, "out")
        self.assertEqual(exit_.punch_method, "card")

    def test_these_rows_are_never_marked_paired(self):
        """Pairing is a Device Portal concern; here the day row is the source."""
        self.transport.route("GET", "attendance/detailed/both", self._detail_body([
            {"uid": "h1", "attendanceTime": 1785574800000, "entryType": 1,
             "punchType": 1}]))
        self.day.action_fetch_punch_detail()
        punch = self.env["tipsoi.punch.log"].search([("tipsoi_log_id", "=", "h1")])
        self.assertEqual(punch.state, "matched")
        self.assertFalse(punch.attendance_id)

    def test_a_missing_punch_is_flagged_rather_than_discarded(self):
        """`punchType` 4 is MISSING -- upstream flags the gap instead of omitting it."""
        self.transport.route("GET", "attendance/detailed/both", self._detail_body([
            {"uid": "h3", "attendanceTime": 1785574800000, "entryType": 1,
             "punchType": 4}]))
        self.day.action_fetch_punch_detail()
        punch = self.env["tipsoi.punch.log"].search([("tipsoi_log_id", "=", "h3")])
        self.assertIn("missing", (punch.state_reason or "").lower())

    def test_the_request_is_bounded_to_one_employee_and_one_day(self):
        self.transport.route("GET", "attendance/detailed/both", self._detail_body([
            {"uid": "h1", "attendanceTime": 1785574800000, "entryType": 1,
             "punchType": 1}]))
        self.day.action_fetch_punch_detail()
        call = self.transport.call_for("attendance/detailed/both")
        self.assertIn("9001", call["url"])
        self.assertEqual(call["params"]["start"],
                         tt.date_to_hrm_millis(DAY_ONE))
        self.assertEqual(call["params"]["end"],
                         tt.date_to_hrm_millis(DAY_ONE, end_of_day=True))

    def test_fetching_twice_upserts_on_the_punch_id(self):
        self.transport.route("GET", "attendance/detailed/both", self._detail_body([
            {"uid": "h1", "attendanceTime": 1785574800000, "entryType": 1,
             "punchType": 1}]))
        self.day.action_fetch_punch_detail()
        self.day.action_fetch_punch_detail()
        self.assertEqual(
            self.env["tipsoi.punch.log"].search_count([("tipsoi_log_id", "=", "h1")]), 1)
