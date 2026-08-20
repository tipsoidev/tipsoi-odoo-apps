# -*- coding: utf-8 -*-
"""Raw device punches, and the pairing that turns them into attendance.

Device Portal mode only. The Tipsoi app has no office-wide punch feed -- its punch
detail is per employee -- so in that mode the app's own day rows are the feed and this
model is unused.

Staging exists because the punch feed cannot be trusted to arrive cleanly once:

* the poll filters on `updated_at` but orders by `created_at`, so a row updated
  mid-pagination shifts the offsets and rows can be skipped *or* duplicated between
  pages -- the unique index below absorbs the duplicates and the window overlap catches
  the skips;
* a row can be updated after insert, so the same punch may legitimately reappear later
  with different data, which is why this upserts rather than ignoring conflicts;
* and the raw strings are kept verbatim, because a small number of devices are
  configured to return `logged_time` already converted to GMT while every other row is
  application-local, and nothing in the payload says which is which.

Pairing deliberately does **not** bucket punches by calendar day. Bucketing is what
breaks an overnight shift -- a 22:00 entry and an 06:00 exit are one span, not two days
-- so pairs are formed by sequence within a maximum shift length instead.

One consequence is worth stating plainly rather than leaving to be discovered: when a new
punch changes the shape of a day, the attendance records this connector created for that
day are **replaced**, not amended. That is what makes re-polling safe, and when nothing
changed nothing is touched at all -- but it does mean a hand edit to a
connector-created attendance is lost the next time a punch for that day arrives. The
device is the source of truth in this mode. Corrections belong upstream, or on an
attendance record the connector did not create.
"""

import json
import logging
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import tipsoi_time

_logger = logging.getLogger(__name__)

#: `log_type` is the *direction* -- `enum('entry','exit','both','access','other')`,
#: nullable. `type` is the *method* -- `enum('card','fingerprint','unrecognized')`.
#: Reading them the other way round is the obvious mistake and produces punches whose
#: direction is a payment method, so both maps are spelled out here.
DIRECTION_BY_LOG_TYPE = {"entry": "in", "exit": "out"}
PUNCH_METHODS = ("card", "fingerprint", "unrecognized")

#: A device whose reader is wired one way tells us the direction its punches must have,
#: which is the fallback when the row itself says `both`, `access` or nothing.
DIRECTION_BY_DEVICE_TYPE = {"entry": "in", "exit": "out"}

_SORT_FLOOR = datetime(1970, 1, 1)


