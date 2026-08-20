# -*- coding: utf-8 -*-
"""Employee sync, and the asymmetry between the two modes.

The rule this file mostly exists to defend: **which system owns the org chart depends on
the mode.** A Device-Portal-only client has no department, designation or manager anywhere
in Tipsoi -- the person table has no such columns -- so Odoo owns them and the sync must
leave them alone. A Tipsoi app client has the full master set, so Tipsoi owns them.

The second rule: employees match on `tipsoi_identifier` **alone**. The Device Portal's
`identifier` and the Tipsoi app's `employeeIdentifier` are the same string, so putting the
backend into the match key would duplicate every employee the day a client moves from one
mode to the other. There is a test for exactly that below.
"""

from odoo.tests import tagged

from .common import TipsoiCase

DP_PEOPLE = [
    {"id": 501, "identifier": "E-001", "name": "Rahim Uddin", "rfid": "CARD-1",
     "primary_display_text": "Rahim", "secondary_display_text": "Ops",
     "photo_url": "https://cdn.example/1.jpg", "from_module": "portal",
     "total_fingerprints": 2},
    {"id": 502, "identifier": "E-002", "name": "", "rfid": "",
     "primary_display_text": "Karim", "secondary_display_text": "Ops",
     "photo_url": False, "total_fingerprints": 0},
]


def hrm_employee_row(**overrides):
    row = {
        "employeeId": 9001,
        "employeeName": "Rahim Uddin",
        # A String upstream, and it can legitimately be non-numeric.
        "employeeOfficeId": "OFF-77A",
        "employeeIdentifier": "E-001",
        "departmentName": "Operations",
        "departmentExternalSyncId": "DEP-SYNC-1",
        "designationName": "Supervisor",
        "designationExternalSyncId": "DES-SYNC-1",
        "employmentTypeExternalSyncId": "EMP-SYNC-1",
        "shiftGroupExternalSyncId": "SHIFT-SYNC-1",
        "workplaceExternalSyncId": "WP-SYNC-1",
        "subsidiaryExternalSyncId": "SUB-SYNC-1",
        "workplaceName": "Head office",
        "shiftStartTime": "09:00 AM",
        "shiftEndTime": "06:00 PM",
        "pictureURL": "https://cdn.example/9001.jpg",
        "status": "Active",
        "attendance": {},
    }
    row.update(overrides)
    return row


