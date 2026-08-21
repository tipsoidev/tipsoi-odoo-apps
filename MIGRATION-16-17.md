# Odoo 16 ↔ 17

One branch per version, per PLAN.md §8.1. Development happens on **18.0**; `16.0` is a
backport of `17.0`, which is itself a backport of `18.0`. Branch from `17.0`, never from
`18.0` — the list-view tag and the `ir.cron` fields have to be in their 17 shape first.

This is the widest of the backports, but not the riskiest, and it is worth being precise
about why. Odoo 17 replaced the old `attrs` domain form with plain-Python view conditions,
so **57 conditions** across seven view files have to be translated back. Odoo 16 rejects
the new form outright, so a condition that is *forgotten* cannot reach a customer:

```
ValueError: Use 0/1/yes/no/true/false/on/off
ValueError: <class 'NameError'>: "name 'central_server_id' is not defined" while evaluating
ParseError: while parsing .../views/tipsoi_device_views.xml:103
```

What *can* reach a customer is a condition translated to a domain of the right shape and the
wrong meaning — that loads cleanly and quietly shows or hides the wrong thing. So the 30
distinct expressions live in an explicit table in `backport-to-16.sh`, and their equivalence
is machine-checked rather than reviewed by eye (see "Proving the domains" below).

## Status — validated on 16.0

Verified 2026-08-21 by installing on the official `odoo:16` image against PostgreSQL 16:

| | Odoo 16.0 |
|---|---|
| Install | ✅ `installed`, exit 0 |
| Module tests | ✅ **254 tests, 0 failed, 0 errors** |
| Views render (`get_views`) | ✅ 11 actions, 20 views, 0 failures |
| Menus / crons registered | ✅ 9 crons, all repeating |
| Unique constraints in Postgres | ✅ all 5 present |
| Domain equivalence | ✅ **172 value combinations, 30 conditions, 0 mismatches** |

## The delta — 3 changes

### 1. View conditions: plain Python → `attrs` domains (57 occurrences, 30 distinct)

`invisible="state != 'done'"` → `attrs="{'invisible': [('state', '!=', 'done')]}"`. Field
types decide the falsy spelling, so they were read off the models rather than guessed:
Boolean compares to `True`, Many2one to `False`, and `central_server_id` is an **Integer**,
whose empty value is `0` and not `False`.

**Char and Text compare against `[False, '']`, and that is measured, not stylistic.** Odoo 16
persists an empty string for a Text field rather than storing NULL:

```
create({'notes': ''}) → invalidate → notes == ''      (not False)
```

Python's `not notes` is True for that value; a bare `('notes', '=', False)` does not match
it, so the field would show where 17 hid it. `in [False, '']` agrees with the Python on both.
It also sidesteps a question that cannot be answered from a shell: `attrs` domains are
evaluated by the **web client**, not by `filtered_domain`, so which of the two coerces `''`
is not observable here. Writing the domain to cover both values makes the answer irrelevant.

Compound conditions become prefix-notation domains — `state != 'error' or not state_reason`
is `['|', ('state', '!=', 'error'), ('state_reason', 'in', [False, ''])]` — and `not in`
takes a list.

`decoration-danger="state == 'error'"` and friends are **not** touched: decoration attributes
have taken Python expressions since well before 16.

### 2. `name_get` instead of `_compute_display_name`

Odoo 17 removed `name_get` and added `_compute_display_name`. On 16 only the former exists,
so a `_compute_display_name` override is code that never runs — and it fails silently: every
sync run displays as `tipsoi.sync.run,4` instead of its job and time. `tipsoi_sync_run.py` is
the one model with a custom display name.

### 3. Manifest version

`17.0.1.0.0` ⇄ `16.0.1.0.0`. Unlike 19, Odoo 16 refuses an out-of-series version outright
rather than quietly marking the module uninstallable.

## Proving the domains

A translated domain that loads is not a translated domain that is *right*, and neither the
install nor the 254 tests look at view visibility. So the equivalence is checked directly:
for every one of the 30 expressions, enumerate the plausible values of each field it mentions
— every value of a Selection, both Booleans, `0` and non-zero for the Integer, and `False`,
`''` and a non-empty string for Char/Text — then evaluate the Python expression and the domain
on the same in-memory record and require them to agree.

**172 combinations, 0 mismatches.** Two things that check had to get right to be worth
anything:

- The value set includes the literals the expression itself names. Without that, a condition
  gets checked against a model that carries the field but not that value in its selection —
  `state != 'unmatched'` on `tipsoi.backend` — and only the not-equal branch is ever
  exercised, so the comparison passes trivially.
- String literals are stripped before field names are extracted. Without that, `'ready'` in
  `state != 'ready'` is read as a field name, no model carries it, and over half the table is
  silently skipped as unmodellable. The first run of the check did exactly that and reported
  16 of 30 conditions skipped.

## What did *not* need changing

- **`_sql_constraints`.** 16 supports it; only 19 dropped it. All five constraints confirmed
  present in `pg_constraint`.
- **`ir.cron`.** 16 has `numbercall` exactly like 17, so the 17 file is already correct —
  verified rather than assumed, since the default of 1 runs a job once and switches it off.
- **`<tree>` and `view_mode`.** Already in 17 shape. This is why the branch must come from
  `17.0` and not `18.0`.
- **`res.groups.category_id`, `base.default_user`, `res.users.groups_id`** — all still exist
  on 16; those are 19's changes.
- **`_inherit`, record rules, `ir.model.access.csv`, field-level `groups=`, `api.constrains`,
  the transport, the wizards, the tests** — identical, and the whole suite passes unmodified.

## A harness trap, not a module bug

Odoo 16's `hr` addon declares `def get_views(self, views, options)` with **no default** for
`options`; 17 gives it one. A render check that calls `get_views(specs)` therefore fails on
`hr.employee` alone, with `TypeError: get_views() missing 1 required positional argument`.
That is the checker's problem, not the module's — pass `{}` explicitly. Recorded because it
looks exactly like two broken inherited views.

## Verify on 16

```sh
odoo -d v16 -i tipsoi_connector --test-enable --test-tags=/tipsoi_connector --stop-after-init
```

Then render every view (passing `options` — see above), list the constraints, and re-run the
domain equivalence check. And once, by hand on a real 16, click through the backend form, the
punch log and the two wizards: no automated check here looks at the rendered page.

## Deltas found, with the exact symptom

| Found | Version | Symptom | Fix |
|---|---|---|---|
| 2026-08-21 | 16 only | `ValueError: Use 0/1/yes/no/true/false/on/off` | plain-Python view condition → `attrs` domain |
| 2026-08-21 | 16 only | `NameError: name 'central_server_id' is not defined while evaluating` | same, on a truthiness check |
| 2026-08-21 | 16 only | a sync run displays as `tipsoi.sync.run,4` — **silent** | `name_get`, not `_compute_display_name` |
| 2026-08-21 | 16 only | Text `''` matches `not x` but not `('x','=',False)` — **silent** | compare Char/Text against `[False, '']` |
| 2026-08-21 | harness | `TypeError: get_views() missing 1 required positional argument: 'options'` | 16's `hr` has no default; pass `{}` |
