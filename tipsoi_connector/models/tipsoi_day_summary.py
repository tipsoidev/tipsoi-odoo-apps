# -*- coding: utf-8 -*-
"""Device Portal day-wise attendance: one row per employee per day, derived in Odoo.

The Device Portal has no day endpoint. It serves raw punches, `tipsoi.punch.log` pairs
them into `hr.attendance`, and this model rolls those pairs up into the day-wise view a
Device Portal customer otherwise never gets -- `tipsoi.day.attendance` is the Tipsoi app's
feed and stays empty on this backend by design.

**This model reads `hr.attendance`; it never writes it.** That is the whole design, and it
is the opposite direction from `tipsoi.day.attendance`, which stages what the app reported
and then *creates* attendance from it. Here pairing is already the writer. A second writer
on the same employee-day would collide on `hr.attendance`'s own overlap constraint, and the
one that lost would look like a random import failure. So the derivation runs one way:

    punches -> hr.attendance (tipsoi.punch.log._pair) -> this summary

which also means a row here can always be thrown away and rebuilt, and that rebuilding it
can never damage attendance.

**What this model deliberately does not have.** No `is_leave`, `is_holiday`, `is_offday` or
`overtime_minutes`. The Tipsoi app reports all four; the Device Portal has no leave, roster
or holiday feed whatsoever, and a stored `False` on a field named `is_leave` reads as "not
on leave" rather than "we have no idea". Better to be missing than to be confidently wrong.

**Why absences are opt-in.** See `generate_absences` on the backend. An absence is the one
thing here that is not evidence of anything -- it is asserted from a working calendar that
nobody has necessarily checked, on employees the Device Portal sync created without setting
one. Until an administrator says the calendars are right, this model reports only what the
punches show.

**Day boundary.** The local date of the *check-in*, in the backend's source timezone. That
is what makes an overnight shift belong wholly to the day it started rather than splitting
at midnight -- the same principle `max_shift_hours` protects during pairing.
"""

import logging
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models

from . import tipsoi_schedule, tipsoi_time

_logger = logging.getLogger(__name__)


