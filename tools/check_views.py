"""Cross-check every <field> and object button in the views against the model source."""
import ast, sys, pathlib, xml.etree.ElementTree as ET

root = pathlib.Path("tipsoi_connector")

def is_field_call(node):
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "fields")

models = {}
for py in list((root/"models").glob("*.py")) + list((root/"wizards").glob("*.py")):
    for node in ast.walk(ast.parse(py.read_text())):
        if not isinstance(node, ast.ClassDef):
            continue
        name, fields, methods = None, set(), set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
                t = stmt.targets[0].id
                if t in ("_name", "_inherit") and isinstance(stmt.value, ast.Constant):
                    name = name or stmt.value.value
                elif is_field_call(stmt.value):
                    fields.add(t)
            elif isinstance(stmt, ast.FunctionDef):
                methods.add(stmt.name)
        if name:
            f, m = models.setdefault(name, (set(), set()))
            f |= fields; m |= methods

# hr.employee / hr.attendance also carry core fields this module does not declare.
CORE = {"id", "display_name", "create_date", "write_date", "create_uid", "write_uid",
        "active", "name", "company_id", "employee_id", "check_in", "check_out",
        "worked_hours", "resource_calendar_id"}

bad = []
for xf in sorted((root/"views").glob("*.xml")):
    for view in ET.parse(xf).iter("record"):
        if view.get("model") != "ir.ui.view":
            continue
        mf = view.find("./field[@name='model']")
        arch = view.find("./field[@name='arch']")
        if mf is None or arch is None or mf.text not in models:
            continue
        model = mf.text
        fields, methods = models[model]
        # Fields nested inside another <field> belong to a sub-model, so only look at
        # elements whose ancestry contains no <field>.
        def walk(el, inside_field):
            for child in el:
                if child.tag == "field":
                    fn = child.get("name")
                    if not inside_field and fn and fn not in fields and fn not in CORE:
                        bad.append(f"{xf.name}: {model}.{fn} — no such field")
                    walk(child, True)
                else:
                    if child.tag == "button" and child.get("type") == "object":
                        bn = child.get("name")
                        if bn and bn not in methods:
                            bad.append(f"{xf.name}: {model}.{bn}() — no such method")
                    walk(child, inside_field)
        walk(arch, False)

for b in bad:
    print("MISMATCH:", b)
print(f"checked {len(models)} models, {len(bad)} problems")
sys.exit(1 if bad else 0)
