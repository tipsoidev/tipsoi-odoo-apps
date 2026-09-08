# -*- coding: utf-8 -*-
"""Working-schedule arithmetic, read from `resource.calendar` the long way round.

Pure functions, no ORM, so they can be reasoned about and tested on their own -- the same
shape as `tipsoi_time`, and for the same reason: this is the only genuinely fiddly logic
in the day builder, and it is worth being able to test it without a database.

**Why the raw fields and not `_attendance_intervals_batch()`.** Odoo's own helper is the
obvious tool and it is the wrong one here. Its signature, its return shape and the way it
folds in leaves all moved between 15.0 and 19.0, and this module ships on all five series
from one source. `resource.calendar.attendance`'s columns -- `dayofweek`, `hour_from`,
`hour_to`, `date_from`, `date_to`, `week_type` -- have not moved in that range, so reading
them directly is what keeps one file working everywhere.

**What a caller must pass.** Plain data, not records. The model reads the recordset once
and hands over a list of `Slot`; that keeps the ORM on one side of the line and the
arithmetic on the other.

**Hours are floats in the calendar's own timezone.** `hour_from = 8.5` is 08:30 local to
`resource.calendar.tz`, which is not necessarily the timezone the devices report in. The
caller resolves that; this module only ever deals in the calendar's own frame.
"""

import math
from collections import namedtuple

#: One `resource.calendar.attendance` row, reduced to the fields that decide whether it
#: applies to a given day. `week_type` is `"0"`, `"1"` or falsy; `date_from` / `date_to`
#: are `date` or None; `is_lunch` folds `day_period == "lunch"`, which exists from 17.0
#: and marks an unpaid break rather than working time.
Slot = namedtuple(
    "Slot", "dayofweek hour_from hour_to date_from date_to week_type is_lunch")

#: Odoo stores `dayofweek` as a string, Monday = "0". Kept explicit because comparing it
#: to `date.weekday()` without the conversion is a silent one-day shift.
MONDAY = 0


def week_type_of(day):
    """`0` or `1` for a two-week calendar, by Odoo's own rule.

    Reproduced rather than imported: it lives on `resource.calendar` as a model method,
    and the whole point of this module is not to depend on that class's surface. The
    formula itself has been stable since two-week calendars were introduced.
    """
    return int(math.floor((day.toordinal() - 1) / 7) % 2)


def _applies(slot, day, two_weeks_calendar):
    if int(slot.dayofweek) != day.weekday():
        return False
    if slot.is_lunch:
        # Unpaid break. Counting it as owed time would inflate every expected day.
        return False
    if slot.date_from and day < slot.date_from:
        return False
    if slot.date_to and day > slot.date_to:
        return False
    if two_weeks_calendar and slot.week_type not in (None, False, ""):
        if int(slot.week_type) != week_type_of(day):
            return False
    return True


def slots_for_day(slots, day, two_weeks_calendar=False):
    """The working intervals on `day`, as sorted `(hour_from, hour_to)` pairs.

    An empty list means the calendar says this is not a working day -- which is the only
    thing that ever justifies calling somebody absent.
    """
    active = [(s.hour_from, s.hour_to) for s in slots
              if _applies(s, day, two_weeks_calendar)]
    # Defensive: a row saved with hour_to before hour_from would otherwise contribute
    # negative hours and quietly cancel out a real interval.
    return sorted((lo, hi) for lo, hi in active if hi > lo)


def expected_hours(slots, day, two_weeks_calendar=False):
    """Hours owed on `day`. `0.0` on a non-working day, and on no calendar at all.

    Overlapping intervals are merged rather than summed: two rows that overlap are a
    data-entry mistake, and double-counting the overlap would make the day look longer
    than any shift the person could actually work.
    """
    merged = _merge(slots_for_day(slots, day, two_weeks_calendar))
    return round(sum(hi - lo for lo, hi in merged), 2)


def expected_span(slots, day, two_weeks_calendar=False):
    """`(first_start, last_end)` as float hours, or `(None, None)` on a non-working day.

    First start and last end rather than each interval, because that is what lateness and
    an early exit are measured against: arriving after the day's first interval opens,
    leaving before its last one closes. What happens over lunch in between is not
    lateness.
    """
    day_slots = slots_for_day(slots, day, two_weeks_calendar)
    if not day_slots:
        return None, None
    return day_slots[0][0], max(hi for _lo, hi in day_slots)


def _merge(intervals):
    """Merge overlapping `(lo, hi)` pairs. Input must already be sorted."""
    merged = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def hours_between(start, moment):
    """Minutes from float-hour `start` to `datetime` `moment`, as an int.

    Positive means after. Used for both lateness and an early exit, with the sign read by
    the caller, so that one rounding rule covers both rather than two that could disagree.
    """
    actual = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
    return int(round((actual - start) * 60))