class TipsoiDaySummary(models.Model):
    _name = "tipsoi.day.summary"
    _description = "Tipsoi Daily Attendance (Device Portal)"
    _order = "day_date desc, id desc"

    backend_id = fields.Many2one(
        "tipsoi.backend", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="backend_id.company_id", store=True, index=True)
    name = fields.Char(compute="_compute_name", store=True)

    employee_id = fields.Many2one(
        "hr.employee", required=True, ondelete="cascade", index=True)
    employee_identifier = fields.Char(
        related="employee_id.tipsoi_identifier", store=True, index=True,
        string="Tipsoi identifier")
    day_date = fields.Date(
        required=True, index=True, string="Day",
        help="The local calendar date in the backend's source timezone, taken from the "
             "check-in. An overnight shift belongs to the day it started.")

    check_in_utc = fields.Datetime(readonly=True, string="Check in (UTC)")
    check_out_utc = fields.Datetime(readonly=True, string="Check out (UTC)")

    first_punch_utc = fields.Datetime(
        readonly=True, string="First punch (UTC)",
        help="The earliest raw punch belonging to this day, paired or not. Deliberately "
             "separate from Check in: Check in is what pairing produced, and a day the "
             "device recorded a single punch on has a first punch and no check-in at "
             "all. This is the column to reconcile against the Tipsoi panel.")
    last_punch_utc = fields.Datetime(
        readonly=True, string="Last punch (UTC)",
        help="The latest raw punch belonging to this day. Equal to the first punch when "
             "the device recorded only one.")

    check_in_display = fields.Datetime(
        compute="_compute_check_in_display", store=True, readonly=True,
        string="Check in",
        help="What this day shows as an arrival: the paired check-in where pairing "
             "produced one, and the day's first raw punch where it has not. This is the "
             "column to read on a day that is still running -- a check-in only becomes a "
             "check-in once its exit punch arrives, so until then the paired field is "
             "empty and the person looks absent.\n\n"
             "Deliberately a second field rather than a wider Check in. The paired value "
             "is what lateness is judged on, and folding an unpaired punch into it would "
             "move somebody's lateness as a side effect of a display change rather than "
             "as a decision somebody made.")

    worked_hours = fields.Float(
        readonly=True, string="Worked",
        help="The day's attendance hours added up, so a break between two pairs is "
             "excluded.")
    span_hours = fields.Float(
        readonly=True, string="On site",
        help="Last check-out minus first check-in, so a break is included. Where this "
             "exceeds Worked, the difference is time the employee was off the clock -- "
             "a distinction the Tipsoi app's own day feed cannot make.")
    expected_hours = fields.Float(
        readonly=True, string="Expected",
        help="What the working calendar says was owed. Zero means the calendar has no "
             "hours for this day, which is also why lateness is not judged on it.")

    attendance_ids = fields.Many2many(
        "hr.attendance", string="Attendance records", readonly=True,
        help="The records this day summarises. They are pairing's output, not this "
             "model's -- nothing here creates or edits them.")
    attendance_count = fields.Integer(readonly=True, string="Pairs")
    punch_count = fields.Integer(
        readonly=True, string="Punches",
        help="Raw punches belonging to this day. A punch that paired belongs to the day "
             "its shift started, so an overnight shift's exit punch counts here rather "
             "than on the following morning. More punches than twice the pairs means the "
             "day had breaks or a punch that has not paired yet.")

    day_type = fields.Selection(
        [("present", "Present"),
         ("partial", "Incomplete"),
         ("absent", "Absent")],
        required=True, default="present", index=True,
        help="Incomplete means punches arrived but the day has no closed pair yet -- "
             "usually an entry still waiting for its exit.")

    is_late = fields.Boolean(readonly=True, string="Late")
    late_minutes = fields.Integer(readonly=True, string="Late by (min)")
    is_early = fields.Boolean(readonly=True, string="Early exit")
    early_minutes = fields.Integer(readonly=True, string="Left early by (min)")
    has_break = fields.Boolean(
        readonly=True, string="Had a break",
        help="On site exceeds worked, so the day had a gap between two pairs. Stored "
             "rather than compared in the view: a view condition has to be expressible "
             "as a domain on the older series, and a domain cannot subtract one field "
             "from another.")
    is_short = fields.Boolean(
        readonly=True, string="Short hours",
        help="Worked less than the working calendar expected. Stored rather than worked "
             "out when the list is filtered, because an Odoo domain compares a field to "
             "a value and cannot compare it to another field.")

    state_reason = fields.Text(readonly=True)

    _sql_constraints = [
        ("uniq_backend_employee_day",
         "unique(backend_id, employee_id, day_date)",
         "This employee already has a day row for that date on this backend."),
    ]

    # ----------------------------------------------------------------------------------
    # display
    # ----------------------------------------------------------------------------------

    @api.depends("check_in_utc", "first_punch_utc")
    def _compute_check_in_display(self):
        """Deliberately absent from `_COMPARED`.

        `_unchanged` indexes `vals[field]` for every name there, and this one never
        appears in `vals` -- it is derived by the ORM from two fields that do. Listing it
        would raise a `KeyError` on the absence path, which is the least-travelled branch
        in the build and so the last place anybody would find it.
        """
        for row in self:
            row.check_in_display = row.check_in_utc or row.first_punch_utc

    @api.depends("employee_id", "day_date")
    def _compute_name(self):
        for row in self:
            row.name = "%s %s" % (row.employee_id.display_name or "", row.day_date or "")

    def action_open_attendance(self):
        """Open the attendance behind this day: the form for one, the list for several."""
        self.ensure_one()
        if not self.attendance_ids:
            raise_reason = self.state_reason or _("No attendance was recorded.")
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {"message": raise_reason, "type": "warning"},
            }
        if len(self.attendance_ids) == 1:
            return {
                "type": "ir.actions.act_window",
                "res_model": "hr.attendance",
                "res_id": self.attendance_ids.id,
                "view_mode": "form",
            }
        return {
            "type": "ir.actions.act_window",
            "name": _("Attendance"),
            "res_model": "hr.attendance",
            "domain": [("id", "in", self.attendance_ids.ids)],
            "view_mode": "list,form",
        }

    def action_open_punches(self):
        """The raw punches behind this day -- the first stop when a number looks wrong.

        The domain mirrors how `_punch_index` buckets, and it has to: filing on
        `punch_day` alone would show a different set from the one Punches counts, so an
        overnight shift would open two punches on a row that says three.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Punches"),
            "res_model": "tipsoi.punch.log",
            "domain": [("backend_id", "=", self.backend_id.id),
                       ("employee_id", "=", self.employee_id.id),
                       "|",
                       ("attendance_id", "in", self.attendance_ids.ids),
                       "&", ("attendance_id", "=", False),
                       ("punch_day", "=", self.day_date)],
            "view_mode": "list,form",
        }

    # ----------------------------------------------------------------------------------
    # building
    # ----------------------------------------------------------------------------------

    @api.model
    def _build(self, backend, run, window_from, window_to):
        """Rebuild every day row in the window. Idempotent, and safe to re-run.

        A rolling rebuild rather than a cursor, for the reason `_attendance_window` gives
        on the backend: these rows are derived, so a punch that arrives late restates a
        day that was already computed. There is no "changed since" to cursor on.
        """
        tz = tipsoi_time.to_tz(backend._source_tz())
        first_day, last_day = self._window_days(tz, window_from, window_to)
        if first_day > last_day:
            return True

        employees = self.env["hr.employee"].with_context(
            active_test=False).search([("tipsoi_backend_id", "=", backend.id)])
        if not employees:
            return True

        # Widened by a shift on each side, then filtered back: a shift that started the
        # evening before the window still belongs to its own start day, and reading only
        # the window's own UTC span would cut it in half.
        margin = timedelta(hours=max(backend.max_shift_hours, 1))
        start_utc = self._local_midnight_utc(tz, first_day) - margin
        end_utc = self._local_midnight_utc(tz, last_day + timedelta(days=1)) + margin

        by_employee_day, day_by_attendance = self._attendance_index(
            employees, start_utc, end_utc, tz, first_day, last_day)
        punches_by_key, absence_bounds = self._punch_index(
            backend, employees, first_day, last_day, day_by_attendance)

        existing = {
            (row.employee_id.id, row.day_date): row
            for row in self.search([
                ("backend_id", "=", backend.id),
                ("employee_id", "in", employees.ids),
                ("day_date", ">=", first_day),
                ("day_date", "<=", last_day)])
        }

        counters = dict(fetched=0, created=0, updated=0, skipped=0, failed=0)
        # New rows are collected and created in one call. A backfill over a quarter is
        # employees times days, which one-at-a-time creates turn into five figures of
        # round trips -- slow enough that the operator assumes it has hung.
        pending = []
        for employee in employees:
            slots, two_weeks, cal_tz = self._calendar_of(employee, backend)
            for offset in range((last_day - first_day).days + 1):
                day = first_day + timedelta(days=offset)
                key = (employee.id, day)
                vals = self._day_values(
                    backend, employee, day, tz, cal_tz,
                    by_employee_day.get(key, self.env["hr.attendance"]),
                    punches_by_key.get(key, (0, None, None)),
                    absence_bounds.get(employee.id),
                    slots, two_weeks)
                new = self._upsert(
                    backend, employee, day, vals, existing.get(key), counters)
                if new:
                    pending.append(new)
        if pending:
            self.create(pending)

        run.fetched += counters.pop("fetched")
        run.created += counters["created"]
        run.updated += counters["updated"]
        run.skipped += counters["skipped"]
        run.failed += counters["failed"]
        return True

    # -- gathering ---------------------------------------------------------------------

    @api.model
    def _window_days(self, tz, window_from, window_to):
        """The window's first and last *local* dates."""
        def local_date(moment):
            return pytz.UTC.localize(moment).astimezone(tz).date()
        return local_date(window_from), local_date(window_to)

    @api.model
    def _local_midnight_utc(self, tz, day):
        """Naive UTC for local midnight starting `day`."""
        return tz.localize(
            datetime.combine(day, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)

    @api.model
    def _attendance_index(self, employees, start_utc, end_utc, tz, first_day, last_day):
        """`({(employee_id, local_day): attendances}, {attendance_id: local_day})`.

        Both are keyed on the check-in's local day. The second is what lets the punch side
        agree with this one: a punch that paired belongs to the day its shift *started*,
        not to the day the punch itself happened.

        Records with no check-in are dropped rather than guessed at: without one there is
        no day to file them under, and inventing one would move somebody's hours.
        """
        attendances = self.env["hr.attendance"].sudo().search([
            ("employee_id", "in", employees.ids),
            ("check_in", ">=", start_utc),
            ("check_in", "<=", end_utc),
        ], order="check_in asc")

        index = {}
        day_by_attendance = {}
        for attendance in attendances:
            if not attendance.check_in:
                continue
            day = pytz.UTC.localize(attendance.check_in).astimezone(tz).date()
            # Recorded before the window filter below, and on purpose: a punch paired to a
            # shift that started outside the window still has to be *recognisable* as
            # such, so it can be dropped rather than fall back to its own date and invent
            # a row on a morning nobody worked.
            day_by_attendance[attendance.id] = day
            if day < first_day or day > last_day:
                continue
            index.setdefault((attendance.employee_id.id, day),
                             self.env["hr.attendance"])
            index[(attendance.employee_id.id, day)] |= attendance
        return index, day_by_attendance

    @api.model
    def _punch_index(self, backend, employees, first_day, last_day, day_by_attendance):
        """`({(employee_id, day): (count, first, last)}, {employee_id: (floor, ceiling)})`.

        **Which day a punch belongs to.** A punch that paired belongs to the local day its
        attendance *started*; a punch that did not belongs to its own `punch_day`. Filing
        every punch under its own date instead is what used to manufacture a phantom
        Incomplete row on the morning after an overnight shift: the 06:00 exit landed on a
        day whose attendance sat on the day before, so that morning looked like a punch
        that never paired when it had paired perfectly -- and looked identical to a real
        single-punch day, which is the one thing this view has to be able to show.

        The search runs a day wider than the window on each side and the results are
        filtered back, because a shift starting 22:00 on the last day keeps its exit punch
        on the day after. A punch whose attendance is missing from `day_by_attendance` is
        dropped, and the invariant has to be read in this direction: *every* attendance
        whose check-in day falls inside the window is loaded, so being missing proves the
        shift started outside it and the punch belongs to a day this run is not building.
        It does not run the other way -- punches come from the widened range, and one on
        the day before the window can legitimately pair to an attendance two days back
        that was never searched for.

        The second return is the absence floor and ceiling. Somebody who had not yet been
        enrolled on a device was not absent -- there was simply nothing to record them
        with, and marking those days absent would invent a fortnight of nothing for every
        new hire.

        A `search` and a Python tally rather than `read_group`: the grouping API's name
        and signature both moved inside the series range this module ships on, and the
        window is one employee-set over a few days, so the aggregation is not worth a
        portability problem. Not `search_read` either -- reading `attendance_id` through
        it pulls a display name for every punch, and on a quarter-long backfill that is
        tens of thousands of dates formatted and thrown away.
        """
        punches = self.env["tipsoi.punch.log"].search([
            ("backend_id", "=", backend.id),
            ("employee_id", "in", employees.ids),
            ("punch_day", ">=", first_day - timedelta(days=1)),
            ("punch_day", "<=", last_day + timedelta(days=1)),
        ])
        counts = {}
        for punch in punches:
            if not punch.employee_id or not punch.punch_day:
                continue
            attendance_id = punch.attendance_id.id
            if attendance_id:
                day = day_by_attendance.get(attendance_id)
                if day is None:
                    continue
            else:
                day = punch.punch_day
            if day < first_day or day > last_day:
                continue
            key = (punch.employee_id.id, day)
            count, first, last = counts.get(key, (0, None, None))
            count += 1
            moment = punch.punch_time_utc or None
            if moment:
                if first is None or moment < first:
                    first = moment
                if last is None or moment > last:
                    last = moment
            counts[key] = (count, first, last)

        bounds = {}
        if backend.generate_absences:
            # Only needed to justify an absence, so it is not paid for otherwise. Two
            # indexed lookups per employee, each returning a single row.
            Punch = self.env["tipsoi.punch.log"]
            for employee in employees:
                domain = [("backend_id", "=", backend.id),
                          ("employee_id", "=", employee.id),
                          ("punch_day", "!=", False)]
                first = Punch.search(domain, order="punch_day asc", limit=1)
                if not first:
                    continue
                bounds[employee.id] = (
                    first.punch_day, self._departure_day(employee, backend, domain))
        return counts, bounds

    @api.model
    def _departure_day(self, employee, backend, punch_domain):
        """The last day this person could have been absent, or None while they are here.

        Without a ceiling, somebody who left in March keeps acquiring a fresh absence
        every working day for as long as the database exists -- and each one is a claim
        that a former employee failed to turn up. `active` is the signal: a Device Portal
        departure archives the employee (`action_tipsoi_depart`), and archived people
        stay in this loop on purpose so their real history still rebuilds.
        """
        if employee.active:
            return None
        # `departure_date` is Odoo's own field and the most honest answer when HR filled
        # it in. Read defensively: this module ships on five series.
        departure = getattr(employee, "departure_date", False)
        if departure:
            return departure
        # Nobody recorded a leaving date, so the last time they were seen on a device is
        # the best evidence available. Guessing later would invent the absences this
        # whole ceiling exists to prevent.
        last = self.env["tipsoi.punch.log"].search(
            punch_domain, order="punch_day desc", limit=1)
        return last.punch_day if last else None

    @api.model
    def _calendar_of(self, employee, backend):
        """`(slots, two_weeks, tz_name)` for an employee's working calendar.

        Falls back to the company's, then to nothing at all. Nothing at all is a real
        answer here and not a failure: it means no day can be called absent, which is
        the correct outcome when there is no schedule to be absent from.
        """
        calendar = employee.resource_calendar_id or employee.company_id.resource_calendar_id
        if not calendar:
            return [], False, backend._source_tz()

        slots = []
        for line in calendar.attendance_ids:
            # `display_type` marks the section headers a two-week calendar shows in the
            # form; they carry no hours. Read defensively -- the field arrived partway
            # through the series range this module supports.
            if getattr(line, "display_type", False):
                continue
            slots.append(tipsoi_schedule.Slot(
                dayofweek=line.dayofweek,
                hour_from=line.hour_from,
                hour_to=line.hour_to,
                date_from=getattr(line, "date_from", False) or None,
                date_to=getattr(line, "date_to", False) or None,
                week_type=getattr(line, "week_type", False),
                is_lunch=getattr(line, "day_period", False) == "lunch",
            ))
        return (slots,
                bool(getattr(calendar, "two_weeks_calendar", False)),
                calendar.tz or backend._source_tz())

    # -- one day -----------------------------------------------------------------------

    @api.model
    def _day_values(self, backend, employee, day, tz, cal_tz, attendances, punches,
                    bounds, slots, two_weeks):
        """The values this day *should* have. Returns None when there is no row to keep.

        `punches` is `(count, first, last)` from `_punch_index` -- the raw evidence, which
        exists on days pairing produced nothing for. That is the whole reason the first
        and last punch are reported separately from check-in and check-out: a day the
        device recorded once has a punch time and no attendance at all, and showing it a
        blank row is what makes it look like the punch never arrived.

        Nothing is written here. Separating the decision from the write is what makes the
        "did anything change?" comparison in `_upsert` possible, and that comparison is
        what keeps a fifteen-minute cron from rewriting every row of a quiet office.
        """
        punch_count, first_punch, last_punch = punches
        expected = tipsoi_schedule.expected_hours(slots, day, two_weeks)
        closed = attendances.filtered(lambda a: a.check_out)

        if not attendances and not punch_count:
            if not self._should_mark_absent(backend, day, bounds, slots, two_weeks):
                return None
            return {
                "day_type": "absent",
                "check_in_utc": False, "check_out_utc": False,
                "first_punch_utc": False, "last_punch_utc": False,
                "worked_hours": 0.0, "span_hours": 0.0, "expected_hours": expected,
                "attendance_ids": [(6, 0, [])],
                "attendance_count": 0, "punch_count": 0,
                "is_late": False, "late_minutes": 0,
                "is_early": False, "early_minutes": 0, "is_short": False,
                "has_break": False,
                "state_reason": _(
                    "No punch on a day the working calendar says was a working day."),
            }

        check_in = min(attendances.mapped("check_in")) if attendances else False
        check_out = max(closed.mapped("check_out")) if closed else False
        span = ((check_out - check_in).total_seconds() / 3600.0
                if check_in and check_out else 0.0)

        vals = {
            "day_type": "present" if closed else "partial",
            "check_in_utc": check_in or False,
            "check_out_utc": check_out or False,
            "first_punch_utc": first_punch or False,
            "last_punch_utc": last_punch or False,
            "worked_hours": round(sum(closed.mapped("worked_hours")), 2),
            "span_hours": round(span, 2),
            "expected_hours": expected,
            "attendance_ids": [(6, 0, attendances.ids)],
            "attendance_count": len(attendances),
            "punch_count": punch_count,
            "state_reason": False,
        }
        if not closed:
            vals["state_reason"] = self._incomplete_reason(
                punch_count, bool(attendances))
        vals.update(self._punctuality(
            backend, day, cal_tz, check_in, check_out, slots, two_weeks))
        # Short hours is the third calendar-derived judgement, so it waits on the same
        # confirmation the other two do, and it is only asked of a day that finished.
        vals["is_short"] = bool(
            backend.generate_absences
            and vals["day_type"] == "present"
            and expected > 0
            and vals["worked_hours"] + 0.005 < expected)
        # Two pairs with a gap between them. The Tipsoi app's own day feed reports only
        # first-in and last-out, so this is a distinction only the derived view can make.
        vals["has_break"] = bool(vals["span_hours"] > vals["worked_hours"] + 0.02)
        return vals

    @api.model
    def _incomplete_reason(self, punch_count, has_attendance):
        """Why a day has no finished pair, in the terms of what actually arrived.

        Split by case rather than worded once for all of them, because the cases want
        different things done about them: an open entry wants waiting, a single punch
        wants nothing at all, and several punches that would not pair want looking at.
        A reader who cannot tell which one they have opens the punches every time.
        """
        if has_attendance:
            return _(
                "An entry is open with no exit recorded against it yet. It closes itself "
                "as soon as the exit punch arrives.")
        if punch_count == 1:
            return _(
                "The device recorded one punch on this day and no second one, so there "
                "is nothing to pair into attendance. The punch itself is shown as First "
                "punch. No hours are claimed for it, because none can be worked out from "
                "a single reading.")
        if punch_count:
            return _(
                "%s punches arrived on this day and none of them paired into an entry "
                "and an exit. Open the punches to see which ones and why.", punch_count)
        return False

    @api.model
    def _should_mark_absent(self, backend, day, bounds, slots, two_weeks):
        """Whether a day with no evidence at all may be called an absence.

        `bounds` is `(first punch day, last day they were employed or None)`. An absence
        is only defensible inside that window: outside it the person had no obligation to
        be anywhere, so "did not turn up" is not a fact about them.
        """
        if not backend.generate_absences:
            return False
        if not slots:
            # No calendar means no working day, and no working day means no absence.
            return False
        if not tipsoi_schedule.slots_for_day(slots, day, two_weeks):
            return False
        if not bounds:
            # Never punched on this backend, so never enrolled on a device here.
            return False
        floor, ceiling = bounds
        if floor is None or day < floor:
            # Before this person's first ever punch they were not absent, they were not
            # yet enrolled.
            return False
        if ceiling is not None and day > ceiling:
            # After they left. Somebody who has gone is not absent.
            return False
        return True

    @api.model
    def _punctuality(self, backend, day, cal_tz, check_in, check_out, slots, two_weeks):
        """Late and early-exit, or all-clear when there is nothing to judge against.

        Measured in the *calendar's* timezone, not the device's: `hour_from = 8.0` means
        08:00 wherever the calendar says it lives, and the two are only the same string
        when somebody has configured them that way.
        """
        blank = {"is_late": False, "late_minutes": 0,
                 "is_early": False, "early_minutes": 0}
        # `is_short` is set by the caller, which is the only place that knows both the
        # worked total and whether the day actually closed.
        if not backend.generate_absences or not slots:
            # Tied to the same switch as absences on purpose: both are claims about a
            # working calendar, so both wait for the same confirmation that it is right.
            return blank

        start, end = tipsoi_schedule.expected_span(slots, day, two_weeks)
        if start is None:
            # Worked a day the calendar calls off. Real, and common -- overtime, a
            # weekend callout. It is not lateness, so it is not flagged as any.
            return blank

        result = dict(blank)
        tz = tipsoi_time.to_tz(cal_tz)
        if check_in:
            local_in = pytz.UTC.localize(check_in).astimezone(tz)
            minutes = tipsoi_schedule.hours_between(start, local_in)
            if minutes > 0:
                result.update(is_late=True, late_minutes=minutes)
        if check_out:
            local_out = pytz.UTC.localize(check_out).astimezone(tz)
            minutes = tipsoi_schedule.hours_between(end, local_out)
            if minutes < 0:
                result.update(is_early=True, early_minutes=-minutes)
        return result

    # -- writing -----------------------------------------------------------------------

    #: Compared to decide whether a stored row still matches what was just computed.
    #: `attendance_ids` is handled separately, being a command list rather than a value.
    _COMPARED = ("day_type", "check_in_utc", "check_out_utc", "first_punch_utc",
                 "last_punch_utc", "worked_hours", "span_hours", "expected_hours",
                 "attendance_count", "punch_count", "is_late", "late_minutes",
                 "is_early", "early_minutes", "is_short", "has_break", "state_reason")

    @api.model
    def _upsert(self, backend, employee, day, vals, row, counters):
        """Decide this row's fate. Returns create-values for a new row, else None.

        Updates and deletes happen here; creates are handed back to be batched by the
        caller. That asymmetry is worth it -- an update touches one row that already
        exists, while creates arrive in the thousands on a backfill.
        """
        counters["fetched"] += 1
        if vals is None:
            if row:
                # The evidence for this row is gone: attendance was deleted, or absences
                # were switched off. Leaving it would be a stale claim that nothing would
                # ever come back to correct.
                row.unlink()
                counters["updated"] += 1
            else:
                counters["skipped"] += 1
            return None

        if not row:
            counters["created"] += 1
            return dict(vals, backend_id=backend.id, employee_id=employee.id,
                        day_date=day)

        if self._unchanged(row, vals):
            counters["skipped"] += 1
            return None
        row.write(vals)
        counters["updated"] += 1
        return None

    @api.model
    def _unchanged(self, row, vals):
        for field in self._COMPARED:
            current = row[field]
            wanted = vals[field]
            if isinstance(current, float) or isinstance(wanted, float):
                if abs((current or 0.0) - (wanted or 0.0)) > 0.005:
                    return False
            elif (current or False) != (wanted or False):
                return False
        return set(row.attendance_ids.ids) == set(vals["attendance_ids"][0][2])
