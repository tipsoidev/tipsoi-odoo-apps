# -*- coding: utf-8 -*-
"""HTTP transport for the two Tipsoi APIs.

One client per backend record, and a backend is *either* the Device Portal *or* the
HRM API — never both. `backend_type` selects the whole pipeline, so these two adapters
never appear in the same sync.

The behaviour encoded here follows the Tipsoi APIs as they are actually implemented,
which differs in places from the published specifications. Where a rule looks surprising,
the comment explains why it exists.
"""

import json
import logging
import re
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------------------
# Photo uploads run through a server-side enhancement step that can take up to two
# minutes per image, so a short read timeout abandons work the server is still doing.
# Everything else is quick.
#
# A finite timeout is mandatory rather than defensive: a request can occasionally fail to
# be answered at all, so every call needs its own ceiling.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
READ_TIMEOUT_PHOTO = 180

# Retry policy. 4xx is never retried (the request is wrong and will stay wrong) with the
# single exception of 429. 5xx and connection errors are transient.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0

# The Device Portal clamps per_page to this and reports the clamped value back in
# meta.per_page rather than erroring, so asking for more achieves nothing.
DP_MAX_PER_PAGE = 10000


class TipsoiError(Exception):
    """Base for every failure this transport reports."""

    def __init__(self, message, status=None, error_code=None, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_code = error_code
        self.payload = payload or {}


class TipsoiAuthError(TipsoiError):
    """401/403 from an authentication failure. Re-login once, then give up."""


class TipsoiInactiveProjectError(TipsoiError):
    """The Device Portal's `client-api-access` middleware refused the project.

    Returned as 403 when the Tipsoi project itself is inactive. This is NOT an auth
    failure and must never be retried or presented as one -- the credentials are fine,
    the account is switched off at the Tipsoi end.
    """


class TipsoiPermanentError(TipsoiError):
    """A 4xx the server will keep rejecting. Never retried."""


class TipsoiTransientError(TipsoiError):
    """A 5xx, a 429, or a connection failure. Retried with backoff."""


class TipsoiPhotoError(TipsoiPermanentError):
    """A photo the pipeline will never accept.

    NO_FACE_DETECTED / INVALID_IMAGE_SIZE, both 422. Surfaced to a human rather than
    retried. ENHANCEMENT_UNAVAILABLE (503) and S3_UPLOAD_FAILED (500) are *not* this --
    they come back as TipsoiTransientError and do get retried.
    """


# Credentials must never reach a log. Tokens are always sent in the Authorization
# header rather than the query string, and anything that slips into a log line is
# scrubbed here.
_REDACT = re.compile(
    r"(api_token=|Bearer\s+|\"(?:token|refreshToken|api_token|password)\"\s*:\s*\")([^\"&\s]+)",
    re.IGNORECASE,
)


def redact(text):
    """Replace credential-looking values with a fixed marker."""
    if not text:
        return text
    return _REDACT.sub(lambda m: m.group(1) + "***", str(text))


class BaseAdapter:
    """Shared HTTP mechanics. Subclasses own authentication and paging dialect."""

    #: Mode isolation is enforced here rather than trusted: a backend in
    #: one mode must issue zero requests to the other API's host, and this is the single
    #: choke point where that can be checked.
    #:
    #: Two forms, because the two APIs are not symmetric. The Device Portal lives on one
    #: host family, so it can be pinned positively. HRM is served from several changing
    #: hosts, so a positive list would rot; what matters is that it never reaches the
    #: Device Portal. Take care with substrings when editing these: an over-broad
    #: fragment can match the other API's hostname and silently disable the guard in one
    #: direction. There is a test for exactly that.
    allowed_host_fragments = ()
    denied_host_fragments = ()

    def __init__(self, backend):
        self.backend = backend
        self.session = requests.Session()

    # -- authentication ----------------------------------------------------------------

    def login(self):
        raise NotImplementedError

    def auth_headers(self):
        raise NotImplementedError

    def reauthenticate(self):
        """Recover from a 401 exactly once per request. Adapters differ: the Device
        Portal has no refresh flow at all, HRM does."""
        return self.login()

    # -- paging dialect ----------------------------------------------------------------

    def page_params(self, page, page_size):
        raise NotImplementedError

    def extract_page(self, body):
        """Return (rows, has_more) from a response body."""
        raise NotImplementedError

    # -- request ------------------------------------------------------------------------

    def url_for(self, path):
        base = (self.backend.base_url or "").rstrip("/")
        if not base:
            raise UserError(_("This Tipsoi backend has no base URL configured."))
        self._assert_host_allowed(base)
        return "%s/%s" % (base, path.lstrip("/"))

    def _assert_host_allowed(self, base):
        host = base.lower()
        bad = any(frag in host for frag in self.denied_host_fragments)
        missing = (self.allowed_host_fragments
                   and not any(frag in host for frag in self.allowed_host_fragments))
        if bad or missing:
            # Not a vague message: this means the backend is pointed at the *other* API,
            # which is the exact mixing the design forbids.
            raise UserError(_(
                "This backend is configured as '%(mode)s' but its base URL (%(url)s) "
                "belongs to the other Tipsoi API. A backend must talk to one API only, "
                "so that people and employee IDs are managed in exactly one place.",
                mode=self.backend.backend_type,
                url=base,
            ))

    def request(self, method, path, params=None, json_body=None, data=None,
                files=None, read_timeout=None, _authenticated=True, _retried_auth=False):
        url = self.url_for(path)
        headers = {"Accept": "application/json"}
        if _authenticated:
            headers.update(self.auth_headers())

        timeout = (CONNECT_TIMEOUT, read_timeout or READ_TIMEOUT)
        last_exc = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, data=data,
                    files=files, headers=headers, timeout=timeout,
                )
            except requests.RequestException as exc:
                last_exc = TipsoiTransientError(
                    _("Could not reach Tipsoi: %s", redact(exc)))
                self._sleep_before_retry(attempt, None)
                continue

            # 401 -> re-authenticate once, then treat as a hard failure.
            if response.status_code == 401 and _authenticated and not _retried_auth:
                _logger.info("Tipsoi 401 on %s; re-authenticating once", path)
                self.reauthenticate()
                return self.request(
                    method, path, params=params, json_body=json_body, data=data,
                    files=files, read_timeout=read_timeout,
                    _authenticated=True, _retried_auth=True,
                )

            try:
                return self._handle(response)
            except TipsoiTransientError as exc:
                last_exc = exc
                if attempt == MAX_ATTEMPTS:
                    break
                self._sleep_before_retry(attempt, response)

        raise last_exc

    def _sleep_before_retry(self, attempt, response):
        """Honour Retry-After when offered, else exponential backoff.

        429 is uncommon on these endpoints today, but it is handled so that enabling a
        rate limit upstream does not turn into failed syncs here.
        """
        delay = BACKOFF_BASE ** (attempt - 1)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except (TypeError, ValueError):
                    pass
        time.sleep(delay)

    # -- response handling -------------------------------------------------------------

    def _handle(self, response):
        status = response.status_code
        body = self._parse_body(response)

        if 200 <= status < 300:
            return body

        message, error_code = self.describe_error(status, body, response.text)

        # The Device Portal's inactive-project refusal. Distinct from auth so that an
        # operator sees "the account is switched off", not "check your password".
        if status == 403 and isinstance(body, dict) and body.get("error") is True:
            raise TipsoiInactiveProjectError(message, status=status, payload=body)

        if status in (401, 403):
            raise TipsoiAuthError(message, status=status, payload=body)

        # Photo failures carry a stable machine code. Two of the four are permanent and
        # must be shown to a human instead of retried forever; the other two are
        # transient and fall through to the retry path below.
        if error_code in ("NO_FACE_DETECTED", "INVALID_IMAGE_SIZE"):
            raise TipsoiPhotoError(
                message, status=status, error_code=error_code, payload=body)

        if status in RETRYABLE_STATUS:
            raise TipsoiTransientError(
                message, status=status, error_code=error_code, payload=body)

        if 400 <= status < 500:
            raise TipsoiPermanentError(
                message, status=status, error_code=error_code, payload=body)

        raise TipsoiTransientError(
            message, status=status, error_code=error_code, payload=body)

    @staticmethod
    def _parse_body(response):
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return {"_raw": response.text}

    def describe_error(self, status, body, raw):
        """Turn a response into (message, error_code).

        There are three different error shapes across the two APIs, so each adapter
        parses its own -- one shared parser would silently mis-read two of them.
        """
        return (redact(raw) or _("HTTP %s", status)), None

    # -- pagination --------------------------------------------------------------------

    def paginate(self, path, params=None, page_size=None, max_pages=None):
        """Yield rows page by page.

        The cursor is deliberately *not* derived from the rows. On the Device Portal,
        the field the server filters on is not the field it reports back, so advancing
        a cursor from a returned value can walk backwards. Callers set the cursor to the
        window end they asked for instead.
        """
        params = dict(params or {})
        page_size = page_size or self.backend.page_size
        page = 1
        while True:
            page_params = dict(params)
            page_params.update(self.page_params(page, page_size))
            body = self.request("GET", path, params=page_params)
            rows, has_more = self.extract_page(body)
            if rows:
                yield rows
            if not has_more or not rows:
                return
            page += 1
            if max_pages and page > max_pages:
                _logger.warning(
                    "Tipsoi %s: stopping at max_pages=%s -- results were truncated, "
                    "this is not a complete read", path, max_pages)
                return


