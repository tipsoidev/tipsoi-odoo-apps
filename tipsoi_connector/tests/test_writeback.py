# -*- coding: utf-8 -*-
"""Write-back: create, update, photo, allocate, revoke and depart, in both modes.

Each mode writes only to its own API. In Tipsoi app mode that is not a limitation -- the
app creates the matching device-portal person itself, so writing the portal from here as
well would double-create against a per-project unique identifier.

The traps pinned here, all read out of the two implementations:

* the portal saves a person **before** the photo step, so a non-200 carrying an
  `error_code` and an `id` means the person exists and only the photo failed. Retrying the
  create would then collide on the unique identifier;
* the portal's allocation call answers **200** with a per-row `status`, so success has to
  be read out of the payload rather than assumed from the HTTP code;
* allocation in the app keys on `employeeOfficeId`, not the identifier, and reports a
  non-active employee as "not found" -- a message that sends support hunting for a record
  that is right there;
* `POST /employee/{id}/status/{status}` accepts 0 and 1 only and rejects the documented
  2 and 3, so a departure has to go through `/employee/resign`;
* and a photo rejected for having no detectable face is a permanent outcome shown to HR,
  not something to retry forever.
"""

import base64
from datetime import datetime

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..models import tipsoi_client
from .common import GIF_1X1, PNG_1X1, TipsoiCase

DP_CREATE_OK = {
    "code": 200, "context": "people", "message": "New Person added successfully",
    "error": None, "id": 12345,
    "payload": {"identifier": "E-001", "name": "Rahim Uddin", "rfid": "CARD-1",
                "primary_display_text": "Rahim Uddin", "secondary_display_text": "Ops",
                "photo_url": "https://cdn.example/12345.jpg", "total_fingerprints": 0},
}


