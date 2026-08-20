# Odoo 17 ↔ 18

One branch per version, per PLAN.md §8.1. Development happens on **18.0**; `17.0` is a
backport branch. This file records the *measured* delta, so backports are mechanical.

## Status — validated on both versions

Verified 2026-08-21 by installing the module on real Odoo instances (official `odoo:18`
and `odoo:17` images against PostgreSQL 16):

| | Odoo 18.0 | Odoo 17.0 |
|---|---|---|
| Install | ✅ `installed`, exit 0 | ✅ `installed`, exit 0 |
| Module tests | ✅ **201 tests, 0 failed, 0 errors** | ✅ **201 tests, 0 failed, 0 errors** |
| Views render (`get_views`) | ✅ all 11 actions + 2 inherited | ✅ all 11 actions + 2 inherited |
| Menus / crons registered | ✅ 11 / 9 | ✅ 11 / 9 |

The 17.0 figures come from running `backport-to-17.sh` over a copy of the 18.0 tree and
installing the result, which also makes the recipe below a tested artefact rather than a
description.

The `models/` layer, the transport, the wizards, the security rules and the tests are
**byte-identical** across both branches — the same tests pass unmodified on each. Only
four things differ, and every one of them was measured rather than guessed.

## The complete delta — 4 changes, in the view files, the cron data and the manifest

### 1. Manifest version — hard failure if wrong

Odoo 17 refuses an `18.0.x` version string before it parses anything else:

```
ValueError: Invalid version '18.0.1.0.0'. Modules should have a version in format
`x.y`, `x.y.z`, `17.0.x.y` or `17.0.x.y.z`.
ValueError: Module tipsoi_connector: invalid manifest
```

`18.0.1.0.0` ⇄ `17.0.1.0.0`.

### 2. List view root tag — hard failure if wrong

Odoo 18 renamed `<tree>` to `<list>`, and **17 does not accept the new name.** Confirmed
by installing the 18-style views on 17 with only the version corrected:

```
odoo.tools.convert.ParseError: while parsing
  /mnt/extra-addons/tipsoi_connector/views/tipsoi_backend_views.xml:4
Error while validating view near:
  <list decoration-danger="state == 'error'" decoration-warning="state == 'partial'">
```

So the branch split is genuinely required — a single arch cannot serve both.

`<list>` elements now exist across **every** view file, which is why the recipe below
globs the directory rather than naming files. An earlier version of this document named
two files explicitly; that recipe silently stopped converting everything the moment a
third view file was added, and the result does not fail at cherry-pick — it fails at
install, later, on a customer's 17.

### 3. `ir.cron` lost two fields in 18 — and 17's default is wrong without them

Odoo 18 removed `numbercall` and `doall` from `ir.cron`. Naming a field that does not
exist is a hard failure, not a warning:

```
ValueError: Invalid field 'numbercall' on model 'ir.cron'
ParseError: while parsing .../data/ir_cron.xml:22, somewhere inside
```

So the 18.0 branch omits both. But **17 still has them, and there `numbercall` defaults
to 1** — which runs each job exactly once and then switches it off. A 17 install that
simply inherited the 18 file would look fine, sync once, and go quiet. So the backport has
to *add* `numbercall` back, which is the one step in this recipe that inserts rather than
substitutes.

(Also measured: `interval_number` is `required=True` in 18 and not in 17. Every cron here
sets it, so nothing to do — recorded so it is not rediscovered.)

### 4. Action `view_mode`

`list,form` on 18 ⇄ `tree,form` on 17, in both view files.

*(Aside, measured: on 17 `get_views` answers to **both** `list` and `tree` as view
*types*, so 17 aliases the type name at lookup time. It does **not** accept `<list>` as
an arch tag, which is what actually matters. Do not let the alias tempt you into thinking
one branch could serve both.)*

## The backport recipe

From a 17.0 branch synced to 18.0:

