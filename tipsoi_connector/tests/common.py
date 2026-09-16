# -*- coding: utf-8 -*-
"""Shared fixtures: a fake HTTP transport, and a base case that installs it.

The fake intercepts `requests.Session.request` -- the *real* HTTP boundary -- rather than
`BaseAdapter.request`. That distinction is load-bearing. `url_for`, and therefore the
mode-isolation host guard, runs inside `BaseAdapter.request`, so patching any higher would
skip the very thing several of these tests exist to prove. Intercepting at the socket
boundary also means the recorded URL is the full one, host included, which is what the
isolation assertions inspect.

An unregistered call raises. A fake that quietly answered `{}` would let a test pass while
the code under it called something nobody expected.
"""

import json
from unittest.mock import patch

import requests

from odoo.tests import TransactionCase

from ..models import tipsoi_client

#: A real 1x1 PNG. `fields.Image` runs incoming bytes through Pillow, so a photo test
#: cannot use junk -- it has to be an image Odoo will actually accept.
PNG_1X1 = (
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    b"AAH/q842iQAAAABJRU5ErkJggg=="
)

#: A real 1x1 GIF, for the "Tipsoi takes JPEG and PNG only" path. Verified to decode to
#: a GIF89a header, because a fixture that is not the format under test proves nothing.
GIF_1X1 = b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

DP_BASE = "https://test.api-inovace360.com/api/v1"
HRM_BASE = "https://test.clients.inovacetech.com/inovace-client/api/v1"
DP_HOST_FRAGMENT = "api-inovace360.com"


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


class Pages(list):
    """Marks a list as *several* response bodies rather than one JSON array.

    Needed because the Device Portal genuinely returns bare arrays -- `GET /devices` and
    `GET /people` both do -- so a plain list is ambiguous and guessing would make a
    pagination test and a bare-array test contradict each other.
    """


class UnregisteredRoute(AssertionError):
    """Raised loudly, because a silent default would hide a wrong call."""


class FakeTransport:
    """A stand-in for `requests.Session.request` that records everything.

    Routes are registered as `(method, path_fragment)`. Matching is longest-fragment
    first, so `employee/profile/picture` wins over `employee` on the same URL.

    A route registered with a list of bodies cycles through them, one per call. Cycling
    rather than stopping at the end is what lets a test poll the same two-page window
    twice and get the same two pages both times, which is how the idempotency tests are
    written.
    """

    def __init__(self):
        self.routes = {}
        self.calls = []

    # -- registration -------------------------------------------------------------------

    def route(self, method, fragment, body=None, status=200, headers=None):
        """Register one route. Wrap the body in `Pages([...])` to cycle bodies."""
        bodies = list(body) if isinstance(body, Pages) else [body]
        self.routes[(method.upper(), fragment)] = {
            "bodies": bodies, "status": status, "headers": headers or {}, "index": 0}
        return self

    def routes_for(self, pairs):
        for args in pairs:
            self.route(*args)
        return self

    # -- invocation ---------------------------------------------------------------------

    def __call__(self, *args, **kwargs):
        # Patching a class attribute with a callable *instance* means the descriptor
        # protocol does not apply and no `self` is passed -- but that is subtle enough to
        # be worth tolerating both shapes rather than depending on it.
        args = list(args)
        if args and isinstance(args[0], requests.Session):
            args.pop(0)
        method = (kwargs.pop("method", None) or (args.pop(0) if args else "GET")).upper()
        url = kwargs.pop("url", None) or (args.pop(0) if args else "")

        self.calls.append({
            "method": method,
            "url": url,
            "params": kwargs.get("params"),
            "json": kwargs.get("json"),
            "data": kwargs.get("data"),
            "files": kwargs.get("files"),
        })

        route = self._match(method, url)
        if route is None:
            raise UnregisteredRoute(
                "No fake route for %s %s. Registered: %s"
                % (method, url, sorted(self.routes)))
        body = route["bodies"][route["index"] % len(route["bodies"])]
        route["index"] += 1
        return FakeResponse(route["status"], body, route["headers"])

    def _match(self, method, url):
        candidates = [(m, frag) for (m, frag) in self.routes
                      if m == method and frag in url]
        if not candidates:
            return None
        # Longest fragment wins, so `employee/profile/picture` beats `employee` on the
        # same URL. Alphabetical is only a tie-break, but it keeps route resolution
        # deterministic rather than dependent on registration order.
        best = min(candidates, key=lambda pair: (-len(pair[1]), pair[1]))
        return self.routes[best]

    # -- assertions helpers --------------------------------------------------------------

    def urls(self, method=None):
        return [c["url"] for c in self.calls
                if method is None or c["method"] == method.upper()]

    def call_for(self, fragment, method=None):
        """The first recorded call whose URL contains `fragment`."""
        for call in self.calls:
            if fragment in call["url"] and (
                    method is None or call["method"] == method.upper()):
                return call
        return None

    def count_for(self, fragment, method=None):
        return len([c for c in self.calls
                    if fragment in c["url"]
                    and (method is None or c["method"] == method.upper())])

    def reset(self):
        self.calls = []
        for route in self.routes.values():
            route["index"] = 0