@tagged("post_install", "-at_install")
class TestDevicePortalWriteBack(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.employee = self._employee(
            "E-001", backend=self.backend, name="Rahim Uddin",
            tipsoi_card_no="CARD-1")

    def test_a_create_sends_the_identifier_and_both_display_texts(self):
        """Upstream validation requires both display texts; they are the device screen."""
        self.transport.route("POST", "people", DP_CREATE_OK)
        self.employee.action_tipsoi_push()

        call = self.transport.call_for("people", "POST")
        self.assertEqual(call["data"]["identifier"], "E-001")
        self.assertEqual(call["data"]["primary_display_text"], "Rahim Uddin")
        self.assertTrue(call["data"]["secondary_display_text"])
        self.assertEqual(call["data"]["rfid"], "CARD-1")
        self.assertNotIn("_method", call["data"])

        self.assertEqual(self.employee.tipsoi_person_id, 12345)
        self.assertEqual(self.employee.tipsoi_photo_url,
                         "https://cdn.example/12345.jpg")
        self.assertFalse(self.employee.tipsoi_push_pending)

    def test_an_update_targets_the_person_and_overrides_the_method(self):
        """The portal's update route is PUT/PATCH, but form-data carrying a file has to
        be POSTed, and `_method` is the override the portal accepts."""
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_person_id": 501})
        self.transport.route("POST", "people", DP_CREATE_OK)
        self.employee.action_tipsoi_push()

        call = self.transport.call_for("people", "POST")
        self.assertIn("people/501", call["url"])
        self.assertEqual(call["data"]["_method"], "PUT")

    def test_a_push_without_an_identifier_is_refused_before_any_call(self):
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_identifier": False})
        with self.assertRaises(UserError):
            self.employee.action_tipsoi_push()
        self.assertFalse(self.transport.calls)

    def test_a_photo_failure_on_create_does_not_retry_the_person(self):
        """The person was saved before the photo step, so it already exists.

        Retrying the create would hit the per-project unique identifier and fail
        confusingly, which is why this branches on `error_code` instead.
        """
        self.transport.route("POST", "people", {
            "code": 422, "context": "people", "id": 777,
            "error_code": "NO_FACE_DETECTED",
            "message": "No face detected in the uploaded photo",
        }, status=422)
        self.employee.action_tipsoi_push()

        self.assertEqual(self.employee.tipsoi_person_id, 777)
        self.assertEqual(self.employee.tipsoi_photo_state, "rejected")
        self.assertEqual(self.employee.tipsoi_photo_error_code, "NO_FACE_DETECTED")
        self.assertEqual(self.transport.count_for("people", "POST"), 1,
                         "a 422 is never retried, and the person is never re-created")

    def test_a_hard_failure_without_an_error_code_still_raises(self):
        self.transport.route("POST", "people", {
            "code": 400, "context": "people",
            "message": "The identifier has already been taken.",
        }, status=400)
        with self.assertRaises(tipsoi_client.TipsoiPermanentError):
            self.employee.action_tipsoi_push()
        self.assertFalse(self.employee.tipsoi_person_id)

    # -- allocation ------------------------------------------------------------------------

    def test_allocation_posts_an_array_of_actions_to_the_device(self):
        device = self._device(self.backend, "TPS-0001", device_type="entry")
        self.transport.route("POST", "/allocations", {"payload": [
            {"status": "pending_sync", "action": "allocate",
             "device_identifier": "TPS-0001", "person_identifier": "E-001"}]})

        results = self.employee._tipsoi_allocate(device, "allocate")

        self.assertEqual([r[2] for r in results], [True])
        call = self.transport.call_for("/allocations", "POST")
        self.assertIn("devices/TPS-0001/allocations", call["url"])
        self.assertEqual(call["json"],
                         [{"action": "allocate", "person_identifier": "E-001"}])

    def test_a_revoke_uses_the_same_endpoint(self):
        device = self._device(self.backend, "TPS-0001")
        self.transport.route("POST", "/allocations", {"payload": [
            {"status": "pending_sync", "action": "revoke"}]})
        self.employee._tipsoi_allocate(device, "revoke")
        self.assertEqual(
            self.transport.call_for("/allocations", "POST")["json"][0]["action"],
            "revoke")

    def test_a_two_hundred_with_a_failed_row_is_reported_as_a_failure(self):
        """`message` there is the raw truthy value, not a message, so `status` is the
        only thing that says whether it worked."""
        device = self._device(self.backend, "TPS-0001")
        self.transport.route("POST", "/allocations", {"payload": [
            {"status": "failed", "action": "allocate", "message": False,
             "device_identifier": "TPS-0001", "person_identifier": "E-001"}]})

        results = self.employee._tipsoi_allocate(device, "allocate")
        self.assertFalse(results[0][2])
        self.assertTrue(results[0][3])

    def test_a_device_from_another_backend_is_refused_without_calling(self):
        other = self._backend("hrm")
        stranger = self._device(other, "HRM-0001")
        results = self.employee._tipsoi_allocate(stranger, "allocate")
        self.assertFalse(results[0][2])
        self.assertFalse(self.transport.calls)

    def test_an_unknown_action_is_refused(self):
        device = self._device(self.backend, "TPS-0001")
        with self.assertRaises(UserError):
            self.employee._tipsoi_allocate(device, "delete")

    # -- departure -------------------------------------------------------------------------

    def test_a_departure_is_a_soft_delete_so_punch_history_keeps_its_subject(self):
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_person_id": 501})
        self.transport.route("DELETE", "people", {"code": 200, "message": "deleted"})

        self.employee.action_tipsoi_depart()

        call = self.transport.call_for("people", "DELETE")
        self.assertIn("people/501", call["url"])
        self.assertFalse(self.employee.active)

    def test_reactivation_is_meaningless_without_a_status_concept(self):
        with self.assertRaises(UserError):
            self.employee.action_tipsoi_reactivate()
        self.assertFalse(self.transport.calls)

    # -- photos ----------------------------------------------------------------------------

    def test_a_photo_rides_on_the_person_update_because_there_is_no_photo_endpoint(self):
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_person_id": 501, "image_1920": PNG_1X1})
        self.transport.route("POST", "people", DP_CREATE_OK)

        self.employee.action_tipsoi_upload_photo()

        call = self.transport.call_for("people", "POST")
        self.assertIn("image", call["files"])
        self.assertEqual(self.employee.tipsoi_photo_state, "done")

    def test_photos_can_be_switched_off_for_a_backend(self):
        self.backend.sync_photos = False
        self.employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        self.transport.route("POST", "people", DP_CREATE_OK)
        self.employee.action_tipsoi_push()
        self.assertIsNone(self.transport.call_for("people", "POST")["files"])


@tagged("post_install", "-at_install")
class TestHrmWriteBack(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm", tipsoi_office_id=456)
        self.employee = self._employee(
            "E-001", backend=self.backend, name="Rahim Uddin",
            tipsoi_employee_id=9001, tipsoi_employee_office_id="OFF-77A",
            tipsoi_department_sync_id="DEP-1", tipsoi_designation_sync_id="DES-1",
            work_email="rahim@example.com")

    # -- create and update -------------------------------------------------------------------

    def test_an_update_targets_the_employee_and_echoes_the_sync_ids(self):
        """The external sync ids are the keys Tipsoi already stores, which is why no
        separate mapping table has to exist."""
        self.transport.route("POST", "employee/profile", {"message": "Updated"})
        self.employee.action_tipsoi_push()

        call = self.transport.call_for("employee/profile", "POST")
        self.assertIn("employee/profile/9001", call["url"])
        self.assertEqual(call["json"]["name"], "Rahim Uddin")
        self.assertEqual(call["json"]["employeeIdentifier"], "E-001")
        self.assertEqual(call["json"]["departmentExternalSyncId"], "DEP-1")
        self.assertEqual(call["json"]["designationExternalSyncId"], "DES-1")
        self.assertEqual(call["json"]["employeeOfficeId"], "OFF-77A")

    def test_creating_an_employee_needs_a_work_email(self):
        """The API requires a valid, non-empty email, and inventing one would create an
        account nobody can reach."""
        fresh = self._employee("E-050", backend=self.backend, name="No Email")
        with self.assertRaises(UserError) as caught:
            fresh.action_tipsoi_push()
        self.assertIn("email", str(caught.exception).lower())
        self.assertFalse(self.transport.calls)

    def test_a_create_carries_the_required_fields_and_a_generated_password(self):
        """A password is required upstream and is not something Odoo holds, so it is
        generated and never stored -- the employee uses the app's own reset flow."""
        fresh = self._employee("E-051", backend=self.backend, name="New Person",
                               work_email="new@example.com")
        self.transport.route("POST", "employee", {"message": "Created"})

        fresh.action_tipsoi_push()

        call = self.transport.call_for("employee", "POST")
        self.assertEqual(call["json"]["name"], "New Person")
        self.assertEqual(call["json"]["email"], "new@example.com")
        self.assertEqual(call["json"]["employeeOfficeId"], "E-051")
        self.assertTrue(call["json"]["password"])
        self.assertEqual(call["json"]["officeId"], 456)
        self.assertNotIn("tipsoi_password", self.env["hr.employee"]._fields,
                         "the generated password must not be persisted")

    # -- allocation ---------------------------------------------------------------------------

    def test_allocation_keys_on_the_office_employee_id_not_the_identifier(self):
        """Verified upstream: the lookup is by officeId + employeeOfficeId + ACTIVE."""
        device = self._device(self.backend, "HRM-0001")
        self.transport.route("POST", "devices/allocate/customer", {"message": "ok"})

        results = self.employee._tipsoi_allocate(device, "allocate")

        self.assertEqual([r[2] for r in results], [True])
        call = self.transport.call_for("devices/allocate/customer", "POST")
        self.assertEqual(call["json"], {
            "employeeOfficeId": "OFF-77A",
            "deviceIdentifier": "HRM-0001",
            "action": "allocate",
        })

    def test_one_call_does_it_because_the_app_forwards_to_the_portal_itself(self):
        device = self._device(self.backend, "HRM-0001")
        self.transport.route("POST", "devices/allocate/customer", {"message": "ok"})
        self.employee._tipsoi_allocate(device, "allocate")
        self.assertEqual(len(self.transport.calls), 1)

    def test_an_employee_with_no_office_id_is_refused_before_calling(self):
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_employee_office_id": False})
        device = self._device(self.backend, "HRM-0001")

        results = self.employee._tipsoi_allocate(device, "allocate")
        self.assertFalse(results[0][2])
        self.assertIn("office", results[0][3].lower())
        self.assertFalse(self.transport.calls)

    def test_not_found_is_translated_to_not_active(self):
        """The endpoint filters on ACTIVE, so "not found" means "not active".

        Taken literally the upstream message sends support hunting for a record that is
        sitting right there in the employee list.
        """
        device = self._device(self.backend, "HRM-0001")
        self.transport.route("POST", "devices/allocate/customer",
                             {"message": "Employee not found with ID: 42"}, status=404)

        results = self.employee._tipsoi_allocate(device, "allocate")

        self.assertFalse(results[0][2])
        self.assertIn("active", results[0][3].lower())

    # -- departure -----------------------------------------------------------------------------

    def test_a_departure_goes_through_resign_not_the_status_path(self):
        """That path accepts 0 and 1 only; the resign call validates 2 or 3."""
        self.transport.route("POST", "employee/resign", {"message": "ok"})

        self.employee.action_tipsoi_depart(status=3, remark="Left in August")

        call = self.transport.call_for("employee/resign", "POST")
        self.assertEqual(call["json"]["employeeId"], 9001)
        self.assertEqual(call["json"]["status"], 3)
        self.assertEqual(call["json"]["statusRemark"], "Left in August")
        self.assertIsInstance(call["json"]["dateOfResignation"], int)
        self.assertFalse([c for c in self.transport.calls if "/status/" in c["url"]],
                         "the status path would reject a departure status")
        self.assertFalse(self.employee.active)

    def test_a_termination_uses_status_two(self):
        self.transport.route("POST", "employee/resign", {"message": "ok"})
        self.employee.action_tipsoi_depart(
            status=2, departure_date=datetime(2026, 8, 1, 0, 0))
        call = self.transport.call_for("employee/resign", "POST")
        self.assertEqual(call["json"]["status"], 2)
        self.assertEqual(call["json"]["dateOfResignation"], 1785542400000)

    def test_a_departure_without_a_synced_employee_id_is_refused(self):
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_employee_id": 0})
        with self.assertRaises(UserError):
            self.employee.action_tipsoi_depart()
        self.assertFalse(self.transport.calls)

    def test_reactivation_uses_the_status_path_with_one(self):
        self.transport.route("POST", "/status/", {"message": "ok"})
        self.employee.with_context(tipsoi_syncing=True).write({"active": False})

        self.employee.action_tipsoi_reactivate()

        self.assertTrue(self.transport.call_for("/status/", "POST")["url"]
                        .endswith("employee/9001/status/1"))
        self.assertTrue(self.employee.active)

    def test_a_hard_delete_is_offered_but_by_the_internal_id_list(self):
        self.transport.route("POST", "employee/delete", {"message": "ok"})
        self.employee.action_tipsoi_delete_remote()
        call = self.transport.call_for("employee/delete", "POST")
        self.assertEqual(call["json"], {"employeeIdList": [9001]})
        self.assertFalse(self.employee.active)

    # -- photos ---------------------------------------------------------------------------------

    def test_a_photo_is_posted_as_multipart_to_the_picture_endpoint(self):
        self.employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        self.transport.route("POST", "employee/profile/picture", {"message": "ok"})

        self.employee.action_tipsoi_upload_photo()

        call = self.transport.call_for("employee/profile/picture", "POST")
        self.assertIn("employee/profile/picture/9001", call["url"])
        filename, data, content_type = call["files"]["file"]
        self.assertTrue(filename.startswith("E-001"))
        self.assertTrue(data)
        # Both APIs accept JPEG and PNG only.
        self.assertIn(content_type, ("image/png", "image/jpeg"))
        self.assertEqual(self.employee.tipsoi_photo_state, "done")

    def test_a_face_that_cannot_be_detected_is_shown_to_hr_not_retried(self):
        self.employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        self.transport.route("POST", "employee/profile/picture", {
            "error_code": "NO_FACE_DETECTED",
            "message": "No face detected. Upload a clear front-facing photo.",
        }, status=422)

        self.employee.action_tipsoi_upload_photo()

        self.assertEqual(self.employee.tipsoi_photo_state, "rejected")
        self.assertEqual(self.employee.tipsoi_photo_error_code, "NO_FACE_DETECTED")
        self.assertIn("No face", self.employee.tipsoi_photo_error)
        self.assertEqual(
            self.transport.count_for("employee/profile/picture", "POST"), 1,
            "a photo the pipeline will never accept must not be retried")

    def test_an_unavailable_enhancement_service_is_retried_and_left_recoverable(self):
        self.employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        self.transport.route("POST", "employee/profile/picture", {
            "error_code": "ENHANCEMENT_UNAVAILABLE",
            "message": "Enhancement service temporarily unavailable",
        }, status=503)

        self.employee.action_tipsoi_upload_photo()

        self.assertEqual(self.employee.tipsoi_photo_state, "error")
        self.assertGreater(
            self.transport.count_for("employee/profile/picture", "POST"), 1,
            "503 is transient and does get retried")

    def test_a_format_tipsoi_cannot_take_is_refused_locally(self):
        """Better than earning a 422: the message names the actual problem.

        Planted through the backing `ir.attachment` rather than by writing the field.
        `image_1920` is a `fields.Image`, so it is attachment-backed -- there is no
        column on `hr_employee` to update -- and writing the field would put the bytes
        through Pillow, which is the very step this check exists to sit in front of.
        """
        self.employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        attachment = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "hr.employee"),
            ("res_id", "=", self.employee.id),
            ("res_field", "=", "image_1920"),
        ], limit=1)
        self.assertTrue(attachment, "fields.Image should be attachment-backed")
        attachment.write({"datas": GIF_1X1})
        self.employee.invalidate_cache(["image_1920"])

        # Stated as a precondition: if the bytes never arrive as a GIF then the guard is
        # unreachable through the ORM, and this test is worthless rather than passing.
        self.assertTrue(
            base64.b64decode(self.employee.image_1920).startswith(b"GIF"),
            "the planted bytes did not survive the round trip")

        with self.assertRaises(UserError) as caught:
            self.employee._tipsoi_photo_payload(strict=True)
        self.assertIn("PNG", str(caught.exception))

        self.employee.action_tipsoi_upload_photo()
        self.assertEqual(self.employee.tipsoi_photo_state, "rejected")
        self.assertFalse(self.transport.calls)

    def test_an_employee_with_no_photo_at_all_is_rejected_with_a_reason(self):
        self.employee.action_tipsoi_upload_photo()
        self.assertEqual(self.employee.tipsoi_photo_state, "rejected")
        self.assertTrue(self.employee.tipsoi_photo_error)
        self.assertFalse(self.transport.calls)

    def test_removing_a_photo_has_its_own_endpoint_in_this_mode(self):
        self.transport.route("POST", "employee/profile/picture/remove",
                             {"message": "ok"})
        self.employee.action_tipsoi_remove_photo()
        self.assertTrue(self.transport.call_for("employee/profile/picture/remove"))
        self.assertEqual(self.employee.tipsoi_photo_state, "none")


