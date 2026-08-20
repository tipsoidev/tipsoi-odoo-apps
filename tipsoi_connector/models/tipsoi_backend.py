# -*- coding: utf-8 -*-
"""Connection configuration for one Tipsoi API.

There is exactly **one** backend record per Odoo instance. `backend_type` selects the
whole pipeline rather than toggling features: a client on the Tipsoi app uses HRM
endpoints for everything, a client on the Device Portal alone uses Device Portal
endpoints for everything, and person records and employee identifiers are therefore
managed in one place only.
"""

import base64
import binascii
import json
import logging
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import tipsoi_client

_logger = logging.getLogger(__name__)

# Refresh a little before the JWT actually lapses, so a long sync started just under the
# wire does not die halfway.
TOKEN_SKEW = timedelta(minutes=5)


class TipsoiBackend(models.Model):
    _name = "tipsoi.backend"
    _description = "Tipsoi Backend"

    active = fields.Boolean(default=True)
    # `name`, not `display_name`: overriding the framework's own computed display_name
    # while also using it as _rec_name invites recursion. Odoo derives display_name from
    # name for free.
    name = fields.Char(compute="_compute_name", store=True)

    backend_type = fields.Selection(
        [("device_portal", "Device Portal only"),
         ("hrm", "Tipsoi app (HRM)")],
        required=True,
        help="Selects the entire pipeline. A Device Portal backend never calls the HRM "
             "API and vice versa -- that separation is what keeps person records and "
             "employee IDs from drifting apart.",
    )
    environment = fields.Selection(
        [("test", "Test"), ("live", "Live")],
        default="test", required=True,
    )
    base_url = fields.Char(
        required=True,
        help="Device Portal: https://test.api-inovace360.com/api/v1 or "
             "https://api-inovace360.com/api/v1\n"
             "Tipsoi app: https://test.clients.inovacetech.com/inovace-client/api/v1 or "
             "the live HRM host.",
    )

    # -- credentials -------------------------------------------------------------------
    # Not in ir.config_parameter: these sit on the record behind a field-level group so
    # that reading them requires the Tipsoi administrator role.
    username = fields.Char(
        string="Username / Email", groups="tipsoi_connector.group_tipsoi_admin",
        help="Device Portal expects a username; the Tipsoi app expects an email.",
    )
    password = fields.Char(
        groups="tipsoi_connector.group_tipsoi_admin")

    access_token = fields.Char(
        readonly=True, copy=False, groups="tipsoi_connector.group_tipsoi_admin")
    refresh_token = fields.Char(
        readonly=True, copy=False, groups="tipsoi_connector.group_tipsoi_admin",
        help="Tipsoi app only. The Device Portal's token never expires.")
    token_expiry = fields.Datetime(
        readonly=True, copy=False,
        help="Read from the JWT's own exp claim. Empty for the Device Portal, whose "
             "token is a column on the project row rather than a JWT.")
    tipsoi_user_id = fields.Integer(
        readonly=True, copy=False,
        help="Returned by HRM sign-in. /auth/refresh requires it alongside the refresh "
             "token, so it must be persisted.")
    tipsoi_office_id = fields.Integer(
        readonly=True, copy=False,
        help="Returned by HRM sign-in. Several HRM reads take it as a query parameter.")

    # -- what the remote told us about itself -------------------------------------------
    remote_has_hrm = fields.Boolean(
        readonly=True, copy=False, string="Remote reports HRM attached",
        help="The Device Portal's login response returns has_hrm, which is whether an "
             "HRM is attached to that project. Used to refuse a mode mismatch.")
    remote_organization = fields.Char(readonly=True, copy=False)

    # -- sync configuration --------------------------------------------------------------
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda s: s.env.company,
        help="Kept on every model even though deployments are one-per-client, so that "
             "multi-company later is a migration rather than a rewrite.")
    page_size = fields.Integer(
        default=500,
        help="Device Portal clamps this to 10000 and reports the clamped value back "
             "rather than erroring, so asking for more achieves nothing.")
    poll_overlap_minutes = fields.Integer(
        default=5,
        help="Re-reads the tail of the previous window. Covers clock skew, and also "
             "covers rows shifting between offset-paginated pages because the Device "
             "Portal filters on updated_at while ordering by created_at.")
    source_timezone = fields.Selection(
        selection="_tz_selection", default="Asia/Dhaka",
        help="Device Portal timestamps are naive wall time in the application's "
             "timezone, which defaults to Asia/Dhaka but is environment-overridable. "
             "Never hardcode it: confirm per deployment.")

    # -- cursors -----------------------------------------------------------------------
    last_log_sync_time = fields.Datetime(
        copy=False,
        help="Device Portal mode. Named for sync time rather than punch time because "
             "the poll cursors on criteria=sync_time, which is what catches punches "
             "from a device that was offline for days and then reconnected.")
    last_attendance_sync = fields.Datetime(
        copy=False, help="Tipsoi app mode. End of the last rolling window read.")
    last_employee_sync = fields.Datetime(copy=False)
    last_device_sync = fields.Datetime(copy=False)

    # -- state -------------------------------------------------------------------------
    state = fields.Selection(
        [("draft", "Draft"), ("ready", "Ready"), ("error", "Error")],
        default="draft", readonly=True, copy=False,
    )
    last_error = fields.Text(readonly=True, copy=False)
    sync_run_ids = fields.One2many("tipsoi.sync.run", "backend_id", readonly=True)

    _sql_constraints = [
        ("uniq_company_backend",
         "unique(company_id)",
         "One Tipsoi backend per company: the backend type selects the whole pipeline, "
         "so a second one would mean mixing the two APIs."),
    ]

    # ----------------------------------------------------------------------------------
    # display
    # ----------------------------------------------------------------------------------

    @api.depends("backend_type", "environment")
    def _compute_name(self):
        labels = dict(self._fields["backend_type"].selection)
        for backend in self:
            backend.name = "%s (%s)" % (
                labels.get(backend.backend_type, _("Unconfigured")),
                backend.environment or "",
            )

    @api.model
    def _tz_selection(self):
        import pytz
        return [(tz, tz) for tz in pytz.common_timezones]

    # ----------------------------------------------------------------------------------
    # validation
    # ----------------------------------------------------------------------------------

    @api.constrains("page_size", "poll_overlap_minutes")
    def _check_limits(self):
        for backend in self:
            if backend.page_size < 1:
                raise ValidationError(_("Page size must be at least 1."))
            if backend.poll_overlap_minutes < 0:
                raise ValidationError(_("Poll overlap cannot be negative."))

    # ----------------------------------------------------------------------------------
    # token helpers
    # ----------------------------------------------------------------------------------

    def _jwt_expiry(self, token):
        """Read `exp` out of a JWT without verifying it.

        We are not authenticating the token, only scheduling around it. Access and
        refresh tokens have very different lifetimes, so the access token's expiry is
        read rather than assumed.
        """
        self.ensure_one()
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            exp = claims.get("exp")
            if exp:
                # Odoo Datetime fields are naive UTC. utcfromtimestamp() is deprecated
                # from 3.12, so build it aware and drop the tzinfo explicitly.
                return datetime.fromtimestamp(
                    int(exp), tz=timezone.utc).replace(tzinfo=None)
        except (IndexError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
            _logger.info("Could not read exp from the Tipsoi token; will rely on 401s")
        return False

    def _token_expired(self):
        self.ensure_one()
        if not self.token_expiry:
            return False        # Device Portal tokens do not expire
        return fields.Datetime.now() >= (self.token_expiry - TOKEN_SKEW)

    # ----------------------------------------------------------------------------------
    # transport
    # ----------------------------------------------------------------------------------

    def client(self):
        """Return the adapter for this backend."""
        self.ensure_one()
        return tipsoi_client.build(self)

    @api.model
    def of_company(self, company=None, expected_type=None):
        """Return the single backend for a company.

        `expected_type` lets a job assert the mode it was written for, so a Device
        Portal job can never run against an HRM backend even by mistake.
        """
        company = company or self.env.company
        backend = self.search([("company_id", "=", company.id)], limit=1)
        if not backend:
            raise UserError(_("No Tipsoi backend is configured for %s.", company.name))
        if expected_type and backend.backend_type != expected_type:
            raise UserError(_(
                "This job is for '%(want)s' backends but %(name)s is '%(got)s'.",
                want=expected_type, name=backend.display_name,
                got=backend.backend_type))
        return backend

    # ----------------------------------------------------------------------------------
    # test connection
    # ----------------------------------------------------------------------------------

    def action_test_connection(self):
        """Authenticate, then check the configuration agrees with the remote.

        The mode check is the point of this button. The Device Portal reports whether an
        HRM is attached to the project, so a mismatch is detectable rather than a thing
        someone discovers months later when two systems have both been writing people.
        """
        self.ensure_one()
        adapter = self.client()
        try:
            adapter.login()
            warnings = self._check_mode_agreement(adapter)
        except tipsoi_client.TipsoiInactiveProjectError as exc:
            return self._fail(_(
                "Authentication worked, but the Tipsoi project is not active: %s\n\n"
                "This is not a credentials problem -- the account is switched off at "
                "the Tipsoi end.", exc.message))
        except tipsoi_client.TipsoiAuthError as exc:
            return self._fail(_("Tipsoi rejected the credentials: %s", exc.message))
        except tipsoi_client.TipsoiError as exc:
            return self._fail(_("Could not reach Tipsoi: %s", exc.message))

        self.write({"state": "ready", "last_error": False})
        message = _("Connected to %s.", self.display_name)
        if self.tipsoi_office_id:
            message += _("\nOffice ID: %s", self.tipsoi_office_id)
        if self.remote_organization:
            message += _("\nOrganization: %s", self.remote_organization)
        if warnings:
            message += "\n\n" + "\n".join(warnings)
        return self._notify(message, warning=bool(warnings))

    def _check_mode_agreement(self, adapter):
        """Refuse a configuration that would mix the two systems.

        Returns a list of non-fatal warnings; raises for the combination that would
        actually cause double-management of people.
        """
        self.ensure_one()
        warnings = []
        if self.backend_type != "device_portal":
            return warnings

        if self.remote_has_hrm:
            # This is the mixup the whole design exists to prevent. The HRM is already
            # creating this project's people -- tagged from_module="hrm" -- so an Odoo
            # write to the portal would fight it, and both would claim the identifier.
            raise UserError(_(
                "This Tipsoi project has the Tipsoi app (HRM) attached, but this "
                "backend is configured as 'Device Portal only'.\n\n"
                "The HRM already manages this project's people. Odoo writing them too "
                "would double-create against a per-project unique identifier.\n\n"
                "Switch this backend to 'Tipsoi app (HRM)' and point it at the HRM "
                "base URL."))
        return warnings

    def _fail(self, message):
        self.ensure_one()
        self.write({"state": "error", "last_error": message})
        return self._notify(message, danger=True)

    def _notify(self, message, warning=False, danger=False):
        kind = "danger" if danger else ("warning" if warning else "success")
        title = _("Tipsoi") if not danger else _("Tipsoi — not connected")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": kind,
                "sticky": danger or warning,
            },
        }
