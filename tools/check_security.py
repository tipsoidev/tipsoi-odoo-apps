"""Every module model with company_id needs an ir.rule and access rows for both groups."""
import ast, csv, sys, pathlib, xml.etree.ElementTree as ET
root = pathlib.Path("tipsoi_connector")

def is_field(n):
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "fields")

models = {}
for py in list((root/"models").glob("*.py")) + list((root/"wizards").glob("*.py")):
    for node in ast.walk(ast.parse(py.read_text())):
        if not isinstance(node, ast.ClassDef):
            continue
        name, fields, transient = None, set(), False
        for b in node.body:
            if isinstance(b, ast.Assign) and isinstance(b.targets[0], ast.Name):
                if b.targets[0].id == "_name" and isinstance(b.value, ast.Constant):
                    name = b.value.value
                elif is_field(b.value):
                    fields.add(b.targets[0].id)
        for base in node.bases:
            if "TransientModel" in ast.dump(base):
                transient = True
        if name:
            models[name] = (fields, transient)

rules = {r.find("./field[@name='model_id']").get("ref")
         for r in ET.parse(root/"security/tipsoi_security.xml").iter("record")
         if r.get("model") == "ir.rule"}
access = {}
for row in csv.DictReader((root/"security/ir.model.access.csv").open()):
    access.setdefault(row["model_id:id"], set()).add(row["group_id:id"])

bad = []
for name, (fields, transient) in sorted(models.items()):
    ref = "model_" + name.replace(".", "_")
    if ref not in access:
        bad.append(f"{name}: no ir.model.access row")
    else:
        for g in ("group_tipsoi_user", "group_tipsoi_admin"):
            if g not in access[ref]:
                bad.append(f"{name}: no access row for {g}")
    if "company_id" in fields and not transient and ref not in rules:
        bad.append(f"{name}: has company_id but no multi-company ir.rule")

for b in bad: print("GAP:", b)
print(f"checked {len(models)} models, {len(bad)} gaps")
sys.exit(1 if bad else 0)