class TipsoiPunchLog(models.Model):
    _name = "tipsoi.punch.log"
    _description = "Tipsoi Punch Log"
    _order = "punch_time_utc desc, id desc"

    backend_id = fields.Many2one(
        "tipsoi.backend", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="backend_id.company_id", store=True, index=True)
    name = fields.Char(compute="_compute_name", store=True)

    tipsoi_log_id = fields.Char(
        required=True, index=True, string="Punch ID",
        help="The punch row's own `uid`, which is unique in the source schema and "
             "survives its archive table -- so it is the idempotency key, and no "
             "composite hash is needed.")
    person_identifier = fields.Char(index=True)
    device_identifier = fields.Char(index=True)
    device_id = fields.Many2one("tipsoi.device", ondelete="set null", index=True)
    employee_id = fields.Many2one("hr.employee", ondelete="set null", index=True)

    punch_time_utc = fields.Datetime(string="Punch time (UTC)", index=True)
    punch_time_raw = fields.Char(
        string="Punch time as received", readonly=True,
        help="The `logged_time` string exactly as Tipsoi sent it. Kept because a few "
             "devices return this field already converted to GMT while the rest are in "
             "the application timezone, and the payload gives no way to tell.")
    sync_time_raw = fields.Char(
        string="Sync time as received", readonly=True,
        help="The field the poll cursors on. Note it reports `created_at` while the "
             "filter runs on `updated_at`, so it is an audit value and never a cursor.")
    punch_day = fields.Date(
        compute="_compute_punch_day", store=True, string="Local day",
        help="The punch's calendar date in the backend's timezone. For grouping and "
             "filtering only -- pairing never uses it, because bucketing by day is "
             "what breaks overnight shifts.")

    direction = fields.Selection(
        [("in", "In"), ("out", "Out"), ("unknown", "Unknown")],
        default="unknown", index=True,
        help="As reported by Tipsoi. Where it is unknown, pairing infers a direction "
             "by alternating but does not overwrite this -- the record stays faithful "
             "to what arrived.")
    punch_method = fields.Selection(
        [("card", "Card"), ("fingerprint", "Fingerprint"),
         ("unrecognized", "Unrecognized"), ("other", "Other")])
    log_type_raw = fields.Char(string="Direction as received", readonly=True)
    location = fields.Char()
    rfid = fields.Char(string="RFID")

    state = fields.Selection(
        [("new", "New"),
         ("matched", "Matched"),
         ("paired", "Paired"),
         ("duplicate", "Duplicate"),
         ("unmatched", "No employee"),
         ("unpaired", "Unpaired"),
         ("error", "Error")],
        default="new", required=True, index=True)
    state_reason = fields.Text(readonly=True)
    attendance_id = fields.Many2one("hr.attendance", ondelete="set null", index=True)
    raw_payload = fields.Text(readonly=True)

    _sql_constraints = [
        ("uniq_backend_log",
         "unique(backend_id, tipsoi_log_id)",
         "This Tipsoi punch has already been imported."),
    ]

    # ----------------------------------------------------------------------------------
    # display
    # ----------------------------------------------------------------------------------

    @api.depends("person_identifier", "punch_time_utc", "direction")
    def _compute_name(self):
        labels = dict(self._fields["direction"].selection)
        for punch in self:
            punch.name = "%s %s %s" % (
                punch.person_identifier or _("unknown person"),
                labels.get(punch.direction, ""),
                punch.punch_time_utc or "",
            )

    @api.depends("punch_time_utc", "backend_id.source_timezone")
    def _compute_punch_day(self):
        for punch in self:
            if not punch.punch_time_utc:
                punch.punch_day = False
                continue
            tz = tipsoi_time.to_tz(punch.backend_id.source_timezone)
            punch.punch_day = fields.Datetime.context_timestamp(
                punch.with_context(tz=str(tz)), punch.punch_time_utc).date()

    def action_open_attendance(self):
        self.ensure_one()
        if not self.attendance_id:
            raise UserError(_("This punch is not part of an attendance record yet."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "hr.attendance",
            "res_id": self.attendance_id.id,
            "view_mode": "form",
        }

    def action_pair_now(self):
        """Re-pair the employees these punches belong to."""
        for backend in self.mapped("backend_id"):
            rows = self.filtered(lambda p, b=backend: p.backend_id == b)
            for employee in rows.mapped("employee_id"):
                times = [p.punch_time_utc for p in rows
                         if p.employee_id == employee and p.punch_time_utc]
                if not times:
                    continue
                self._pair_employee(backend, employee, min(times), counters={})
        return True

    # ----------------------------------------------------------------------------------
    # poll
    # ----------------------------------------------------------------------------------

    @api.model
    def _poll(self, backend, run, window_from, window_to):
        """Read the punch feed for a window and upsert every row.

        `criteria=sync_time` rather than punch time on purpose: it is what catches
        punches from a device that was offline for days and then reconnected, which a
        punch-time cursor would step straight over.
        """
        adapter = backend.client()
        tzname = backend._source_tz()
        params = {
            "criteria": "sync_time",
            # Parsed in the API server's application timezone, so the window has to
            # be expressed in local wall time. Sending UTC shifts it by the offset.
            "start": tipsoi_time.utc_to_dp_param(window_from, tzname),
            "end": tipsoi_time.utc_to_dp_param(window_to, tzname),
            "order_key": "sync_time",
            "order_direction": "asc",
        }
        devices = self._device_index(backend)
        counters = dict(fetched=0, created=0, updated=0, skipped=0, failed=0)

        for page in adapter.paginate("logs", params):
            counters["fetched"] += len(page)
            for row in page:
                self._upsert_row(backend, row, devices, counters)
            self._flush_counters(run, counters)
            # Commit per page so a long backfill makes progress that survives a
            # later failure. checkpoint() is a no-op inside a test.
            self.env["tipsoi.sync.run"].checkpoint()
        self._flush_counters(run, counters)
        return True

    @api.model
    def _device_index(self, backend):
        devices = self.env["tipsoi.device"].with_context(active_test=False).search(
            [("backend_id", "=", backend.id)])
        return {d.identifier: d for d in devices if d.identifier}

    def _flush_counters(self, run, counters):
        """Write accumulated counts onto the run.

        Counted locally rather than on the record because a per-row savepoint rollback
        would otherwise leave the run's cached values disagreeing with the database.
        """
        if not any(counters.values()):
            return
        run.write({
            "fetched": run.fetched + counters["fetched"],
            "created": run.created + counters["created"],
            "updated": run.updated + counters["updated"],
            "skipped": run.skipped + counters["skipped"],
            "failed": run.failed + counters["failed"],
        })
        for key in counters:
            counters[key] = 0

    @api.model
    def _upsert_row(self, backend, row, devices, counters):
        """Upsert one punch, isolated so a single bad row cannot fail the page."""
        if not isinstance(row, dict):
            counters["skipped"] += 1
            return self.browse()
        uid = self._text(row.get("uid"))
        if not uid:
            # Without the uid there is no idempotency key, so importing it would create
            # a duplicate on every subsequent poll. Counted, not silently dropped.
            counters["skipped"] += 1
            _logger.warning("Tipsoi punch without a uid skipped: %s", row)
            return self.browse()

        # The context-manager form, not `savepoint.close()`: `close()` defaults to
        # rollback=True, so closing one explicitly on the success path throws the row
        # away. `with` releases on success and rolls back only on an exception.
        try:
            with self.env.cr.savepoint():
                existing = self.search([
                    ("backend_id", "=", backend.id),
                    ("tipsoi_log_id", "=", uid),
                ], limit=1)
                vals = self._row_to_vals(backend, row, devices)
                if existing:
                    # A punch whose time or direction changed upstream has to be paired
                    # again. The attendance link is left in place so pairing can find
                    # the record it already created and reconcile it rather than
                    # duplicate it.
                    repair = (existing.state == "paired"
                              and (existing.punch_time_utc != vals.get("punch_time_utc")
                                   or existing.direction != vals.get("direction")))
                    if repair:
                        vals["state"] = "matched"
                        vals["state_reason"] = _(
                            "Re-read from Tipsoi with a different time or direction, "
                            "so this day is queued for pairing again.")
                    existing.write(vals)
                    counters["updated"] += 1
                    record = existing
                else:
                    record = self.create(dict(vals, backend_id=backend.id,
                                              tipsoi_log_id=uid))
                    counters["created"] += 1
                record.flush_recordset()
        except Exception as exc:              # noqa: BLE001 - recorded, not swallowed
            # The cache can still hold values written inside the rolled-back savepoint,
            # and reading them later would be reading rows that no longer exist.
            self.env.invalidate_all(flush=False)
            counters["failed"] += 1
            _logger.warning("Tipsoi punch %s could not be imported: %s", uid, exc)
            return self.browse()
        return record

    @api.model
    def _row_to_vals(self, backend, row, devices):
        identifier = self._text(row.get("person_identifier"))
        device_identifier = self._text(row.get("device_identifier"))
        device = devices.get(device_identifier)
        employee = self.env["hr.employee"]._tipsoi_find(backend, identifier)

        vals = {
            "person_identifier": identifier,
            "device_identifier": device_identifier,
            "device_id": device.id if device else False,
            "employee_id": employee.id if employee else False,
            "punch_time_utc": tipsoi_time.dp_to_utc(
                row.get("logged_time"), backend._source_tz()),
            "punch_time_raw": self._text(row.get("logged_time")),
            "sync_time_raw": self._text(row.get("sync_time")),
            "direction": self._direction(row.get("log_type"), device),
            "punch_method": self._method(row.get("type")),
            "log_type_raw": self._text(row.get("log_type")),
            "location": self._text(row.get("location")),
            "rfid": self._text(row.get("rfid")),
            "raw_payload": json.dumps(row, default=str, sort_keys=True),
        }
        if employee:
            vals.update({"state": "matched", "state_reason": False})
        else:
            vals.update({
                "state": "unmatched",
                "state_reason": _(
                    "No Odoo employee carries the Tipsoi identifier %s. Link one on "
                    "the Unmatched Punches screen and this punch will pair itself.",
                    identifier or _("(none supplied)")),
            })
        return vals

    @api.model
    def _direction(self, log_type, device=None):
        """Direction from `log_type`, falling back to how the reader is wired."""
        text = self._text(log_type).lower()
        if text in DIRECTION_BY_LOG_TYPE:
            return DIRECTION_BY_LOG_TYPE[text]
        if device and device.device_type in DIRECTION_BY_DEVICE_TYPE:
            return DIRECTION_BY_DEVICE_TYPE[device.device_type]
        return "unknown"

    @api.model
    def _method(self, value):
        text = self._text(value).lower()
        if text in PUNCH_METHODS:
            return text
        return "other" if text else False

    # ----------------------------------------------------------------------------------
    # pairing
    # ----------------------------------------------------------------------------------

    @api.model
    def _pair(self, backend, run):
        """Pair every employee that has punches waiting.

        Employees with nothing new are skipped entirely, so a five-minute cron over a
        quiet office does no writes at all.
        """
        pending = self.search([
            ("backend_id", "=", backend.id),
            ("state", "in", ("new", "matched", "error")),
            ("employee_id", "!=", False),
            ("punch_time_utc", "!=", False),
        ], order="punch_time_utc asc")

        earliest = {}
        for punch in pending:
            earliest.setdefault(punch.employee_id, punch.punch_time_utc)

        counters = dict(fetched=len(pending), created=0, updated=0, skipped=0, failed=0)
        for employee, since in earliest.items():
            self._pair_employee(backend, employee, since, counters)
        self._flush_counters(run, counters)
        return True

    @api.model
    def _pair_employee(self, backend, employee, since, counters):
        """Recompute one employee's pairs over the window around `since`."""
        span = timedelta(hours=max(backend.max_shift_hours, 1))
        rows = self._pairing_window(backend, employee, since, span)
        if not rows:
            return

        plan, pairs = self._plan_pairs(backend, rows, span)
        existing = rows.mapped("attendance_id").sorted(
            lambda a: (a.check_in or _SORT_FLOOR, a.id))

        desired_key = sorted((p_in.punch_time_utc, p_out.punch_time_utc)
                             for p_in, p_out in pairs)
        existing_key = sorted((a.check_in or _SORT_FLOOR, a.check_out or _SORT_FLOOR)
                              for a in existing)

        if desired_key == existing_key:
            # Nothing about this employee's day changed, so the attendance records are
            # already right. Only the staging rows' own bookkeeping is touched, and only
            # where it actually differs -- this is what keeps a five-minute cron quiet.
            self._attach(pairs, existing, plan)
            self._apply_plan(plan)
            return

        # Removing first matters: an interval that is growing would otherwise transiently
        # overlap the record that used to sit where it is expanding into, and
        # hr.attendance refuses overlaps outright.
        if existing:
            existing.unlink()
        created = self._create_attendance(employee, pairs, plan, counters)
        self._attach(pairs, created, plan)
        self._apply_plan(plan)

    @api.model
    def _pairing_window(self, backend, employee, since, span):
        """The punches to reason about, widened until it cannot cut a pair in half.

        Two shift lengths back, because a pair whose entry sits before the window and
        whose exit sits inside it would otherwise look like an exit with no entry -- and
        that would unlink an attendance that was perfectly correct.
        """
        rows = self.search([
            ("backend_id", "=", backend.id),
            ("employee_id", "=", employee.id),
            ("punch_time_utc", ">=", since - (span * 2)),
            ("punch_time_utc", "!=", False),
            ("state", "!=", "unmatched"),
        ])
        linked = rows.mapped("attendance_id")
        if linked:
            rows |= self.search([
                ("backend_id", "=", backend.id),
                ("employee_id", "=", employee.id),
                ("attendance_id", "in", linked.ids),
            ])
        return rows.sorted(lambda p: (p.punch_time_utc or _SORT_FLOOR, p.id))

    @api.model
    def _plan_pairs(self, backend, rows, span):
        """Return `({row: (state, reason)}, [(entry_row, exit_row), ...])`.

        Nothing is written here. Separating the decision from the write is what makes
        the "did anything change?" comparison above possible.
        """
        plan = {}
        window = timedelta(seconds=max(backend.pair_duplicate_seconds, 0))

        kept = []
        for punch in rows:
            if kept and window and (
                    punch.punch_time_utc - kept[-1].punch_time_utc) < window:
                # A second read within seconds is the same punch -- a repeated finger
                # press, not an exit. Treating it as one would halve the day.
                plan[punch] = ("duplicate", _(
                    "Within %s seconds of the previous punch, so treated as the same "
                    "one.", backend.pair_duplicate_seconds))
                continue
            kept.append(punch)

        pairs = []
        open_entry = None
        expected = "in"
        for punch in kept:
            direction = punch.direction if punch.direction in ("in", "out") else expected
            if direction == "in":
                if open_entry is not None:
                    plan[open_entry] = ("unpaired", _(
                        "Two entry punches in a row -- no exit was recorded between "
                        "them."))
                open_entry = punch
                expected = "out"
                continue
            if open_entry is None:
                plan[punch] = ("unpaired", _(
                    "An exit punch with no entry punch before it."))
                expected = "in"
                continue
            if (punch.punch_time_utc - open_entry.punch_time_utc) > span:
                plan[open_entry] = ("unpaired", _(
                    "No exit punch within the longest shift (%s hours).",
                    backend.max_shift_hours))
                plan[punch] = ("unpaired", _(
                    "More than %s hours after the previous entry, so it does not close "
                    "that shift.", backend.max_shift_hours))
                open_entry = None
                expected = "in"
                continue
            pairs.append((open_entry, punch))
            open_entry = None
            expected = "in"

        if open_entry is not None:
            # Deliberately left open in staging rather than written as an attendance
            # with no check-out: Odoo allows only one open attendance per employee, so
            # creating one here would block every later record for that person.
            plan[open_entry] = ("unpaired", _(
                "No exit punch yet. It will pair itself when one arrives."))
        return plan, pairs

    @api.model
    def _create_attendance(self, employee, pairs, plan, counters):
        """Create one attendance per pair, in time order.

        A pair that collides with an attendance the connector does not own -- a manual
        entry somebody typed in Odoo -- is recorded on the two punch rows and skipped.
        One contested day must not fail the run.
        """
        Attendance = self.env["hr.attendance"].sudo()
        # One slot per pair, `None` where the create was refused. Returning only the
        # successes would shift every later pair onto the wrong attendance record.
        created = []
        for entry, exit_ in pairs:
            # `with`, not `close()`: see `_upsert_row` -- an explicit close would roll
            # back the attendance that was just created successfully.
            try:
                with self.env.cr.savepoint():
                    attendance = Attendance.create({
                        "employee_id": employee.id,
                        "check_in": entry.punch_time_utc,
                        "check_out": exit_.punch_time_utc,
                    })
                    attendance.flush_recordset()
            except ValidationError as exc:
                self.env.invalidate_all(flush=False)
                message = _("Odoo would not accept this pair: %s", exc)
                plan[entry] = ("error", message)
                plan[exit_] = ("error", message)
                counters["failed"] = counters.get("failed", 0) + 1
                created.append(None)
                _logger.warning("Tipsoi pairing rejected for %s: %s",
                                employee.display_name, exc)
            else:
                created.append(attendance)
                counters["created"] = counters.get("created", 0) + 1
        return created

    @api.model
    def _attach(self, pairs, attendances, plan):
        """Link each pair's two punches to its attendance, positionally.

        Both lists are in time order, so position is the match. Pairs whose attendance
        was refused keep whatever the plan already says about them.
        """
        ordered = list(attendances)
        for index, (entry, exit_) in enumerate(pairs):
            attendance = ordered[index] if index < len(ordered) else None
            if not attendance:
                continue
            for punch in (entry, exit_):
                if plan.get(punch, ("paired",))[0] == "error":
                    continue
                plan[punch] = ("paired", False, attendance)

    @api.model
    def _apply_plan(self, plan):
        """Write the planned states, skipping rows that already say the same thing."""
        for punch, outcome in plan.items():
            state, reason = outcome[0], outcome[1]
            attendance = outcome[2] if len(outcome) > 2 else None
            vals = {}
            if punch.state != state:
                vals["state"] = state
            if (punch.state_reason or False) != (reason or False):
                vals["state_reason"] = reason
            if attendance is not None and punch.attendance_id != attendance:
                vals["attendance_id"] = attendance.id
            if state != "paired" and punch.attendance_id and attendance is None:
                vals["attendance_id"] = False
            if vals:
                punch.write(vals)

    # ----------------------------------------------------------------------------------

    @staticmethod
    def _text(value):
        if value in (None, False):
            return ""
        return str(value).strip()
