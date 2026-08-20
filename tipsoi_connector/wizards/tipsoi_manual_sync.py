# -*- coding: utf-8 -*-
"""Run one sync job now, optionally over a window the operator chooses.

Two things make this more than a row of buttons. First, every job belongs to one backend
mode and only one -- that split is the core design rule of this addon, since a client
talks to the Device Portal API or to the Tipsoi app API but never to both -- so the
wizard shows the mismatch on the form before anything is attempted rather than letting a
run fail halfway. Second, the two windowed jobs can be pointed at an older window for a
backfill, and doing that must not disturb the live cursor.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: Which backend mode each job belongs to; ``None`` means the job runs in both modes.
#: The warning on the form and the guard in :meth:`action_run` both read this, so the
#: rule exists in exactly one place and cannot drift between them.
JOB_MODES = {
    "employees": "device_portal",
    "devices": None,
    "punches": "device_portal",
    "pairing": "device_portal",
    "attendance": "hrm",
    "day_import": "hrm",
    "photos": None,
}

#: The backend method each job calls when it runs over the backend's own window.
JOB_ACTIONS = {
    "employees": "action_sync_employees",
    "devices": "action_sync_devices",
    "punches": "action_poll_punches",
    "pairing": "action_pair_punches",
    "attendance": "action_sync_attendance",
    "day_import": "action_import_days",
    "photos": "action_push_photos",
}

#: The jobs that read a time window, and can therefore be aimed at a different one.
WINDOWED_JOBS = ("punches", "attendance")


class TipsoiManualSync(models.TransientModel):
    _name = "tipsoi.manual.sync"
    _description = "Tipsoi Manual Sync"

    # Defined before the fields because `default=_default_backend` resolves the name at
    # class-body evaluation time.
    @api.model
    def _default_backend(self):
        # A plain search rather than tipsoi.backend.of_company(): that helper raises
        # when nothing is configured, and a default that raises makes the dialog
        # impossible to open -- the one moment someone needs to see what is wrong.
        return self.env["tipsoi.backend"].search(
            [("company_id", "=", self.env.company.id)], limit=1)

    backend_id = fields.Many2one(
        "tipsoi.backend", required=True, default=_default_backend)
    job = fields.Selection(
        [("employees", "Employees"),
         ("devices", "Devices"),
         ("punches", "Punch poll"),
         ("pairing", "Pairing"),
         ("attendance", "Attendance & masters"),
         ("day_import", "Day import"),
         ("photos", "Photo backfill")],
        required=True, default="devices",
    )

    date_from = fields.Datetime(
        string="From",
        help="Punch poll and attendance only. Leave both dates empty to use the "
             "backend's own window.")
    date_to = fields.Datetime(string="To")

    mode_warning = fields.Char(compute="_compute_mode_warning")
    result = fields.Text(readonly=True)

    # ----------------------------------------------------------------------------------
    # form feedback
    # ----------------------------------------------------------------------------------

    @api.depends("job", "backend_id.backend_type")
    def _compute_mode_warning(self):
        labels = dict(
            self.env["tipsoi.backend"]._fields["backend_type"].selection)
        for wizard in self:
            wanted = JOB_MODES.get(wizard.job)
            actual = wizard.backend_id.backend_type
            if not wanted or not actual or wanted == actual:
                wizard.mode_warning = False
                continue
            wizard.mode_warning = _(
                "This job is written for %(want)s backends, and this one is %(got)s. "
                "The two APIs are never combined, so running it here would do nothing.",
                want=labels.get(wanted, wanted), got=labels.get(actual, actual))

    # ----------------------------------------------------------------------------------
    # run
    # ----------------------------------------------------------------------------------

    def action_run(self):
        self.ensure_one()
        backend = self.backend_id
        wanted = JOB_MODES.get(self.job)
        # Raised rather than left to the job: `_run` returns False on a mode mismatch
        # without creating a sync run, so without this check the user would see nothing
        # happen and a summary of somebody else's earlier run.
        if wanted and backend.backend_type != wanted:
            raise UserError(self.mode_warning or _(
                "This job does not apply to this backend's mode."))

        use_window = False
        if self.job in WINDOWED_JOBS:
            if bool(self.date_from) != bool(self.date_to):
                raise UserError(_(
                    "Give both a start and an end, or neither. A half-open window has "
                    "no sensible reading: the missing end would have to be either now "
                    "or the beginning of time."))
            use_window = bool(self.date_from and self.date_to)
        if use_window and self.date_from >= self.date_to:
            raise UserError(_("The start of the window must come before its end."))

        # Captured before the call so the summary can tell "this run" from "the last run
        # that happened to exist" -- a job with nothing to do creates no run at all.
        previous = self._latest_run(backend)
        if use_window:
            self._run_window(backend)
        else:
            getattr(backend, JOB_ACTIONS[self.job])()

        self.result = self._summarise(self._latest_run(backend), previous)
        return self._reopen()

    def _run_window(self, backend):
        """Run a windowed job over the dates on this wizard rather than its own window.

        The cursor is deliberately not touched. Moving `last_log_sync_time` back to a
        backfill's window would make the next poll re-scan everything since; moving it
        forward to the window's end would skip every punch between that end and now.
        The same reasoning applies to `last_attendance_sync`.
        """
        self.ensure_one()
        start, end = self.date_from, self.date_to
        if self.job == "punches":
            backend._run(
                "punches",
                lambda b, run: self.env["tipsoi.punch.log"]._poll(b, run, start, end),
                mode="device_portal", window_from=start, window_to=end)
        else:
            backend._run(
                "attendance",
                lambda b, run: self.env["tipsoi.day.attendance"]._sync(
                    b, run, start, end),
                mode="hrm", window_from=start, window_to=end)

    # ----------------------------------------------------------------------------------
    # reporting
    # ----------------------------------------------------------------------------------

    @api.model
    def _latest_run(self, backend):
        # sudo() because tipsoi.sync.run rows are created sudo'd by the audit context
        # manager; reporting "nothing ran" because of a record rule would be worse than
        # the bypass.
        return self.env["tipsoi.sync.run"].sudo().search(
            [("backend_id", "=", backend.id)], limit=1)

    def _summarise(self, run, previous):
        """A short human read of what the job did, or a plain statement that it did not.

        Comparing against the newest run from before the call is what makes this
        honest: an empty photo queue, or a job the backend declined, creates no run, and
        rendering the newest run unconditionally would report the previous run's
        counters as if they belonged to this one.
        """
        self.ensure_one()
        job_labels = dict(self._fields["job"].selection)
        if not run or run == previous:
            return _(
                "%s: nothing to do -- no work was queued, so no sync run was recorded.",
                job_labels.get(self.job, self.job))

        state_labels = dict(run._fields["state"].selection)
        lines = [
            _("Job: %s", job_labels.get(self.job, self.job)),
            _("Status: %s", state_labels.get(run.state, run.state)),
        ]
        if run.window_from and run.window_to:
            lines.append(_("Window: %(start)s to %(end)s UTC",
                           start=run.window_from, end=run.window_to))
        lines += [
            _("Fetched: %s", run.fetched),
            _("Created: %s", run.created),
            _("Updated: %s", run.updated),
            _("Skipped: %s", run.skipped),
            _("Failed: %s", run.failed),
        ]
        if run.notes:
            lines.append(_("Notes: %s", run.notes))
        if run.error:
            lines.append(_("Error: %s", run.error))
        return "\n".join(lines)

    def _reopen(self):
        """Re-open this same wizard so the outcome lands in the dialog already open."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "tipsoi.manual.sync",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
