# -*- coding: utf-8 -*-
"""Transport tests.

These cover the classification rules the connector's correctness rests on, all of them
read out of the two implementations rather than guessed:

* the Device Portal's inactive-project 403 is distinct from an auth failure;
* photo failures split into permanent (422 NO_FACE_DETECTED / INVALID_IMAGE_SIZE) and
  transient (503 ENHANCEMENT_UNAVAILABLE / 500 S3_UPLOAD_FAILED);
* HRM validation is 400, so the rule is "4xx never retries";
* the two paging dialects differ (page/per_page vs pageNumber/perPage);
* credentials never reach a log;
* and mode isolation -- a backend in one mode cannot reach the other API's host.

The last one is a test rather than a convention on purpose: it is what keeps person
records and employee IDs managed in exactly one place as the addon grows.
"""

import json

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import tipsoi_client as tc


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


@tagged("post_install", "-at_install")
class TestTipsoiTransport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dp_backend = cls.env["tipsoi.backend"].create({
            "backend_type": "device_portal",
            "environment": "test",
            "base_url": "https://test.api-inovace360.com/api/v1",
            "username": "u", "password": "p",
        })
        cls.dp = cls.dp_backend.client()

    def _hrm_backend(self, base_url="https://test.clients.inovacetech.com/inovace-client/api/v1"):
        # One backend per company is enforced by a SQL constraint, so an HRM backend for
        # these tests needs its own company.
        company = self.env["res.company"].create({"name": "HRM test co"})
        return self.env["tipsoi.backend"].create({
            "backend_type": "hrm", "environment": "test",
            "base_url": base_url, "company_id": company.id,
            "username": "a@b.c", "password": "p",
        })

    # -- error classification ----------------------------------------------------------

    def test_success_returns_body(self):
        self.assertEqual(self.dp._handle(FakeResponse(200, {"ok": 1})), {"ok": 1})

    def test_inactive_project_is_not_an_auth_error(self):
        with self.assertRaises(tc.TipsoiInactiveProjectError):
            self.dp._handle(FakeResponse(
                403, {"error": True, "message": "Your account is not active."}))

    def test_401_is_auth_error(self):
        with self.assertRaises(tc.TipsoiAuthError):
            self.dp._handle(FakeResponse(401, {"message": "failed to authenticate"}))

    def test_no_face_detected_is_permanent(self):
        with self.assertRaises(tc.TipsoiPhotoError):
            self.dp._handle(FakeResponse(422, {
                "error_code": "NO_FACE_DETECTED", "message": "No face detected"}))

    def test_invalid_image_size_is_permanent(self):
        with self.assertRaises(tc.TipsoiPhotoError):
            self.dp._handle(FakeResponse(422, {
                "error_code": "INVALID_IMAGE_SIZE", "message": "bad size"}))

    def test_enhancement_unavailable_is_transient(self):
        with self.assertRaises(tc.TipsoiTransientError):
            self.dp._handle(FakeResponse(503, {
                "error_code": "ENHANCEMENT_UNAVAILABLE", "message": "down"}))

    def test_s3_upload_failed_is_transient(self):
        with self.assertRaises(tc.TipsoiTransientError):
            self.dp._handle(FakeResponse(500, {
                "error_code": "S3_UPLOAD_FAILED", "message": "s3"}))

    def test_validation_400_never_retries(self):
        with self.assertRaises(tc.TipsoiPermanentError):
            self.dp._handle(FakeResponse(400, {"message": "Invalid inputs"}))

    def test_photo_failure_preserves_created_person_id(self):
        """A photo failure returns non-200 but the person was already saved.

        Retrying the whole create would then collide with the per-project unique
        identifier, so the id has to survive on the exception for callers to branch on.
        """
        with self.assertRaises(tc.TipsoiPhotoError) as caught:
            self.dp._handle(FakeResponse(422, {
                "error_code": "NO_FACE_DETECTED", "id": 999, "message": "x"}))
        self.assertEqual(caught.exception.payload.get("id"), 999)
        self.assertEqual(caught.exception.error_code, "NO_FACE_DETECTED")

    # -- redaction ---------------------------------------------------------------------

    def test_credentials_never_reach_a_log(self):
        scrubbed = tc.redact(
            'GET /logs?api_token=SECRET123 Authorization: Bearer aa.bb.cc '
            '{"refreshToken":"rrr","password":"pw"}')
        for leak in ("SECRET123", "aa.bb.cc", "rrr", "pw"):
            self.assertNotIn(leak, scrubbed)

    # -- paging dialects ---------------------------------------------------------------

    def test_device_portal_paging_clamps_to_max(self):
        self.assertEqual(self.dp.page_params(2, 99999),
                         {"page": 2, "per_page": tc.DP_MAX_PER_PAGE})

    def test_hrm_paging_uses_camel_case(self):
        hrm = self._hrm_backend().client()
        self.assertEqual(hrm.page_params(3, 50), {"pageNumber": 3, "perPage": 50})

    def test_device_portal_bare_list_is_a_single_page(self):
        """`GET /people` disables pagination upstream, so the whole list arrives at once."""
        rows, has_more = self.dp.extract_page([{"identifier": "1"}])
        self.assertEqual(rows, [{"identifier": "1"}])
        self.assertFalse(has_more)

    def test_hrm_attendance_paging(self):
        hrm = self._hrm_backend().client()
        rows, has_more = hrm.extract_page(
            {"attendance": [{"e": 1}], "currentPage": 1, "totalPages": 4})
        self.assertEqual(rows, [{"e": 1}])
        self.assertTrue(has_more)
        _rows, has_more = hrm.extract_page(
            {"attendance": [{"e": 1}], "currentPage": 4, "totalPages": 4})
        self.assertFalse(has_more)

    # -- mode isolation ----------------------------------------------------------------

    def test_device_portal_backend_refuses_hrm_host(self):
        self.dp_backend.base_url = "https://app.tipsoi.ai/inovace-client/api/v1"
        with self.assertRaises(UserError):
            self.dp_backend.client().url_for("logs")

    def test_hrm_backend_refuses_device_portal_host(self):
        hrm = self._hrm_backend(base_url="https://api-inovace360.com/api/v1")
        with self.assertRaises(UserError):
            hrm.client().url_for("employee")

    def test_allowed_host_is_accepted(self):
        self.assertEqual(self.dp.url_for("logs"),
                         "https://test.api-inovace360.com/api/v1/logs")

    # -- HRM error shapes --------------------------------------------------------------

    def test_hrm_bean_validation_names_the_fields(self):
        hrm = self._hrm_backend().client()
        message, _code = hrm.describe_error(
            400, {"message": "Invalid inputs",
                  "errorFieldNames": ["employeeIdentifier"]}, "")
        self.assertIn("employeeIdentifier", message)

    def test_hrm_spring_default_500_shape_still_yields_a_message(self):
        hrm = self._hrm_backend().client()
        message, _code = hrm.describe_error(
            500, {"timestamp": 1, "status": 500,
                  "error": "Internal Server Error", "path": "/employee"}, "")
        self.assertTrue(message)

    # -- mode agreement ----------------------------------------------------------------

    def test_device_portal_mode_refused_when_remote_has_hrm(self):
        """The mixup the design exists to prevent.

        A project with the Tipsoi app attached is already having its people managed by
        HRM, so configuring Odoo as Device-Portal-only against it would double-manage
        them.
        """
        self.dp_backend.remote_has_hrm = True
        with self.assertRaises(UserError):
            self.dp_backend._check_mode_agreement(self.dp_backend.client())

    def test_device_portal_mode_accepted_when_remote_has_no_hrm(self):
        self.dp_backend.remote_has_hrm = False
        self.assertEqual(
            self.dp_backend._check_mode_agreement(self.dp_backend.client()), [])
