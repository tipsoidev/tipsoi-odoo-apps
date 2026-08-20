# -*- coding: utf-8 -*-
"""Device refresh, in both modes.

The two payloads are genuinely different shapes and this file pins both:

* the Device Portal returns a **bare JSON array** with no envelope and no pagination, and
  live connection state is a *second* call that can fail on its own;
* the Tipsoi app returns `{"devices": [...]}` with connectivity already folded in and
  every timestamp in epoch milliseconds.

The rule worth stating: "we could not find out whether it is online" is a third state, not
a synonym for offline. Painting it as offline sends someone to check a working reader.
"""

from datetime import datetime

from odoo.tests import tagged

from .common import DP_BASE, TipsoiCase

# `GET /devices` on the Device Portal: raw model rows plus three computed fields.
DP_DEVICES = [
    {"id": 11, "identifier": "TPS-0001", "description": "Front gate",
     "location": "Reception", "phone_number": "+8801700000001", "type": "entry",
     "firmware_version": "3.4.1", "is_mqtt_enabled": True,
     "last_communication_at": "2026-08-01 09:00:00",
     "total_allocated": 42, "status": "active", "last_seen": "5 minutes ago"},
    {"id": 12, "identifier": "TPS-0002", "description": "Back gate",
     "location": "Yard", "type": "exit", "is_mqtt_enabled": False,
     "last_communication_at": "2026-07-20 08:00:00",
     "total_allocated": 0, "status": "inactive", "last_seen": "12 days ago"},
]

# The MQTT call reports epoch millis, unlike the wall-clock string on the device row.
DP_CONNECTIVITY = [
    {"identifier": "TPS-0001", "is_online": True,
     "last_communication_at": 1785574800000, "connected_at": 1785574000000},
    {"identifier": "TPS-0002", "is_online": False,
     "last_communication_at": 1784707200000, "connected_at": None},
]

HRM_DEVICES = {"devices": [
    {"id": 7, "identifier": "HRM-0001", "officeId": 456, "phoneNumber": "+880171",
     "description": "Main door", "type": "both", "status": 1, "centralServerId": 991,
     "location": "Lobby", "workplaceName": "Head office", "workplaceId": 3,
     "updatedAt": 1785574800000, "isOnline": True,
     "connectedAt": 1785574000000, "lastCommunicationAt": 1785574800000},
    {"id": 8, "identifier": "HRM-0002", "type": "access", "status": 0,
     "description": "Server room", "isOnline": False,
     "connectedAt": None, "lastCommunicationAt": None},
]}


