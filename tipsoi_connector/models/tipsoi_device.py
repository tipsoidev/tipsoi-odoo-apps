# -*- coding: utf-8 -*-
"""Tipsoi readers, in whichever mode the backend is in.

Both APIs expose the device list, and the connector reads whichever one belongs to the
backend's mode -- never both. The two payloads differ enough to be worth stating:

* **Device Portal** `GET /devices` returns a *bare JSON array* of device rows with no
  envelope and no pagination, each carrying a computed `total_allocated` and a `status`
  derived from how recently the device spoke. Live connectivity is a second call,
  `GET /devices/connectivity_status`, which reaches out to the MQTT broker.
* **HRM** `GET /devices` returns `{"devices": [...]}` with connectivity already folded
  in (`isOnline`, `connectedAt`, `lastCommunicationAt`) and all timestamps as epoch
  milliseconds, so one call is enough.

A device that disappears from a refresh is deactivated, never unlinked: punch rows
reference it, and deleting the reader would take the history with it.
"""

import json
import logging

from odoo import _, api, fields, models

from . import tipsoi_client, tipsoi_time

_logger = logging.getLogger(__name__)

#: Both APIs use the same four words for what a reader does, which is also what decides
#: a punch's direction when the punch row itself does not say.
DEVICE_TYPES = [
    ("entry", "Entry"),
    ("exit", "Exit"),
    ("both", "Entry & exit"),
    ("access", "Access control"),
    ("other", "Other"),
]

#: HRM device rows carry `status` as an integer; the Device Portal computes a word.
HRM_DEVICE_STATUS = {-1: "archived", 0: "inactive", 1: "active"}


