# -*- coding: utf-8 -*-
"""The three operator dialogs.

These are the primary UI behind two of the things the module promises -- resolving
unmatched punches, and allocating people onto readers -- so they are worth the same care
as the sync itself.

Two behaviours here are load-bearing and easy to break silently:

* a manual backfill must **not** move the live cursor. Moving it forward would skip every
  punch between the backfill window's end and now; moving it back would make the next poll
  re-scan everything since. Neither shows up as an error.
* a failure while pairing after a manual link must not lose the link. The audit run
  commits before the job body, so by the time pairing can fail the link is already saved,
  and a traceback would leave the operator unable to tell whether their work took effect.

The third is a reporting rule that reads as cosmetic and is not: a job with nothing to do
creates no audit run, so rendering "the newest run" unconditionally would show the
*previous* run's counters as though they belonged to this one.
"""

from datetime import datetime
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import TipsoiCase


def page(rows, current=1, last=1):
    return {"data": rows, "meta": {"current_page": current, "last_page": last}}


@tagged("post_install", "-at_install")
class TestManualSyncWizard(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")

    def _wizard(self, **vals):
        values = {"backend_id": self.backend.id, "job": "devices"}
        values.update(vals)
        return self.env["tipsoi.manual.sync"].create(values)

    def _device_routes(self):
        self.transport.route("GET", "devices/connectivity_status", [])
        self.transport.route("GET", "devices", [])

    def _runs(self):
        return self.env["tipsoi.sync.run"].sudo().search(
            [("backend_id", "=", self.backend.id)])

    # -- the default ---------------------------------------------------------------------

    def test_the_backend_default_fires_when_the_dialog_opens(self):
        """A default only runs on `default_get`, so installing never exercises it."""
        defaults = self.env["tipsoi.manual.sync"].default_get(["backend_id", "job"])
        self.assertEqual(defaults.get("backend_id"), self.backend.id)
        self.assertEqual(defaults.get("job"), "devices")

    def test_the_dialog_still_opens_with_no_backend_configured(self):
        """The one moment someone needs to see what is wrong is the moment it is wrong."""
        self.backend.unlink()
        defaults = self.env["tipsoi.manual.sync"].default_get(["backend_id"])
        self.assertFalse(defaults.get("backend_id"))

    # -- the mode split ------------------------------------------------------------------

    def test_no_warning_for_a_job_that_belongs_to_this_mode(self):
        self.assertFalse(self._wizard(job="punches").mode_warning)
        self.assertFalse(self._wizard(job="pairing").mode_warning)

    def test_a_job_from_the_other_mode_is_flagged_before_anything_runs(self):
        wizard = self._wizard(job="attendance")
        self.assertTrue(wizard.mode_warning)
        self.assertIn("never combined", wizard.mode_warning)

    def test_a_job_that_belongs_to_both_modes_never_warns(self):
        self.assertFalse(self._wizard(job="devices").mode_warning)
        self.assertFalse(self._wizard(job="photos").mode_warning)
        hrm = self._backend("hrm")
        self.assertFalse(
            self._wizard(job="devices", backend_id=hrm.id).mode_warning)

    def test_a_mismatched_job_raises_rather_than_quietly_doing_nothing(self):
        """Otherwise the operator sees nothing happen and a summary of an older run."""
        wizard = self._wizard(job="attendance")
        with self.assertRaises(UserError):
            wizard.action_run()
        self.assertFalse(self._runs())
        self.assertFalse(self.transport.calls)

    # -- windows -------------------------------------------------------------------------

    def test_a_windowed_job_runs_over_exactly_the_dates_given(self):
        self.transport.route("GET", "logs", page([]))
        wizard = self._wizard(job="punches",
                              date_from=datetime(2026, 7, 1, 0, 0),
                              date_to=datetime(2026, 7, 2, 0, 0))
        wizard.action_run()

        params = self.transport.calls[0]["params"]
        # Sent in the API server's own application timezone, not UTC.
        self.assertEqual(params["start"], "2026-07-01 06:00:00")
        self.assertEqual(params["end"], "2026-07-02 06:00:00")
        run = self._runs()[:1]
        self.assertEqual(run.window_from, datetime(2026, 7, 1, 0, 0))
        self.assertEqual(run.window_to, datetime(2026, 7, 2, 0, 0))

    def test_a_punch_backfill_leaves_the_live_cursor_exactly_where_it_was(self):
        """Forward would skip everything since; backward would re-scan forever."""
        self.backend.last_log_sync_time = datetime(2026, 8, 20, 12, 0)
        self.transport.route("GET", "logs", page([]))
        self._wizard(job="punches",
                     date_from=datetime(2026, 7, 1, 0, 0),
                     date_to=datetime(2026, 7, 2, 0, 0)).action_run()
        self.assertEqual(self.backend.last_log_sync_time,
                         datetime(2026, 8, 20, 12, 0))

    def test_an_attendance_backfill_leaves_its_cursor_alone_too(self):
        hrm = self._backend("hrm", tipsoi_office_id=456)
        hrm.last_attendance_sync = datetime(2026, 8, 20, 12, 0)
        self.transport.route("GET", "attendance",
                             {"attendance": [], "currentPage": 1, "totalPages": 1})
        self.env["tipsoi.manual.sync"].create({
            "backend_id": hrm.id, "job": "attendance",
            "date_from": datetime(2026, 7, 1, 0, 0),
            "date_to": datetime(2026, 7, 4, 0, 0),
        }).action_run()
        self.assertEqual(hrm.last_attendance_sync, datetime(2026, 8, 20, 12, 0))

    def test_a_windowed_job_without_dates_uses_the_backend_window(self):
        self.transport.route("GET", "logs", page([]))
        self._wizard(job="punches").action_run()
        self.assertTrue(self.backend.last_log_sync_time,
                        "the ordinary path does advance the cursor")

    def test_half_a_window_is_refused(self):
        """A missing end would have to mean either now or the beginning of time."""
        with self.assertRaises(UserError):
            self._wizard(job="punches",
                         date_from=datetime(2026, 7, 1, 0, 0)).action_run()
        with self.assertRaises(UserError):
            self._wizard(job="punches",
                         date_to=datetime(2026, 7, 1, 0, 0)).action_run()
        self.assertFalse(self.transport.calls)

    def test_a_backwards_window_is_refused(self):
        with self.assertRaises(UserError):
            self._wizard(job="punches",
                         date_from=datetime(2026, 7, 2, 0, 0),
                         date_to=datetime(2026, 7, 1, 0, 0)).action_run()

    def test_an_equal_window_is_refused(self):
        moment = datetime(2026, 7, 2, 0, 0)
        with self.assertRaises(UserError):
            self._wizard(job="punches", date_from=moment,
                         date_to=moment).action_run()

    def test_dates_are_ignored_for_a_job_that_has_no_window(self):
        self._device_routes()
        wizard = self._wizard(job="devices",
                              date_from=datetime(2026, 7, 2, 0, 0),
                              date_to=datetime(2026, 7, 1, 0, 0))
        wizard.action_run()
        self.assertIn("Devices", wizard.result)

    # -- reporting -------------------------------------------------------------------------

    def test_a_job_that_did_something_reports_its_own_counters(self):
        self._device_routes()
        wizard = self._wizard(job="devices")
        wizard.action_run()
        for expected in ("Job:", "Status:", "Fetched:", "Created:", "Failed:"):
            self.assertIn(expected, wizard.result)

    def test_a_job_with_nothing_to_do_says_so_instead_of_showing_the_last_run(self):
        """A job that queues no work records no run at all.

        Rendering the newest run unconditionally would put an earlier job's counters in
        front of the operator as though they were this one's.
        """
        self._device_routes()
        self._wizard(job="devices").action_run()
        self.assertTrue(self._runs(), "there is now a previous run to mistake")

        wizard = self._wizard(job="photos")
        wizard.action_run()
        self.assertIn("nothing to do", wizard.result)
        self.assertNotIn("Fetched:", wizard.result)

    def test_the_outcome_lands_in_the_dialog_that_is_already_open(self):
        self._device_routes()
        wizard = self._wizard(job="devices")
        action = wizard.action_run()
        self.assertEqual(action["res_model"], "tipsoi.manual.sync")
        self.assertEqual(action["res_id"], wizard.id)
        self.assertEqual(action["target"], "new")


@tagged("post_install", "-at_install")
class TestLinkEmployeeWizard(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.employee = self._employee(backend=self.backend, name="Rahim Uddin")
        self.Punch = self.env["tipsoi.punch.log"]

    def _punch(self, uid, identifier, moment, direction="unknown"):
        return self.Punch.create({
            "backend_id": self.backend.id,
            "tipsoi_log_id": uid,
            "person_identifier": identifier,
            "punch_time_utc": moment,
            "direction": direction,
            "state": "unmatched",
            "state_reason": "No Odoo employee carries %s" % identifier,
        })

    def _wizard(self, punches, **vals):
        values = {"punch_log_ids": [(6, 0, punches.ids)],
                  "employee_id": self.employee.id}
        values.update(vals)
        return self.env["tipsoi.link.employee"].create(values)

    def _pair_of_punches(self, identifier="GHOST-1"):
        return (self._punch("u1", identifier, datetime(2026, 8, 1, 3, 0), "in")
                | self._punch("u2", identifier, datetime(2026, 8, 1, 11, 0), "out"))

    # -- defaults and validation -----------------------------------------------------------

    def test_the_selected_punches_arrive_as_the_default(self):
        punches = self._pair_of_punches()
        defaults = self.env["tipsoi.link.employee"].with_context(
            active_ids=punches.ids).default_get(["punch_log_ids"])
        self.assertEqual(set(defaults["punch_log_ids"][0][2]), set(punches.ids))

    def test_the_identifier_is_shown_before_anything_is_written(self):
        wizard = self._wizard(self._pair_of_punches())
        self.assertEqual(wizard.identifier, "GHOST-1")

    def test_linking_nothing_is_refused(self):
        wizard = self._wizard(self.Punch.browse())
        with self.assertRaises(UserError):
            wizard.action_link()

    def test_two_identifiers_at_once_is_refused_and_both_are_named(self):
        """One employee owns one identifier -- the uniqueness constraint says so.

        Quietly picking one would mislink every future punch that arrives under the
        other, and nobody would notice for months.
        """
        punches = (self._punch("u1", "GHOST-1", datetime(2026, 8, 1, 3, 0))
                   | self._punch("u2", "GHOST-2", datetime(2026, 8, 1, 11, 0)))
        wizard = self._wizard(punches, set_identifier=True)
        with self.assertRaises(UserError) as caught:
            wizard.action_link()
        message = str(caught.exception)
        self.assertIn("GHOST-1", message)
        self.assertIn("GHOST-2", message)
        self.assertFalse(self.employee.tipsoi_identifier)

    def test_two_identifiers_can_be_linked_when_the_employee_is_left_alone(self):
        punches = (self._punch("u1", "GHOST-1", datetime(2026, 8, 1, 3, 0))
                   | self._punch("u2", "GHOST-2", datetime(2026, 8, 1, 11, 0)))
        self._wizard(punches, set_identifier=False).action_link()
        self.assertEqual(set(punches.mapped("employee_id").ids), {self.employee.id})
        self.assertFalse(self.employee.tipsoi_identifier)

    # -- the link itself ---------------------------------------------------------------------

    def test_linking_attaches_every_selected_punch_to_the_employee(self):
        punches = self._pair_of_punches()
        wizard = self._wizard(punches)
        wizard.action_link()
        for punch in punches:
            self.assertEqual(punch.employee_id, self.employee)
            self.assertNotEqual(punch.state, "unmatched")
        self.assertIn("Linked 2", wizard.result)

    def test_linking_writes_the_identifier_so_later_punches_match_on_their_own(self):
        self._wizard(self._pair_of_punches()).action_link()
        self.assertEqual(self.employee.tipsoi_identifier, "GHOST-1")
        self.assertEqual(self.employee.tipsoi_backend_id, self.backend)

    def test_unticking_the_box_leaves_the_employee_identifier_alone(self):
        wizard = self._wizard(self._pair_of_punches(), set_identifier=False)
        wizard.action_link()
        self.assertFalse(self.employee.tipsoi_identifier)
        self.assertIn("left alone", wizard.result)

    def test_linking_never_queues_an_outbound_push(self):
        """This is inbound work: Tipsoi already knows this identifier perfectly well."""
        self.backend.auto_push_employees = True
        self._wizard(self._pair_of_punches()).action_link()
        self.assertFalse(self.employee.tipsoi_push_pending)

    # -- pairing, and what a failure there costs ---------------------------------------------

    def test_a_successful_link_pairs_at_once_and_leaves_the_crons_running(self):
        punches = self._pair_of_punches()
        wizard = self._wizard(punches)
        wizard.action_link()

        self.assertEqual(set(punches.mapped("state")), {"paired"})
        self.assertEqual(
            self.env["hr.attendance"].search_count(
                [("employee_id", "=", self.employee.id)]), 1)
        self.assertIn("paired into attendance", wizard.result)
        # The backend is still selected by every scheduled job.
        self.assertEqual(self.backend.state, "ready")
        self.assertIn(self.backend, self.env["tipsoi.backend"]._ready_backends())

    def test_a_failed_pairing_does_not_lose_the_link(self):
        """The link is saved before pairing can fail, and must stay saved.

        A traceback here would leave the operator unable to tell whether their work took
        effect, which is worse than a failure they can read.
        """
        punches = self._pair_of_punches()
        wizard = self._wizard(punches)
        punch_class = type(self.Punch)

        with patch.object(punch_class, "_pair",
                          side_effect=RuntimeError("pairing exploded")):
            wizard.action_link()          # must not propagate

        for punch in punches:
            self.assertEqual(punch.employee_id, self.employee)
        self.assertEqual(self.employee.tipsoi_identifier, "GHOST-1")
        # Asserts the *shape* of the message rather than a phrase: the reason has to be
        # quoted back, and it has to say the punches will finish on their own, because
        # they will -- they are linked and `matched`, which is what the pairing cron
        # selects. Telling HR to escalate here would be teaching them not to trust it.
        self.assertIn("pairing exploded", wizard.result)
        self.assertIn("next scheduled pass", wizard.result)
        self.assertIn("pairing exploded", wizard.result)

    def test_a_transient_pairing_failure_leaves_every_scheduled_job_running(self):
        """A failure the next tick can fix must not take the schedule down with it.

        This is the guarantee, not merely the current behaviour. A transient failure has
        already exhausted the transport's own backoff by the time the audit wrapper sees
        it, so the next scheduled tick is exactly how it recovers -- and demoting the
        backend here would instead stop all nine scheduled jobs, leaving nothing but
        growing staleness as the symptom.
        """
        punches = self._pair_of_punches()
        wizard = self._wizard(punches)
        punch_class = type(self.Punch)

        with patch.object(punch_class, "_pair",
                          side_effect=RuntimeError("pairing exploded")):
            wizard.action_link()

        self.assertEqual(self.backend.state, "ready")
        self.assertIn(self.backend, self.env["tipsoi.backend"]._ready_backends())
        # The reason is still recorded where an administrator will find it.
        self.assertIn("pairing exploded", self.backend.last_error)
        # And the link itself survived, which the previous test also covers.
        self.assertEqual(set(punches.mapped("employee_id").ids), {self.employee.id})

    def test_linking_in_tipsoi_app_mode_does_not_try_to_pair(self):
        """There the app has already paired each day; pairing is a Device Portal job."""
        hrm = self._backend("hrm")
        employee = self._employee(backend=hrm, name="Karim")
        punch = self.env["tipsoi.punch.log"].create({
            "backend_id": hrm.id, "tipsoi_log_id": "h1",
            "person_identifier": "GHOST-9",
            "punch_time_utc": datetime(2026, 8, 1, 3, 0), "state": "unmatched",
        })
        wizard = self.env["tipsoi.link.employee"].create({
            "punch_log_ids": [(6, 0, punch.ids)], "employee_id": employee.id})
        wizard.action_link()

        self.assertEqual(punch.employee_id, employee)
        self.assertEqual(punch.state, "matched")
        self.assertEqual(hrm.state, "ready")

    def test_the_outcome_lands_in_the_dialog_that_is_already_open(self):
        wizard = self._wizard(self._pair_of_punches())
        action = wizard.action_link()
        self.assertEqual(action["res_id"], wizard.id)
        self.assertEqual(action["target"], "new")


@tagged("post_install", "-at_install")
class TestAllocationWizard(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("hrm", tipsoi_office_id=456)
        self.device = self._device(self.backend, "HRM-0001")
        self.ready = self._employee(
            "A-1", backend=self.backend, name="Allocatable",
            tipsoi_employee_id=9001, tipsoi_employee_office_id="OFF-1")
        # No office employee ID, which is the value allocation keys on -- so this one
        # cannot be allocated and is refused without a request being made.
        self.unready = self._employee(
            "A-2", backend=self.backend, name="Not synced yet",
            tipsoi_employee_id=9002)

    def _wizard(self, employees=None, devices=None, **vals):
        values = {
            "backend_id": self.backend.id,
            "employee_ids": [(6, 0, (employees or self.ready).ids)],
            "device_ids": [(6, 0, (devices or self.device).ids)],
        }
        values.update(vals)
        return self.env["tipsoi.allocation"].create(values)

    def _tipsoi_user(self):
        """A Tipsoi user, deliberately without the administrator role.

        Given the user group so the dialog itself opens -- what is under test is the
        outbound write, not whether they can reach the form.
        """
        return self.env["res.users"].create({
            "name": "Tipsoi viewer", "login": "tipsoi-viewer-alloc",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("hr.group_hr_manager").id,
                self.env.ref("tipsoi_connector.group_tipsoi_user").id,
            ])],
        })

    # -- defaults ---------------------------------------------------------------------------

    def test_the_backend_default_fires_when_the_dialog_opens(self):
        defaults = self.env["tipsoi.allocation"].default_get(
            ["backend_id", "action_type"])
        self.assertEqual(defaults.get("backend_id"), self.backend.id)
        self.assertEqual(defaults.get("action_type"), "allocate")

    def test_opening_over_a_device_list_seeds_the_devices_and_their_backend(self):
        """Otherwise the default backend can contradict the device domain, and the
        operator opens the dialog to find the devices they picked filtered out."""
        other_backend = self._backend("device_portal")
        other_device = self._device(other_backend, "TPS-0001")
        defaults = self.env["tipsoi.allocation"].with_context(
            active_model="tipsoi.device",
            active_ids=other_device.ids).default_get(["backend_id", "device_ids"])
        self.assertEqual(defaults["backend_id"], other_backend.id)
        self.assertEqual(defaults["device_ids"][0][2], other_device.ids)

    def test_opening_over_an_employee_list_seeds_the_employees(self):
        defaults = self.env["tipsoi.allocation"].with_context(
            active_model="hr.employee",
            active_ids=self.ready.ids).default_get(["employee_ids"])
        self.assertEqual(defaults["employee_ids"][0][2], self.ready.ids)

    def test_the_device_choice_is_restricted_to_the_chosen_backend(self):
        domain = self.env["tipsoi.allocation"]._fields["device_ids"].domain
        self.assertIn("backend_id", domain)

    # -- applying ----------------------------------------------------------------------------

    def test_a_clean_batch_reports_what_it_did(self):
        self.transport.route("POST", "devices/allocate/customer", {"message": "ok"})
        wizard = self._wizard()
        wizard.action_apply()

        self.assertEqual(wizard.state, "done")
        self.assertIn("1 succeeded, 0 failed", wizard.result)
        self.assertIn("Allocatable", wizard.result)

    def test_a_revoke_reads_the_same_way(self):
        self.transport.route("POST", "devices/allocate/customer", {"message": "ok"})
        wizard = self._wizard(action_type="revoke")
        wizard.action_apply()
        self.assertIn("Revoke", wizard.result)
        self.assertIn("1 succeeded", wizard.result)

    def test_a_mixed_batch_reports_both_halves_rather_than_stopping(self):
        """One employee upstream refuses must not cancel the twenty that would work."""
        self.transport.route("POST", "devices/allocate/customer", {"message": "ok"})
        wizard = self._wizard(employees=self.ready | self.unready)
        wizard.action_apply()

        self.assertEqual(wizard.state, "done")
        self.assertIn("1 succeeded, 1 failed", wizard.result)
        self.assertIn("Failed:", wizard.result)
        self.assertIn("Not synced yet", wizard.result)
        self.assertIn("Done:", wizard.result)
        self.assertIn("Allocatable", wizard.result)
        # The one that could work still went out.
        self.assertEqual(
            self.transport.count_for("devices/allocate/customer", "POST"), 1)

    def test_a_batch_where_everything_fails_still_completes_and_explains(self):
        wizard = self._wizard(employees=self.unready)
        wizard.action_apply()
        self.assertEqual(wizard.state, "done")
        self.assertIn("0 succeeded, 1 failed", wizard.result)
        self.assertFalse(self.transport.calls)

    def test_choosing_nothing_is_refused(self):
        wizard = self._wizard()
        wizard.device_ids = [(5, 0, 0)]
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertFalse(self.transport.calls)

    def test_the_outcome_lands_in_the_dialog_that_is_already_open(self):
        self.transport.route("POST", "devices/allocate/customer", {"message": "ok"})
        wizard = self._wizard()
        action = wizard.action_apply()
        self.assertEqual(action["res_id"], wizard.id)
        self.assertEqual(action["target"], "new")

    # -- permissions --------------------------------------------------------------------------

    def test_a_tipsoi_user_without_the_administrator_role_cannot_allocate(self):
        user = self._tipsoi_user()
        wizard = self._wizard().with_user(user)
        with self.assertRaises(AccessError):
            wizard.action_apply()
        self.assertFalse(self.transport.calls)
