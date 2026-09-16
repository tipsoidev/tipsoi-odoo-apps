# -*- coding: utf-8 -*-
"""Employees, in both directions.

**Matching is on `tipsoi_identifier` alone.** The Device Portal person's `identifier`
and the Tipsoi app's `employeeIdentifier` are verifiably the same string -- the app sets
one from the other when it creates the portal person -- so the backend record is
provenance and must never enter the match key. If it did, a client moving from Device
Portal mode to the Tipsoi app would duplicate every employee on the first sync.

**Which system owns the org chart depends on the mode, and that asymmetry is the point.**
A Device-Portal-only client has no department, designation or manager anywhere in Tipsoi
-- the person table simply has no such columns -- so Odoo owns those fields and the sync
must never touch them. A Tipsoi app client has the full master set, with the external
sync ids that write-back needs, so Tipsoi owns them and Odoo follows.

Write-back is explicit rather than automatic by default. Pushing on every ORM write is a
fast route to a surprising outbound call, so a push happens when someone asks for it, or
from a cron over records that were flagged -- see `auto_push_employees` on the backend.
"""

import base64
import binascii
import logging
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from . import tipsoi_client, tipsoi_time

_logger = logging.getLogger(__name__)

#: The portal accepts jpg/jpeg/png at up to 3 MB, and the Tipsoi app delegates image
#: validation to the portal, so one limit covers both.
PHOTO_MAX_BYTES = 3 * 1000 * 1000
_PHOTO_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png", "png"),
                     (b"\xff\xd8\xff", "image/jpeg", "jpg"))

#: HRM employee status codes as the API defines them. Note ARCHIVED = -1: the
#: documented 0..4 range is incomplete, and a negative status is a real value.
HRM_STATUS_ARCHIVED = -1
HRM_STATUS_INACTIVE = 0
HRM_STATUS_ACTIVE = 1
HRM_STATUS_TERMINATED = 2
HRM_STATUS_RESIGNED = 3
HRM_STATUS_TRANSFERRED = 4