class DevicePortalAdapter(BaseAdapter):
    """The Tipsoi Device Portal API.

    Owns raw punches, devices, allocations, enrolment and people. Its token is a column
    on the Project row and does not expire, so there is no refresh flow -- a 401 means
    the token was rotated and we simply log in again.
    """

    # The Device Portal is one host family, so pin it positively.
    allowed_host_fragments = ("api-inovace360.com",)

    def login(self):
        body = self.request(
            "POST", "auth/login",
            json_body={
                "username": self.backend.username,
                "password": self.backend.password,
            },
            _authenticated=False,
        )
        token = body.get("api_token")
        if not token:
            raise TipsoiAuthError(
                _("Device Portal login returned no api_token."), payload=body)
        # has_hrm tells us whether a Tipsoi app (HRM) is attached to this project. It
        # is the mode-detection signal, so it is stored rather than glanced at.
        self.backend.sudo().write({
            "access_token": token,
            "token_expiry": False,          # never expires; a column, not a JWT
            "remote_has_hrm": bool(body.get("has_hrm")),
            "remote_organization": body.get("organization") or "",
        })
        return token

    def auth_headers(self):
        token = self.backend.access_token or self.login()
        # The API also accepts the token as a query parameter, but that puts the
        # credential into access logs. Always use the header.
        return {"Authorization": "Bearer %s" % token}

    def page_params(self, page, page_size):
        return {"page": page, "per_page": min(page_size, DP_MAX_PER_PAGE)}

    def extract_page(self, body):
        if not isinstance(body, dict):
            return (body or []), False
        rows = body.get("data")
        if rows is None:
            # Some endpoints return a bare list with no envelope, and the people list
            # is not paginated at all -- the whole set arrives in one response.
            rows = body if isinstance(body, list) else []
            return rows, False
        meta = body.get("meta") or {}
        current = meta.get("current_page")
        last = meta.get("last_page")
        has_more = bool(current and last and current < last)
        return rows, has_more

    def describe_error(self, status, body, raw):
        # {"code": .., "context": .., "message": ..} on most failures, plus an
        # "error_code" on photo failures, plus {"error": true, "message": ..} from the
        # inactive-project middleware.
        if isinstance(body, dict):
            message = body.get("message") or redact(raw)
            context = body.get("context")
            if context:
                message = "%s (%s)" % (message, context)
            return message, body.get("error_code")
        return redact(raw), None