@tagged("post_install", "-at_install")
class TestTipsoiDevices(TipsoiCase):

    def _dp_backend_with_routes(self, devices=None, connectivity=None,
                                connectivity_status=200):
        backend = self._backend("device_portal")
        self.transport.route("GET", "devices/connectivity_status",
                             connectivity if connectivity is not None
                             else DP_CONNECTIVITY,
                             status=connectivity_status)
        self.transport.route("GET", "devices",
                             DP_DEVICES if devices is None else devices)
        return backend

    def _sync(self, backend):
        with self._run(backend, "devices") as run:
            self.env["tipsoi.device"]._sync_backend(backend, run)
        return run

    # -- Device Portal -------------------------------------------------------------------

    def test_device_portal_returns_a_bare_array_with_no_envelope(self):
        backend = self._dp_backend_with_routes()
        run = self._sync(backend)

        devices = self.env["tipsoi.device"].search([("backend_id", "=", backend.id)])
        self.assertEqual(len(devices), 2)
        self.assertEqual(run.fetched, 2)
        self.assertEqual(run.created, 2)

        front = devices.filtered(lambda d: d.identifier == "TPS-0001")
        self.assertEqual(front.tipsoi_id, 11)
        self.assertEqual(front.description, "Front gate")
        self.assertEqual(front.device_type, "entry")
        self.assertEqual(front.firmware_version, "3.4.1")
        self.assertTrue(front.is_mqtt_enabled)
        # `total_allocated` and `status` are computed by the portal, not stored columns.
        self.assertEqual(front.total_allocated, 42)
        self.assertEqual(front.remote_status, "active")
        self.assertEqual(front.name, "Front gate")

    def test_live_state_comes_from_the_second_call_in_epoch_millis(self):
        """Both calls report `last_communication_at`; the epoch one is unambiguous."""
        backend = self._dp_backend_with_routes()
        self._sync(backend)

        front = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "TPS-0001")])
        self.assertTrue(front.is_online)
        self.assertTrue(front.connectivity_known)
        self.assertEqual(front.last_communication_at,
                         datetime(2026, 8, 1, 9, 0))
        self.assertEqual(front.connected_at, datetime(2026, 8, 1, 8, 46, 40))

        back = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "TPS-0002")])
        self.assertFalse(back.is_online)
        self.assertTrue(back.connectivity_known)

    def test_a_broken_mqtt_call_does_not_fail_the_refresh(self):
        """The connectivity endpoint reaches out to the broker and can 500 alone.

        The device list is still worth having, and the honest outcome is "unknown"
        connection state rather than a list of readers falsely marked offline.
        """
        backend = self._dp_backend_with_routes(
            connectivity={"message": "broker unreachable"}, connectivity_status=500)
        run = self._sync(backend)

        devices = self.env["tipsoi.device"].search([("backend_id", "=", backend.id)])
        self.assertEqual(len(devices), 2, "the device list must still land")
        self.assertEqual(run.state, "done")
        self.assertTrue(run.notes, "the failure has to be recorded somewhere")
        for device in devices:
            self.assertFalse(device.connectivity_known)
            self.assertFalse(device.is_online)

    def test_the_wall_clock_column_is_used_when_live_state_is_unavailable(self):
        backend = self._dp_backend_with_routes(
            connectivity={"message": "down"}, connectivity_status=500)
        self._sync(backend)
        front = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "TPS-0001")])
        # "2026-08-01 09:00:00" Asia/Dhaka -> 03:00 UTC.
        self.assertEqual(front.last_communication_at, datetime(2026, 8, 1, 3, 0))

    # -- Tipsoi app ----------------------------------------------------------------------

    def test_hrm_returns_an_envelope_with_connectivity_folded_in(self):
        backend = self._backend("hrm")
        self.transport.route("GET", "devices", HRM_DEVICES)
        run = self._sync(backend)

        self.assertEqual(run.created, 2)
        main = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "HRM-0001")])
        self.assertEqual(main.tipsoi_id, 7)
        self.assertEqual(main.central_server_id, 991)
        self.assertEqual(main.workplace_name, "Head office")
        self.assertEqual(main.device_type, "both")
        self.assertTrue(main.is_online)
        self.assertTrue(main.connectivity_known)
        self.assertEqual(main.last_communication_at, datetime(2026, 8, 1, 9, 0))
        self.assertEqual(main.connected_at, datetime(2026, 8, 1, 8, 46, 40))
        # `status` is an integer here and a word on the Device Portal.
        self.assertEqual(main.remote_status, "active")

        other = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "HRM-0002")])
        self.assertEqual(other.remote_status, "inactive")
        self.assertTrue(other.connectivity_known)
        self.assertFalse(other.is_online)

    def test_hrm_refresh_makes_only_one_call(self):
        backend = self._backend("hrm")
        self.transport.route("GET", "devices", HRM_DEVICES)
        self._sync(backend)
        self.assertEqual(len(self.transport.calls), 1)

    # -- lifecycle -----------------------------------------------------------------------

    def test_a_device_that_disappears_is_deactivated_not_deleted(self):
        """Punch rows point at the device, so unlinking would take history with it."""
        backend = self._dp_backend_with_routes()
        self._sync(backend)
        gone_id = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "TPS-0002")]).id

        self.transport.route("GET", "devices", [DP_DEVICES[0]])
        run = self._sync(backend)

        gone = self.env["tipsoi.device"].with_context(active_test=False).browse(gone_id)
        self.assertTrue(gone.exists(), "the record must survive")
        self.assertFalse(gone.active)
        self.assertTrue(run.notes)

    def test_a_deactivated_device_is_revived_rather_than_duplicated(self):
        backend = self._dp_backend_with_routes()
        self._sync(backend)
        self.transport.route("GET", "devices", [DP_DEVICES[0]])
        self._sync(backend)
        self.transport.route("GET", "devices", DP_DEVICES)
        self._sync(backend)

        all_devices = self.env["tipsoi.device"].with_context(
            active_test=False).search([("backend_id", "=", backend.id)])
        self.assertEqual(len(all_devices), 2)
        self.assertTrue(all(d.active for d in all_devices))

    def test_refreshing_twice_changes_nothing(self):
        backend = self._dp_backend_with_routes()
        self._sync(backend)
        before = self.env["tipsoi.device"].search(
            [("backend_id", "=", backend.id)]).ids

        run = self._sync(backend)
        after = self.env["tipsoi.device"].search(
            [("backend_id", "=", backend.id)]).ids
        self.assertEqual(sorted(before), sorted(after))
        self.assertEqual(run.created, 0)
        self.assertEqual(run.updated, 2)

    def test_a_row_without_an_identifier_is_counted_not_dropped_silently(self):
        backend = self._dp_backend_with_routes(
            devices=[DP_DEVICES[0], {"id": 99, "description": "No identifier"}])
        run = self._sync(backend)
        self.assertEqual(run.skipped, 1)
        self.assertEqual(
            self.env["tipsoi.device"].search_count([("backend_id", "=", backend.id)]), 1)

    def test_an_unknown_reader_type_is_recorded_as_other(self):
        backend = self._dp_backend_with_routes(
            devices=[dict(DP_DEVICES[0], type="turnstile")])
        self._sync(backend)
        device = self.env["tipsoi.device"].search([("backend_id", "=", backend.id)])
        self.assertEqual(device.device_type, "other")

    def test_the_raw_row_is_kept_because_both_payloads_carry_more_than_this_model(self):
        backend = self._dp_backend_with_routes()
        self._sync(backend)
        device = self.env["tipsoi.device"].search([
            ("backend_id", "=", backend.id), ("identifier", "=", "TPS-0001")])
        self.assertIn("last_seen", device.raw_payload)

    def test_devices_only_ever_talk_to_their_own_api(self):
        backend = self._dp_backend_with_routes()
        self._sync(backend)
        for url in self.transport.urls():
            self.assertTrue(url.startswith(DP_BASE), url)
