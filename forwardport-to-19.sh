#!/bin/bash
# Mechanically convert the 18.0 working tree to 19.0 shape, per MIGRATION-18-19.md.
# Run from the repo root on the 19.0 branch after merging 18.0 into it.
#
# Unlike backport-to-17.sh this script cannot do the whole job. Four of the six 18->19
# deltas are structural (group privileges, the default-user group, the renamed users field,
# and _sql_constraints -> models.Constraint) and a sed over them would be a good way to
# produce a tree that installs and is quietly wrong. So this converts the two substitutions
# that are safe and *verifies* the other four, naming what is missing.
set -uo pipefail
cd "${1:-$PWD}"
M=tipsoi_connector
[ -f "$M/__manifest__.py" ] || { echo "run me from the repo root (or pass it)"; exit 2; }

# 1. Manifest version. On 19 a wrong series prefix does NOT raise -- it logs a warning,
#    sets installable=False and exits 0 -- so getting this wrong is invisible at install.
sed -i 's|"version": "18.0|"version": "19.0|' $M/__manifest__.py

# 2. Search views: 19 dropped `expand` and `string` from <group>. Glob, never a file list:
#    a new search view must not slip through.
sed -i 's|<group expand="0" string="Group By">|<group>|g' $M/views/*.xml

# --- prove it ---------------------------------------------------------------------------
fail=0
note() { echo "FAIL: $*"; fail=1; }

grep -q '"version": "19.0' $M/__manifest__.py || note "manifest version is not 19.0.x"
if grep -rn '<group expand\|string="Group By"' $M/views >/dev/null; then
  note "an 18-shaped search <group> survived"
fi

# 19 ignores _sql_constraints with only a warning, so its presence means every uniqueness
# guarantee in the module is silently gone: punch dedup, day-row idempotency, the unique
# Tipsoi identifier, and one-backend-per-company.
if grep -rn '_sql_constraints' $M/models >/dev/null; then
  note "_sql_constraints present -- 19 ignores it and drops the constraint. Convert each to
      _<name> = models.Constraint(definition, message)  (see MIGRATION-18-19.md §2)"
fi
want=5
have=$(grep -rc 'models.Constraint' $M/models/*.py | awk -F: '{s+=$2} END {print s+0}')
[ "$have" = "$want" ] || note "$have models.Constraint found, expected $want"

# The three security-data changes. Each one is a hard install failure if missing, but
# checking here turns a five-minute container round trip into an instant answer.
S=$M/security/tipsoi_security.xml
grep -q 'model="res.groups.privilege"' $S || note "no res.groups.privilege record ($S)"
grep -q 'name="privilege_id"' $S         || note "groups still use category_id, not privilege_id"
grep -q 'category_id" ref="module_category_tipsoi"' $S \
  || note "the privilege does not carry the module category"
grep -q 'id="base.default_user_group"' $S || note "still writing to base.default_user, which 19 removed"
grep -q 'name="groups_id"' $S && note "res.users.groups_id was renamed group_ids in 19"

if grep -rn '<tree \|>tree,form<' $M/views >/dev/null; then
  note "17-shaped arch found -- did this branch get merged from 17.0 rather than 18.0?"
fi

[ "$fail" = 0 ] && cat <<'MSG'
forward-port clean: version 19.0.x, search groups converted, 5 models.Constraint,
privilege + default_user_group wired.

Not provable from here, and not optional -- on 19 a clean exit does NOT mean the module
installed. After installing, confirm both:

  ir.module.module state == 'installed'      (a version mismatch exits 0 and installs nothing)
  5 unique constraints on the tipsoi_* / hr_employee tables in pg_constraint
                                            (_sql_constraints is dropped with a warning only)
MSG
exit $fail
