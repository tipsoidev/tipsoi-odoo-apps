# Odoo 18 → 19

One branch per version, per PLAN.md §8.1. Development happens on **18.0**; `19.0` is a
forward-port branch, the mirror image of `17.0`. This file records the *measured* delta.

Odoo 19 is a wider break than 17↔18. The list-view tag and the `ir.cron` fields — the two
things that force the 17/18 split — are unchanged here, but 19 restructured access groups,
removed the default-user template, and replaced `_sql_constraints`. Two of those changes
fail **silently**, which is why this document leads with them.

## Status — validated on 19.0

Verified 2026-08-21 by installing on the official `odoo:19` image against PostgreSQL 16,
with an `odoo:18` control run of the untouched `18.0` tree in the same harness:

| | Odoo 19.0 | Odoo 18.0 (control) |
|---|---|---|
| Install | ✅ `installed`, exit 0 | ✅ `installed`, exit 0 |
| Module tests | ✅ **254 tests, 0 failed, 0 errors** | ✅ **254 tests, 0 failed, 0 errors** |
| Views render (`get_views`) | ✅ 11 actions, 20 views, 0 failures | ✅ 11 actions, 20 views, 0 failures |
| Menus / crons registered | ✅ 13 / 9 | ✅ 13 / 9 |
| Unique constraints in Postgres | ✅ all 5 present | ✅ all 5 present |
| A new user gets the Tipsoi admin group | ✅ | ✅ |

`models/`, `wizards/` and the transport are otherwise byte-identical to `18.0`; the two
test files differ only by a helper that asks the model which field name to use. That claim
is measured, not assumed: the `18.0` tree with those two files taken from `19.0` and nothing
else changed installs and passes **254 tests, 0 failed, 0 errors** on `odoo:18`, so they can
be adopted there and on `17.0` unchanged whenever that is convenient.

## The two silent failures — read these first

### 1. A wrong version string does not raise on 19. It exits 0 and installs nothing.

Odoo 17 refuses an out-of-series version with a hard `ValueError` (see
`MIGRATION-17-18.md` §1). Odoo 19 does not. Installing the tree with `18.0.1.0.0` left in
the manifest produced:

```
WARNING odoo.modules.module: The module tipsoi_connector has an incompatible version,
        setting installable=False
exit=0
```

The module is marked uninstallable, `-i tipsoi_connector` finds nothing to install, and the
process **succeeds**. A CI check that only reads the exit code calls that a pass. So the
forward-port script asserts the installed version from the database rather than trusting
the run, and any pipeline for this branch must do the same.

`18.0.1.0.0` ⇄ `19.0.1.0.0`.

### 2. `_sql_constraints` is ignored on 19 with only a warning.

```
WARNING odoo.registry: Model attribute '_sql_constraints' is no longer supported,
        please define models.Constraint on the model.
```

Five of those, one per constrained model — and the install still succeeds. The constraints
are simply never created, which on this module means: duplicate punches import instead of
deduplicating, a day row can be staged twice for the same employee, two employees can share
a Tipsoi identifier, and **a company can hold two backends at once**, which is precisely
the mixing of the two APIs that `uniq_company_backend` exists to prevent.

All five moved to the 19 form — a class attribute whose name becomes the constraint name:

```python
_uniq_company_backend = models.Constraint(
    "unique(company_id)",
    "One Tipsoi backend per company: ...")
```

Nothing in the log distinguishes "constraint created" from "constraint quietly dropped", so
this is verified against `pg_constraint` rather than against the absence of a warning:

```
tipsoi_backend_uniq_company_backend             UNIQUE (company_id)
hr_employee_uniq_tipsoi_identifier              UNIQUE (company_id, tipsoi_identifier)
tipsoi_punch_log_uniq_backend_log               UNIQUE (backend_id, tipsoi_log_id)
tipsoi_day_attendance_uniq_backend_employee_day UNIQUE (backend_id, employee_identifier, day_date)
tipsoi_device_uniq_backend_identifier           UNIQUE (backend_id, identifier)
```

## The rest of the delta

### 3. Access groups now hang off a privilege — `res.groups.category_id` is gone

```
ValueError: Invalid field 'category_id' in 'res.groups'
ParseError: while parsing .../security/tipsoi_security.xml:10
```

Odoo 19 added `res.groups.privilege`, which carries the `ir.module.category` link; the
group points at the privilege via `privilege_id`. Pattern taken from 19's own
`base/security/base_groups.xml`. `full_name` renders as `"<privilege> / <group>"`, so the
group names lose their `Tipsoi / ` prefix or it would appear twice.

`implied_ids` on `res.groups` is unchanged and still works.

### 4. `base.default_user` no longer exists

```
Exception: Cannot update missing record 'base.default_user'
```

The template-user record is gone. `res.users._default_groups` now returns `base.group_user`
plus `base.default_user_group.implied_ids`, so granting a group to new users by default
means implying it from that group instead:

```xml
<record id="base.default_user_group" model="res.groups">
  <field name="implied_ids" eval="[(4, ref('group_tipsoi_admin'))]"/>
</record>
```

**Verified by creating a user, not by the install succeeding.** The suite cannot cover this:
both of its user helpers pass explicit group lists precisely to avoid the default, so the one
behaviour this change touches is the one behaviour no test exercises. It also needed checking
because `base.default_user_group` lives inside a `<data noupdate="1">` block in 19's own
`base_groups.xml`, which is exactly where a write from another module can silently not apply.
It does apply — asked of a live instance on both series:

```
odoo:19   base.default_user_group implies ['Administrator']   new user has the group: True
odoo:18   base.default_user template implies it               new user has the group: True
```

### 5. `res.users.groups_id` was renamed `group_ids`

`res_users.py` in 19 defines `group_ids` (plus a new computed `all_group_ids`); `res.groups`
correspondingly renamed `users` to `user_ids`. The XML above uses the new name directly.

The two test helpers that build a user ask the model rather than branching on a version:

```python
return "group_ids" if "group_ids" in self.env["res.users"]._fields else "groups_id"
```

One file then serves every series, which keeps the test tree portable in both directions.
Confirmed working on 19 by the suite passing, and on 18 by the control run.

### 6. Search views: `<group>` lost `expand` and `string`

```
WARNING odoo.tools.view_validation: RELAXNG_ERR_INVALIDATTR: Invalid attribute expand
        for element group
ParseError: Invalid view tipsoi.device.search in .../views/tipsoi_device_views.xml
```

`<group expand="0" string="Group By">` → `<group>`, in all four search views. 19's own
`hr_employee_views.xml` writes group-by blocks as a bare `<group>`. Unlike 1 and 2 this one
is a hard failure, so it cannot be missed.

## What did *not* need changing

- **`<list>` and action `view_mode`.** Unchanged from 18 — this is the whole 17↔18 delta and
  none of it applies here. The 18 tree's arch is already 19-shaped.
- **`ir.cron`.** 19 has no `numbercall`/`doall`, same as 18, so the 18 cron file is already
  correct. (The `17.0` backport still has to add `numbercall` back; see the other file.)
- **Dynamic visibility.** `invisible="state != 'done'"` and friends work unchanged on 19 —
  all 57 conditions across the seven view files, verified by the render pass.
- **`kanban-box`.** Still renders on 19; still deprecated. Same standing decision as 18.
- **`_inherit`, record rules, `ir.model.access.csv`, field-level `groups=`, `api.constrains`,
  `_compute_display_name`** — all identical and all verified working.

## Known, pre-existing, not introduced here

The manifest's RST `description` renders with a docutils error:

```
<string>:38: (ERROR/3) Unexpected indentation.
<string>:43: (WARNING/2) Block quote ends without a blank line; unexpected unindent.
```

The control run reproduces this on **18.0** at the same lines, so it is not a 19 delta. It
affects the rendered text on the Apps listing for every series, and is left for a change
that lands on `18.0` first and is ported to both other branches together.

## The forward-port recipe

From a `19.0` branch synced to `18.0`, `./forwardport-to-19.sh`. Two of the six changes are
mechanical substitutions; the other four are not, which is why the script converts what it
safely can and then *verifies* the rest, failing loudly with the reason rather than pretending
to have done it.

## Verify on 19

```sh
odoo -d v19 -i tipsoi_connector --test-enable --test-tags=/tipsoi_connector --stop-after-init
```

Then, because on this series a clean exit does not mean the module installed:

```python
env["ir.module.module"].search([("name", "=", "tipsoi_connector")]).state    # 'installed'
```

…and render every view, and list the real constraints:

```sql
select conname, pg_get_constraintdef(oid) from pg_constraint
where contype = 'u' and conrelid::regclass::text like 'tipsoi%';
```

Use `--test-tags=/tipsoi_connector`. Plain `--test-enable` runs the whole dependency chain.

## Deltas found, with the exact symptom

| Found | Version | Symptom | Fix |
|---|---|---|---|
| 2026-08-21 | 19 only | `incompatible version, setting installable=False` — **warning, exit 0, nothing installed** | manifest version → `19.0.x` |
| 2026-08-21 | 19 only | `'_sql_constraints' is no longer supported` — **warning only; all 5 constraints silently dropped** | `models.Constraint` attributes |
| 2026-08-21 | 19 only | `ValueError: Invalid field 'category_id' in 'res.groups'` | new `res.groups.privilege` record + `privilege_id` |
| 2026-08-21 | 19 only | `Exception: Cannot update missing record 'base.default_user'` | imply the group from `base.default_user_group` |
| 2026-08-21 | 19 only | `res.users.groups_id` renamed | `group_ids`; tests ask the model which name to use |
| 2026-08-21 | 19 only | `RELAXNG_ERR_INVALIDATTR: Invalid attribute expand for element group` | `<group expand="0" string="Group By">` → `<group>` |
| 2026-08-21 | **both** | manifest RST `Unexpected indentation` | pre-existing on 18, not fixed here |
