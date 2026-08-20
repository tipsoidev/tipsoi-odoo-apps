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
# Match the field, not the word: the file's own comment explains numbercall, and a bare
# grep for it would find the explanation and skip the insertion.
if ! grep -q '<field name="numbercall"' $M/data/ir_cron.xml; then
  sed -i '/<field name="interval_type">/a\      <field name="numbercall">-1</field>' \
    $M/data/ir_cron.xml
fi

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
