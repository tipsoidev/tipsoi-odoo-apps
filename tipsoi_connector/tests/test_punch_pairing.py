# -*- coding: utf-8 -*-
"""The Device Portal punch feed, and the pairing that turns it into attendance.

Three rules here were read out of the upstream schema and each fails quietly when it is
wrong, which is why they are pinned:

* on a punch row `type` is the *method* (`card` / `fingerprint` / `unrecognized`) and
  `log_type` is the *direction* (`entry` / `exit` / `both` / `access` / `other`). Reading
  them the other way round yields punches whose direction is a payment method;
* the window parameters are parsed in the application timezone, so a request built in UTC
  asks for the wrong window;
* `uid` is unique in the source schema, so it is the idempotency key.

And two rules come from `hr.attendance` itself, measured on 17 and 18: overlapping
intervals are refused, and only one attendance per employee may be left open. Both shape
the pairing design -- an odd punch count leaves staging unpaired rather than writing an
open record, because one open record blocks every later one for that person.
"""

from datetime import datetime, timedelta

from odoo.tests import tagged

from .common import Pages, TipsoiCase


def punch_row(uid, logged_time, log_type="entry", identifier="E-001",
              method="fingerprint", device="TPS-0001", **overrides):
    """A row as the punch feed sends it: naive local strings, `type`, `log_type`."""
    row = {
        "uid": uid,
        "sync_time": logged_time,
        "logged_time": logged_time,
        "type": method,
        "log_type": log_type,
        "device_identifier": device,
        "location": "Gate",
        "person_id": 501,
        "person_identifier": identifier,
        "rfid": "",
        "primary_display_text": "Rahim",
        "secondary_display_text": "Ops",
    }
    row.update(overrides)
    return row


def page(rows, current=1, last=1):
    return {"data": rows, "meta": {"current_page": current, "last_page": last,
                                   "per_page": 500}}