```sh
# Glob, never a file list: a new view file must not be able to slip through.
sed -i 's|<list |<tree |g; s|</list>|</tree>|g; s|<list>|<tree>|g' \
  tipsoi_connector/views/*.xml

sed -i 's|>list,form<|>tree,form<|g; s|>kanban,list,form<|>kanban,tree,form<|g' \
  tipsoi_connector/views/*.xml

sed -i 's|"version": "18.0|"version": "17.0|' tipsoi_connector/__manifest__.py

# The one insertion: 17 needs numbercall back, or every job runs once and stops.
sed -i '/<field name="interval_type">/a\      <field name="numbercall">-1</field>' \
  tipsoi_connector/data/ir_cron.xml

# Then prove it: nothing 18-shaped may remain, and every cron repeats.
! grep -rn '<list\|list,form\|"version": "18' tipsoi_connector/views tipsoi_connector/__manifest__.py
# Count the field, not the word: the file's own comment mentions numbercall, and a bare
# grep for it reports a match that is only the explanation.
test "$(grep -c '<field name="numbercall"' tipsoi_connector/data/ir_cron.xml)" \
   = "$(grep -c 'interval_type' tipsoi_connector/data/ir_cron.xml)"
```

That is the whole backport. Nothing in `models/`, `security/` or `tests/` changes — if a
future change makes them diverge, fix the change rather than forking the file.

## What did *not* need changing

- **Conditional visibility.** The views use `invisible="backend_type != 'hrm'"` with plain
  Python expressions rather than the removed `attrs={'invisible': [...]}` domain syntax.
  That form works on 17 and 18 alike (17 is where `attrs` was dropped). Confirmed by the
  successful 17 install.
- **`_sql_constraints`**, field-level `groups=`, record rules, `api.constrains` — all
  identical and all verified working on both.
- **`fields.Datetime` / JWT parsing** — `datetime.fromtimestamp(..., tz=utc)` rather than
  the deprecated `utcfromtimestamp`, so it is clean on 3.10 through 3.12.

## Verify on both

```sh
odoo -d t18 -i tipsoi_connector --test-enable --test-tags=/tipsoi_connector --stop-after-init
odoo -d t17 -i tipsoi_connector --test-enable --test-tags=/tipsoi_connector --stop-after-init
```

Then render everything, because installing proves the arch parses and not that a view
builds. Ask the instance rather than a static checker — a static field-name checker on
this project once produced nine false positives by validating an x2many subtree against
its parent model:

```python
for action in <the module's ir.actions.act_window>:
    env[action.res_model].get_views(
        [(False, m) for m in action.view_mode.split(",")] + [(False, "search")])
```

Use `--test-tags=/tipsoi_connector`. Plain `--test-enable` runs the whole dependency
chain's suites (base, hr, hr_attendance) and takes over ten minutes.

No phase after 1 is done until both are green.

## Known deprecation, deliberately left alone

Odoo 18 logs `'kanban-box' is deprecated, define a 'card' template instead` for the device
kanban. It renders, and `kanban-box` is the only spelling that works on **both** series —
`<t t-name="card">` produces an empty card on 17. Converting would buy a clean log on 18 at
the cost of a second divergent file and a template shape that cannot be verified from here.
Revisit when 17 support is dropped: the change is `kanban-box` → `card` plus removing the
wrapping `oe_kanban_global_click` div.

## Deltas found, with the exact symptom

| Found | Version | Symptom | Fix |
|---|---|---|---|
| 2026-08-21 | 17 only | `ValueError: Invalid version '18.0.1.0.0'` | manifest version → `17.0.x` |
| 2026-08-21 | 17 only | `ParseError … near: <list …>` | `<list>` → `<tree>` in every view file |
| 2026-08-21 | 18 only | `ValueError: Invalid field 'numbercall' on model 'ir.cron'` | omit `numbercall`/`doall` on 18; add `numbercall=-1` back on 17 |
| 2026-08-21 | 18 only | `'kanban-box' is deprecated` (warning) | left as is, see above |
| 2026-08-21 | **both** | 97 tests fail with `Cannot commit or rollback a cursor from inside a test` | `registry.in_test_mode()` is False during 18's post-install tests, which patch `cr.commit` instead. Guard on `config["test_enable"]` as well — `tipsoi.sync.run.checkpoint()` |