class HrmAdapter(BaseAdapter):
    """The Tipsoi app (HRM) API.

    Owns employees, the org masters and derived attendance. Authentication is a JWT with
    a refresh token, and the refresh call needs the userId that sign-in returned -- so
    both are persisted on the backend record.
    """

    # Deny rather than allow: HRM hosts change, but there is exactly one other API, and
    # "inovace360.com" as an allow-fragment would have matched api-inovace360.com too.
    denied_host_fragments = ("api-inovace360.com",)

    def login(self):
        body = self.request(
            "POST", "auth/external-sync/sign-in",
            json_body={
                "email": self.backend.username,
                "password": self.backend.password,
            },
            _authenticated=False,
        )
        token = body.get("token")
        if not token:
            raise TipsoiAuthError(
                _("HRM sign-in returned no token."), payload=body)
        self.backend.sudo().write({
            "access_token": token,
            "refresh_token": body.get("refreshToken") or "",
            # /auth/refresh takes {refreshToken, userId}; officeId is required as a query
            # param by several HRM reads. Sign-in is the only place both are offered.
            "tipsoi_user_id": body.get("userId") or 0,
            "tipsoi_office_id": body.get("officeId") or 0,
            "token_expiry": self.backend._jwt_expiry(token),
        })
        return token

    def reauthenticate(self):
        """Refresh if we can, else sign in again."""
        if self.backend.refresh_token and self.backend.tipsoi_user_id:
            try:
                body = self.request(
                    "POST", "auth/refresh",
                    json_body={
                        "refreshToken": self.backend.refresh_token,
                        "userId": str(self.backend.tipsoi_user_id),
                    },
                    _authenticated=False,
                )
                token = body.get("token")
                if token:
                    self.backend.sudo().write({
                        "access_token": token,
                        "refresh_token": body.get("refreshToken") or self.backend.refresh_token,
                        "token_expiry": self.backend._jwt_expiry(token),
                    })
                    return token
            except TipsoiError as exc:
                _logger.info("HRM token refresh failed (%s); signing in again", exc)
        return self.login()

    def auth_headers(self):
        token = self.backend.access_token
        if not token or self.backend._token_expired():
            token = self.reauthenticate()
        return {"Authorization": "Bearer %s" % token}

    def page_params(self, page, page_size):
        # camelCase here, snake_case on the Device Portal. The param names live in the
        # adapter precisely so the rest of the addon never has to know.
        return {"pageNumber": page, "perPage": page_size}

    def extract_page(self, body):
        if not isinstance(body, dict):
            return (body or []), False
        for key in ("attendance", "employees", "data", "content"):
            if isinstance(body.get(key), list):
                rows = body[key]
                break
        else:
            return [], False
        current = body.get("currentPage")
        total = body.get("totalPages")
        has_more = bool(current and total and current < total)
        return rows, has_more

    def describe_error(self, status, body, raw):
        # Three shapes here alone: {"message": ..} from the @ControllerAdvice,
        # {"message": .., "errorFieldNames": [..]} from bean validation, and Spring
        # Boot's default {timestamp, status, error, path} when nothing handled it.
        if isinstance(body, dict):
            message = body.get("message") or body.get("error") or redact(raw)
            fields = body.get("errorFieldNames")
            if fields:
                message = "%s: %s" % (message, ", ".join(fields))
            return message, None
        return redact(raw), None


ADAPTERS = {
    "device_portal": DevicePortalAdapter,
    "hrm": HrmAdapter,
}


def build(backend):
    """Return the adapter for a backend record."""
    try:
        return ADAPTERS[backend.backend_type](backend)
    except KeyError:
        raise UserError(_("Unknown Tipsoi backend type: %s", backend.backend_type))
