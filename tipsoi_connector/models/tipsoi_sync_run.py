# -*- coding: utf-8 -*-
"""One row per cron execution.

The first debugging stop in production, so it is built in Phase 1 rather than bolted on
once something has already gone wrong at a customer.
"""

import logging
import traceback
from contextlib import contextmanager

from odoo import _, api, fields, models

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
        run.flush_recordset()
        # Commit the run row on its own so a later rollback still leaves a record of what
        # was attempted -- that is the point of the audit. Never during tests, where a
        # commit would break the enclosing TransactionCase.
        if not self.env.registry.in_test_mode():
            self.env.cr.commit()
        try:
            yield run
        except Exception as exc:
            run.sudo().write({
                "state": "error",
                "finished_at": fields.Datetime.now(),
                "error": traceback.format_exc(),
            })
            backend.sudo().write({"state": "error", "last_error": str(exc)})
            _logger.exception("Tipsoi %s sync failed for %s", job, backend.display_name)
            raise
        else:
            run.sudo().write({
                "state": "partial" if run.failed else "done",
                "finished_at": fields.Datetime.now(),
            })

    def name_get(self):
        labels = dict(self._fields["job"].selection)
        return [(r.id, "%s — %s" % (labels.get(r.job, r.job), r.started_at or ""))
                for r in self]
