# Prove each attrs domain means exactly what the Odoo 17 Python expression meant.
#
# The install cannot check this: a domain of the right shape and the wrong operator loads
# fine and simply shows or hides the wrong thing. So for every condition, enumerate the
# plausible values of every field it mentions, evaluate the Python expression directly, and
# evaluate the domain with filtered_domain on an in-memory record. They must agree on every
# combination -- not on a case somebody thought to write down.
import itertools, re

PAIRS = [
    ("not tipsoi_identifier", "[('tipsoi_identifier', 'in', [False, ''])]"),
    ("tipsoi_photo_state != 'rejected'", "[('tipsoi_photo_state', '!=', 'rejected')]"),
    ("not tipsoi_photo_error_code", "[('tipsoi_photo_error_code', 'in', [False, ''])]"),
    ("not tipsoi_photo_url", "[('tipsoi_photo_url', 'in', [False, ''])]"),
    ("tipsoi_photo_state == 'rejected' or not tipsoi_photo_error",
     "['|', ('tipsoi_photo_state', '=', 'rejected'), ('tipsoi_photo_error', 'in', [False, ''])]"),
    ("state != 'ready'", "[('state', '!=', 'ready')]"),
    ("state != 'ready' or backend_type != 'device_portal'",
     "['|', ('state', '!=', 'ready'), ('backend_type', '!=', 'device_portal')]"),
    ("state != 'ready' or backend_type != 'hrm'",
     "['|', ('state', '!=', 'ready'), ('backend_type', '!=', 'hrm')]"),
    ("backend_type != 'device_portal'", "[('backend_type', '!=', 'device_portal')]"),
    ("backend_type != 'hrm'", "[('backend_type', '!=', 'hrm')]"),
    ("backend_type", "[('backend_type', '!=', False)]"),
    ("backend_type != 'device_portal' or not source_timezone",
     "['|', ('backend_type', '!=', 'device_portal'), ('source_timezone', 'in', [False, ''])]"),
    ("not last_error", "[('last_error', 'in', [False, ''])]"),
    ("state == 'imported'", "[('state', '=', 'imported')]"),
    ("not attendance_id", "[('attendance_id', '=', False)]"),
    ("state != 'unmatched'", "[('state', '!=', 'unmatched')]"),
    ("state != 'unpaired'", "[('state', '!=', 'unpaired')]"),
    ("state != 'error' or not state_reason",
     "['|', ('state', '!=', 'error'), ('state_reason', 'in', [False, ''])]"),
    ("active", "[('active', '=', True)]"),
    ("connectivity_known", "[('connectivity_known', '=', True)]"),
    ("not central_server_id", "[('central_server_id', '=', 0)]"),
    ("not workplace_name", "[('workplace_name', 'in', [False, ''])]"),
    ("state in ('paired', 'duplicate')", "[('state', 'in', ['paired', 'duplicate'])]"),
    ("not error", "[('error', 'in', [False, ''])]"),
    ("not notes", "[('notes', 'in', [False, ''])]"),
    ("not mode_warning", "[('mode_warning', 'in', [False, ''])]"),
    ("job not in ('punches', 'attendance')", "[('job', 'not in', ['punches', 'attendance'])]"),
    ("not result", "[('result', 'in', [False, ''])]"),
    ("state != 'done'", "[('state', '!=', 'done')]"),
    ("state == 'done'", "[('state', '=', 'done')]"),
]

MODELS = ["tipsoi.backend", "tipsoi.device", "tipsoi.punch.log", "tipsoi.day.attendance",
          "tipsoi.sync.run", "hr.employee", "tipsoi.manual.sync", "tipsoi.link.employee",
          "tipsoi.allocation"]
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEYWORDS = {"not", "or", "and", "in", "None", "True", "False"}


def fields_of(expr):
    # Strip quoted literals first. Without this, 'ready' in "state != 'ready'" is picked up
    # as a field name and the condition is skipped as unmodellable -- which silently drops
    # over half the table from the check.
    bare = re.sub(r"'[^']*'", " ", expr)
    return sorted({t for t in IDENT.findall(bare) if t not in KEYWORDS})


def literals(expr):
    """The quoted values the expression itself compares against."""
    return re.findall(r"'([^']*)'", expr)


def values_for(model, name, extra=()):
    """Plausible stored values for one field, by type -- both branches of every test."""
    f = env[model]._fields[name]
    if f.type == "selection":
        sel = f.selection
        if isinstance(sel, str):
            sel = getattr(env[model], sel)()
        elif callable(sel):
            sel = sel(env[model])
        vals = [v for v, _lbl in sel]
        if len(vals) > 6:
            vals = vals[:2]
        # Add the values the expression names. A condition may be checked against a model
        # that carries the field but not that value in its selection -- state != 'unmatched'
        # on tipsoi.backend, say -- and then only the not-equal branch is ever exercised and
        # the comparison passes trivially. Adding the literals forces both branches.
        return [False] + sorted(set(vals) | set(extra))
    if f.type == "boolean":
        return [True, False]
    if f.type == "integer":
        return [0, 7]
    if f.type == "many2one":
        return [False, 1]
    # "" is included on purpose: the probe below shows Odoo 16 persists it for a Text
    # field, so it is a value a real record can hold and the domain has to handle it.
    return [False, "", "something"]


# The one assumption above, measured: write "" to a Char and read it back.
probe = env["tipsoi.sync.run"].create({
    "backend_id": env["tipsoi.backend"].create({
        "backend_type": "device_portal", "base_url": "https://example.invalid/api/v1",
        "username": "u", "password": "p"}).id,
    "job": "devices", "notes": ""})
# `invalidate_recordset` arrived in 16; 15 has only `invalidate_cache`.
(getattr(probe, "invalidate_recordset", None) or probe.invalidate_cache)()
print("EMPTY-STRING PROBE: notes stored as %r -- falsy=%s"
      % (probe.notes, not probe.notes))

checked = mismatches = skipped = 0
for expr, domain in PAIRS:
    names = fields_of(expr)
    model = next((m for m in MODELS
                  if all(n in env[m]._fields for n in names)), None)
    if model is None:
        print("SKIP  no single model carries %s  (%s)" % (names, expr))
        skipped += 1
        continue
    lits = literals(expr)
    grid = [values_for(model, n, lits) for n in names]
    bad = []
    for combo in itertools.product(*grid):
        vals = dict(zip(names, combo))
        rec = env[model].new(vals)
        py = bool(eval(expr, {}, dict(vals)))
        dom = bool(rec.filtered_domain(eval(domain)))
        checked += 1
        if py != dom:
            bad.append((vals, py, dom))
    if bad:
        mismatches += 1
        print("MISMATCH  %-55s on %s" % (expr, model))
        for vals, py, dom in bad[:4]:
            print("            %s  python=%s domain=%s" % (vals, py, dom))
    else:
        print("ok    %-55s %s (%d combos)" % (expr, model, len(list(itertools.product(*grid)))))

print("EQUIV CHECKED %d combinations across %d conditions; %d mismatched, %d skipped"
      % (checked, len(PAIRS), mismatches, skipped))
