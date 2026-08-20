# -*- coding: utf-8 -*-
"""Timestamp handling for the two Tipsoi APIs.

Pure functions, no ORM, so they can be reasoned about and tested on their own. Every
rule here was read out of the two implementations, because the two APIs represent time
in genuinely different ways and getting either wrong shifts attendance by hours without
raising anything.

**Device Portal** hands back naive wall-clock strings (`Y-m-d H:i:s`) in the API
server's own application timezone -- `Asia/Dhaka` by default, but `APP_TIMEZONE` is environment
overridable, so it is a per-backend setting here and never a constant. The `start` and
`end` query parameters are parsed in that same zone, so requests must be built in local
wall time too, not in UTC.

**HRM** works in epoch milliseconds, and where it formats a time for display it does so
at a *fixed* `+06:00` offset -- an offset compiled into the API rather than a named or
configurable timezone. A fixed offset has no DST, so plain arithmetic is
exactly right on that path and pytz would be the wrong tool. Day keys in the attendance
map are start-of-day at that offset, and `firstLoggedTime` / `lastLoggedTime` are
`hh:mm a` strings in it (or the literal `"-"` when there is no punch).
"""

from datetime import date, datetime, time, timedelta

import pytz

#: HRM's hardcoded display offset. Not configurable, because it is not configurable
#: upstream either -- it is a compiled-in constant, so making it a setting here would
#: invite someone to "fix" it to their own timezone and silently shift every day row.
HRM_UTC_OFFSET = timedelta(hours=6)

DP_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: The formats seen on the Device Portal's timestamps. The plain one is what the
#: curated responses emit; the others turn up on raw database columns the API
#: serialises directly.
_DP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
)

#: `hh:mm a` as Java's DateTimeFormatter renders it, plus the 24-hour variants an
#: upstream change could plausibly switch to. Tolerant on purpose: this value is a
#: display string, so it is the field most likely to be reformatted without notice.
_HRM_TIME_FORMATS = ("%I:%M %p", "%I:%M%p", "%H:%M:%S", "%H:%M")

#: Values that mean "no punch". The API writes a literal "-" when the underlying
#: milliseconds are null, so this is a sentinel and not a parse failure.
_EMPTY_TIME_VALUES = ("", "-", "--", "N/A", "n/a", "null", "None")


def to_tz(name):
    """Return a tzinfo for a timezone name, falling back to Asia/Dhaka.

    Asia/Dhaka is the Device Portal's own default, so it is the right fallback rather
    than UTC -- guessing UTC would silently move every punch by six hours.
    """
    try:
        return pytz.timezone(name or "Asia/Dhaka")
    except pytz.UnknownTimeZoneError:
        return pytz.timezone("Asia/Dhaka")


# --------------------------------------------------------------------------------------
# Device Portal: naive local wall time
# --------------------------------------------------------------------------------------

def parse_self_describing(value):
    """Naive UTC for a timestamp that carries its own offset, else None.

    Kept as its own function because this case and the naive one must not both run: a
    string with an offset is already absolute, and localizing the result as though it
    were local wall time moves it by the backend's offset a second time.
    """
    if not value or isinstance(value, datetime):
        return None
    text = str(value).strip()
    if len(text) <= 19 or not (text[-6] in "+-" or text.endswith("Z")):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(pytz.UTC).replace(tzinfo=None)


def dp_to_utc(value, tzname):
    """Parse a Device Portal timestamp into a naive UTC datetime.

    A string carrying its own offset means what it says, whatever the backend's timezone
    is set to, so that case is handled first and returned outright. Everything else is
    naive wall time in the application timezone.

    `is_dst=None` would raise on the ambiguous hour of a DST transition. Asia/Dhaka has
    no DST, but the field is configurable and some deployments are not in Dhaka, so
    ambiguity is resolved rather than raised: a punch that lands in a repeated hour is
    still a real punch and must not fail the whole page.
    """
    absolute = parse_self_describing(value)
    if absolute is not None:
        return absolute
    naive = parse_dp_naive(value)
    if naive is None:
        return None
    tz = to_tz(tzname)
    try:
        aware = tz.localize(naive, is_dst=False)
    except (AttributeError, ValueError):
        aware = naive.replace(tzinfo=pytz.UTC)
    return aware.astimezone(pytz.UTC).replace(tzinfo=None)


