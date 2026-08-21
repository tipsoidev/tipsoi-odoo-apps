# -*- coding: utf-8 -*-
"""Tipsoi app (HRM) attendance: one staged row per employee per day.

`GET /attendance` is the whole of this mode's sync. One paginated call carries identity,
the full org master set with its external sync ids, *and* a per-day grid keyed by epoch
start-of-day -- so employees, masters and attendance all arrive together and there is no
second feed to keep in step.

**Why stage rows that already arrive computed.** Upstream has already paired each day,
so it would be possible to write `hr.attendance` straight from the response. Staging
them anyway is what makes the import replayable: the row records exactly what Tipsoi
said, so a failed or wrong import can be re-run without re-fetching, and a support
question about a number has an answer that does not depend on the API still returning
the same thing.

**Why this mode has a window and not a cursor.** These rows are *derived*. A manual
entry approved this morning changes yesterday's row; leave applied today rewrites last
week's. There is no "updated since" to cursor on, so the sync re-reads the last few days
every time and upserts. Re-reading is both simpler and more correct than trying to
cursor a computed view -- see `hrm_window_days` on the backend.

**Two timestamp traps, both handled in `tipsoi_time`.** The day keys are start-of-day at
a *fixed* `+06:00` -- an offset compiled into the API, not a named or configurable
timezone -- and `firstLoggedTime` / `lastLoggedTime` are `hh:mm a` **display strings** at that
same offset -- minute precision only, and the literal `"-"` when there was no punch.
Reading either as UTC shifts every row by six hours.
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import tipsoi_time

_logger = logging.getLogger(__name__)

#: States a row can be in when the import should look at it again. `unpaired` is
#: included because a day with no exit time is a day whose exit may still arrive -- a
#: late manual entry is exactly the case the rolling window exists to catch.
IMPORTABLE_STATES = ("new", "error", "unpaired")

#: `entryType` on the detailed punch row. Explicit direction, which is the biggest
#: advantage this punch feed has over the Device Portal's -- no alternating guesswork.
ENTRY_TYPE_DIRECTION = {1: "in", 2: "out"}

#: `punchType` on the detailed punch row. 4 is MISSING: upstream *flags* a gap rather than
#: silently omitting the row, so it is worth surfacing rather than discarding.
PUNCH_TYPE_MISSING = 4
PUNCH_TYPE_METHOD = {1: "fingerprint", 2: "card", 3: "other", 4: "other",
                     5: "other", 6: "other", 7: "other"}

#: One page is plenty for a single employee on a single day, and this endpoint must
#: never be turned into an office-wide poll -- it is per employee by construction.
PUNCH_DETAIL_PAGE_SIZE = 500


class TipsoiDayAttendance(models.Model):
    _name = "tipsoi.day.attendance"
    _description = "Tipsoi Daily Attendance"
    _order = "day_date desc, id desc"

    backend_id = fields.Many2one(
        "tipsoi.backend", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="backend_id.company_id", store=True, index=True)
    name = fields.Char(compute="_compute_name", store=True)

    employee_identifier = fields.Char(
        required=True, index=True,
        help="Tipsoi's `employeeIdentifier`. The join key, and the same string the "
             "Device Portal stores as a person's identifier.")
    employee_id = fields.Many2one(
        "hr.employee", ondelete="set null", index=True)

    day_date = fields.Date(
        required=True, index=True,
        help="The day this row covers, read from the attendance-map key at Tipsoi's "
             "fixed +06:00 offset.")
    day_epoch = fields.Char(
        readonly=True,
        help="The attendance-map key exactly as received. Kept for forensics: the "
             "derived date is what everything else uses, and this is how to check it.")

    # The two display strings, verbatim. Kept for the same reason the Device Portal's
    # raw punch time is kept: when a number looks wrong, the first question is what the
    # API actually said, and a parsed value cannot answer that.
    first_logged_raw = fields.Char(readonly=True, string="Entry (as reported)")
    last_logged_raw = fields.Char(readonly=True, string="Exit (as reported)")

    check_in_utc = fields.Datetime(readonly=True, string="Check in (UTC)")
    check_out_utc = fields.Datetime(readonly=True, string="Check out (UTC)")

    total_hours = fields.Float(readonly=True, string="Hours")
    total_hour_millis = fields.Integer(readonly=True)
    overtime_minutes = fields.Integer(readonly=True)
    att_status_text = fields.Char(readonly=True, string="Tipsoi status")

    is_present = fields.Boolean(readonly=True, string="Present")
    is_late = fields.Boolean(readonly=True, string="Late")
    is_early = fields.Boolean(readonly=True, string="Early exit")
    is_inadequate = fields.Boolean(readonly=True, string="Short hours")
    is_leave = fields.Boolean(readonly=True, string="On leave")
    is_holiday = fields.Boolean(readonly=True, string="Holiday")
    is_offday = fields.Boolean(readonly=True, string="Off day")
    is_half_day = fields.Boolean(readonly=True, string="Half day")
    entry_manual = fields.Boolean(readonly=True, string="Entry entered manually")
    exit_manual = fields.Boolean(readonly=True, string="Exit entered manually")
    leave_pending = fields.Boolean(
        readonly=True, string="Leave applied, not approved")

    state = fields.Selection(
        [("new", "New"),
         ("imported", "Imported"),
         ("skipped", "Skipped"),
         ("unpaired", "No usable times"),
         ("unmatched", "No employee"),
         ("error", "Error")],
        default="new", required=True, index=True)
    state_reason = fields.Text(readonly=True)
    attendance_id = fields.Many2one(
        "hr.attendance", ondelete="set null", index=True,
        help="The Odoo attendance this row produced. Holding the link is what makes a "
             "re-read update that record instead of adding a second one.")
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        # Keyed on the *date*, not the epoch the plan originally called for: an epoch in
        # milliseconds is around 1.7e12, and Odoo's Integer is an int4 in Postgres, so
        # storing it would overflow. The date is derived deterministically from the same
        # key at the fixed +06:00 offset, so it is the identical key in a form that
        # fits, and `day_epoch` keeps the original for checking.
        ("uniq_backend_employee_day",
         "unique(backend_id, employee_identifier, day_date)",
         "This day is already staged for this employee. Re-reading a day updates the "
         "existing row rather than adding another."),
    ]

    @api.depends("employee_id", "employee_identifier", "day_date")
    def _compute_name(self):
        for row in self:
            who = row.employee_id.name or row.employee_identifier or _("Unknown")
            row.name = "%s - %s" % (who, row.day_date or "")

    # ----------------------------------------------------------------------------------
    # sync: GET /attendance
    # ----------------------------------------------------------------------------------

    @api.model
    def _sync(self, backend, run, window_from, window_to):
        """Read the rolling window and upsert employees, masters and day rows.

        `from` and `to` are epoch milliseconds upstream, and the window is expressed in
        whole days at Tipsoi's fixed offset -- asking for a partial day would drop the
        day row that is still accumulating.
        """
        adapter = backend.client()
        params = {
            "from": tipsoi_time.date_to_hrm_millis(window_from.date()),
            "to": tipsoi_time.date_to_hrm_millis(window_to.date(), end_of_day=True),
        }
        if backend.tipsoi_office_id:
            params["officeId"] = backend.tipsoi_office_id

        Employee = self.env["hr.employee"]
        manager_map = {}
        employee_rows = day_rows = 0

        for page in adapter.paginate("attendance", params):
            for row in page:
                if not isinstance(row, dict):
                    run.skipped += 1
                    continue
                employee = Employee._upsert_from_hrm(backend, row, run)
                employee_rows += 1
                manager = self._int(row.get("lineManagerId"))
                if employee and manager:
                    manager_map[employee.id] = manager
                for day_key, day in (row.get("attendance") or {}).items():
                    if not isinstance(day, dict):
                        run.skipped += 1
                        continue
                    day_rows += 1
                    self._upsert_day(backend, employee, row, day_key, day, run)
            # Commit per page so a failure late in a long read does not discard the
            # pages that already landed. checkpoint() is a no-op inside a test.
            self.env["tipsoi.sync.run"].checkpoint()

        linked = Employee._link_hrm_managers(backend, manager_map)

        # The run's created/updated totals count employees and day rows together,
        # because both are upserted by this one job. The breakdown goes here so the
        # numbers stay interpretable.
        run.add_note(_(
            "%(employees)s employee row(s), %(days)s day row(s), "
            "%(managers)s line manager link(s).",
            employees=employee_rows, days=day_rows, managers=linked))
        return True

    @api.model
    def _upsert_day(self, backend, employee, employee_row, day_key, day, run):
        """Upsert one day, updating in place when it is already staged."""
        run.fetched += 1
        day_date = tipsoi_time.hrm_day_to_date(day_key)
        if not day_date:
            run.skipped += 1
            return self.browse()

        identifier = self._text(employee_row.get("employeeIdentifier"))
        if not identifier:
            run.skipped += 1
            return self.browse()

        first_raw = self._text(day.get("firstLoggedTime"))
        last_raw = self._text(day.get("lastLoggedTime"))
        check_in, check_out = tipsoi_time.hrm_day_span(day_key, first_raw, last_raw)
        millis = self._int(day.get("totalHourInMillis"))

        # `isPresent` is a boxed Boolean upstream, so it can legitimately be absent from
        # the payload. Odoo Booleans cannot hold "unknown", so an absent flag falls back
        # to whether there are usable times. Defaulting it to False instead would make a
        # single upstream rename silently skip every day; defaulting to True would import
        # leave and holidays as attendance.
        if "isPresent" in day:
            present = bool(day.get("isPresent"))
        else:
            present = bool(check_in)

        vals = {
            "backend_id": backend.id,
            "employee_identifier": identifier,
            "employee_id": employee.id if employee else False,
            "day_date": day_date,
            "day_epoch": str(day_key),
            "first_logged_raw": first_raw,
            "last_logged_raw": last_raw,
            "check_in_utc": check_in,
            "check_out_utc": check_out,
            "total_hour_millis": millis,
            "total_hours": (millis / 3600000.0) if millis else 0.0,
            "overtime_minutes": self._int(day.get("overtimeInMinutes")),
            "att_status_text": self._text(day.get("attStatusText")),
            "is_present": present,
            "is_late": bool(day.get("isLate")),
            "is_early": bool(day.get("isEarly")),
            "is_inadequate": bool(day.get("isInadequate")),
            "is_leave": bool(day.get("isLeave")),
            "is_holiday": bool(day.get("isHoliday")),
            "is_offday": bool(day.get("isOffday")),
            "is_half_day": bool(day.get("isHalfDay")),
            "entry_manual": bool(day.get("entryManual")),
            "exit_manual": bool(day.get("exitManual")),
            "leave_pending": bool(day.get("leaveAppliedButNotApproved")),
            "raw_payload": json.dumps(day, default=str, sort_keys=True),
        }

        existing = self.search([
            ("backend_id", "=", backend.id),
            ("employee_identifier", "=", identifier),
            ("day_date", "=", day_date),
        ], limit=1)

        try:
            with self.env.cr.savepoint():
                if existing:
                    # Only re-open the row for import when something that matters
                    # actually moved. Without this test every re-read of the window
                    # would re-import every day in it, which is a lot of writes to
                    # achieve nothing.
                    changed = (existing.check_in_utc != check_in
                               or existing.check_out_utc != check_out
                               or existing.is_present != present)
                    if changed or existing.state in ("error", "unmatched"):
                        vals["state"] = "new"
                        vals["state_reason"] = False
                    existing.write(vals)
                    run.updated += 1
                    return existing
                row = self.create(vals)
                run.created += 1
                return row
        except Exception as exc:            # noqa: BLE001 - counted, not swallowed
            run.failed += 1
            _logger.warning("Tipsoi day row %s/%s failed: %s",
                            identifier, day_date, exc)
            return self.browse()

    # ----------------------------------------------------------------------------------
    # import: staging -> hr.attendance
    # ----------------------------------------------------------------------------------

    @api.model
    def _import_days(self, backend, run):
        rows = self.search([
            ("backend_id", "=", backend.id),
            ("state", "in", IMPORTABLE_STATES),
        ])
        return self._import_rows(rows, run)

    @api.model
    def _import_rows(self, rows, run):
        """Import each row independently.

        One day that cannot be imported -- an overlap with an attendance somebody
        entered by hand, most often -- must not take the rest of the batch with it, so
        each row gets its own savepoint and its own recorded reason.
        """
        run.fetched += len(rows)
        for row in rows:
            try:
                # Used as a context manager on purpose: `Savepoint.close()` rolls back
                # by default, so the explicit-close idiom silently discards successful
                # work. `with` releases on a clean exit and rolls back on an exception.
                with self.env.cr.savepoint():
                    outcome = row._import_one()
            except ValidationError as exc:
                # The cache still holds values from the rolled-back attempt.
                self.env.cache.invalidate()
                row.write({"state": "error", "state_reason": str(exc)})
                run.failed += 1
            except Exception as exc:        # noqa: BLE001 - recorded, not swallowed
                self.env.cache.invalidate()
                row.write({"state": "error", "state_reason": str(exc)})
                run.failed += 1
                _logger.warning("Tipsoi day import failed for %s: %s", row.name, exc)
            else:
                if outcome == "imported":
                    run.created += 1
                else:
                    run.skipped += 1
        return True

    def _import_one(self):
        """Create or update the `hr.attendance` for this day. Returns the new state."""
        self.ensure_one()
        if not self.employee_id:
            self.write({
                "state": "unmatched",
                "state_reason": _(
                    "No Odoo employee carries the Tipsoi identifier %s. Set it on the "
                    "employee, or let the next sync create them.",
                    self.employee_identifier),
            })
            return "unmatched"

        if not self.is_present:
            # This is what keeps leave, holidays and off days out of attendance: on
            # those days upstream reports the employee as not present.
            self.write({
                "state": "skipped",
                "state_reason": _(
                    "Tipsoi reports no attendance on this day (%s).",
                    self.att_status_text or _("not present")),
            })
            return "skipped"

        if not self.check_in_utc:
            self.write({
                "state": "unpaired",
                "state_reason": _(
                    "No entry time on this day -- Tipsoi reported %s.",
                    self.first_logged_raw or _("nothing")),
            })
            return "unpaired"

        if not self.check_out_utc:
            # Deliberately never imported as an open attendance. Odoo allows only one
            # attendance without a check-out per employee, so creating one would block
            # every later day for that person until somebody closed it by hand.
            self.write({
                "state": "unpaired",
                "state_reason": _(
                    "Entry but no exit on this day -- Tipsoi reported %s. Left here "
                    "rather than imported: Odoo allows only one open attendance per "
                    "employee, so an unclosed day would block the following ones.",
                    self.last_logged_raw or _("nothing")),
            })
            return "unpaired"

        vals = {
            "employee_id": self.employee_id.id,
            "check_in": self.check_in_utc,
            "check_out": self.check_out_utc,
        }
        attendance = self.attendance_id
        if attendance and attendance.exists():
            # Update in place. This is the idempotency guarantee: re-reading the window
            # after a manual entry is approved changes this record rather than adding a
            # second one for the same day.
            if (attendance.check_in != self.check_in_utc
                    or attendance.check_out != self.check_out_utc):
                attendance.sudo().write(vals)
        else:
            # sudo: a Tipsoi administrator is not necessarily an HR officer, and the
            # attendance record is the connector's own output rather than something the
            # user is authoring.
            attendance = self.env["hr.attendance"].sudo().create(vals)

        self.write({
            "state": "imported",
            "state_reason": False,
            "attendance_id": attendance.id,
        })
        return "imported"

    # ----------------------------------------------------------------------------------
    # buttons
    # ----------------------------------------------------------------------------------

    def action_import_now(self):
        """Import the selected days immediately, grouped by backend."""
        for backend in self.mapped("backend_id"):
            rows = self.filtered(lambda r, b=backend: r.backend_id == b)
            with self.env["tipsoi.sync.run"].track(backend, "day_import") as run:
                self._import_rows(rows, run)
        return True

    def action_open_attendance(self):
        self.ensure_one()
        if not self.attendance_id:
            raise UserError(_(
                "This day has not produced an Odoo attendance record. %s",
                self.state_reason or ""))
        return {
            "type": "ir.actions.act_window",
            "res_model": "hr.attendance",
            "res_id": self.attendance_id.id,
            "view_mode": "form",
        }

    # ----------------------------------------------------------------------------------
    # repair: the per-employee punch feed
    # ----------------------------------------------------------------------------------

    def action_fetch_punch_detail(self):
        """Fetch the real punches behind this day, for one employee and one day.

        `firstLoggedTime` / `lastLoggedTime` are first-and-last for the day, so a lunch
        break is invisible in the staged row. This is the way to see what actually
        happened -- and it is deliberately a per-record action, because the endpoint is
        per employee and cannot drive an office-wide poll.

        This is a better punch row than the Device Portal's: epoch millis
        instead of naive local strings, an explicit `entryType` instead of alternating
        guesswork, two stable identifiers, and a flag on a *missing* punch rather than a
        silent omission.
        """
        self.ensure_one()
        backend = self.backend_id
        if backend.backend_type != "hrm":
            raise UserError(_(
                "This is the Tipsoi app's punch feed. A Device Portal backend already "
                "stores every raw punch."))
        if not self.employee_id or not self.employee_id.tipsoi_employee_id:
            raise UserError(_(
                "This day has no linked employee with a Tipsoi app employee ID, which "
                "the punch endpoint keys on. Run an attendance sync first."))

        body = backend.client().request(
            "GET", "attendance/detailed/both/%s" % self.employee_id.tipsoi_employee_id,
            params={
                # 0-based. Inferred, not confirmed: every other numeric parameter on
                # this endpoint has a minimum of 1 while `page` alone allows 0, which is
                # the usual convention for a zero-based page index.
                "page": 0,
                "count": PUNCH_DETAIL_PAGE_SIZE,
                "start": tipsoi_time.date_to_hrm_millis(self.day_date),
                "end": tipsoi_time.date_to_hrm_millis(self.day_date, end_of_day=True),
            })

        punches = self.env["tipsoi.punch.log"].browse()
        for entry in (body or {}).get("attendances") or []:
            if not isinstance(entry, dict):
                continue
            # Tolerant on the collection name: the response POJO names it once and the
            # published spec disagrees, and the rows are what matter.
            logs = (entry.get("entries") or entry.get("logs")
                    or entry.get("attendanceLogs") or [])
            for log in logs:
                if isinstance(log, dict):
                    punches |= self._upsert_punch_detail(backend, log)

        if not punches:
            raise UserError(_(
                "Tipsoi returned no punch detail for %(who)s on %(day)s.",
                who=self.employee_id.display_name, day=self.day_date))

        return {
            "type": "ir.actions.act_window",
            "name": _("Punches - %(who)s, %(day)s",
                      who=self.employee_id.display_name, day=self.day_date),
            "res_model": "tipsoi.punch.log",
            "view_mode": "list,form",
            "domain": [("id", "in", punches.ids)],
        }

    def _upsert_punch_detail(self, backend, log):
        """Store one detailed punch as a punch-log row.

        Never marked `paired`: pairing is a Device Portal concern, because there the
        punches *are* the attendance source. Here the day row is the source and these
        rows exist for a person to look at, so pairing must not touch them.
        """
        self.ensure_one()
        Punch = self.env["tipsoi.punch.log"]
        log_id = self._text(log.get("uid")) or self._text(log.get("id"))
        if not log_id:
            return Punch.browse()

        punch_type = self._int(log.get("punchType"))
        identifier = (self._text(log.get("empIdentifier"))
                      or self.employee_identifier)
        device_identifier = self._text(log.get("deviceIdentifier"))
        device = self.env["tipsoi.device"].with_context(active_test=False).search([
            ("backend_id", "=", backend.id),
            ("identifier", "=", device_identifier),
        ], limit=1) if device_identifier else self.env["tipsoi.device"].browse()

        reasons = []
        if punch_type == PUNCH_TYPE_MISSING:
            reasons.append(_("Tipsoi flagged this as a missing punch."))
        if log.get("manualEntryReason"):
            reasons.append(_("Entered manually: %s", log["manualEntryReason"]))

        vals = {
            "backend_id": backend.id,
            "tipsoi_log_id": log_id,
            "person_identifier": identifier,
            "device_identifier": device_identifier,
            "device_id": device.id if device else False,
            "employee_id": self.employee_id.id if self.employee_id else False,
            "punch_time_utc": tipsoi_time.millis_to_utc(log.get("attendanceTime")),
            "punch_time_raw": str(log.get("attendanceTime") or ""),
            "direction": ENTRY_TYPE_DIRECTION.get(
                self._int(log.get("entryType")), "unknown"),
            "punch_method": PUNCH_TYPE_METHOD.get(punch_type, "other"),
            "log_type_raw": str(log.get("entryType") or ""),
            "location": self._text(log.get("location")),
            "state": "matched" if self.employee_id else "unmatched",
            "state_reason": "\n".join(reasons) or False,
            "raw_payload": json.dumps(log, default=str, sort_keys=True),
        }

        existing = Punch.search([
            ("backend_id", "=", backend.id),
            ("tipsoi_log_id", "=", log_id),
        ], limit=1)
        if existing:
            existing.write(vals)
            return existing
        return Punch.create(vals)

    # ----------------------------------------------------------------------------------

    @staticmethod
    def _text(value):
        if value in (None, False):
            return ""
        return str(value).strip()

    @staticmethod
    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