class TipsoiCase(TransactionCase):
    """Base case with the fake transport installed."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport()
        patcher = patch.object(requests.Session, "request", self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Retries back off with real sleeps, which would add seconds to any test that
        # exercises a 5xx.
        sleep_patcher = patch.object(tipsoi_client.time, "sleep", lambda *_a: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self._companies = 0

    # -- fixtures -----------------------------------------------------------------------

    def _company(self, name):
        """A company that `hr.employee` can actually be created against.

        A bare new company can lack a working calendar, and `hr.employee` carries a
        resource, so the calendar is filled in explicitly rather than hoped for.
        """
        company = self.env["res.company"].create({"name": name})
        if not company.resource_calendar_id:
            company.resource_calendar_id = self.env["resource.calendar"].create({
                "name": "%s hours" % name, "company_id": company.id})
        self.env.user.company_ids = [(4, company.id)]
        return company

    def _backend(self, backend_type="device_portal", **overrides):
        """A backend that is already authenticated and ready.

        `access_token` is preset so route tables do not have to stub a login on every
        test; `test_transport.py` covers the login flow itself.
        """
        vals = {
            "backend_type": backend_type,
            "environment": "test",
            "base_url": DP_BASE if backend_type == "device_portal" else HRM_BASE,
            "username": "u" if backend_type == "device_portal" else "a@b.c",
            "password": "p",
            "access_token": "tok",
            "state": "ready",
        }
        vals.update(overrides)
        if "company_id" not in vals:
            existing = self.env["tipsoi.backend"].with_context(
                active_test=False).search(
                    [("company_id", "=", self.env.company.id)], limit=1)
            if existing:
                self._companies += 1
                vals["company_id"] = self._company(
                    "Tipsoi test co %s" % self._companies).id
        return self.env["tipsoi.backend"].create(vals)

    def _employee(self, identifier=None, backend=None, **vals):
        """A Tipsoi-linked employee, returned on a *context-free* recordset.

        The bypass context is used for the create and then deliberately dropped. A
        context rides along on the recordset it was set on, so returning the bypassing
        one would short-circuit the `write` override in every later line of the test --
        including the tests whose whole subject is that override. Handing back a clean
        recordset is what makes those tests test anything.
        """
        values = {"name": vals.pop("name", identifier or "Someone")}
        if backend is not None:
            values["company_id"] = backend.company_id.id
            values["tipsoi_backend_id"] = backend.id
        if identifier:
            values["tipsoi_identifier"] = identifier
        values.update(vals)
        employee = self.env["hr.employee"].with_context(
            tipsoi_syncing=True).create(values)
        return employee.with_env(self.env)

    def _device(self, backend, identifier, **vals):
        values = {"backend_id": backend.id, "identifier": identifier}
        values.update(vals)
        return self.env["tipsoi.device"].create(values)

    def _run(self, backend, job):
        """The audited run wrapper the jobs themselves use."""
        return self.env["tipsoi.sync.run"].track(backend, job)

    def _groups_field(self):
        """`res.users.groups_id` was renamed `group_ids` in Odoo 19.

        Asked of the model rather than branched on a version number, so this one file
        serves every supported series and the test tree stays byte-identical across
        branches -- the invariant the backport recipe depends on.
        """
        users = self.env["res.users"]
        return "group_ids" if "group_ids" in users._fields else "groups_id"

    def _plain_user(self):
        """A user with HR rights but *not* the Tipsoi administrator role.

        Built explicitly rather than by `res.users.create` defaults, because the module's
        security data grants the Tipsoi admin group to the default-user template -- so a
        default-built user would have exactly the group under test.
        """
        return self.env["res.users"].create({
            "name": "Plain HR", "login": "plain-hr-tipsoi",
            self._groups_field(): [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("hr.group_hr_manager").id,
            ])],
        })