def parse_dp_naive(value):
    """Parse a Device Portal timestamp without moving it. Returns None if unparseable.

    A self-describing string is resolved to UTC here too, for callers that just want one
    value out of a field whose format varies. `dp_to_utc` checks that case before calling
    this, so it never converts twice.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    absolute = parse_self_describing(value)
    if absolute is not None:
        return absolute
    text = str(value).strip()
    if text in _EMPTY_TIME_VALUES:
        return None
    for fmt in _DP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def utc_to_dp_param(value, tzname):
    """Render a naive UTC datetime as a Device Portal query parameter.

    `start` and `end` are parsed in the API server's application timezone, so sending UTC
    would shift the window by the offset -- six hours, by default. The window has to
    be expressed in the same local wall time the server reads it in.
    """
    if not value:
        return None
    aware = value.replace(tzinfo=pytz.UTC).astimezone(to_tz(tzname))
    return aware.strftime(DP_DATETIME_FORMAT)


# --------------------------------------------------------------------------------------
# HRM: epoch milliseconds at a fixed +06:00
# --------------------------------------------------------------------------------------

def millis_to_utc(value):
    """Epoch milliseconds to a naive UTC datetime."""
    if value in (None, "", False):
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime(1970, 1, 1) + timedelta(milliseconds=millis)


def utc_to_millis(value):
    """Naive UTC datetime to epoch milliseconds."""
    if not value:
        return None
    return int((value - datetime(1970, 1, 1)).total_seconds() * 1000)


def hrm_day_to_date(day_key):
    """Turn an attendance-map key into the calendar date it represents.

    The key is start-of-day in epoch millis at HRM's fixed +06:00, so the date has to be
    read at that offset. Reading it as UTC lands on the previous day for every row.
    """
    moment = millis_to_utc(day_key)
    if moment is None:
        return None
    return (moment + HRM_UTC_OFFSET).date()


def date_to_hrm_millis(day, end_of_day=False):
    """Epoch millis for the start (or end) of a date at HRM's fixed +06:00."""
    if isinstance(day, datetime):
        day = day.date()
    moment = datetime.combine(day, time.max if end_of_day else time.min)
    return utc_to_millis(moment - HRM_UTC_OFFSET)


def parse_hrm_time(value):
    """Parse `firstLoggedTime` / `lastLoggedTime` into a time, or None.

    Returns None for the literal "-" that upstream writes when there is no punch, which
    is a normal value and not an error -- treat it as "no time recorded".
    """
    if not value:
        return None
    text = str(value).strip()
    if text in _EMPTY_TIME_VALUES:
        return None
    for fmt in _HRM_TIME_FORMATS:
        try:
            return datetime.strptime(text.upper().replace(".", ""), fmt).time()
        except ValueError:
            continue
    return None


def hrm_day_span(day_key, first_logged, last_logged):
    """Build (check_in_utc, check_out_utc) for one HRM attendance day.

    The two inputs are minute-precision display strings; the day is a date. Two rules
    make this correct rather than merely plausible:

    * both are at HRM's fixed +06:00, so converting is subtraction -- no DST, no pytz;
    * an exit earlier in the day than the entry means the shift crossed midnight, so the
      exit belongs to the following date. Without this an overnight shift produces a
      negative span, which `hr.attendance` rejects outright.
    """
    day = hrm_day_to_date(day_key) if not isinstance(day_key, date) else day_key
    if day is None:
        return None, None
    entry = parse_hrm_time(first_logged)
    exit_ = parse_hrm_time(last_logged)
    if entry is None:
        return None, None
    check_in = datetime.combine(day, entry) - HRM_UTC_OFFSET
    if exit_ is None:
        return check_in, None
    check_out = datetime.combine(day, exit_) - HRM_UTC_OFFSET
    if check_out < check_in:
        check_out += timedelta(days=1)
    return check_in, check_out