class TipsoiDevice(models.Model):
    _name = "tipsoi.device"
    _description = "Tipsoi Device"
    _order = "name"

    backend_id = fields.Many2one(
        "tipsoi.backend", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="backend_id.company_id", store=True, index=True)

    name = fields.Char(compute="_compute_name", store=True)
    identifier = fields.Char(
        required=True, index=True,
        help="The device's own identifier. This is what allocation calls key on, in "
             "both modes, so it is the field that matters rather than the numeric id.")
    tipsoi_id = fields.Integer(
        string="Remote ID", index=True,
        help="The device's primary key in whichever system this backend talks to.")
    central_server_id = fields.Integer(
        help="Tipsoi app mode only: the same device's id in the Device Portal. Recorded "
             "for support conversations -- the connector never calls the portal in this "
             "mode.")

    description = fields.Char()
    location = fields.Char()
    phone_number = fields.Char()
    device_type = fields.Selection(DEVICE_TYPES, string="Reader type")
    workplace_name = fields.Char(help="Tipsoi app mode only.")
    firmware_version = fields.Char(help="Device Portal mode only.")
    is_mqtt_enabled = fields.Boolean(help="Device Portal mode only.")

    is_online = fields.Boolean(
        readonly=True,
        help="Live MQTT session state, not merely whether the device is registered.")
    connected_at = fields.Datetime(readonly=True)
    last_communication_at = fields.Datetime(
        readonly=True, string="Last heard from")
    connectivity_known = fields.Boolean(
        readonly=True, default=False,
        help="Whether the last refresh actually learned the connection state. The "
             "Device Portal reads it from the MQTT broker in a separate call that can "
             "fail on its own, and 'we did not find out' must not read as 'offline'.")

    total_allocated = fields.Integer(
        readonly=True, string="People allocated",
        help="Device Portal mode only -- the portal computes it. The Tipsoi app does "
             "not return a count on the device list.")
    remote_status = fields.Char(readonly=True)
    raw_payload = fields.Text(
        readonly=True,
        help="The row exactly as received. Kept because both device payloads carry "
             "fields this model does not model, and support questions land on them.")
    last_sync = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)

    _uniq_backend_identifier = models.Constraint(
        "unique(backend_id, identifier)",
        "This device is already registered for this Tipsoi backend.")

    @api.depends("description", "location", "identifier")
    def _compute_name(self):
        for device in self:
            device.name = (device.description or device.location
                           or device.identifier or _("Device"))

    def action_open_punches(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Punches from %s", self.name),
            "res_model": "tipsoi.punch.log",
            "view_mode": "list,form",
            "domain": [("device_id", "=", self.id)],
        }

    # ----------------------------------------------------------------------------------
    # sync
    # ----------------------------------------------------------------------------------

    @api.model
    def _sync_backend(self, backend, run):
        """Refresh every device for one backend, in that backend's mode only."""
        adapter = backend.client()
        if backend.backend_type == "device_portal":
            rows = self._fetch_device_portal(backend, adapter, run)
        else:
            rows = self._fetch_hrm(backend, adapter)

        run.fetched += len(rows)
        seen = self.browse()
        for vals in rows:
            if not vals.get("identifier"):
                run.skipped += 1
                continue
            device = self.with_context(active_test=False).search([
                ("backend_id", "=", backend.id),
                ("identifier", "=", vals["identifier"]),
            ], limit=1)
            vals["last_sync"] = fields.Datetime.now()
            if device:
                device.write(dict(vals, active=True))
                run.updated += 1
            else:
                device = self.create(dict(vals, backend_id=backend.id))
                run.created += 1
            seen |= device

        # Absent devices are deactivated rather than deleted: punch rows point at them,
        # and a device can also drop out of a response for a transient reason.
        stale = self.search([("backend_id", "=", backend.id), ("id", "not in", seen.ids)])
        if stale:
            stale.write({"active": False})
            run.add_note(_(
                "%s device(s) no longer returned by Tipsoi were deactivated.",
                len(stale)))
        return seen

    # -- Device Portal ------------------------------------------------------------------

    @api.model
    def _fetch_device_portal(self, backend, adapter, run):
        body = adapter.request("GET", "devices")
        rows = body if isinstance(body, list) else (body or {}).get("data") or []
        connectivity = self._fetch_dp_connectivity(adapter, run)

        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = self._as_text(row.get("identifier"))
            live = connectivity.get(identifier)
            vals = {
                "identifier": identifier,
                "tipsoi_id": self._as_int(row.get("id")),
                "description": self._as_text(row.get("description")),
                "location": self._as_text(row.get("location")),
                "phone_number": self._as_text(row.get("phone_number")),
                "device_type": self._device_type(row.get("type")),
                "firmware_version": self._as_text(row.get("firmware_version")),
                "is_mqtt_enabled": bool(row.get("is_mqtt_enabled")),
                "total_allocated": self._as_int(row.get("total_allocated")) or 0,
                # `status` here is the portal's own two-hour heuristic on
                # last_communication_at, not a stored column.
                "remote_status": self._as_text(row.get("status")),
                "raw_payload": json.dumps(row, default=str, sort_keys=True),
            }
            if live is not None:
                vals.update({
                    "is_online": bool(live.get("is_online")),
                    "connectivity_known": True,
                    "connected_at": tipsoi_time.millis_to_utc(live.get("connected_at"))
                    or tipsoi_time.parse_dp_naive(live.get("connected_at")),
                    "last_communication_at":
                        tipsoi_time.millis_to_utc(live.get("last_communication_at")),
                })
            else:
                # Fall back to the wall-clock column on the device row itself. It is in
                # the application timezone, unlike the connectivity call's epoch millis.
                vals["last_communication_at"] = tipsoi_time.dp_to_utc(
                    row.get("last_communication_at"), backend.source_timezone)
            parsed.append(vals)
        return parsed

    @api.model
    def _fetch_dp_connectivity(self, adapter, run):
        """Live MQTT state, keyed by identifier. Never fatal.

        This endpoint calls out to the MQTT broker and answers 500 when the broker is
        unreachable. That is a reason to show the device list without live state, not a
        reason to fail the whole refresh -- so the failure is recorded and swallowed.
        """
        try:
            body = adapter.request("GET", "devices/connectivity_status")
        except tipsoi_client.TipsoiError as exc:
            _logger.info("Tipsoi connectivity_status unavailable: %s", exc)
            run.add_note(_(
                "Device list refreshed, but live connection state was unavailable "
                "(%s). Connection state is left as it was.", exc.message))
            return {}
        rows = body if isinstance(body, list) else (body or {}).get("data") or []
        return {self._as_text(r.get("identifier")): r
                for r in rows if isinstance(r, dict) and r.get("identifier")}

    # -- HRM ---------------------------------------------------------------------------

    @api.model
    def _fetch_hrm(self, backend, adapter):
        params = {}
        if backend.tipsoi_office_id:
            params["officeId"] = backend.tipsoi_office_id
        body = adapter.request("GET", "devices", params=params)
        rows = (body or {}).get("devices") if isinstance(body, dict) else body
        parsed = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            status = self._as_int(row.get("status"))
            has_live = "isOnline" in row and row.get("isOnline") is not None
            parsed.append({
                "identifier": self._as_text(row.get("identifier")),
                "tipsoi_id": self._as_int(row.get("id")),
                "central_server_id": self._as_int(row.get("centralServerId")),
                "description": self._as_text(row.get("description")),
                "location": self._as_text(row.get("location")),
                "phone_number": self._as_text(row.get("phoneNumber")),
                "device_type": self._device_type(row.get("type")),
                "workplace_name": self._as_text(row.get("workplaceName")),
                "is_online": bool(row.get("isOnline")),
                "connectivity_known": has_live,
                "connected_at": tipsoi_time.millis_to_utc(row.get("connectedAt")),
                "last_communication_at":
                    tipsoi_time.millis_to_utc(row.get("lastCommunicationAt")),
                "remote_status": HRM_DEVICE_STATUS.get(
                    status, self._as_text(row.get("status"))),
                "raw_payload": json.dumps(row, default=str, sort_keys=True),
            })
        return parsed

    # ----------------------------------------------------------------------------------
    # tolerant readers
    # ----------------------------------------------------------------------------------
    # Both payloads are raw model rows rather than curated resources, so a column can
    # arrive as a number where a string is expected and vice versa. Coercing here beats
    # a traceback halfway through a page.

    @staticmethod
    def _as_text(value):
        if value in (None, False):
            return ""
        return str(value).strip()

    @staticmethod
    def _as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _device_type(value):
        known = {code for code, _label in DEVICE_TYPES}
        text = (str(value or "").strip().lower())
        if text in known:
            return text
        return "other" if text else False
