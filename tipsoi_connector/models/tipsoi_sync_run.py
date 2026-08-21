# -*- coding: utf-8 -*-
"""One row per cron execution.

The first debugging stop in production, so it is built in Phase 1 rather than bolted on
once something has already gone wrong at a customer.
"""

import logging
import traceback
from contextlib import contextmanager

from odoo import _, api, fields, models
from odoo.tools import config

_logger = logging.getLogger(__name__)


class TipsoiSyncRun(models.Model):
    _name = "tipsoi.sync.run"
    _description = "Tipsoi Sync Run"
    _order = "started_at desc, id desc"
    _rec_name = "job"

    backend_id = fields.Many2one(
        "tipsoi.backend", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="backend_id.company_id", store=True)
    job = fields.Selection(
        [("employees", "Employees"),
         ("devices", "Devices & projects"),
         ("punches", "Punch poll"),
         ("pairing", "Pairing"),
         ("attendance", "Attendance & masters"),
         ("day_import", "Day import"),
         ("photos", "Photo backfill"),
         ("writeback", "Write-back to Tipsoi"),
         ("manual", "Manual sync")],
        required=True, index=True)

    started_at = fields.Datetime(required=True, default=fields.Datetime.now)
    finished_at = fields.Datetime()
    duration_seconds = fields.Float(compute="_compute_duration", store=True)

    # The window actually requested. Recorded because the cursor is set from this, never
    # from the returned rows -- see tipsoi_client.paginate for why.
    window_from = fields.Datetime()
    window_to = fields.Datetime()

    fetched = fields.Integer(default=0)
    created = fields.Integer(default=0)
    updated = fields.Integer(default=0)
    skipped = fields.Integer(default=0)
    failed = fields.Integer(default=0)

    state = fields.Selection(
        [("running", "Running"), ("done", "Done"),
         ("partial", "Completed with errors"), ("error", "Failed")],
        default="running", required=True, index=True)
    error = fields.Text()
    notes = fields.Text()

    @api.depends("started_at", "finished_at")
    def _compute_duration(self):
        for run in self:
            if run.started_at and run.finished_at:
                run.duration_seconds = (run.finished_at - run.started_at).total_seconds()
            else:
                run.duration_seconds = 0.0

    # ----------------------------------------------------------------------------------

    @api.model
    def checkpoint(self):
        """Commit, unless we are inside a test.

        A long sync commits between pages so that a failure late on does not discard the
        pages that already landed. Inside a test that same commit breaks the enclosing
        case's cursor.

        `registry.in_test_mode()` is not a sufficient check. Odoo 18 runs post-install
        tests with the registry *not* in test mode and instead replaces `commit` on the
        test cursor with a function that raises -- so the guard passed and every job that
        commits failed. Reading the `test_enable` config covers both series, and a
        production server never has it set.
        """
        if config.get("test_enable") or self.env.registry.in_test_mode():
            return False
        self.env.cr.commit()
        return True

    @api.model
    @contextmanager
    def track(self, backend, job, window_from=None, window_to=None):
        """Run a job inside an audited, self-closing sync run.

        The run row is committed independently of the job's own transaction, so a
        rollback still leaves a record of what was attempted -- which is the whole point
        of having it.
        """
        run = self.sudo().create({
            "backend_id": backend.id,
            "job": job,
            "window_from": window_from,
            "window_to": window_to,
        })
        run.flush()
        # Commit the run row on its own so a later rollback still leaves a record of what
        # was attempted -- that is the point of the audit.
        self.checkpoint()
        try:
            yield run
        except Exception as exc:
            run.sudo().write({
                "state": "error",
                "finished_at": fields.Datetime.now(),
                "error": traceback.format_exc(),
            })
            # The backend decides whether this failure should halt the schedule. A
            # blanket `state = "error"` here is what used to take a backend out of every
            # cron over a single network blip.
            backend._record_job_failure(exc)
            _logger.exception("Tipsoi %s sync failed for %s", job, backend.display_name)
            raise
        else:
            run.sudo().write({
                "state": "partial" if run.failed else "done",
                "finished_at": fields.Datetime.now(),
            })
            backend._record_job_success()

    def name_get(self):
        # `name_get`, not `_compute_display_name`: Odoo 17 removed the former and added
        # the latter, so on 16 a `_compute_display_name` override is code that never runs
        # and every run shows as "tipsoi.sync.run,4" instead of the job and its time.
        labels = dict(self._fields["job"].selection)
        return [(run.id, "%s — %s" % (labels.get(run.job, run.job), run.started_at or ""))
                for run in self]

    def add_note(self, text):
        """Append a line to `notes` rather than replacing what is there.

        Two different observations can land in one run -- "the MQTT broker was
        unreachable" and "three devices were deactivated" -- and the first is usually the
        more important one. Assigning to `notes` silently discards it.
        """
        self.ensure_one()
        existing = (self.notes or "").strip()
        self.notes = ("%s\n%s" % (existing, text)) if existing else text
        return self.notes