@tagged("post_install", "-at_install")
class TestDevicePortalEmployeeSync(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")

    def _sync(self, people=None):
        self.transport.route("GET", "people",
                             DP_PEOPLE if people is None else people)
        with self._run(self.backend, "employees") as run:
            self.env["hr.employee"]._sync_device_portal(self.backend, run)
        return run

    def _find(self, identifier):
        return self.env["hr.employee"].with_context(active_test=False).search([
            ("tipsoi_identifier", "=", identifier),
            ("company_id", "=", self.backend.company_id.id)])

    def test_people_become_employees_matched_on_the_identifier(self):
        run = self._sync()
        self.assertEqual(run.fetched, 2)
        self.assertEqual(run.created, 2)

        rahim = self._find("E-001")
        self.assertEqual(rahim.name, "Rahim Uddin")
        self.assertEqual(rahim.tipsoi_person_id, 501)
        self.assertEqual(rahim.tipsoi_card_no, "CARD-1")
        self.assertEqual(rahim.tipsoi_photo_url, "https://cdn.example/1.jpg")
        self.assertEqual(rahim.tipsoi_backend_id, self.backend)
        self.assertTrue(rahim.tipsoi_last_sync)

    def test_the_display_text_names_someone_with_no_name_column(self):
        """A punch row carries no person name at all, only display texts."""
        self._sync()
        self.assertEqual(self._find("E-002").name, "Karim")

    def test_a_person_without_an_identifier_is_counted_never_dropped_silently(self):
        run = self._sync(people=[DP_PEOPLE[0], {"id": 999, "name": "No key"}])
        self.assertEqual(run.skipped, 1)
        self.assertEqual(run.created, 1)

    def test_a_second_sync_updates_rather_than_duplicating(self):
        self._sync()
        run = self._sync()
        self.assertEqual(run.created, 0)
        self.assertEqual(run.updated, 2)
        self.assertEqual(len(self._find("E-001")), 1)

    def test_odoo_keeps_ownership_of_the_org_fields_and_the_name(self):
        """The portal has no department, job or manager columns to sync *from*.

        So overwriting them here would blank out whatever HR typed, on every hourly run.
        """
        self._sync()
        rahim = self._find("E-001")
        department = self.env["hr.department"].create(
            {"name": "Odoo-owned dept", "company_id": self.backend.company_id.id})
        job = self.env["hr.job"].create(
            {"name": "Odoo-owned job", "company_id": self.backend.company_id.id})
        rahim.with_context(tipsoi_syncing=True).write({
            "name": "Rahim U. (edited in Odoo)",
            "department_id": department.id,
            "job_id": job.id,
        })

        self._sync()
        rahim.invalidate_recordset()
        self.assertEqual(rahim.name, "Rahim U. (edited in Odoo)")
        self.assertEqual(rahim.department_id, department)
        self.assertEqual(rahim.job_id, job)

    def test_an_existing_unlinked_employee_is_adopted_by_identifier(self):
        existing = self._employee("E-001", backend=self.backend, name="Already here")
        self._sync()
        self.assertEqual(len(self._find("E-001")), 1)
        self.assertEqual(self._find("E-001"), existing)


@tagged("post_install", "-at_install")
class TestHrmEmployeeSync(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm")

    def _upsert(self, row=None, run=None):
        row = hrm_employee_row() if row is None else row
        if run is None:
            with self._run(self.backend, "attendance") as run:
                return self.env["hr.employee"]._upsert_from_hrm(
                    self.backend, row, run)
        return self.env["hr.employee"]._upsert_from_hrm(self.backend, row, run)

    def test_identity_masters_and_sync_ids_all_land(self):
        employee = self._upsert()
        self.assertEqual(employee.tipsoi_identifier, "E-001")
        self.assertEqual(employee.tipsoi_employee_id, 9001)
        self.assertEqual(employee.name, "Rahim Uddin")
        self.assertEqual(employee.tipsoi_shift_start, "09:00 AM")
        self.assertEqual(employee.tipsoi_shift_end, "06:00 PM")
        self.assertEqual(employee.tipsoi_workplace_name, "Head office")
        self.assertEqual(employee.tipsoi_department_sync_id, "DEP-SYNC-1")
        self.assertEqual(employee.tipsoi_designation_sync_id, "DES-SYNC-1")
        self.assertEqual(employee.tipsoi_workplace_sync_id, "WP-SYNC-1")
        self.assertEqual(employee.tipsoi_shift_group_sync_id, "SHIFT-SYNC-1")
        self.assertEqual(employee.tipsoi_subsidiary_sync_id, "SUB-SYNC-1")
        self.assertEqual(employee.tipsoi_employment_type_sync_id, "EMP-SYNC-1")

    def test_the_office_employee_id_stays_a_string(self):
        """It is a String upstream and allocation sends it as one.

        Storing it as an integer would corrupt any non-numeric value, and allocation --
        which keys on this rather than on the identifier -- would then fail.
        """
        employee = self._upsert()
        self.assertEqual(employee.tipsoi_employee_office_id, "OFF-77A")

    def test_tipsoi_owns_department_and_job_in_this_mode(self):
        employee = self._upsert()
        self.assertEqual(employee.department_id.name, "Operations")
        self.assertEqual(employee.job_id.name, "Supervisor")

    def test_masters_are_reused_rather_than_recreated(self):
        first = self._upsert()
        second = self._upsert(hrm_employee_row(
            employeeIdentifier="E-002", employeeId=9002, employeeName="Karim"))
        self.assertEqual(first.department_id, second.department_id)
        self.assertEqual(
            self.env["hr.department"].search_count([("name", "=", "Operations")]), 1)

    def test_sync_org_fields_off_leaves_the_org_chart_to_odoo(self):
        self.backend.sync_org_fields = False
        employee = self._upsert()
        self.assertFalse(employee.department_id)
        self.assertFalse(employee.job_id)

    # -- status ---------------------------------------------------------------------------

    def test_an_active_status_keeps_the_employee_active(self):
        self.assertTrue(self._upsert().active)

    def test_departed_statuses_archive_the_employee(self):
        for index, status in enumerate(
                ("Resigned", "Terminated", "Archived", "Transferred", "Inactive")):
            employee = self._upsert(hrm_employee_row(
                employeeIdentifier="D-%s" % index, employeeId=9100 + index,
                status=status))
            self.assertFalse(employee.active, status)

    def test_an_unrecognised_status_leaves_active_alone(self):
        """`status` is display text upstream, not the 0-4 integer.

        An unknown or empty value is not evidence that somebody has left, so it must not
        archive them -- that would be a destructive read of a string nobody validated.
        """
        employee = self._upsert()
        self.assertTrue(employee.active)
        self._upsert(hrm_employee_row(status="On probation"))
        employee.invalidate_recordset()
        self.assertTrue(employee.active)
        self._upsert(hrm_employee_row(status=""))
        employee.invalidate_recordset()
        self.assertTrue(employee.active)

    def test_a_returning_employee_is_reactivated(self):
        self._upsert(hrm_employee_row(status="Resigned"))
        employee = self._upsert(hrm_employee_row(status="Active"))
        self.assertTrue(employee.active)

    def test_archive_departed_off_suppresses_archiving_entirely(self):
        self.backend.archive_departed = False
        employee = self._upsert(hrm_employee_row(status="Resigned"))
        self.assertTrue(employee.active)
        self.assertEqual(employee.tipsoi_status, "Resigned")

    # -- managers -------------------------------------------------------------------------

    def test_line_managers_are_linked_in_a_second_pass(self):
        """The manager can appear later in the feed than the person reporting to them."""
        report = self._upsert(hrm_employee_row(
            employeeIdentifier="E-010", employeeId=9010, employeeName="Report",
            lineManagerId=9020))
        manager_map = {report.id: 9020}
        # The manager only exists after the report -- which is the case under test.
        self.assertEqual(
            self.env["hr.employee"]._link_hrm_managers(self.backend, manager_map), 0)

        manager = self._upsert(hrm_employee_row(
            employeeIdentifier="E-020", employeeId=9020, employeeName="Manager"))
        self.assertEqual(
            self.env["hr.employee"]._link_hrm_managers(self.backend, manager_map), 1)
        report.invalidate_recordset()
        self.assertEqual(report.parent_id, manager)

    def test_an_employee_is_never_made_their_own_manager(self):
        employee = self._upsert(hrm_employee_row(employeeId=9030))
        self.env["hr.employee"]._link_hrm_managers(self.backend, {employee.id: 9030})
        employee.invalidate_recordset()
        self.assertFalse(employee.parent_id)

    # -- the reason the match key is what it is --------------------------------------------

    def test_a_client_moving_from_device_portal_to_the_app_keeps_one_employee(self):
        """The migration case, and why the backend is not part of the match key.

        The portal's `identifier` and the app's `employeeIdentifier` are verifiably the
        same string, so matching on the identifier alone is what makes this a single
        employee across the switch instead of two.
        """
        # The same backend record, switched between modes -- which is what a real
        # migration is. One backend per company is a SQL constraint, so there is no
        # second record to create.
        backend = self.backend
        backend.write({
            "backend_type": "device_portal",
            "base_url": "https://test.api-inovace360.com/api/v1",
        })
        self.transport.route("GET", "people", [DP_PEOPLE[0]])
        with self._run(backend, "employees") as run:
            self.env["hr.employee"]._sync_device_portal(backend, run)
        created = self.env["hr.employee"].search([("tipsoi_identifier", "=", "E-001")])
        self.assertEqual(len(created), 1)

        # The client adopts the Tipsoi app: same backend record, new mode and base URL.
        backend.write({
            "backend_type": "hrm",
            "base_url": "https://test.clients.inovacetech.com/inovace-client/api/v1",
        })
        with self._run(backend, "attendance") as run:
            after = self.env["hr.employee"]._upsert_from_hrm(
                backend, hrm_employee_row(), run)

        self.assertEqual(after, created, "the same employee, not a duplicate")
        self.assertEqual(
            self.env["hr.employee"].with_context(active_test=False).search_count(
                [("tipsoi_identifier", "=", "E-001")]), 1)
        # Both ids now coexist on the one record, which is what write-back needs.
        self.assertEqual(after.tipsoi_person_id, 501)
        self.assertEqual(after.tipsoi_employee_id, 9001)

    def test_a_row_without_an_identifier_is_skipped(self):
        with self._run(self.backend, "attendance") as run:
            employee = self.env["hr.employee"]._upsert_from_hrm(
                self.backend, hrm_employee_row(employeeIdentifier=""), run)
        self.assertFalse(employee)
        self.assertEqual(run.skipped, 1)
