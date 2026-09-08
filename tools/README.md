# Static checks

Two checks that need no Odoo, no database and no addons path — run them from the repo
root before pushing, and on every branch after a backport:

```bash
python3 tools/check_views.py      # every <field>/button in a view resolves on its model
python3 tools/check_security.py   # every model has access rows, and a company rule if it needs one
```

Both exit non-zero on a problem, so they drop straight into CI.

They exist because both gaps they catch are invisible until Odoo loads the module — and
one of them (a missing multi-company `ir.rule`) is not an install failure at all. It is a
silent data leak between companies that no test would notice unless it was written to.

`check_views.py` parses the model source with `ast` and every view's arch with
`ElementTree`, then reports any field or object button a view references that the model
does not define. Fields nested inside another `<field>` are skipped: those belong to the
sub-model, and validating them against the parent is where this kind of check usually
produces its false positives.

`check_security.py` asserts that every non-transient model has `ir.model.access` rows for
both Tipsoi groups, and that any model carrying `company_id` also has a matching
`ir.rule`. Adding a model and forgetting the rule is the easy mistake; it is also the
expensive one.

Neither replaces running the suite. They catch the wiring, not the behaviour.
