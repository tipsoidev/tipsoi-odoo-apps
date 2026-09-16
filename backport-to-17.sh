#!/bin/bash
# Mechanically convert the 18.0 working tree to 17.0 shape, per
# MIGRATION-17-18.md in this repo. Run from the repo root on the 17.0 branch
# after merging 18.0 into it.
set -euo pipefail
cd "${1:-$PWD}"                 # repo root; defaults to where you invoked it
M=tipsoi_connector
[ -f "$M/__manifest__.py" ] || { echo "run me from the repo root (or pass it)"; exit 2; }

# 1. List view root tag. Glob, never a file list: a new view file must not slip through.
sed -i 's|<list |<tree |g; s|</list>|</tree>|g; s|<list>|<tree>|g' $M/views/*.xml

# 2. Action view_mode.
sed -i 's|>list,form<|>tree,form<|g; s|>kanban,list,form<|>kanban,tree,form<|g' $M/views/*.xml

# 3. Manifest version.
sed -i 's|"version": "18.0|"version": "17.0|' $M/__manifest__.py

# 4. ir.cron: 17 still has numbercall, and its default of 1 would run each job once and
#    then switch it off. This is the one insertion in the recipe.
#
# Per record, not per file. The first version of this skipped the whole file when *any*
# numbercall was already present, which is right for a clean 18.0 tree and wrong for the
# case that actually happens: merging 18.0 into 17.0 leaves a file whose existing crons
# carry numbercall and whose newly added one does not. The guard then saw the old ones,
# skipped, and the new job would have run exactly once and switched itself off. The
# verification below caught it, but a check that only fails is worse than a step that is
# simply idempotent -- so this now inserts after any interval_type not already followed
# by a numbercall, and re-running it changes nothing.
python3 - "$M" <<'PY'
import re, sys
path = "%s/data/ir_cron.xml" % sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out, added = [], 0
for i, line in enumerate(lines):
    out.append(line)
    if '<field name="interval_type">' not in line:
        continue
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    if '<field name="numbercall"' in nxt:
        continue
    indent = re.match(r"\s*", line).group(0)
    out.append('%s<field name="numbercall">-1</field>\n' % indent)
    added += 1
open(path, "w", encoding="utf-8").write("".join(out))
print("numbercall: added %d, file now has %d cron(s)"
      % (added, sum('<field name="interval_type">' in l for l in lines)))
PY

# --- prove it ---------------------------------------------------------------------------
fail=0
if grep -rn '<list\|list,form\|"version": "18' $M/views $M/__manifest__.py; then
  echo "FAIL: 18-shaped arch or version survived"; fail=1
fi
crons=$(grep -c 'interval_type' $M/data/ir_cron.xml)
repeats=$(grep -c '<field name="numbercall"' $M/data/ir_cron.xml)
if [ "$crons" != "$repeats" ]; then
  echo "FAIL: $crons cron(s) but $repeats numbercall(s) — some job would run once and stop"
  fail=1
fi
if grep -rn 'kanban-box' $M/views >/dev/null; then
  echo "ok: kanban-box kept — the only spelling that renders on 17"
fi
[ "$fail" = 0 ] && echo "backport clean: $crons crons, all repeating"
exit $fail
