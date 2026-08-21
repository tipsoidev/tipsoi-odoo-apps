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

    # -- what to sync ------------------------------------------------------------------
    # Every one of these is a switch a customer will eventually want, and each defaults
    # to the behaviour the mode implies rather than to "on".
    sync_employees = fields.Boolean(
        default=True, string="Sync employees",
        help="Device Portal mode reads the people list; Tipsoi app mode gets employees "
             "from the attendance feed, so there this only controls whether they are "
             "written to Odoo.")
    sync_devices = fields.Boolean(default=True, string="Sync devices")
    sync_attendance = fields.Boolean(default=True, string="Sync attendance")
    sync_photos = fields.Boolean(
        default=True, string="Send photos to Tipsoi",
        help="Uploads run in their own slow job. A photo is only queued when someone "
             "changes it in Odoo, so switching this on does not push every existing "
             "photo at once.")
    sync_org_fields = fields.Boolean(
        default=True, string="Tipsoi owns department & job",
        help="Tipsoi app mode only, and the direction of truth for the org chart. The "
             "Device Portal holds no department, designation or manager at all, so in "
             "that mode Odoo owns them and this setting has no effect.")
    archive_departed = fields.Boolean(
        default=True, string="Archive departed employees",
        help="Tipsoi app mode only. An employee Tipsoi reports as terminated, resigned "
             "or transferred is archived in Odoo. An unrecognised status is left "
             "alone -- it is not evidence that someone has left.")
    auto_push_employees = fields.Boolean(
        default=False, string="Push employee changes automatically",
        help="Off by default on purpose: with it on, editing an employee in Odoo queues "
             "an outbound write to Tipsoi. Leave it off to push only when asked.")

    hrm_window_days = fields.Integer(
        default=3, string="Rolling window (days)",
        help="Tipsoi app mode. These rows are derived, so they change after the fact "
             "when a manual entry is approved or leave is applied. Re-reading the last "
             "few days and upserting is both simpler and more correct than trying to "
             "cursor a computed view -- which is why this mode has a window rather than "
             "a cursor.")
    punch_backfill_days = fields.Integer(
        default=7, string="First-run backfill (days)",
        help="How far back the very first punch poll reaches, before there is a cursor "
             "to continue from.")
    photo_batch_size = fields.Integer(
        default=3, string="Photos per run",
        help="The enhancement pipeline upstream runs three at a time and allows up to "
             "120 seconds per image, so firing more in parallel only builds a queue.")
    pair_duplicate_seconds = fields.Integer(
        default=60, string="Collapse duplicates within (s)",
        help="Device Portal mode. Two reads of the same person this close together are "
             "one punch -- a second finger press, not an exit.")
    max_shift_hours = fields.Integer(
        default=16, string="Longest shift (hours)",
        help="Device Portal mode. How far after a check-in an exit punch can still "
             "close that shift. This is what makes an overnight shift pair correctly "
             "instead of splitting at midnight, so it must exceed the longest real "
             "shift and stay under the gap to the next one.")

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

    # -- at-a-glance counts --------------------------------------------------------------
    device_count = fields.Integer(compute="_compute_counts")
    punch_count = fields.Integer(compute="_compute_counts")
    unmatched_punch_count = fields.Integer(compute="_compute_counts")
    day_count = fields.Integer(compute="_compute_counts")
    employee_count = fields.Integer(compute="_compute_counts")
    pending_photo_count = fields.Integer(compute="_compute_counts")

    def _compute_counts(self):
        for backend in self:
            backend.device_count = self.env["tipsoi.device"].search_count(
                [("backend_id", "=", backend.id)])
            backend.punch_count = self.env["tipsoi.punch.log"].search_count(
                [("backend_id", "=", backend.id)])
            backend.unmatched_punch_count = self.env["tipsoi.punch.log"].search_count(
                [("backend_id", "=", backend.id), ("state", "=", "unmatched")])
            backend.day_count = self.env["tipsoi.day.attendance"].search_count(
                [("backend_id", "=", backend.id)])
            backend.employee_count = self.env["hr.employee"].with_context(
                active_test=False).search_count([("tipsoi_backend_id", "=", backend.id)])
            backend.pending_photo_count = self.env["hr.employee"].with_context(
                active_test=False).search_count([
                    ("tipsoi_backend_id", "=", backend.id),
                    ("tipsoi_photo_state", "=", "pending")])

    def _source_tz(self):
        """The timezone the Device Portal's naive timestamps are in."""
        self.ensure_one()
        return self.source_timezone or "Asia/Dhaka"

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
        """Return the adapter for this backend.

        `sudo()` because the credentials sit behind a field-level group: without it a
        sync triggered by a Tipsoi user would read the username as False and fail as an
        authentication error rather than as the permission problem it is. The secrets
        stay unreadable through the UI and the ORM -- they are only used in here.
        """
        self.ensure_one()
        return tipsoi_client.build(self.sudo())

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

    # ----------------------------------------------------------------------------------
    # stat buttons
    # ----------------------------------------------------------------------------------
    # Returned from Python rather than pointing the button at an action id with an
    # `active_id` context. Odoo's view validator reads `active_id` in a button context as
    # a field on this model, does not find one, and refuses to install the form -- and
    # building the domain here is clearer anyway, since it is the same domain
    # `_compute_counts` uses.

    def _open(self, name, model, domain, context=None, view_mode="list,form"):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": view_mode,
            "domain": domain,
            "context": dict(context or {}, default_backend_id=self.id),
        }

    def action_open_devices(self):
        return self._open(_("Devices"), "tipsoi.device",
                          [("backend_id", "=", self.id)], view_mode="kanban,list,form")

    def action_open_employees(self):
        return self._open(
            _("Synced employees"), "hr.employee",
            [("tipsoi_backend_id", "=", self.id)],
            # Departed employees are archived, and they are exactly the ones somebody
            # opening this count is likely to be looking for.
            context={"active_test": False})

    def action_open_punches(self):
        return self._open(_("Punch logs"), "tipsoi.punch.log",
                          [("backend_id", "=", self.id)])

    def action_open_unmatched_punches(self):
        return self._open(_("Unmatched punches"), "tipsoi.punch.log",
                          [("backend_id", "=", self.id), ("state", "=", "unmatched")])

    def action_open_days(self):
        return self._open(_("Daily attendance"), "tipsoi.day.attendance",
                          [("backend_id", "=", self.id)])

    def action_open_pending_photos(self):
        return self._open(
            _("Photos queued for Tipsoi"), "hr.employee",
            [("tipsoi_backend_id", "=", self.id),
             ("tipsoi_photo_state", "=", "pending")],
            context={"active_test": False})

    # ----------------------------------------------------------------------------------
    # jobs
    # ----------------------------------------------------------------------------------
    # Every job is both a cron entry point and a button, and every one of them asserts
    # the mode it was written for. That assertion is not defensive tidiness: a Device
    # Portal job running against a Tipsoi app backend is precisely the mixing this
    # design exists to prevent, and a cron is where such a mistake would go unnoticed.

    @api.model
    def _ready_backends(self, mode=None):
        """Configured backends, optionally narrowed to one mode.

        Errored backends are **included**, and that is deliberate. A backend only reaches
        `error` from a problem a human has to fix -- bad credentials, an inactive Tipsoi
        project, a mode mismatch -- and it must stay visible on the form until they do.
        But excluding it here would mean a backend could never recover on its own, and
        the earlier version of this method did exactly that: one failure took the backend
        out of every scheduled job until somebody pressed Test Connection, with nothing
        but growing staleness to show for it. A transient failure now leaves the state
        alone entirely (see `_record_job_failure`), and a successful run clears the error.

        Draft backends are still skipped: one that has never connected has nothing to say.
        """
        domain = [("state", "in", ("ready", "error"))]
        if mode:
            domain.append(("backend_type", "=", mode))
        return self.sudo().search(domain)

    def _record_job_failure(self, exc):
        """Record a failed job, and decide whether it should halt the schedule.

        The distinction matters more than it looks. A *configuration* problem has to stop
        and be seen: no amount of retrying fixes a wrong password or a project that has
        been switched off, and quietly retrying it every five minutes helps nobody. A
        *transient* problem must not touch the state at all, because the next tick is
        exactly how it recovers -- the transport has already exhausted its own backoff by
        the time we get here.
        """
        self.ensure_one()
        halting = (tipsoi_client.TipsoiAuthError,
                   tipsoi_client.TipsoiInactiveProjectError,
                   tipsoi_client.TipsoiPermanentError,
                   UserError, ValidationError)
        vals = {"last_error": str(exc)}
        if isinstance(exc, halting):
            vals["state"] = "error"
        self.sudo().write(vals)

    def _record_job_success(self):
        """Clear a stale error once a job has actually worked."""
        self.ensure_one()
        if self.state != "ready" or self.last_error:
            self.sudo().write({"state": "ready", "last_error": False})

    def _run(self, job, method, mode=None, window_from=None, window_to=None):
        """Execute one job inside an audited sync run.

        A failure is recorded on the run and re-raised, so a cron logs it and the next
        tick tries again, rather than a job quietly deciding it succeeded.
        """
        self.ensure_one()
        if mode and self.backend_type != mode:
            return False
        backend = self.sudo()
        with self.env["tipsoi.sync.run"].track(
                backend, job, window_from=window_from, window_to=window_to) as run:
            method(backend, run)
        return True

    # -- employees ---------------------------------------------------------------------

    @api.model
    def _cron_sync_employees(self):
        for backend in self._ready_backends("device_portal"):
            if backend.sync_employees:
                backend.action_sync_employees()

    def action_sync_employees(self):
        """Device Portal mode. In Tipsoi app mode employees arrive with attendance.

        There is no updated-since filter on the people list, so this reads the whole set
        every time. That is upstream's shape, not a choice here.
        """
        for backend in self:
            if backend.backend_type != "device_portal":
                raise UserError(_(
                    "In Tipsoi app mode employees come from the attendance feed, "
                    "because one paginated call carries identity, the org masters and "
                    "the days together. Use Sync Attendance instead."))
            backend._run(
                "employees",
                lambda b, run: (
                    self.env["hr.employee"]._sync_device_portal(b, run),
                    b.write({"last_employee_sync": fields.Datetime.now()})),
                mode="device_portal")
        return True

    @api.model
    def _cron_push_employees(self):
        """Push the employees that were flagged by an Odoo-side edit."""
        for backend in self._ready_backends():
            if not backend.auto_push_employees:
                continue
            pending = self.env["hr.employee"].with_context(active_test=False).search([
                ("tipsoi_backend_id", "=", backend.id),
                ("tipsoi_push_pending", "=", True),
            ])
            if pending:
                backend._run("writeback", lambda b, run, recs=pending:
                             b._push_employees(recs, run))

    def _push_employees(self, employees, run):
        """Push each employee independently, so one bad record cannot stop the rest."""
        self.ensure_one()
        run.fetched += len(employees)
        notes = []
        for employee in employees:
            # The context-manager form, deliberately: `Savepoint.close()` defaults to
            # rollback=True, so closing one explicitly after a *successful* call throws
            # that work away. `with` releases on success and rolls back only on an
            # exception.
            try:
                with self.env.cr.savepoint():
                    employee.sudo().action_tipsoi_push()
            except Exception as exc:           # noqa: BLE001 - recorded, not swallowed
                # The savepoint has already rolled back, so the ORM cache may still hold
                # values for rows that no longer exist.
                self.env.cache.invalidate()
                run.failed += 1
                notes.append("%s: %s" % (employee.display_name, exc))
                _logger.warning("Tipsoi push failed for %s: %s",
                                employee.display_name, exc)
            else:
                run.updated += 1
        if notes:
            run.add_note("\n".join(notes[:50]))

    # -- devices -----------------------------------------------------------------------

    @api.model
    def _cron_sync_devices(self):
        for backend in self._ready_backends():
            if backend.sync_devices:
                backend.action_sync_devices()

    def action_sync_devices(self):
        """Both modes read their own device list; neither reads the other's."""
        for backend in self:
            backend._run(
                "devices",
                lambda b, run: (
                    self.env["tipsoi.device"]._sync_backend(b, run),
                    b.write({"last_device_sync": fields.Datetime.now()})))
        return True

    # -- Device Portal punches -----------------------------------------------------------

    @api.model
    def _cron_poll_punches(self):
        for backend in self._ready_backends("device_portal"):
            if backend.sync_attendance:
                backend.action_poll_punches()

    def _punch_window(self):
        """The window to ask the Device Portal for.

        The overlap is not only clock-skew insurance. The portal filters on `updated_at`
        while ordering by `created_at`, so a row updated mid-pagination shifts the
        offsets and rows can be skipped between pages. Re-reading the tail is what
        catches them; the unique index on the punch id absorbs the duplicates.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        if self.last_log_sync_time:
            start = self.last_log_sync_time - timedelta(
                minutes=max(self.poll_overlap_minutes, 0))
        else:
            start = now - timedelta(days=max(self.punch_backfill_days, 1))
        return start, now

    def action_poll_punches(self):
        for backend in self:
            if backend.backend_type != "device_portal":
                raise UserError(_(
                    "The Tipsoi app has no office-wide punch feed -- its punch detail "
                    "is per employee. Use Sync Attendance, which reads the day rows "
                    "the app has already paired."))
            window_from, window_to = backend._punch_window()
            backend._run(
                "punches",
                lambda b, run, f=window_from, t=window_to: (
                    self.env["tipsoi.punch.log"]._poll(b, run, f, t),
                    # The cursor advances to the window end that was *requested*. The
                    # rows report `created_at` under the name `sync_time` while the
                    # filter runs on `updated_at`, so a cursor taken from a returned
                    # value can walk backwards and re-scan forever.
                    b.write({"last_log_sync_time": t})),
                mode="device_portal", window_from=window_from, window_to=window_to)
        return True

    @api.model
    def _cron_pair_punches(self):
        for backend in self._ready_backends("device_portal"):
            if backend.sync_attendance:
                backend.action_pair_punches()

    def action_pair_punches(self):
        for backend in self:
            if backend.backend_type != "device_portal":
                raise UserError(_(
                    "Pairing is Device Portal mode only. In Tipsoi app mode the app has "
                    "already paired each day."))
            backend._run(
                "pairing",
                lambda b, run: self.env["tipsoi.punch.log"]._pair(b, run),
                mode="device_portal")
        return True

    # -- Tipsoi app attendance -----------------------------------------------------------

    @api.model
    def _cron_sync_attendance(self):
        for backend in self._ready_backends("hrm"):
            if backend.sync_attendance:
                backend.action_sync_attendance()

    def _attendance_window(self):
        """A rolling re-read, deliberately not a cursor.

        `last_attendance_sync` records when the last read happened; it does not narrow
        the next one. Narrowing it would miss exactly the changes this window exists to
        catch -- a manual entry approved yesterday, leave applied this morning.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        return now - timedelta(days=max(self.hrm_window_days, 1)), now

    def action_sync_attendance(self):
        for backend in self:
            if backend.backend_type != "hrm":
                raise UserError(_(
                    "This is the Tipsoi app feed. In Device Portal mode use Poll "
                    "Punches, which reads raw punches and pairs them here."))
            window_from, window_to = backend._attendance_window()
            backend._run(
                "attendance",
                lambda b, run, f=window_from, t=window_to: (
                    self.env["tipsoi.day.attendance"]._sync(b, run, f, t),
                    b.write({"last_attendance_sync": t,
                             "last_employee_sync": t})),
                mode="hrm", window_from=window_from, window_to=window_to)
        return True

    @api.model
    def _cron_import_days(self):
        for backend in self._ready_backends("hrm"):
            if backend.sync_attendance:
                backend.action_import_days()

    def action_import_days(self):
        for backend in self:
            if backend.backend_type != "hrm":
                raise UserError(_("Day import is Tipsoi app mode only."))
            backend._run(
                "day_import",
                lambda b, run: self.env["tipsoi.day.attendance"]._import_days(b, run),
                mode="hrm")
        return True

    # -- photos ------------------------------------------------------------------------

    @api.model
    def _cron_push_photos(self):
        for backend in self._ready_backends():
            if backend.sync_photos:
                backend.action_push_photos()

    def action_push_photos(self):
        """Upload queued photos, a few at a time.

        Serialised on purpose. Upstream allows three concurrent enhancements at up to
        120 seconds each, so a bulk backfill that fires fifty at once just queues behind
        that limit while holding a cron worker.
        """
        for backend in self:
            pending = self.env["hr.employee"].with_context(active_test=False).search([
                ("tipsoi_backend_id", "=", backend.id),
                ("tipsoi_photo_state", "=", "pending"),
            ], limit=max(backend.photo_batch_size, 1))
            if not pending:
                continue
            backend._run("photos", lambda b, run, recs=pending:
                         b._upload_photos(recs, run))
        return True

    def _upload_photos(self, employees, run):
        self.ensure_one()
        run.fetched += len(employees)
        for employee in employees:
            # See `_push_employees`: `close()` rolls back by default, so the explicit
            # form would discard each successful upload's state write.
            try:
                with self.env.cr.savepoint():
                    employee.sudo().action_tipsoi_upload_photo()
            except Exception as exc:           # noqa: BLE001 - recorded, not swallowed
                self.env.cache.invalidate()
                run.failed += 1
                _logger.warning("Tipsoi photo upload failed for %s: %s",
                                employee.display_name, exc)
            else:
                # A rejected photo is a completed attempt, not a failure to retry: the
                # pipeline will reject the same image every time.
                if employee.tipsoi_photo_state == "rejected":
                    run.skipped += 1
                elif employee.tipsoi_photo_state == "done":
                    run.updated += 1
                else:
                    run.failed += 1

    # -- housekeeping --------------------------------------------------------------------

    @api.model
    def _cron_vacuum(self, staging_days=90, run_days=30):
        """Drop staging rows and audit rows that have outlived their usefulness.

        Only *paired* staging rows go: an unmatched punch is an open question for HR and
        must survive until someone answers it.
        """
        now = fields.Datetime.now()
        staging_cutoff = now - timedelta(days=staging_days)
        punches = self.env["tipsoi.punch.log"].sudo().search([
            ("state", "in", ("paired", "duplicate")),
            ("punch_time_utc", "<", staging_cutoff),
        ])
        days = self.env["tipsoi.day.attendance"].sudo().search([
            ("state", "in", ("imported", "skipped")),
            ("create_date", "<", staging_cutoff),
        ])
        runs = self.env["tipsoi.sync.run"].sudo().search([
            ("started_at", "<", now - timedelta(days=run_days))])
        counts = (len(punches), len(days), len(runs))
        punches.unlink()
        days.unlink()
        runs.unlink()
        _logger.info("Tipsoi vacuum removed %s punches, %s day rows, %s sync runs",
                     *counts)
        return counts
