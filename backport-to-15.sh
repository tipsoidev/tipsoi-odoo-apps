#!/bin/bash
# Mechanically convert the 16.0 working tree to 15.0 shape, per MIGRATION-15-16.md.
# Run from the repo root on the 15.0 branch after merging 16.0 into it.
#
# Branch from 16.0, never from 17.0 or later: the 57 view conditions have to already be in
# their attrs form, and that is 16's work. What is left is small -- three ORM methods that
# Odoo 16 renamed, and the manifest version.
set -euo pipefail
cd "${1:-$PWD}"
M=tipsoi_connector
[ -f "$M/__manifest__.py" ] || { echo "run me from the repo root (or pass it)"; exit 2; }

# 1. Odoo 16 split `flush()` into `flush_model()`/`flush_recordset()` and added
#    `invalidate_recordset()` beside `invalidate_cache()`. 15 has only the older pair, and
#    calling the newer name is an AttributeError at runtime, not at install -- 118 of the
#    254 tests error on the flush alone.
#
#    The 15 spellings are broader: `flush()` and `invalidate_cache()` act on the model rather
#    than just this recordset. That is safe in every call site here -- flushing or dropping
#    more than needed costs a query, it does not change an outcome -- but it is the reason
#    this is a backport direction only and must not be carried forward.
sed -i 's|\.flush_recordset(|.flush(|g' $M/models/*.py $M/wizards/*.py
sed -i 's|\.invalidate_recordset(|.invalidate_cache(|g' $M/models/*.py $M/wizards/*.py $M/tests/*.py

#    `env.invalidate_all(flush=False)` arrived in 16 as well. Its body there is exactly
#    `self.cache.invalidate()`, and 15 has that, so the replacement is the same operation
#    rather than an approximation of it -- which matters, because every call site is a
#    post-rollback cache drop where flushing would push back the values just rolled back.
sed -i 's|\.env\.invalidate_all(flush=False)|.env.cache.invalidate()|g' $M/models/*.py $M/wizards/*.py

# 2. Manifest version.
sed -i 's|"version": "16.0|"version": "15.0|' $M/__manifest__.py

# --- prove it ---------------------------------------------------------------------------
fail=0
note() { echo "FAIL: $*"; fail=1; }

grep -q '"version": "15.0' $M/__manifest__.py || note "manifest version is not 15.0.x"

# Match the call, not the word: this script's own comments name both spellings, and a bare
# grep for them finds the explanation rather than a real call site.
if grep -rn '\.flush_recordset(\|\.invalidate_recordset(' $M --include=*.py >/dev/null; then
  note "a 16-only ORM method survived (see above) -- AttributeError at runtime, not install"
  grep -rn '\.flush_recordset(\|\.invalidate_recordset(' $M --include=*.py
fi
if grep -rn '\.invalidate_all(' $M --include=*.py >/dev/null; then
  note "env.invalidate_all arrived in 16; on 15 it is env.cache.invalidate()"
fi
if grep -rn '\.get_views(' $M --include=*.py >/dev/null; then
  note "get_views arrived in 16; on 15 it is load_views"
fi

# 16's work must already be here, or this branch came from the wrong place.
grep -rq 'attrs="{' $M/views \
  || note "no attrs in the views -- branch from 16.0, not 17.0 or later"
if grep -rnE '\b(invisible|readonly|required|column_invisible)="[^"]*"' $M/views \
     | grep -vE '="(1|0|True|False)"' >/dev/null; then
  note "a 17-shaped dynamic view condition is present -- branch from 16.0"
fi
grep -rq 'def name_get' $M/models/tipsoi_sync_run.py \
  || note "tipsoi_sync_run has no name_get -- runs would display as an id"
grep -rq 'def _compute_display_name' $M/models \
  && note "15 needs name_get, not _compute_display_name"

crons=$(grep -c 'interval_type' $M/data/ir_cron.xml)
repeats=$(grep -c '<field name="numbercall"' $M/data/ir_cron.xml)
[ "$crons" = "$repeats" ] || note "$crons cron(s) but $repeats numbercall(s)"

[ "$fail" = 0 ] && cat <<'MSG'
backport clean: version 15.0.x, ORM methods in their 15 spelling, attrs views inherited
from 16.0, crons repeating.

Note for whoever verifies this: 15 has no get_views, so a render check has to call
load_views instead, and a checker written against 16+ reports every view as broken.
MSG
exit $fail
