#!/usr/bin/env python3
"""Prove the two copies of the domain table agree.

`backport-to-16.sh` holds the table that *applies* each translation;
`check-view-conditions.py` holds the table that *proves* it. MIGRATION-16-17.md flags
this as the one maintenance edge on the backport: nothing checked that the two agree, so
editing a domain in one and not the other leaves the checker confirming a translation
that is no longer in the tree -- a green run against code that is not there.

Unlike `check-view-conditions.py` this needs no Odoo and no database, so it can run in CI
and before a push. It also checks the third thing the pair implies: that the table covers
exactly the conditions the views actually use, with nothing missing and nothing stale.
"""
import ast, glob, re, sys, pathlib

root = pathlib.Path(__file__).resolve().parent


def applied():
    """The dict literal inside backport-to-16.sh."""
    src = (root / "backport-to-16.sh").read_text()
    body = src[src.index("TABLE = {"):]
    body = body[:body.index("\n}") + 2]
    return ast.literal_eval(body[len("TABLE = "):])


def proved():
    """The PAIRS list inside check-view-conditions.py."""
    src = (root / "check-view-conditions.py").read_text()
    body = src[src.index("PAIRS = ["):]
    body = body[:body.index("\n]") + 2]
    return dict(ast.literal_eval(body[len("PAIRS = "):]))


def used():
    """Every dynamic condition the views actually carry."""
    STATIC = {"1", "0", "True", "False"}
    pat = re.compile(r'\b(?:invisible|readonly|required|column_invisible)="([^"]*)"')
    out = {}
    for path in sorted(glob.glob(str(root / "tipsoi_connector/views/*.xml"))):
        for expr in pat.findall(pathlib.Path(path).read_text()):
            if expr not in STATIC:
                out.setdefault(expr, set()).add(pathlib.Path(path).name)
    return out


def attrs_used():
    """Conditions already converted to attrs, read back out of the views."""
    pat = re.compile(r"attrs=\"\{'(?:invisible|readonly|required|column_invisible)': ([^\"]*)\}\"")
    found = set()
    for path in sorted(glob.glob(str(root / "tipsoi_connector/views/*.xml"))):
        found |= set(pat.findall(pathlib.Path(path).read_text()))
    return found


problems = []
apply_t, prove_t = applied(), proved()

for expr in sorted(set(apply_t) | set(prove_t)):
    a, p = apply_t.get(expr), prove_t.get(expr)
    if a is None:
        problems.append(f"proved but never applied:  {expr!r}")
    elif p is None:
        problems.append(f"applied but never proved:  {expr!r}")
    elif " ".join(a.split()) != " ".join(p.split()):
        problems.append(f"the two tables disagree:   {expr!r}\n"
                        f"    backport-to-16.sh      {a}\n"
                        f"    check-view-conditions  {p}")

# On a converted tree the views hold attrs, not Python, so "used" is empty and the attrs
# are what to compare. On an unconverted one it is the other way round. Handle both.
raw = used()
if raw:
    for expr in sorted(raw):
        if expr not in apply_t:
            problems.append(f"view condition not in the table: {expr!r} "
                            f"({', '.join(sorted(raw[expr]))})")
    stale = set(apply_t) - set(raw)
    print(f"tree is UNCONVERTED: {len(raw)} distinct conditions in views")
else:
    domains = attrs_used()
    stale = {e for e, d in apply_t.items() if d not in domains}
    print(f"tree is CONVERTED: {len(domains)} distinct attrs domains in views")
for expr in sorted(stale):
    print(f"note: table entry no longer used by any view: {expr!r}")

for p in problems:
    print("PROBLEM:", p)
print(f"\n{len(apply_t)} applied, {len(prove_t)} proved, {len(problems)} problems")
sys.exit(1 if problems else 0)
