# -*- coding: utf-8 -*-
"""Allocate people onto readers, or revoke them.

Allocation is what decides which devices will recognise someone, so it is the one
outbound operation an operator runs in bulk: a new joiner onto every door on their
floor, a leaver off all of them at once.

Bulk is also why this reports per pair. `_tipsoi_allocate` deliberately keeps going
after a failure and hands back one result per employee/device pair, because one employee
that upstream refuses -- not ACTIVE yet, no office employee ID synced -- must not cancel
the other twenty that would have worked. This wizard's job is to render that report
somewhere the operator will read it.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TipsoiAllocation(models.TransientModel):
    _name = "tipsoi.allocation"
    _description = "Tipsoi Device Allocation"

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
    employee_ids = fields.Many2many(
        "hr.employee", string="Employees", required=True,
        domain=[("tipsoi_identifier", "!=", False)],
        help="Only employees that carry a Tipsoi identifier can be allocated: the "
             "identifier is what the device is told to recognise.")
    device_ids = fields.Many2many(
        "tipsoi.device", string="Devices", required=True,
        domain="[('backend_id', '=', backend_id)]")
    action_type = fields.Selection(
        [("allocate", "Allocate"), ("revoke", "Revoke")],
        required=True, default="allocate", string="Action")

    result = fields.Text(readonly=True)
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done")], default="draft")

    @api.model
    def default_get(self, fields_list):
        """Seed from whichever list the wizard was opened over."""
        values = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_ids = self.env.context.get("active_ids") or []
        if not active_ids:
            return values
        if active_model == "hr.employee":
            employees = self.env["hr.employee"].browse(active_ids).exists()
            values["employee_ids"] = [(6, 0, employees.ids)]
        elif active_model == "tipsoi.device":
            devices = self.env["tipsoi.device"].browse(active_ids).exists()
            values["device_ids"] = [(6, 0, devices.ids)]
            # Take the backend from the devices too. Otherwise the default backend can
            # contradict the device domain, and the operator opens the dialog to find
            # the very devices they selected filtered out of it.
            if devices.backend_id[:1]:
                values["backend_id"] = devices.backend_id[:1].id
        return values

    # ----------------------------------------------------------------------------------
    # apply
    # ----------------------------------------------------------------------------------

    def action_apply(self):
        self.ensure_one()
        if not self.employee_ids or not self.device_ids:
            raise UserError(_("Choose at least one employee and one device."))
        results = self.employee_ids._tipsoi_allocate(self.device_ids, self.action_type)
        self.write({"result": self._summarise(results), "state": "done"})
        return self._reopen()

    def _summarise(self, results):
        """Render the per-pair tuples, counts first.

        The counts go at the top because the interesting case is a partial success: with
        forty pairs, the two that failed are the only lines anyone needs to act on, and
        they must not have to be counted by hand.
        """
        self.ensure_one()
        succeeded = [r for r in results if r[2]]
        failed = [r for r in results if not r[2]]
        label = dict(self._fields["action_type"].selection).get(
            self.action_type, self.action_type)
        lines = [_("%(action)s: %(ok)s succeeded, %(bad)s failed.",
                   action=label, ok=len(succeeded), bad=len(failed))]
        if failed:
            lines.append("")
            lines.append(_("Failed:"))
            for employee, device, _ok, message in failed:
                lines.append("  %s -> %s: %s" % (
                    employee.display_name, device.name, message or _("unknown error")))
        if succeeded:
            lines.append("")
            lines.append(_("Done:"))
            for employee, device, _ok, _message in succeeded:
                lines.append("  %s -> %s" % (employee.display_name, device.name))
        return "\n".join(lines)

    def _reopen(self):
        """Re-open this same wizard so the outcome lands in the dialog already open."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "tipsoi.allocation",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
