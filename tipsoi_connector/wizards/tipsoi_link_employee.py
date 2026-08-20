# -*- coding: utf-8 -*-
"""Attach unmatched punches to the employee they belong to.

This is the screen HR opens every day. A punch is unmatched when the person
identifier the reader sent matches no employee in Odoo -- a new joiner enrolled on the
device before being created here, a re-enrolment under a fresh identifier, a typo. The
punch is not wrong and it is not a duplicate: it is an open question, which is why the
vacuum leaves it alone until someone answers it here.

Answering it has two halves worth separating. Linking the selected punches fixes
history; writing the identifier onto the employee fixes the future, so the next punch
matches on its own and nobody comes back here tomorrow for the same person.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TipsoiLinkEmployee(models.TransientModel):
    _name = "tipsoi.link.employee"
    _description = "Link Tipsoi Punches to an Employee"

    # Defined before the fields because `default=_default_punch_logs` resolves the name
    # at class-body evaluation time.
    @api.model
    def _default_punch_logs(self):
        # A (6, 0, ids) command rather than a bare list: a plain list of ids is not a
        # reliable default for an x2many.
        return [(6, 0, self.env.context.get("active_ids") or [])]

    punch_log_ids = fields.Many2many(
        "tipsoi.punch.log", string="Punches", default=_default_punch_logs)
    employee_id = fields.Many2one("hr.employee", required=True)
    identifier = fields.Char(
        string="Tipsoi identifier", compute="_compute_identifier", readonly=True,
        help="The identifier the devices sent for these punches.")
    set_identifier = fields.Boolean(
        default=True, string="Also link future punches",
        help="Writes the identifier onto the employee, so punches that arrive later "
             "match them without anyone having to come back to this screen. Untick it "
             "to link only the punches selected here.")
    result = fields.Text(readonly=True)

    @api.depends("punch_log_ids.person_identifier")
    def _compute_identifier(self):
        for wizard in self:
            wizard.identifier = ", ".join(wizard._identifiers()) or False

    def _identifiers(self):
        """The distinct identifiers across the selected punches, in a stable order."""
        self.ensure_one()
        return sorted({p.person_identifier for p in self.punch_log_ids
                       if p.person_identifier})

    # ----------------------------------------------------------------------------------
    # link
    # ----------------------------------------------------------------------------------

    def action_link(self):
        self.ensure_one()
        punches = self.punch_log_ids
        if not punches:
            raise UserError(_("Select at least one punch to link."))

        identifiers = self._identifiers()
        if self.set_identifier and len(identifiers) > 1:
            # One employee owns one Tipsoi identifier -- the uniqueness constraint says
            # so. Quietly picking one of these would mislink every future punch that
            # arrives under the others, and nobody would notice for months.
            raise UserError(_(
                "These punches carry %(count)s different Tipsoi identifiers: "
                "%(identifiers)s.\n\n"
                "An employee can only own one of them. Link one identifier at a time, "
                "or untick '%(field)s' to link just these punches and leave the "
                "employee's identifier as it is.",
                count=len(identifiers), identifiers=", ".join(identifiers),
                field=self._fields["set_identifier"].string))

        employee = self.employee_id
        if self.set_identifier and identifiers:
            vals = {"tipsoi_identifier": identifiers[0]}
            backend = punches.backend_id[:1]
            if backend:
                vals["tipsoi_backend_id"] = backend.id
            # tipsoi_syncing: this is inbound work. Without it the write queues an
            # outbound push of an employee Tipsoi already knows perfectly well.
            employee.with_context(tipsoi_syncing=True).write(vals)

        punches.write({
            "employee_id": employee.id,
            "state": "matched",
            "state_reason": False,
        })

        pair_error = self._pair(punches)
        self.result = self._summarise(punches, identifiers, pair_error)
        return self._reopen()

    def _pair(self, punches):
        """Pair straight away, so the punches just resolved become attendance now.

        Returns an error message rather than raising. The audit run inside pairing
        commits before the job body, so the link made above is already saved by the time
        anything can go wrong here -- a traceback would leave HR unable to tell whether
        their link took effect at all.

        Failing here is recoverable, and the summary must say so rather than sending
        somebody to escalate. The link leaves each punch `matched` with an employee
        attached, which is exactly what the pairing cron selects, so the next scheduled
        pass finishes the job on its own.
        """
        self.ensure_one()
        backends = punches.backend_id.filtered(
            lambda b: b.backend_type == "device_portal")
        if not backends:
            return ""
        try:
            backends.action_pair_punches()
        except Exception as exc:               # noqa: BLE001 - reported, not swallowed
            _logger.warning("Tipsoi pairing after a manual link failed: %s", exc)
            return str(exc)
        return ""

    # ----------------------------------------------------------------------------------
    # reporting
    # ----------------------------------------------------------------------------------

    def _summarise(self, punches, identifiers, pair_error):
        self.ensure_one()
        lines = [_("Linked %(count)s punch(es) to %(employee)s.",
                   count=len(punches), employee=self.employee_id.display_name)]
        if self.set_identifier and identifiers:
            lines.append(_(
                "Identifier %s now sits on the employee, so later punches match on "
                "their own.", identifiers[0]))
        else:
            lines.append(_(
                "The employee's identifier was left alone, so punches arriving later "
                "will need linking too."))
        paired = len(punches.filtered(lambda p: p.state == "paired"))
        if pair_error:
            # Deliberately does not send anyone to escalate. The punches are linked and
            # `matched`, which is what the pairing cron selects, so the next scheduled
            # pass finishes this without help. Only a configuration failure -- bad
            # credentials, an inactive project -- puts the backend into its error state,
            # and it stays schedulable even then, so it recovers by itself once the
            # configuration is fixed.
            lines.append(_(
                "The link is saved, but pairing it into attendance did not finish: "
                "%(error)s\n\n"
                "Nothing is lost and nothing needs doing -- these punches are linked "
                "and will pair on the next scheduled pass. If the same message keeps "
                "coming back, the connection itself needs looking at: the reason is "
                "kept on the backend and in Sync Runs.", error=pair_error))
        else:
            lines.append(_("%(paired)s of %(count)s are now paired into attendance.",
                           paired=paired, count=len(punches)))
        return "\n".join(lines)

    def _reopen(self):
        """Re-open this same wizard so the outcome lands in the dialog already open."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "tipsoi.link.employee",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
