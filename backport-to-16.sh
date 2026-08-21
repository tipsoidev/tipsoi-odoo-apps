#!/bin/bash
# Mechanically convert the 17.0 working tree to 16.0 shape, per MIGRATION-16-17.md.
# Run from the repo root on the 16.0 branch after merging 17.0 into it.
#
# The bulk of this backport is one change repeated 57 times: Odoo 17 introduced plain-Python
# view conditions (invisible="state != 'done'"), and 16 has only the older attrs domain form.
# 16 rejects the new form outright -- `ValueError: Use 0/1/yes/no/true/false/on/off`, or a
# NameError on the field -- so a condition that is *forgotten* cannot reach a customer. A
# condition that is *mistranslated* can, which is why every expression is spelled out in the
# table below rather than pattern-matched, and why the table is the thing to review.
set -euo pipefail
cd "${1:-$PWD}"
M=tipsoi_connector
[ -f "$M/__manifest__.py" ] || { echo "run me from the repo root (or pass it)"; exit 2; }

python3 - "$M" <<'PY'
import glob, re, sys

M = sys.argv[1]

# Every distinct dynamic condition in the 17.0 views, with its 16.0 domain. Field types were
# read off the models, and they decide the falsy spelling: Boolean compares to True,
# Many2one to False, `central_server_id` is an Integer whose empty value is 0 and not False,
# and Char/Text compare against [False, ''].
#
# That last one is measured, not stylistic. Odoo 16 persists an empty string for a Text
# field rather than storing NULL -- `create({'notes': ''})` reads back as '' -- so Python's
# `not notes` is True where a bare `('notes', '=', False)` does not match, and the field
# would show when 17 hid it. `in [False, '']` agrees with the Python on both values. It also
# sidesteps a question this repo cannot answer from a shell: attrs domains are evaluated by
# the web client, not by filtered_domain, so which of the two coerces '' is not observable
# here. Writing the domain to cover both makes the answer irrelevant.
TABLE = {
    "not tipsoi_identifier":        "[('tipsoi_identifier', 'in', [False, ''])]",
    "tipsoi_photo_state != 'rejected'": "[('tipsoi_photo_state', '!=', 'rejected')]",
    "not tipsoi_photo_error_code":  "[('tipsoi_photo_error_code', 'in', [False, ''])]",
    "not tipsoi_photo_url":         "[('tipsoi_photo_url', 'in', [False, ''])]",
    "tipsoi_photo_state == 'rejected' or not tipsoi_photo_error":
        "['|', ('tipsoi_photo_state', '=', 'rejected'), ('tipsoi_photo_error', 'in', [False, ''])]",
    "state != 'ready'":             "[('state', '!=', 'ready')]",
    "state != 'ready' or backend_type != 'device_portal'":
        "['|', ('state', '!=', 'ready'), ('backend_type', '!=', 'device_portal')]",
    "state != 'ready' or backend_type != 'hrm'":
        "['|', ('state', '!=', 'ready'), ('backend_type', '!=', 'hrm')]",
    "backend_type != 'device_portal'": "[('backend_type', '!=', 'device_portal')]",
    "backend_type != 'hrm'":        "[('backend_type', '!=', 'hrm')]",
    # Truthiness on a Selection: shown only before a type has been chosen.
    "backend_type":                 "[('backend_type', '!=', False)]",
    "backend_type != 'device_portal' or not source_timezone":
        "['|', ('backend_type', '!=', 'device_portal'), ('source_timezone', 'in', [False, ''])]",
    "not last_error":               "[('last_error', 'in', [False, ''])]",
    "state == 'imported'":          "[('state', '=', 'imported')]",
    "not attendance_id":            "[('attendance_id', '=', False)]",
    "state != 'unmatched'":         "[('state', '!=', 'unmatched')]",
    "state != 'unpaired'":          "[('state', '!=', 'unpaired')]",
    "state != 'error' or not state_reason":
        "['|', ('state', '!=', 'error'), ('state_reason', 'in', [False, ''])]",
    # Booleans, both of them shown when the flag is *set*.
    "active":                       "[('active', '=', True)]",
    "connectivity_known":           "[('connectivity_known', '=', True)]",
    "not central_server_id":        "[('central_server_id', '=', 0)]",
    "not workplace_name":           "[('workplace_name', 'in', [False, ''])]",
    "state in ('paired', 'duplicate')": "[('state', 'in', ['paired', 'duplicate'])]",
    "not error":                    "[('error', 'in', [False, ''])]",
    "not notes":                    "[('notes', 'in', [False, ''])]",
    "not mode_warning":             "[('mode_warning', 'in', [False, ''])]",
    "job not in ('punches', 'attendance')":
        "[('job', 'not in', ['punches', 'attendance'])]",
    "not result":                   "[('result', 'in', [False, ''])]",
    "state != 'done'":              "[('state', '!=', 'done')]",
    "state == 'done'":              "[('state', '=', 'done')]",
}