@tagged("post_install", "-at_install")
class TestWriteBackQueueing(TipsoiCase):
    """When an Odoo edit queues an outbound write, and when it deliberately does not."""

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm", tipsoi_office_id=456)
        self.employee = self._employee(
            "E-001", backend=self.backend, name="Rahim",
            tipsoi_employee_id=9001, tipsoi_employee_office_id="OFF-1",
            work_email="r@example.com")

    def test_a_new_photo_is_queued_whatever_else_is_configured(self):
        """Queueing is not sending -- the photo job decides whether to run."""
        self.employee.write({"image_1920": PNG_1X1})
        self.assertEqual(self.employee.tipsoi_photo_state, "pending")

    def test_a_requeued_photo_clears_the_previous_failure(self):
        self.employee.with_context(tipsoi_syncing=True).write({
            "tipsoi_photo_state": "rejected",
            "tipsoi_photo_error": "No face detected",
            "tipsoi_photo_error_code": "NO_FACE_DETECTED"})
        self.employee.write({"image_1920": PNG_1X1})
        self.assertEqual(self.employee.tipsoi_photo_state, "pending")
        self.assertFalse(self.employee.tipsoi_photo_error)

    def test_editing_a_name_queues_nothing_by_default(self):
        """Off by default on purpose: with it on, an Odoo edit becomes an outbound write."""
        self.assertFalse(self.backend.auto_push_employees)
        self.employee.write({"name": "Rahim Uddin"})
        self.assertFalse(self.employee.tipsoi_push_pending)

    def test_editing_a_name_queues_a_push_when_asked(self):
        self.backend.auto_push_employees = True
        self.employee.write({"name": "Rahim Uddin"})
        self.assertTrue(self.employee.tipsoi_push_pending)

    def test_an_unrelated_field_never_queues_a_push(self):
        self.backend.auto_push_employees = True
        self.employee.write({"mobile_phone": "+8801700000000"})
        self.assertFalse(self.employee.tipsoi_push_pending)

    def test_an_employee_with_no_tipsoi_link_is_never_queued(self):
        self.backend.auto_push_employees = True
        stranger = self.env["hr.employee"].create(
            {"name": "Nobody", "company_id": self.backend.company_id.id})
        stranger.write({"name": "Still nobody", "image_1920": PNG_1X1})
        self.assertFalse(stranger.tipsoi_push_pending)
        self.assertEqual(stranger.tipsoi_photo_state, "none")

    def test_the_sync_itself_never_queues_a_push_back(self):
        """Otherwise every inbound sync would schedule an outbound write of what it just
        read."""
        self.backend.auto_push_employees = True
        self.employee.with_context(tipsoi_syncing=True).write({"name": "From Tipsoi"})
        self.assertFalse(self.employee.tipsoi_push_pending)

    def test_the_photo_job_uploads_only_a_batch_at_a_time(self):
        """Upstream runs three enhancements at a time with up to 120 seconds each, so
        firing more only builds a queue while holding a cron worker."""
        self.backend.photo_batch_size = 2
        for index in range(3):
            employee = self._employee(
                "P-%s" % index, backend=self.backend, name="Person %s" % index,
                tipsoi_employee_id=9100 + index)
            employee.write({"image_1920": PNG_1X1})
        self.employee.with_context(tipsoi_syncing=True).write(
            {"tipsoi_photo_state": "none"})
        self.transport.route("POST", "employee/profile/picture", {"message": "ok"})

        self.backend.action_push_photos()

        self.assertEqual(
            self.transport.count_for("employee/profile/picture", "POST"), 2)
        self.assertEqual(
            self.env["hr.employee"].search_count([
                ("tipsoi_backend_id", "=", self.backend.id),
                ("tipsoi_photo_state", "=", "pending")]), 1)

    def test_the_photo_job_records_a_rejection_as_completed_not_failed(self):
        self.employee.write({"image_1920": PNG_1X1})
        self.transport.route("POST", "employee/profile/picture", {
            "error_code": "NO_FACE_DETECTED", "message": "No face"}, status=422)

        self.backend.action_push_photos()

        run = self.env["tipsoi.sync.run"].search(
            [("backend_id", "=", self.backend.id), ("job", "=", "photos")], limit=1)
        self.assertEqual(run.skipped, 1)
        self.assertEqual(run.failed, 0)

    def test_the_push_job_survives_one_bad_employee(self):
        self.backend.auto_push_employees = True
        good = self._employee(
            "G-1", backend=self.backend, name="Good",
            tipsoi_employee_id=9200, work_email="g@example.com")
        bad = self._employee("B-1", backend=self.backend, name="Bad")
        (good | bad).write({"name": "Renamed"})
        self.assertTrue(good.tipsoi_push_pending)
        self.transport.route("POST", "employee/profile", {"message": "ok"})
        self.transport.route("POST", "employee", {"message": "ok"})

        self.backend._cron_push_employees()

        run = self.env["tipsoi.sync.run"].search(
            [("backend_id", "=", self.backend.id), ("job", "=", "writeback")], limit=1)
        self.assertEqual(run.failed, 1, "the employee with no email")
        self.assertGreaterEqual(run.updated, 1)
        self.assertTrue(run.notes)
        good.invalidate_cache()
        self.assertFalse(good.tipsoi_push_pending,
                         "a successful push must survive the loop's savepoint")


@tagged("post_install", "-at_install")
class TestWriteBackPermissions(TipsoiCase):

    def test_writing_to_tipsoi_needs_the_tipsoi_administrator_role(self):
        backend = self._backend("hrm", tipsoi_office_id=456)
        employee = self._employee(
            "E-001", backend=backend, name="Rahim", tipsoi_employee_id=9001,
            work_email="r@example.com")
        user = self._plain_user()

        with self.assertRaises(AccessError):
            employee.with_user(user).action_tipsoi_push()
        self.assertFalse(self.transport.calls)
