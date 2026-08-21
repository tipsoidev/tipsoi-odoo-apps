# Odoo 15 ↔ 16

One branch per version, per PLAN.md §8.1. `15.0` is a backport of **`16.0`** — branch from
there, never from 17.0 or later. The 57 view conditions have to already be in their `attrs`
form, and that is 16's work (see `MIGRATION-16-17.md`); starting from 17 means doing it twice.

Given that, this is the smallest of the ports. Odoo 16 renamed three ORM methods and 15 has
only the older names. Every one of them is an `AttributeError` at **runtime**, not at install,
which is the thing worth knowing: the module installs perfectly and then falls over the first
time a sync runs.

## Status — validated on 15.0

Verified 2026-08-21 by installing on the official `odoo:15` image against PostgreSQL 16:

| | Odoo 15.0 |
|---|---|
| Install | ✅ `installed`, exit 0 |
| Module tests | ✅ **254 tests, 0 failed, 0 errors** |
| Views render (`load_views`) | ✅ 11 actions, 20 views, 0 failures |
| Menus / crons registered | ✅ 9 crons, all repeating |
| Unique constraints in Postgres | ✅ all 5 present |
| Domain equivalence | ✅ **172 combinations, 30 conditions, 0 mismatches** |

Python on this image is 3.9.2, and the module carries no 3.10+ syntax.

## The delta — 2 changes

### 1. Three ORM methods Odoo 16 renamed

| 16+ | 15 | Where |
|---|---|---|
| `records.flush_recordset()` | `records.flush()` | 3 sites in `models/` |
| `records.invalidate_recordset()` | `records.invalidate_cache()` | 19 sites, all in `tests/` |
| `env.invalidate_all(flush=False)` | `env.cache.invalidate()` | 6 sites in `models/` |

Measured cost of getting the first one wrong: **118 of the 254 tests error**, all with
`AttributeError: 'tipsoi.sync.run' object has no attribute 'flush_recordset'`, while the
install itself reports success.

Two notes on fidelity, because a rename that changes behaviour is not a rename:

- `flush()` and `invalidate_cache()` on 15 act on the **model**, not just the recordset they
  are called on. That is broader than the 16 methods. It is safe at every call site here —
  flushing or dropping more cache than needed costs a query and does not change an outcome —
  but it is why this substitution is backport-only and must never be carried forward.
- `env.invalidate_all(flush=False)`'s body on 16 is exactly `self.cache.invalidate()`, so
  `env.cache.invalidate()` is the same operation rather than an approximation. That matters:
  every call site is a post-rollback cache drop, where flushing would push back the very
  values that were just rolled back.

### 2. Manifest version

`16.0.1.0.0` ⇄ `15.0.1.0.0`.

## What did *not* need changing

- **The whole view layer.** Inherited from `16.0` unchanged: `attrs` domains, `<tree>`,
  `view_mode`, the bare search `<group>`. The domain equivalence check was re-run on 15 and
  reports the same 172 combinations with no mismatches, so the translations hold on both.
- **`name_get`.** Correct for 15 as well as 16; 17 is where it was removed.
- **`_sql_constraints`**, `ir.cron`'s `numbercall`, `res.groups.category_id`,
  `base.default_user`, `res.users.groups_id` — all unchanged on 15.
- **`fields.Image`, `datetime.fromtimestamp(..., tz=utc)`, `_inherit`, record rules,
  `ir.model.access.csv`, field-level `groups=`, `api.constrains`** — all fine on 3.9.2.

## A harness trap, not a module bug

`get_views` arrived in **16**; on 15 the method is `load_views`. A render check written
against 16+ therefore reports *every* view in the module as broken —
`AttributeError: 'tipsoi.backend' object has no attribute 'get_views'`, 31 of them — which
looks exactly like a catastrophically broken port and is nothing of the kind.
`check-view-conditions.py` and the render pass both resolve the method by asking the model,
so they work unchanged on every series.

## Support status — read before promising this to anyone

Odoo maintains the **three most recent series**. With 19 released, that is 19, 18 and 17, so
**15.0 and 16.0 receive no further fixes from Odoo itself**, security fixes included. The
official `odoo:15` image was last rebuilt 2024-09-27.

This branch exists so an installation that has not upgraded can still connect Tipsoi. It is
not a reason to stay on 15.

## Verify on 15

```sh
odoo -d v15 -i tipsoi_connector --test-enable --test-tags=/tipsoi_connector --stop-after-init
```

Then render every view (via `load_views` — see above), list the constraints, and re-run
`check-view-conditions.py`. And once, by hand: no automated check here looks at a rendered
page.

## Deltas found, with the exact symptom

| Found | Version | Symptom | Fix |
|---|---|---|---|
| 2026-08-21 | 15 only | `AttributeError: no attribute 'flush_recordset'` — 118 tests, **install still succeeds** | `flush()` |
| 2026-08-21 | 15 only | `AttributeError: 'Environment' object has no attribute 'invalidate_all'` | `env.cache.invalidate()` |
| 2026-08-21 | 15 only | `AttributeError: no attribute 'invalidate_recordset'` | `invalidate_cache()` |
| 2026-08-21 | harness | `AttributeError: no attribute 'get_views'` on every view | 15 has `load_views`; ask the model |