# Static values keep the 17 spelling: `invisible="1"` is valid on 16 too.
STATIC = {"1", "0", "True", "False"}
ATTRS = ("invisible", "readonly", "required", "column_invisible")
PATTERN = re.compile(r'\b(%s)="([^"]*)"' % "|".join(ATTRS))

converted = unknown = 0
for path in sorted(glob.glob("%s/views/*.xml" % M)):
    src = open(path, encoding="utf-8").read()

    def repl(m):
        global converted, unknown
        attr, expr = m.group(1), m.group(2)
        if expr in STATIC:
            return m.group(0)
        domain = TABLE.get(expr)
        if domain is None:
            unknown += 1
            print("UNKNOWN CONDITION  %s  %s=\"%s\"" % (path, attr, expr))
            return m.group(0)
        converted += 1
        # Single quotes inside, because the attribute itself is double-quoted.
        return "attrs=\"{'%s': %s}\"" % (attr, domain)

    out = PATTERN.sub(repl, src)
    if out != src:
        open(path, "w", encoding="utf-8").write(out)

print("converted %d condition(s); %d unknown" % (converted, unknown))
sys.exit(1 if unknown else 0)
PY

# Manifest version. 16 refuses an out-of-series version outright, unlike 19.
sed -i 's|"version": "17.0|"version": "16.0|' $M/__manifest__.py

# `name_get` was removed in 17 and `_compute_display_name` added; 16 has only the former, so
# a `_compute_display_name` override there is code that never runs and the record shows its
# id instead of its name.
# Match the *definition*, not the word: the replacement's own comment explains why
# _compute_display_name is wrong here, and a bare grep finds that explanation.
if grep -q 'def _compute_display_name' $M/models/tipsoi_sync_run.py; then
  echo "TODO: tipsoi_sync_run still overrides _compute_display_name -- 16 needs name_get"
fi

# --- prove it ---------------------------------------------------------------------------
fail=0
note() { echo "FAIL: $*"; fail=1; }

grep -q '"version": "16.0' $M/__manifest__.py || note "manifest version is not 16.0.x"

# The whole point of the table: nothing 17-shaped may survive. Static 1/0 is fine on both.
if grep -rnE '\b(invisible|readonly|required|column_invisible)="[^"]*"' $M/views \
     | grep -vE '="(1|0|True|False)"' >/dev/null; then
  note "a 17-shaped dynamic view condition survived (see above)"
  grep -rnE '\b(invisible|readonly|required|column_invisible)="[^"]*"' $M/views \
    | grep -vE '="(1|0|True|False)"'
fi
grep -rq 'attrs="{' $M/views || note "no attrs produced at all -- did the table run?"
grep -rq 'def _compute_display_name' $M/models \
  && note "16 needs name_get, not _compute_display_name"
grep -rq 'def name_get' $M/models/tipsoi_sync_run.py \
  || note "tipsoi_sync_run has no name_get -- runs would display as an id on 16"

# 16 keeps numbercall, exactly like 17, so the 17 file is already right -- but verify rather
# than assume, since a job with the default of 1 runs once and then switches itself off.
crons=$(grep -c 'interval_type' $M/data/ir_cron.xml)
repeats=$(grep -c '<field name="numbercall"' $M/data/ir_cron.xml)
[ "$crons" = "$repeats" ] || note "$crons cron(s) but $repeats numbercall(s)"

[ "$fail" = 0 ] && cat <<'MSG'
backport clean: version 16.0.x, every dynamic condition converted to attrs, crons repeating.

Reminder about what this does and does not prove. 16 rejects an unconverted condition at
install, so nothing can have been missed. It does NOT check that a domain means what the
Python expression meant -- review the table in this script for that, and click through the
backend form, the punch log and the wizards once on a real 16.
MSG
exit $fail
