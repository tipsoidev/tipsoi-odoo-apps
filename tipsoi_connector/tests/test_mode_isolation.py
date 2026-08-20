# -*- coding: utf-8 -*-
"""One system of record per client, enforced rather than trusted.

The rule: if a client runs the Tipsoi app, every call goes to the app's API; if a client
runs the Device Portal alone, every call goes to the portal's. Person records and employee
identifiers are managed in exactly one place, and no client ever talks to both. Otherwise
two systems each believe they own a person, employee IDs drift apart, and attendance ends
up attached to the wrong people.

These tests assert it **per operation**, not per adapter. That distinction is the whole
point: a shared helper that quietly calls the Device Portal from Tipsoi app mode would pass
an adapter-level test and still break the rule. So every read and every write is exercised
in both modes, and each one has to issue at least one request and zero requests to the
other API's host.

The fake transport intercepts `requests.Session.request`, so what is inspected here is the
URL that would actually have gone out.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models import tipsoi_client
from .common import DP_HOST_FRAGMENT, PNG_1X1, TipsoiCase


@tagged("post_install", "-at_install")
class TestDevicePortalModeIsolation(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.device = self._device(self.backend, "TPS-0001", device_type="entry")
        self.transport.routes_for([
            ("GET", "devices/connectivity_status", []),
            ("GET", "devices", []),
            ("GET", "people", []),
            ("GET", "logs", {"data": [], "meta": {"current_page": 1, "last_page": 1}}),
            ("POST", "people", {"code": 200, "context": "people", "id": 1,
                                "payload": {"photo_url": ""}}),
            ("POST", "/allocations", {"payload": [{"status": "pending_sync"}]}),
            ("DELETE", "people", {"code": 200, "message": "deleted"}),
        ])

    def _fresh(self, tag):
        return self._employee(
            "DP-%s" % tag, backend=self.backend, name="Person %s" % tag,
            tipsoi_person_id=501, work_email="dp%s@example.com" % tag)

    def _operations(self):
        return [
            ("sync devices", lambda e: self.backend.action_sync_devices()),
            ("sync people", lambda e: self.backend.action_sync_employees()),
            ("poll punches", lambda e: self.backend.action_poll_punches()),
            ("push person", lambda e: e.action_tipsoi_push()),
            ("upload photo", self._upload_photo),
            ("allocate", lambda e: e._tipsoi_allocate(self.device, "allocate")),
            ("revoke", lambda e: e._tipsoi_allocate(self.device, "revoke")),
            ("depart", lambda e: e.action_tipsoi_depart()),
            ("delete", lambda e: e.action_tipsoi_delete_remote()),
        ]

    @staticmethod
    def _upload_photo(employee):
        employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        return employee.action_tipsoi_upload_photo()

    def test_every_device_portal_operation_stays_on_the_device_portal(self):
        for index, (label, operation) in enumerate(self._operations()):
            with self.subTest(operation=label):
                self.transport.calls = []
                operation(self._fresh(index))
                urls = self.transport.urls()
                self.assertTrue(urls, "%s issued no request at all" % label)
                for url in urls:
                    self.assertIn(DP_HOST_FRAGMENT, url,
                                  "%s reached outside the Device Portal: %s"
                                  % (label, url))

    def test_the_tipsoi_app_endpoints_are_never_reached(self):
        """A belt-and-braces read of the same evidence, by path rather than by host."""
        for index, (label, operation) in enumerate(self._operations()):
            with self.subTest(operation=label):
                self.transport.calls = []
                operation(self._fresh(100 + index))
                for url in self.transport.urls():
                    for hrm_only in ("inovace-client", "/employee",
                                     "devices/allocate/customer", "/attendance"):
                        self.assertNotIn(hrm_only, url,
                                         "%s used a Tipsoi app path" % label)


@tagged("post_install", "-at_install")
class TestHrmModeIsolation(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm", tipsoi_office_id=456)
        self.device = self._device(self.backend, "HRM-0001")
        self.transport.routes_for([
            ("GET", "devices", {"devices": []}),
            ("GET", "attendance", {"attendance": [], "currentPage": 1,
                                   "totalPages": 1}),
            ("POST", "employee/profile/picture/remove", {"message": "ok"}),
            ("POST", "employee/profile/picture", {"message": "ok"}),
            ("POST", "employee/profile", {"message": "ok"}),
            ("POST", "employee/resign", {"message": "ok"}),
            ("POST", "employee/delete", {"message": "ok"}),
            ("POST", "employee", {"message": "ok"}),
            ("POST", "/status/", {"message": "ok"}),
            ("POST", "devices/allocate/customer", {"message": "ok"}),
        ])

    def _fresh(self, tag):
        return self._employee(
            "HRM-%s" % tag, backend=self.backend, name="Person %s" % tag,
            tipsoi_employee_id=9000 + tag, tipsoi_employee_office_id="OFF-%s" % tag,
            work_email="hrm%s@example.com" % tag)

    def _operations(self):
        return [
            ("sync devices", lambda e: self.backend.action_sync_devices()),
            ("sync attendance", lambda e: self.backend.action_sync_attendance()),
            ("push employee", lambda e: e.action_tipsoi_push()),
            ("upload photo", self._upload_photo),
            ("remove photo", lambda e: e.action_tipsoi_remove_photo()),
            ("allocate", lambda e: e._tipsoi_allocate(self.device, "allocate")),
            ("revoke", lambda e: e._tipsoi_allocate(self.device, "revoke")),
            ("reactivate", lambda e: e.action_tipsoi_reactivate()),
            ("depart", lambda e: e.action_tipsoi_depart()),
            ("delete", lambda e: e.action_tipsoi_delete_remote()),
        ]

    @staticmethod
    def _upload_photo(employee):
        employee.with_context(tipsoi_syncing=True).write({"image_1920": PNG_1X1})
        return employee.action_tipsoi_upload_photo()

    def test_no_tipsoi_app_operation_ever_touches_the_device_portal(self):
        """The one that matters most.

        The app already creates the device-portal person itself, so an Odoo write to the
        portal here would fight it and both would claim the same identifier.
        """
        for index, (label, operation) in enumerate(self._operations()):
            with self.subTest(operation=label):
                self.transport.calls = []
                operation(self._fresh(index))
                urls = self.transport.urls()
                self.assertTrue(urls, "%s issued no request at all" % label)
                for url in urls:
                    self.assertNotIn(DP_HOST_FRAGMENT, url,
                                     "%s reached the Device Portal: %s" % (label, url))

    def test_the_device_portal_paths_are_never_used_either(self):
        for index, (label, operation) in enumerate(self._operations()):
            with self.subTest(operation=label):
                self.transport.calls = []
                operation(self._fresh(100 + index))
                for url in self.transport.urls():
                    for dp_only in ("/people", "/logs", "connectivity_status",
                                    "/allocations"):
                        self.assertNotIn(dp_only, url,
                                         "%s used a Device Portal path" % label)


@tagged("post_install", "-at_install")
class TestHostGuard(TipsoiCase):
    """The choke point itself: a backend pointed at the wrong API cannot make a request."""

    def test_a_device_portal_backend_pointed_at_the_app_refuses_to_call(self):
        backend = self._backend(
            "device_portal",
            base_url="https://app.tipsoi.ai/inovace-client/api/v1")
        with self.assertRaises(UserError):
            backend.action_sync_devices()
        self.assertFalse(self.transport.calls, "the guard must fire before the request")

    def test_an_app_backend_pointed_at_the_device_portal_refuses_to_call(self):
        backend = self._backend(
            "hrm", base_url="https://api-inovace360.com/api/v1")
        with self.assertRaises(UserError):
            backend.action_sync_devices()
        self.assertFalse(self.transport.calls)

    def test_the_guard_names_the_actual_problem(self):
        backend = self._backend(
            "hrm", base_url="https://api-inovace360.com/api/v1")
        with self.assertRaises(UserError) as caught:
            backend.client().url_for("employee")
        message = str(caught.exception)
        self.assertIn("api-inovace360.com", message)
        self.assertIn("one API only", message)

    def test_the_app_adapter_denies_rather_than_allows_because_of_a_substring(self):
        """The near-miss this design avoids.

        `inovace360.com` as an *allow* fragment also matches `api-inovace360.com`, so the
        guard would have passed for the Device Portal host and the isolation would have
        held in one direction only. Denying the one host there is exactly one other API to
        avoid is what makes it symmetric.
        """
        self.assertIn("inovace360.com", "api-inovace360.com",
                      "the substring overlap that makes an allow-list wrong here")
        self.assertEqual(tipsoi_client.HrmAdapter.allowed_host_fragments, ())
        self.assertEqual(tipsoi_client.HrmAdapter.denied_host_fragments,
                         (DP_HOST_FRAGMENT,))
        self.assertEqual(tipsoi_client.DevicePortalAdapter.allowed_host_fragments,
                         (DP_HOST_FRAGMENT,))

    def test_a_backend_with_no_base_url_refuses_rather_than_guessing(self):
        """`base_url` is required, so the reachable case is the empty string.

        NOT NULL does not exclude "", which is exactly the value a half-finished
        configuration leaves behind.
        """
        backend = self._backend("hrm", base_url="")
        with self.assertRaises(UserError):
            backend.client().url_for("employee")
        self.assertFalse(self.transport.calls)


@tagged("post_install", "-at_install")
class TestJobsAssertTheirMode(TipsoiCase):
    """Each job refuses the mode it was not written for, with a message that explains."""

    def test_the_punch_poll_is_refused_in_tipsoi_app_mode(self):
        """The app has no office-wide punch feed; its punch detail is per employee."""
        backend = self._backend("hrm")
        with self.assertRaises(UserError):
            backend.action_poll_punches()
        with self.assertRaises(UserError):
            backend.action_pair_punches()
        self.assertFalse(self.transport.calls)

    def test_the_people_sync_is_refused_in_tipsoi_app_mode(self):
        backend = self._backend("hrm")
        with self.assertRaises(UserError) as caught:
            backend.action_sync_employees()
        self.assertIn("attendance", str(caught.exception).lower())

    def test_the_attendance_feed_is_refused_in_device_portal_mode(self):
        backend = self._backend("device_portal")
        with self.assertRaises(UserError):
            backend.action_sync_attendance()
        with self.assertRaises(UserError):
            backend.action_import_days()
        self.assertFalse(self.transport.calls)

    def test_a_cron_skips_a_backend_in_the_wrong_mode_instead_of_failing(self):
        """A cron must not raise on a mode it does not apply to -- it selects by mode."""
        hrm = self._backend("hrm")
        self.transport.route("GET", "logs",
                             {"data": [], "meta": {"current_page": 1, "last_page": 1}})
        self.env["tipsoi.backend"]._cron_poll_punches()
        self.assertFalse(self.transport.calls,
                         "an app-mode backend must not be polled for punches")
        self.assertEqual(hrm.state, "ready")

    def test_a_cron_skips_a_backend_that_has_never_connected(self):
        """A draft backend has nothing useful to say to a cron."""
        backend = self._backend("device_portal", state="draft")
        self.env["tipsoi.backend"]._cron_sync_devices()
        self.assertFalse(self.transport.calls)
        self.assertNotIn(backend, self.env["tipsoi.backend"]._ready_backends())


@tagged("post_install", "-at_install")
class TestJobFailureClassification(TipsoiCase):
    """Which failures halt the schedule, and which the next tick is expected to fix.

    The distinction is the whole point and it is easy to regress, because both halves look
    the same from inside a job: an exception came out. What separates them is whether a
    human has to do something. A wrong password or a switched-off Tipsoi project is not
    going to fix itself, so it stops and stays visible. A gateway error already had the
    transport's full backoff spent on it before the audit wrapper saw it, so the only
    useful response is to try again on the next tick.

    An errored backend is still scheduled either way. That is what lets a corrected
    credential recover on its own, and it is the specific bug this replaced: one failure
    used to take all nine jobs out until somebody pressed Test Connection, with nothing but
    growing staleness to show for it.
    """

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")

    def _fail_a_device_sync(self, body, status, expected):
        """Run a device sync that fails, and keep the side effects.

        Caught by hand rather than with `self.assertRaises`, and that is the whole point
        of this helper. Odoo's `assertRaises` opens a savepoint and rolls it back when the
        expected exception arrives -- its own comment says so -- which discards everything
        the failing call wrote. Every assertion in this class is about what a failure
        *leaves behind*: the sync run, the reason, the backend's state. Under
        `assertRaises` they all read as though the job had never run at all.
        """
        self.transport.route("GET", "devices/connectivity_status", [])
        self.transport.route("GET", "devices", body, status=status)
        try:
            self.backend.action_sync_devices()
        except expected:
            return
        self.fail("expected the device sync to raise %s" % expected.__name__)

    def _schedulable(self):
        return self.backend in self.env["tipsoi.backend"]._ready_backends()

    # -- transient: leave it alone ---------------------------------------------------------

    def test_a_transient_failure_does_not_demote_the_backend(self):
        self._fail_a_device_sync({"message": "bad gateway"}, 502,
                                 tipsoi_client.TipsoiTransientError)
        self.assertEqual(self.backend.state, "ready")
        self.assertTrue(self._schedulable())
        self.assertIn("bad gateway", self.backend.last_error)

    def test_the_transport_has_already_exhausted_its_backoff_by_then(self):
        """Which is why there is nothing left for the state machine to add."""
        self._fail_a_device_sync({"message": "bad gateway"}, 502,
                                 tipsoi_client.TipsoiTransientError)
        self.assertEqual(self.transport.count_for("devices", "GET"),
                         tipsoi_client.MAX_ATTEMPTS)

    def test_a_transient_failure_still_records_the_reason(self):
        self._fail_a_device_sync({"message": "bad gateway"}, 502,
                                 tipsoi_client.TipsoiTransientError)
        run = self.env["tipsoi.sync.run"].sudo().search(
            [("backend_id", "=", self.backend.id)], limit=1)
        self.assertEqual(run.state, "error", "the run still says what happened")
        self.assertTrue(run.error)

    # -- configuration: stop and be seen ----------------------------------------------------

    def test_an_inactive_project_demotes_the_backend(self):
        """No amount of retrying switches an account back on."""
        self._fail_a_device_sync(
            {"error": True, "message": "Your account is not active."}, 403,
            tipsoi_client.TipsoiInactiveProjectError)
        self.assertEqual(self.backend.state, "error")
        self.assertIn("not active", self.backend.last_error)

    def test_a_permanent_rejection_demotes_the_backend(self):
        self._fail_a_device_sync({"message": "Invalid inputs"}, 400,
                                 tipsoi_client.TipsoiPermanentError)
        self.assertEqual(self.backend.state, "error")

    def test_an_errored_backend_is_still_scheduled_so_a_fix_takes_effect_on_its_own(self):
        """The heart of it. Visible in error, and still trying.

        Excluding it here is what made the old behaviour require a human: a corrected
        password would sit unused until somebody thought to press Test Connection.
        """
        self._fail_a_device_sync({"message": "Invalid inputs"}, 400,
                                 tipsoi_client.TipsoiPermanentError)
        self.assertEqual(self.backend.state, "error")
        self.assertTrue(self._schedulable(),
                        "an errored backend must still recover on its own")

    def test_a_cron_still_picks_up_an_errored_backend(self):
        self._fail_a_device_sync({"message": "Invalid inputs"}, 400,
                                 tipsoi_client.TipsoiPermanentError)
        self.transport.calls = []
        self.transport.route("GET", "devices", [])
        self.env["tipsoi.backend"]._cron_sync_devices()
        self.assertTrue(self.transport.calls, "the cron must keep trying")

    # -- recovery ----------------------------------------------------------------------------

    def test_a_successful_job_clears_the_error_and_restores_ready(self):
        self._fail_a_device_sync({"message": "Invalid inputs"}, 400,
                                 tipsoi_client.TipsoiPermanentError)
        self.assertEqual(self.backend.state, "error")

        self.transport.route("GET", "devices", [])
        self.backend.action_sync_devices()

        self.assertEqual(self.backend.state, "ready")
        self.assertFalse(self.backend.last_error)
        self.assertTrue(self._schedulable())

    def test_a_stale_error_is_cleared_even_when_the_state_was_already_ready(self):
        """A transient failure leaves `last_error` behind without demoting anything, so
        something has to wipe it once things work again."""
        self._fail_a_device_sync({"message": "bad gateway"}, 502,
                                 tipsoi_client.TipsoiTransientError)
        self.assertEqual(self.backend.state, "ready")
        self.assertTrue(self.backend.last_error)

        self.transport.route("GET", "devices", [])
        self.backend.action_sync_devices()
        self.assertFalse(self.backend.last_error)

    def test_a_run_that_merely_reported_row_level_failures_does_not_demote(self):
        """A contested attendance day is a data problem, not a connection problem."""
        self.transport.route("GET", "devices/connectivity_status", [])
        self.transport.route("GET", "devices", [{"identifier": "TPS-1"}])
        with self._run(self.backend, "pairing") as run:
            run.failed = 3
        self.assertEqual(run.state, "partial")
        self.assertEqual(self.backend.state, "ready")
        self.assertTrue(self._schedulable())

    # -- the one exclusion that remains -------------------------------------------------------

    def test_a_draft_backend_is_still_excluded(self):
        """One that has never connected has nothing to say to a cron."""
        draft = self._backend("hrm", state="draft")
        self.assertNotIn(draft, self.env["tipsoi.backend"]._ready_backends())