#: The attendance feed reports `status` as **display text**, not as one of the integers
#: above. Anything not in these sets leaves Odoo's `active` alone,
#: which is deliberate: an unrecognised status is not evidence that someone has left.
HRM_STATUS_TEXT_ACTIVE = {"active"}
HRM_STATUS_TEXT_DEPARTED = {"inactive", "terminated", "resigned", "archived",
                            "transferred"}


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    tipsoi_identifier = fields.Char(
        string="Tipsoi identifier", index=True, copy=False, tracking=True,
        help="The single key that links this employee to Tipsoi. The same string "
             "identifies the person in the Device Portal and the employee in the Tipsoi "
             "app, which is why it is the only field matched on.")
    tipsoi_backend_id = fields.Many2one(
        "tipsoi.backend", copy=False, readonly=True, ondelete="set null",
        string="Synced from",
        help="Provenance only. Never part of the match key -- see the module docs.")
    tipsoi_person_id = fields.Integer(
        copy=False, readonly=True, string="Device Portal person ID",
        help="The person's id in the Device Portal. Needed to update or delete them "
             "in Device Portal mode.")
    tipsoi_employee_id = fields.Integer(
        copy=False, readonly=True, string="Tipsoi app employee ID", index=True,
        help="The employee's internal id in the Tipsoi app. Delete, resign and photo "
             "upload all key on it, so it has to be synced before those can run.")
    tipsoi_employee_office_id = fields.Char(
        copy=False, readonly=True, string="Tipsoi office employee ID",
        help="Device allocation keys on this, not on the identifier, and it is a "
             "string upstream. Attendance joins on the identifier instead -- both are "
             "needed, which is why both are stored.")
    tipsoi_card_no = fields.Char(
        copy=False, string="Tipsoi RFID",
        help="Kept here rather than in the standard badge field: Odoo's badge ID is "
             "used by the attendance kiosk and is unique, and overwriting it from a "
             "sync would break a feature the customer may already rely on.")
    tipsoi_status = fields.Char(
        copy=False, readonly=True, string="Status in Tipsoi")
    tipsoi_last_sync = fields.Datetime(copy=False, readonly=True)

    # -- masters, as the sync ids write-back needs ---------------------------------------
    # Storing the external sync ids means an Odoo-side update can echo the key Tipsoi
    # already knows, so no separate mapping table has to exist.
    tipsoi_department_sync_id = fields.Char(copy=False, readonly=True)
    tipsoi_designation_sync_id = fields.Char(copy=False, readonly=True)
    tipsoi_workplace_sync_id = fields.Char(copy=False, readonly=True)
    tipsoi_shift_group_sync_id = fields.Char(copy=False, readonly=True)
    tipsoi_subsidiary_sync_id = fields.Char(copy=False, readonly=True)
    tipsoi_employment_type_sync_id = fields.Char(copy=False, readonly=True)
    tipsoi_shift_start = fields.Char(copy=False, readonly=True, string="Shift start")
    tipsoi_shift_end = fields.Char(copy=False, readonly=True, string="Shift end")
    tipsoi_workplace_name = fields.Char(copy=False, readonly=True, string="Workplace")

    # -- photo -------------------------------------------------------------------------
    tipsoi_photo_state = fields.Selection(
        [("none", "Not sent"),
         ("pending", "Queued"),
         ("done", "Uploaded"),
         ("rejected", "Rejected by Tipsoi"),
         ("error", "Upload failed")],
        default="none", copy=False, readonly=True, string="Tipsoi photo")
    tipsoi_photo_error = fields.Text(copy=False, readonly=True)
    tipsoi_photo_error_code = fields.Char(copy=False, readonly=True)
    tipsoi_photo_url = fields.Char(copy=False, readonly=True)

    tipsoi_push_pending = fields.Boolean(
        default=False, copy=False, readonly=True, string="Push queued",
        help="Set when a synced field changes and the backend is configured to push "
             "automatically. The push cron clears it.")

    # NULLs are distinct in Postgres, so this constrains only the employees that
    # actually carry an identifier -- which is exactly the intent.
    _uniq_tipsoi_identifier = models.Constraint(
        "unique(company_id, tipsoi_identifier)",
        "Another employee in this company already uses that Tipsoi identifier. The "
        "identifier is the link to Tipsoi, so it has to be unique.")

    # ----------------------------------------------------------------------------------
    # queueing
    # ----------------------------------------------------------------------------------

    #: Fields whose change is worth pushing back. Deliberately short: these are the ones
    #: both APIs actually accept, so a longer list would queue pushes that change
    #: nothing remotely.
    _TIPSOI_PUSHED_FIELDS = ("name", "tipsoi_identifier", "tipsoi_card_no", "work_email",
                             "department_id", "job_id")

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("tipsoi_syncing"):
            return res
        linked = self.filtered("tipsoi_backend_id")
        if not linked:
            return res
        # A new photo is queued whichever way the backend is configured. Queueing is
        # not sending: the photo job only runs when the backend has photos enabled.
        if "image_1920" in vals:
            linked.with_context(tipsoi_syncing=True).write({
                "tipsoi_photo_state": "pending",
                "tipsoi_photo_error": False,
                "tipsoi_photo_error_code": False,
            })
        if any(f in vals for f in self._TIPSOI_PUSHED_FIELDS):
            auto = linked.filtered(lambda e: e.tipsoi_backend_id.auto_push_employees)
            if auto:
                auto.with_context(tipsoi_syncing=True).write(
                    {"tipsoi_push_pending": True})
        return res

    # ----------------------------------------------------------------------------------
    # matching
    # ----------------------------------------------------------------------------------

    @api.model
    def _tipsoi_find(self, backend, identifier):
        """Find the employee for a Tipsoi identifier.

        `active_test=False` matters: a departed employee is archived, and without it the
        next sync would not find them and would create a duplicate.
        """
        if not identifier:
            return self.browse()
        return self.with_context(active_test=False).search([
            ("tipsoi_identifier", "=", identifier),
            ("company_id", "=", backend.company_id.id),
        ], limit=1)

    @api.model
    def _tipsoi_department(self, backend, name):
        """Find or create a department by name. Tipsoi app mode only."""
        if not name:
            return self.env["hr.department"].browse()
        Department = self.env["hr.department"]
        found = Department.with_context(active_test=False).search([
            ("name", "=", name), ("company_id", "in", (backend.company_id.id, False)),
        ], limit=1)
        return found or Department.create(
            {"name": name, "company_id": backend.company_id.id})

    @api.model
    def _tipsoi_job(self, backend, name):
        """Find or create a job position by name. Tipsoi app mode only."""
        if not name:
            return self.env["hr.job"].browse()
        Job = self.env["hr.job"]
        found = Job.with_context(active_test=False).search([
            ("name", "=", name), ("company_id", "in", (backend.company_id.id, False)),
        ], limit=1)
        return found or Job.create({"name": name, "company_id": backend.company_id.id})

    # ----------------------------------------------------------------------------------
    # Device Portal mode: GET /people
    # ----------------------------------------------------------------------------------

    @api.model
    def _sync_device_portal(self, backend, run):
        """Upsert every person from the Device Portal.

        `GET /people` is not paginated at all: the whole set arrives in one response and
        there is no page parameter to reach for. That is a sizing concern for
        a large client rather than something the connector can page around.
        """
        adapter = backend.client()
        body = adapter.request("GET", "people")
        rows = body if isinstance(body, list) else (body or {}).get("data") or []
        run.fetched += len(rows)

        for row in rows:
            if not isinstance(row, dict):
                run.skipped += 1
                continue
            identifier = self._text(row.get("identifier"))
            if not identifier:
                # Without the identifier there is no join key, so the row cannot be
                # matched to anyone. Counted rather than dropped silently.
                run.skipped += 1
                continue

            employee = self._tipsoi_find(backend, identifier)
            name = (self._text(row.get("name"))
                    or self._text(row.get("primary_display_text"))
                    or identifier)
            vals = {
                "tipsoi_identifier": identifier,
                "tipsoi_backend_id": backend.id,
                "tipsoi_person_id": self._int(row.get("id")),
                "tipsoi_card_no": self._text(row.get("rfid")),
                "tipsoi_photo_url": self._text(row.get("photo_url")),
                "tipsoi_last_sync": fields.Datetime.now(),
            }
            # Odoo owns the org data in this mode, and the name too once the employee
            # exists: Tipsoi's person row holds display text for a device screen, not an
            # HR record, so it must not overwrite what HR typed.
            if employee:
                employee.with_context(tipsoi_syncing=True).write(vals)
                run.updated += 1
            else:
                vals.update({"name": name, "company_id": backend.company_id.id})
                self.with_context(tipsoi_syncing=True).create(vals)
                run.created += 1
        return True

    # ----------------------------------------------------------------------------------
    # Tipsoi app mode: from the GET /attendance payload
    # ----------------------------------------------------------------------------------

    @api.model
    def _upsert_from_hrm(self, backend, row, run=None):
        """Upsert one employee from one row of the attendance feed.

        The bulk attendance feed carries identity, the full master set and the per-day
        grid together, so this is called from the attendance sync rather than needing a
        feed of its own.
        """
        identifier = self._text(row.get("employeeIdentifier"))
        if not identifier:
            if run:
                run.skipped += 1
            return self.browse()

        employee = self._tipsoi_find(backend, identifier)
        vals = {
            "tipsoi_identifier": identifier,
            "tipsoi_backend_id": backend.id,
            "tipsoi_employee_id": self._int(row.get("employeeId")),
            # A string upstream, and used as one on the allocation call.
            "tipsoi_employee_office_id": self._text(row.get("employeeOfficeId")),
            "tipsoi_status": self._text(row.get("status")),
            "tipsoi_shift_start": self._text(row.get("shiftStartTime")),
            "tipsoi_shift_end": self._text(row.get("shiftEndTime")),
            "tipsoi_workplace_name": self._text(row.get("workplaceName")),
            "tipsoi_department_sync_id": self._text(row.get("departmentExternalSyncId")),
            "tipsoi_designation_sync_id":
                self._text(row.get("designationExternalSyncId")),
            "tipsoi_workplace_sync_id": self._text(row.get("workplaceExternalSyncId")),
            "tipsoi_shift_group_sync_id":
                self._text(row.get("shiftGroupExternalSyncId")),
            "tipsoi_subsidiary_sync_id": self._text(row.get("subsidiaryExternalSyncId")),
            "tipsoi_employment_type_sync_id":
                self._text(row.get("employmentTypeExternalSyncId")),
            "tipsoi_last_sync": fields.Datetime.now(),
        }
        name = self._text(row.get("employeeName"))
        if name:
            vals["name"] = name

        # Tipsoi owns the org chart in this mode, so these are written -- the opposite of
        # Device Portal mode, where they are left alone.
        if backend.sync_org_fields:
            department = self._tipsoi_department(
                backend, self._text(row.get("departmentName")))
            if department:
                vals["department_id"] = department.id
            job = self._tipsoi_job(backend, self._text(row.get("designationName")))
            if job:
                vals["job_id"] = job.id

        status = self._text(row.get("status")).lower()
        if backend.archive_departed and status:
            if status in HRM_STATUS_TEXT_ACTIVE:
                vals["active"] = True
            elif status in HRM_STATUS_TEXT_DEPARTED:
                vals["active"] = False

        if employee:
            employee.with_context(tipsoi_syncing=True).write(vals)
            if run:
                run.updated += 1
        else:
            vals["company_id"] = backend.company_id.id
            vals.setdefault("name", identifier)
            employee = self.with_context(tipsoi_syncing=True).create(vals)
            if run:
                run.created += 1
        return employee

    @api.model
    def _link_hrm_managers(self, backend, manager_map):
        """Second pass: resolve line managers once every employee exists.

        Managers cannot be linked as the rows arrive, because a manager can appear later
        in the feed than the person reporting to them.
        """
        if not manager_map or not backend.sync_org_fields:
            return 0
        linked = 0
        employees = self.with_context(active_test=False).browse(list(manager_map))
        for employee in employees.exists():
            manager = self.with_context(active_test=False).search([
                ("tipsoi_employee_id", "=", manager_map[employee.id]),
                ("company_id", "=", backend.company_id.id),
            ], limit=1)
            if manager and manager != employee and employee.parent_id != manager:
                employee.with_context(tipsoi_syncing=True).write(
                    {"parent_id": manager.id})
                linked += 1
        return linked

    # ----------------------------------------------------------------------------------
    # write-back
    # ----------------------------------------------------------------------------------

    def _tipsoi_backend(self):
        """The backend to write through, refusing anything ambiguous."""
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group(
                "tipsoi_connector.group_tipsoi_admin"):
            raise AccessError(_(
                "Writing to Tipsoi requires the Tipsoi Administrator role."))
        backend = self.tipsoi_backend_id
        if not backend:
            backend = self.env["tipsoi.backend"].sudo().search(
                [("company_id", "=", self.company_id.id)], limit=1)
        if not backend:
            raise UserError(_(
                "No Tipsoi backend is configured for %s.", self.company_id.name))
        return backend

    def action_tipsoi_push(self):
        """Create or update this employee in Tipsoi, through the backend's mode only."""
        for employee in self:
            backend = employee._tipsoi_backend()
            if backend.backend_type == "device_portal":
                employee._push_device_portal(backend)
            else:
                employee._push_hrm(backend)
        return True

    # -- Device Portal write path -------------------------------------------------------

    def _push_device_portal(self, backend):
        """Create or update the Device Portal person.

        This is the only mode in which Odoo writes the portal directly. In Tipsoi app
        mode the app creates the portal person itself, so writing it from here as well
        would double-create against a per-project unique identifier.
        """
        self.ensure_one()
        if not self.tipsoi_identifier:
            raise UserError(_(
                "%s has no Tipsoi identifier. Set one before pushing -- it is the key "
                "Tipsoi stores the person under.", self.display_name))
        adapter = backend.client()
        # Both display texts are required by the portal's validation, and they are what
        # the device screen shows, so they are worth filling meaningfully.
        data = {
            "identifier": self.tipsoi_identifier,
            "name": self.name or self.tipsoi_identifier,
            "primary_display_text": self.name or self.tipsoi_identifier,
            "secondary_display_text": (self.job_id.name or self.department_id.name
                                       or self.tipsoi_identifier),
        }
        if self.tipsoi_card_no:
            data["rfid"] = self.tipsoi_card_no

        files = None
        photo = self._tipsoi_photo_payload(strict=False)
        if photo and backend.sync_photos:
            files = {"image": photo}

        if self.tipsoi_person_id:
            path, method = "people/%s" % self.tipsoi_person_id, "POST"
            # The portal's update route is PUT/PATCH, but form-data carrying a file has
            # to be POSTed; `_method` is the override the portal accepts for that.
            data["_method"] = "PUT"
        else:
            path, method = "people", "POST"

        try:
            body = adapter.request(
                method, path, data=data, files=files,
                read_timeout=tipsoi_client.READ_TIMEOUT_PHOTO if files else None)
        except tipsoi_client.TipsoiError as exc:
            # The portal saves the person *before* the photo step, so a failure that
            # carries an error_code means the person now exists and only the photo
            # failed. Retrying the create would then collide on the unique identifier.
            person_id = self._int((exc.payload or {}).get("id"))
            if exc.error_code and person_id:
                self.with_context(tipsoi_syncing=True).write({
                    "tipsoi_person_id": person_id,
                    "tipsoi_push_pending": False,
                })
                self._record_photo_failure(exc)
                self.message_post(body=_(
                    "Created in Tipsoi, but the photo was rejected: %s", exc.message))
                return True
            raise

        payload = (body or {}).get("payload") or {} if isinstance(body, dict) else {}
        vals = {"tipsoi_push_pending": False}
        if isinstance(body, dict) and body.get("id"):
            vals["tipsoi_person_id"] = self._int(body["id"])
        if payload.get("photo_url"):
            vals["tipsoi_photo_url"] = payload["photo_url"]
        if files:
            vals.update({"tipsoi_photo_state": "done", "tipsoi_photo_error": False,
                         "tipsoi_photo_error_code": False})
        self.with_context(tipsoi_syncing=True).write(vals)
        self.message_post(body=_("Pushed to the Tipsoi Device Portal."))
        return True

    # -- Tipsoi app write path ----------------------------------------------------------

    def _push_hrm(self, backend):
        """Create or update the Tipsoi app employee, and let the app propagate.

        Creating requires an email and a password upstream. The email has to come from
        Odoo, because inventing one would create an account nobody can reach; the
        password is generated and never stored, since the employee is expected to use
        the app's own password reset.
        """
        self.ensure_one()
        adapter = backend.client()
        if self.tipsoi_employee_id:
            body = adapter.request(
                "POST", "employee/profile/%s" % self.tipsoi_employee_id,
                json_body=self._hrm_update_payload())
            self.with_context(tipsoi_syncing=True).write(
                {"tipsoi_push_pending": False})
            self.message_post(body=_("Updated in the Tipsoi app."))
            return body

        if not self.tipsoi_identifier:
            raise UserError(_(
                "%s has no Tipsoi identifier. Set one before pushing.",
                self.display_name))
        if not self.work_email:
            raise UserError(_(
                "The Tipsoi app requires a work email to create an employee, and %s "
                "has none. Add the work email, then push again.", self.display_name))
        payload = self._hrm_update_payload()
        payload.update({
            "name": self.name,
            "employeeOfficeId": (self.tipsoi_employee_office_id
                                 or self.tipsoi_identifier),
            "email": self.work_email,
            # Generated, not stored: Tipsoi requires one at creation and the employee
            # sets their own through the app's reset flow.
            "password": secrets.token_urlsafe(18),
        })
        if backend.tipsoi_office_id:
            payload["officeId"] = backend.tipsoi_office_id
        body = adapter.request("POST", "employee", json_body=payload)
        self.with_context(tipsoi_syncing=True).write({
            "tipsoi_push_pending": False,
            "tipsoi_employee_office_id": payload["employeeOfficeId"],
        })
        self.message_post(body=_(
            "Created in the Tipsoi app. Tipsoi creates the matching device-portal "
            "person itself, so nothing further is needed here."))
        return body

    def _hrm_update_payload(self):
        """The fields both the create and update requests share.

        Masters go back as their external sync ids -- the keys Tipsoi already stores --
        which is why those ids are kept on the employee and no mapping table exists.
        """
        self.ensure_one()
        payload = {
            "name": self.name,
            "employeeIdentifier": self.tipsoi_identifier or "",
        }
        if self.work_email:
            payload["email"] = self.work_email
        if self.tipsoi_card_no:
            payload["rfid"] = self.tipsoi_card_no
        if self.tipsoi_employee_office_id:
            payload["employeeOfficeId"] = self.tipsoi_employee_office_id
        for key, value in (
            ("departmentExternalSyncId", self.tipsoi_department_sync_id),
            ("designationExternalSyncId", self.tipsoi_designation_sync_id),
            ("workplaceExternalSyncId", self.tipsoi_workplace_sync_id),
            ("shiftGroupExternalSyncId", self.tipsoi_shift_group_sync_id),
            ("subsidiaryExternalSyncId", self.tipsoi_subsidiary_sync_id),
            ("employmentTypeExternalSyncId", self.tipsoi_employment_type_sync_id),
        ):
            if value:
                payload[key] = value
        return payload

    # -- departure ---------------------------------------------------------------------

    def action_tipsoi_depart(self, status=HRM_STATUS_RESIGNED, remark=None,
                             departure_date=None):
        """Record a departure in Tipsoi, preferring a status change to a delete.

        A hard delete removes the person that existing attendance rows point at. The
        Tipsoi app has real departure states, so use them; the Device Portal has no
        status concept at all, and there a departure is the soft delete.
        """
        for employee in self:
            backend = employee._tipsoi_backend()
            adapter = backend.client()
            if backend.backend_type == "hrm":
                if not employee.tipsoi_employee_id:
                    raise UserError(_(
                        "%s has no Tipsoi app employee ID yet, so Tipsoi cannot be "
                        "told about the departure. Run an attendance sync first.",
                        employee.display_name))
                # `/employee/{id}/status/{status}` accepts only 0 or 1: that path
                # variable rejects the documented 2 and 3 outright. So terminated and
                # resigned go through the purpose-built resign call, which also wants
                # the date.
                adapter.request("POST", "employee/resign", json_body={
                    "employeeId": employee.tipsoi_employee_id,
                    "status": int(status),
                    "statusRemark": remark or _("Departed in Odoo"),
                    "dateOfResignation": tipsoi_time.utc_to_millis(
                        departure_date or fields.Datetime.now()),
                })
                employee.message_post(body=_("Departure recorded in the Tipsoi app."))
            else:
                if not employee.tipsoi_person_id:
                    raise UserError(_(
                        "%s has no Device Portal person ID yet.",
                        employee.display_name))
                # Soft delete: the people table uses soft deletes, so punch history
                # keeps its subject.
                adapter.request("DELETE", "people/%s" % employee.tipsoi_person_id)
                employee.message_post(body=_(
                    "Removed from the Tipsoi Device Portal. The portal keeps the row "
                    "soft-deleted, so existing punch history is intact."))
            employee.with_context(tipsoi_syncing=True).write({"active": False})
        return True

    def action_tipsoi_reactivate(self):
        """Bring an employee back to active in Tipsoi. Tipsoi app mode only."""
        for employee in self:
            backend = employee._tipsoi_backend()
            if backend.backend_type != "hrm":
                raise UserError(_(
                    "The Device Portal has no employee status, so there is nothing to "
                    "reactivate. Push the person again instead."))
            if not employee.tipsoi_employee_id:
                raise UserError(_("%s has no Tipsoi app employee ID yet.",
                                  employee.display_name))
            backend.client().request(
                "POST", "employee/%s/status/%s"
                % (employee.tipsoi_employee_id, HRM_STATUS_ACTIVE))
            employee.with_context(tipsoi_syncing=True).write({"active": True})
            employee.message_post(body=_("Reactivated in the Tipsoi app."))
        return True

    def action_tipsoi_delete_remote(self):
        """Hard-delete in Tipsoi. Offered, but a departure is nearly always better."""
        for employee in self:
            backend = employee._tipsoi_backend()
            adapter = backend.client()
            if backend.backend_type == "hrm":
                if not employee.tipsoi_employee_id:
                    raise UserError(_("%s has no Tipsoi app employee ID yet.",
                                      employee.display_name))
                # Upstream deletes the device-portal person first and only then the app
                # employee, so a portal outage leaves both intact and this retryable.
                adapter.request("POST", "employee/delete", json_body={
                    "employeeIdList": [employee.tipsoi_employee_id]})
            else:
                if not employee.tipsoi_person_id:
                    raise UserError(_("%s has no Device Portal person ID yet.",
                                      employee.display_name))
                adapter.request("DELETE", "people/%s" % employee.tipsoi_person_id)
            employee.with_context(tipsoi_syncing=True).write({
                "active": False,
                "tipsoi_person_id": 0,
                "tipsoi_employee_id": 0,
            })
            employee.message_post(body=_("Deleted in Tipsoi."))
        return True

    # -- allocation --------------------------------------------------------------------

    def _tipsoi_allocate(self, devices, action):
        """Allocate or revoke these employees on these devices.

        One call per employee/device pair in the Tipsoi app, one call per device carrying
        the action array in the Device Portal -- each API's own shape. Both are
        idempotent, so a retry after a transient failure is safe.

        Returns a list of `(employee, device, ok, message)` so the caller can report per
        pair instead of failing the lot on the first problem.
        """
        results = []
        if action not in ("allocate", "revoke"):
            raise UserError(_("Action must be 'allocate' or 'revoke'."))
        for employee in self:
            backend = employee._tipsoi_backend()
            for device in devices:
                if device.backend_id != backend:
                    results.append((employee, device, False, _(
                        "%s belongs to a different Tipsoi backend.", device.name)))
                    continue
                try:
                    if backend.backend_type == "hrm":
                        employee._allocate_hrm(backend, device, action)
                    else:
                        employee._allocate_device_portal(backend, device, action)
                except tipsoi_client.TipsoiError as exc:
                    results.append((employee, device, False,
                                    employee._humanise_allocation_error(exc)))
                except UserError as exc:
                    results.append((employee, device, False, str(exc)))
                else:
                    results.append((employee, device, True, ""))
        for employee, device, ok, message in results:
            employee.message_post(body=(
                _("%(action)s on %(device)s: done", action=action.title(),
                  device=device.name) if ok else
                _("%(action)s on %(device)s failed: %(message)s",
                  action=action.title(), device=device.name, message=message)))
        return results

    def _allocate_hrm(self, backend, device, action):
        """One call; the Tipsoi app forwards it to the device portal itself.

        Keyed on `employeeOfficeId`, not the identifier, and upstream requires the
        employee to be ACTIVE -- see `_humanise_allocation_error` for why that matters.
        """
        self.ensure_one()
        if not self.tipsoi_employee_office_id:
            raise UserError(_(
                "%s has no Tipsoi office employee ID, which is what allocation keys "
                "on. Run an attendance sync first.", self.display_name))
        return backend.client().request("POST", "devices/allocate/customer", json_body={
            "employeeOfficeId": self.tipsoi_employee_office_id,
            "deviceIdentifier": device.identifier,
            "action": action,
        })

    def _allocate_device_portal(self, backend, device, action):
        """One call per device, carrying an array of actions."""
        self.ensure_one()
        if not self.tipsoi_identifier:
            raise UserError(_("%s has no Tipsoi identifier.", self.display_name))
        body = backend.client().request(
            "POST", "devices/%s/allocations" % device.identifier,
            json_body=[{"action": action,
                        "person_identifier": self.tipsoi_identifier}])
        # The portal answers 200 with a per-row payload, so success has to be read out
        # of `status` rather than assumed from the HTTP code. Its `message` key carries
        # the raw truthy/error value and is not a message at all.
        rows = (body or {}).get("payload") if isinstance(body, dict) else None
        for row in rows or []:
            if isinstance(row, dict) and row.get("status") == "failed":
                raise UserError(_(
                    "Tipsoi refused to %(action)s %(person)s on %(device)s.",
                    action=action, person=self.tipsoi_identifier,
                    device=device.identifier))
        return body

    def _humanise_allocation_error(self, exc):
        """Translate the one upstream message that reliably misleads.

        A terminated employee comes back as "Employee not found with ID: ..." because
        the lookup filters on ACTIVE. Taken literally it sends support hunting for a
        record that is right there.
        """
        message = exc.message or ""
        if "not found" in message.lower() and "employee" in message.lower():
            return _(
                "Tipsoi reports this employee as not found, which on this endpoint "
                "means they are not *active* in Tipsoi. Check their Tipsoi status "
                "before allocating. (Tipsoi said: %s)", message)
        return message

    # -- photos ------------------------------------------------------------------------

    def action_tipsoi_queue_photo(self):
        self.with_context(tipsoi_syncing=True).write({
            "tipsoi_photo_state": "pending",
            "tipsoi_photo_error": False,
            "tipsoi_photo_error_code": False,
        })
        return True

    def action_tipsoi_upload_photo(self):
        """Upload the Odoo photo to Tipsoi, one employee at a time.

        Kept out of the employee sync on purpose. The upload runs through a server-side
        enhancement step with a 120-second timeout and a concurrency limit of three, so
        it is a slow, resumable job of its own rather than a field on a bulk read.
        """
        for employee in self:
            backend = employee._tipsoi_backend()
            try:
                photo = employee._tipsoi_photo_payload(strict=True)
            except UserError as exc:
                employee.with_context(tipsoi_syncing=True).write({
                    "tipsoi_photo_state": "rejected",
                    "tipsoi_photo_error": str(exc),
                })
                continue
            try:
                if backend.backend_type == "hrm":
                    employee._upload_photo_hrm(backend, photo)
                else:
                    # The portal has no photo-only endpoint: the image rides on the
                    # person create/update, which is what this does.
                    employee._push_device_portal(backend)
            except tipsoi_client.TipsoiPhotoError as exc:
                # A photo the pipeline will never accept. Shown on the employee so HR
                # can supply a different picture, rather than retried forever.
                employee._record_photo_failure(exc)
            except tipsoi_client.TipsoiError as exc:
                employee.with_context(tipsoi_syncing=True).write({
                    "tipsoi_photo_state": "error",
                    "tipsoi_photo_error": exc.message,
                    "tipsoi_photo_error_code": exc.error_code or "",
                })
            else:
                employee.with_context(tipsoi_syncing=True).write({
                    "tipsoi_photo_state": "done",
                    "tipsoi_photo_error": False,
                    "tipsoi_photo_error_code": False,
                })
        return True

    def _upload_photo_hrm(self, backend, photo):
        """One call that also enrols the face on the recognition service.

        Upstream uploads to the portal, stores the returned URL, and then enrols the
        face best-effort -- so face enrolment comes free with the photo in this mode.
        """
        self.ensure_one()
        if not self.tipsoi_employee_id:
            raise UserError(_(
                "%s has no Tipsoi app employee ID yet, which the photo endpoint keys "
                "on. Run an attendance sync first.", self.display_name))
        return backend.client().request(
            "POST", "employee/profile/picture/%s" % self.tipsoi_employee_id,
            files={"file": photo}, read_timeout=tipsoi_client.READ_TIMEOUT_PHOTO)

    def action_tipsoi_remove_photo(self):
        """Remove the stored photo in Tipsoi. Tipsoi app mode only."""
        for employee in self:
            backend = employee._tipsoi_backend()
            if backend.backend_type != "hrm":
                raise UserError(_(
                    "The Device Portal has no separate photo-removal call; push the "
                    "person without an image instead."))
            if not employee.tipsoi_employee_id:
                raise UserError(_("%s has no Tipsoi app employee ID yet.",
                                  employee.display_name))
            backend.client().request(
                "POST",
                "employee/profile/picture/remove/%s" % employee.tipsoi_employee_id)
            employee.with_context(tipsoi_syncing=True).write({
                "tipsoi_photo_state": "none", "tipsoi_photo_url": False})
        return True

    def _tipsoi_photo_payload(self, strict=True):
        """Return a `(filename, bytes, content_type)` tuple, or None.

        Both APIs accept jpg and png only, at up to 3 MB. Checking here turns a remote
        422 into a message that names the actual problem, and tries the smaller stored
        resolution before giving up on size.
        """
        self.ensure_one()
        for field_name in ("image_1920", "image_1024"):
            raw = self[field_name]
            if not raw:
                continue
            try:
                data = base64.b64decode(raw)
            except (binascii.Error, TypeError, ValueError):
                continue
            kind = next((s for s in _PHOTO_SIGNATURES if data.startswith(s[0])), None)
            if not kind:
                if strict:
                    raise UserError(_(
                        "Tipsoi accepts JPEG and PNG photos only, and %s's photo is "
                        "neither. Re-upload it as a JPEG or PNG.", self.display_name))
                return None
            if len(data) > PHOTO_MAX_BYTES:
                continue
            return ("%s.%s" % (self.tipsoi_identifier or "photo", kind[2]),
                    data, kind[1])
        if strict:
            raise UserError(_(
                "%s has no photo Tipsoi can take -- it needs a JPEG or PNG under 3 MB.",
                self.display_name))
        return None

    def _record_photo_failure(self, exc):
        self.ensure_one()
        self.with_context(tipsoi_syncing=True).write({
            "tipsoi_photo_state": "rejected",
            "tipsoi_photo_error": exc.message,
            "tipsoi_photo_error_code": exc.error_code or "",
        })
        self.message_post(body=_(
            "Tipsoi will not accept this photo (%(code)s): %(message)s",
            code=exc.error_code or _("no code"), message=exc.message))

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