@tagged("post_install", "-at_install")
class TestPunchPoll(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.employee = self._employee("E-001", backend=self.backend, name="Rahim")

    def _poll(self, window_from=None, window_to=None):
        window_from = window_from or datetime(2026, 8, 1, 0, 0)
        window_to = window_to or datetime(2026, 8, 2, 0, 0)
        with self._run(self.backend, "punches") as run:
            self.env["tipsoi.punch.log"]._poll(
                self.backend, run, window_from, window_to)
        return run

    def _punches(self):
        return self.env["tipsoi.punch.log"].search(
            [("backend_id", "=", self.backend.id)])

    # -- the request ---------------------------------------------------------------------

    def test_the_poll_cursors_on_sync_time_not_punch_time(self):
        """`sync_time` is what catches a device that was offline for days.

        A punch-time cursor would step straight over backdated punches when the device
        finally reconnected.
        """
        self.transport.route("GET", "logs", page([]))
        self._poll()
        params = self.transport.calls[0]["params"]
        self.assertEqual(params["criteria"], "sync_time")
        self.assertEqual(params["order_key"], "sync_time")

    def test_the_window_is_sent_in_local_wall_time_not_utc(self):
        """`start`/`end` are parsed in the API server's own application timezone."""
        self.transport.route("GET", "logs", page([]))
        self._poll(datetime(2026, 8, 1, 0, 0), datetime(2026, 8, 1, 12, 0))
        params = self.transport.calls[0]["params"]
        self.assertEqual(params["start"], "2026-08-01 06:00:00")
        self.assertEqual(params["end"], "2026-08-01 18:00:00")

    # -- upsert on uid ---------------------------------------------------------------------

    def test_punches_land_with_their_raw_strings_kept(self):
        self.transport.route("GET", "logs", page(
            [punch_row("u1", "2026-08-01 09:00:00")]))
        run = self._poll()

        punch = self._punches()
        self.assertEqual(len(punch), 1)
        self.assertEqual(run.created, 1)
        self.assertEqual(punch.tipsoi_log_id, "u1")
        self.assertEqual(punch.punch_time_utc, datetime(2026, 8, 1, 3, 0))
        # Kept verbatim: a few devices return this field already converted to GMT and the
        # payload gives no way to tell, so the original string is the only record.
        self.assertEqual(punch.punch_time_raw, "2026-08-01 09:00:00")
        self.assertEqual(punch.sync_time_raw, "2026-08-01 09:00:00")
        self.assertEqual(punch.employee_id, self.employee)
        self.assertEqual(punch.state, "matched")

    def test_a_uid_repeated_across_pages_is_upserted_once(self):
        """Filter column and order column differ upstream, so rows shift between pages."""
        self.transport.route("GET", "logs", Pages([
            page([punch_row("u1", "2026-08-01 09:00:00"),
                  punch_row("u2", "2026-08-01 09:05:00")], current=1, last=2),
            page([punch_row("u2", "2026-08-01 09:05:00"),
                  punch_row("u3", "2026-08-01 18:00:00")], current=2, last=2),
        ]))
        run = self._poll()
        self.assertEqual(len(self._punches()), 3)
        self.assertEqual(run.fetched, 4)
        self.assertEqual(run.created, 3)
        self.assertEqual(run.updated, 1)

    def test_repolling_the_same_window_creates_nothing(self):
        self.transport.route("GET", "logs", page(
            [punch_row("u1", "2026-08-01 09:00:00")]))
        self._poll()
        run = self._poll()
        self.assertEqual(len(self._punches()), 1)
        self.assertEqual(run.created, 0)
        self.assertEqual(run.updated, 1)

    def test_a_punch_whose_time_changed_upstream_is_updated_not_duplicated(self):
        """Rows genuinely can be updated after insert, so this upserts rather than
        ignoring conflicts."""
        self.transport.route("GET", "logs", page(
            [punch_row("u1", "2026-08-01 09:00:00")]))
        self._poll()
        self.transport.route("GET", "logs", page(
            [punch_row("u1", "2026-08-01 09:30:00")]))
        self._poll()

        punch = self._punches()
        self.assertEqual(len(punch), 1)
        self.assertEqual(punch.punch_time_utc, datetime(2026, 8, 1, 3, 30))

    def test_a_row_without_a_uid_is_counted_and_skipped(self):
        """With no uid there is no idempotency key, so importing it would duplicate on
        every later poll."""
        self.transport.route("GET", "logs", page(
            [punch_row("u1", "2026-08-01 09:00:00"),
             punch_row(None, "2026-08-01 09:01:00")]))
        run = self._poll()
        self.assertEqual(len(self._punches()), 1)
        self.assertEqual(run.skipped, 1)

    def test_a_punch_for_nobody_is_surfaced_not_dropped(self):
        self.transport.route("GET", "logs", page(
            [punch_row("u9", "2026-08-01 09:00:00", identifier="GHOST-7")]))
        self._poll()
        punch = self._punches()
        self.assertEqual(punch.state, "unmatched")
        self.assertIn("GHOST-7", punch.state_reason)
        self.assertFalse(punch.employee_id)

    def test_a_known_device_is_linked_to_the_punch(self):
        device = self._device(self.backend, "TPS-0001", device_type="entry")
        self.transport.route("GET", "logs", page(
            [punch_row("u1", "2026-08-01 09:00:00")]))
        self._poll()
        self.assertEqual(self._punches().device_id, device)


@tagged("post_install", "-at_install")
class TestPunchColumnMeanings(TipsoiCase):
    """`type` is the method and `log_type` is the direction, not the other way round."""

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.Punch = self.env["tipsoi.punch.log"]

    def test_log_type_gives_the_direction(self):
        self.assertEqual(self.Punch._direction("entry"), "in")
        self.assertEqual(self.Punch._direction("exit"), "out")

    def test_an_ambiguous_log_type_falls_back_to_how_the_reader_is_wired(self):
        entry_reader = self._device(self.backend, "IN-1", device_type="entry")
        exit_reader = self._device(self.backend, "OUT-1", device_type="exit")
        for log_type in ("both", "access", "other", None, ""):
            self.assertEqual(self.Punch._direction(log_type, entry_reader), "in",
                             log_type)
            self.assertEqual(self.Punch._direction(log_type, exit_reader), "out",
                             log_type)

    def test_with_no_log_type_and_no_device_the_direction_is_unknown(self):
        self.assertEqual(self.Punch._direction(None), "unknown")
        self.assertEqual(self.Punch._direction("both"), "unknown")

    def test_a_both_reader_gives_no_direction_either(self):
        both = self._device(self.backend, "BOTH-1", device_type="both")
        self.assertEqual(self.Punch._direction("both", both), "unknown")

    def test_type_is_read_as_the_method(self):
        self.assertEqual(self.Punch._method("card"), "card")
        self.assertEqual(self.Punch._method("fingerprint"), "fingerprint")
        self.assertEqual(self.Punch._method("unrecognized"), "unrecognized")

    def test_a_direction_word_is_not_accepted_as_a_method(self):
        self.assertEqual(self.Punch._method("entry"), "other")

    def test_a_card_punch_does_not_become_a_direction(self):
        """The headline confusion: `type: "card"` says nothing about in or out."""
        vals = self.Punch._row_to_vals(
            self.backend,
            punch_row("u1", "2026-08-01 09:00:00", log_type=None, method="card"),
            {})
        self.assertEqual(vals["punch_method"], "card")
        self.assertEqual(vals["direction"], "unknown")


@tagged("post_install", "-at_install")
class TestPunchPairing(TipsoiCase):

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.employee = self._employee("E-001", backend=self.backend, name="Rahim")
        self.Punch = self.env["tipsoi.punch.log"]

    def _punch(self, uid, moment, direction="unknown", employee=None, state="matched"):
        return self.Punch.create({
            "backend_id": self.backend.id,
            "tipsoi_log_id": uid,
            "person_identifier": "E-001",
            "employee_id": (employee or self.employee).id,
            "punch_time_utc": moment,
            "direction": direction,
            "state": state,
        })

    def _pair(self):
        with self._run(self.backend, "pairing") as run:
            self.Punch._pair(self.backend, run)
        return run

    def _attendances(self):
        return self.env["hr.attendance"].search(
            [("employee_id", "=", self.employee.id)], order="check_in")

    # -- the ordinary case ----------------------------------------------------------------

    def test_an_in_and_an_out_become_one_attendance(self):
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()

        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_in, datetime(2026, 8, 1, 3, 0))
        self.assertEqual(attendance.check_out, datetime(2026, 8, 1, 11, 0))
        self.assertEqual(
            set(self.Punch.search([("backend_id", "=", self.backend.id)])
                .mapped("state")), {"paired"})

    def test_two_separate_spans_in_one_day_both_pair(self):
        """A lunch break is two spans, which is exactly what the raw feed can express."""
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 7, 0), "out")
        self._punch("u3", datetime(2026, 8, 1, 8, 0), "in")
        self._punch("u4", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        self.assertEqual(len(self._attendances()), 2)

    def test_directions_are_inferred_by_alternating_when_tipsoi_does_not_say(self):
        self._punch("u1", datetime(2026, 8, 1, 3, 0))
        self._punch("u2", datetime(2026, 8, 1, 11, 0))
        self._pair()
        self.assertEqual(len(self._attendances()), 1)

    def test_an_inferred_direction_is_not_written_back_onto_the_record(self):
        """The staging row stays faithful to what Tipsoi actually sent."""
        first = self._punch("u1", datetime(2026, 8, 1, 3, 0))
        self._punch("u2", datetime(2026, 8, 1, 11, 0))
        self._pair()
        first.invalidate_recordset()
        self.assertEqual(first.direction, "unknown")
        self.assertEqual(first.state, "paired")

    # -- overnight ------------------------------------------------------------------------

    def test_an_overnight_shift_is_one_span_not_two_days(self):
        """Bucketing by calendar day is what breaks a night shift, so pairing does not.

        22:00 Dhaka is 16:00 UTC and 06:00 the next morning is 00:00 UTC, so this pair
        also straddles midnight in UTC -- the case a day bucket splits in half.
        """
        self._punch("u1", datetime(2026, 8, 1, 16, 0), "in")
        self._punch("u2", datetime(2026, 8, 2, 0, 0), "out")
        self._pair()

        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_in, datetime(2026, 8, 1, 16, 0))
        self.assertEqual(attendance.check_out, datetime(2026, 8, 2, 0, 0))

    def test_a_punch_beyond_the_longest_shift_does_not_close_it(self):
        self.backend.max_shift_hours = 16
        entry = self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        late = self._punch("u2", datetime(2026, 8, 1, 23, 0), "out")
        self._pair()

        self.assertEqual(len(self._attendances()), 0)
        entry.invalidate_recordset()
        late.invalidate_recordset()
        self.assertEqual(entry.state, "unpaired")
        self.assertEqual(late.state, "unpaired")

    # -- duplicates -----------------------------------------------------------------------

    def test_a_second_read_within_seconds_is_the_same_punch(self):
        """A repeated finger press is not an exit; treating it as one halves the day."""
        self.backend.pair_duplicate_seconds = 60
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        repeat = self._punch("u2", datetime(2026, 8, 1, 3, 0, 30), "in")
        self._punch("u3", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()

        repeat.invalidate_recordset()
        self.assertEqual(repeat.state, "duplicate")
        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance.check_out, datetime(2026, 8, 1, 11, 0))

    def test_collapsing_can_be_switched_off(self):
        self.backend.pair_duplicate_seconds = 0
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 3, 0, 30), "out")
        self._pair()
        self.assertEqual(len(self._attendances()), 1)

    # -- incomplete days -------------------------------------------------------------------

    def test_an_odd_punch_count_leaves_the_last_one_unpaired(self):
        """And crucially does *not* write an attendance with no check-out.

        Odoo allows one open attendance per employee, so creating one here would block
        every later record for that person until somebody closed it by hand.
        """
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        trailing = self._punch("u3", datetime(2026, 8, 1, 12, 0), "in")
        self._pair()

        attendance = self._attendances()
        self.assertEqual(len(attendance), 1)
        self.assertFalse(attendance.filtered(lambda a: not a.check_out),
                         "no open attendance may be created")
        trailing.invalidate_recordset()
        self.assertEqual(trailing.state, "unpaired")
        self.assertTrue(trailing.state_reason)

    def test_two_entry_punches_in_a_row_close_the_first_as_unpaired(self):
        first = self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        second = self._punch("u2", datetime(2026, 8, 1, 4, 0), "in")
        self._pair()

        self.assertEqual(len(self._attendances()), 0)
        first.invalidate_recordset()
        second.invalidate_recordset()
        self.assertEqual(first.state, "unpaired")
        self.assertIn("entry", (first.state_reason or "").lower())
        self.assertEqual(second.state, "unpaired")

    def test_an_exit_with_no_entry_before_it_is_unpaired(self):
        orphan = self._punch("u1", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        self.assertEqual(len(self._attendances()), 0)
        orphan.invalidate_recordset()
        self.assertEqual(orphan.state, "unpaired")

    def test_a_late_exit_pairs_a_previously_unpaired_entry(self):
        """The reason unpaired rows stay in the window rather than being written off."""
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._pair()
        self.assertEqual(len(self._attendances()), 0)

        self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        self.assertEqual(len(self._attendances()), 1)

    # -- idempotency ------------------------------------------------------------------------

    def test_pairing_three_times_neither_duplicates_nor_churns(self):
        """The headline guarantee: a five-minute cron must not rewrite settled records."""
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        first_ids = self._attendances().ids
        self.assertEqual(len(first_ids), 1)

        for _round in range(2):
            self._pair()
            self.assertEqual(self._attendances().ids, first_ids)
        self.assertFalse(self.Punch.search_count([
            ("backend_id", "=", self.backend.id), ("state", "=", "error")]))

    def test_recomputing_an_unchanged_day_reuses_the_same_attendance(self):
        """Exercises the "did anything change?" comparison rather than the quiet skip.

        Forcing the rows back to `matched` is what a re-read with a changed timestamp
        does, so this is the path that must not unlink and recreate for nothing.
        """
        first = self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        second = self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        original = self._attendances()
        self.assertEqual(len(original), 1)

        (first | second).write({"state": "matched"})
        self._pair()
        self.assertEqual(self._attendances().ids, original.ids)

    def test_a_corrected_punch_time_moves_the_existing_attendance(self):
        first = self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        second = self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        self.assertEqual(len(self._attendances()), 1)

        second.write({"punch_time_utc": datetime(2026, 8, 1, 12, 0),
                      "state": "matched"})
        first.write({"state": "matched"})
        self._pair()

        attendance = self._attendances()
        self.assertEqual(len(attendance), 1, "the day must not gain a second record")
        self.assertEqual(attendance.check_out, datetime(2026, 8, 1, 12, 0))

    def test_a_quiet_office_does_no_work_at_all(self):
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._pair()
        run = self._pair()
        self.assertEqual(run.fetched, 0)
        self.assertEqual(run.created, 0)

    # -- contested days ---------------------------------------------------------------------

    def test_an_overlap_with_a_hand_typed_attendance_is_recorded_not_fatal(self):
        """Odoo refuses overlapping intervals, and one contested day must not stop the run."""
        self.env["hr.attendance"].create({
            "employee_id": self.employee.id,
            "check_in": datetime(2026, 8, 1, 4, 0),
            "check_out": datetime(2026, 8, 1, 12, 0),
        })
        first = self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        second = self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")

        run = self._pair()

        self.assertEqual(run.state, "partial")
        self.assertEqual(run.failed, 1)
        first.invalidate_recordset()
        second.invalidate_recordset()
        self.assertEqual(first.state, "error")
        self.assertTrue(first.state_reason)
        self.assertEqual(second.state, "error")

    def test_one_contested_employee_does_not_stop_another(self):
        other = self._employee("E-002", backend=self.backend, name="Karim")
        self.env["hr.attendance"].create({
            "employee_id": self.employee.id,
            "check_in": datetime(2026, 8, 1, 4, 0),
            "check_out": datetime(2026, 8, 1, 12, 0),
        })
        self._punch("u1", datetime(2026, 8, 1, 3, 0), "in")
        self._punch("u2", datetime(2026, 8, 1, 11, 0), "out")
        self._punch("u3", datetime(2026, 8, 1, 3, 0), "in", employee=other)
        self._punch("u4", datetime(2026, 8, 1, 11, 0), "out", employee=other)

        self._pair()
        self.assertEqual(
            self.env["hr.attendance"].search_count([("employee_id", "=", other.id)]), 1)

    def test_an_unmatched_punch_is_never_paired(self):
        ghost = self.Punch.create({
            "backend_id": self.backend.id,
            "tipsoi_log_id": "g1",
            "person_identifier": "GHOST",
            "punch_time_utc": datetime(2026, 8, 1, 3, 0),
            "state": "unmatched",
        })
        self._pair()
        ghost.invalidate_recordset()
        self.assertEqual(ghost.state, "unmatched")
        self.assertFalse(ghost.attendance_id)


@tagged("post_install", "-at_install")
class TestPollAndPairTogether(TipsoiCase):
    """The whole Device Portal pipeline, run repeatedly the way a cron would."""

    def setUp(self):
        super().setUp()
        self.backend = self._backend("device_portal")
        self.employee = self._employee("E-001", backend=self.backend, name="Rahim")
        self._device(self.backend, "TPS-0001", device_type="entry")
        self.transport.route("GET", "logs", page([
            punch_row("u1", "2026-08-01 09:00:00", log_type="entry"),
            punch_row("u2", "2026-08-01 18:00:00", log_type="exit"),
            punch_row("u3", "2026-08-02 09:00:00", log_type="entry"),
            punch_row("u4", "2026-08-02 18:00:00", log_type="exit"),
        ]))

    def _cycle(self):
        self.backend.action_poll_punches()
        self.backend.action_pair_punches()

    def test_the_same_window_imported_three_times_yields_the_same_records(self):
        self._cycle()
        attendances = self.env["hr.attendance"].search(
            [("employee_id", "=", self.employee.id)], order="check_in")
        self.assertEqual(len(attendances), 2)
        first_ids = attendances.ids

        for _round in range(2):
            self._cycle()
            again = self.env["hr.attendance"].search(
                [("employee_id", "=", self.employee.id)], order="check_in")
            self.assertEqual(again.ids, first_ids,
                             "re-running a window must not churn attendance")

        punches = self.env["tipsoi.punch.log"].search(
            [("backend_id", "=", self.backend.id)])
        self.assertEqual(len(punches), 4)
        self.assertEqual(set(punches.mapped("state")), {"paired"})
        self.assertFalse(punches.filtered(lambda p: p.state == "error"))

    def test_the_cursor_advances_to_the_window_that_was_requested(self):
        """Never to a value read back from the rows.

        The rows report `created_at` under the name `sync_time` while the filter runs on
        `updated_at`, so a cursor taken from a returned value can walk backwards and
        re-scan forever.
        """
        before = self.backend.last_log_sync_time
        self.backend.action_poll_punches()
        self.assertTrue(self.backend.last_log_sync_time)
        self.assertNotEqual(self.backend.last_log_sync_time, before)
        run = self.env["tipsoi.sync.run"].search(
            [("backend_id", "=", self.backend.id), ("job", "=", "punches")], limit=1)
        self.assertEqual(self.backend.last_log_sync_time, run.window_to)

    def test_the_overlap_window_reaches_back_before_the_last_cursor(self):
        self.backend.write({
            "last_log_sync_time": datetime(2026, 8, 5, 12, 0),
            "poll_overlap_minutes": 5,
        })
        window_from, _window_to = self.backend._punch_window()
        self.assertEqual(window_from, datetime(2026, 8, 5, 11, 55))

    def test_the_first_run_backfills_rather_than_reading_nothing(self):
        self.backend.write({"last_log_sync_time": False, "punch_backfill_days": 7})
        window_from, window_to = self.backend._punch_window()
        self.assertAlmostEqual(
            (window_to - window_from).total_seconds(),
            timedelta(days=7).total_seconds(), delta=60)
