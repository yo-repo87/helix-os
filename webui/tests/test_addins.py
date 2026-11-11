"""Offline tests for the add-in manifest validation (the root helper's safety gate)."""
import importlib.machinery, importlib.util, json, os, sys

path = os.path.join(os.path.dirname(__file__), "..", "..", "addins", "helix-addin")
spec = importlib.util.spec_from_loader("helix_addin", importlib.machinery.SourceFileLoader("helix_addin", path))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

good = json.load(open(os.path.join(os.path.dirname(__file__), "..", "backend", "data", "addins", "index.json")))["addins"][0]
ok = m.validate(good); assert ok["id"] == "nvidia" and ok["reboot"]

def bad(mut, why):
    e = json.loads(json.dumps(good)); mut(e)
    try: m.validate(e)
    except SystemExit: return
    raise AssertionError("accepted: " + why)

bad(lambda e: e.update(id="../x"), "path id")
bad(lambda e: e["apt"]["packages"].append("a; rm -rf /"), "shell in package")
bad(lambda e: e["apt"]["sources"].append({"name": "x", "line": "deb http://evil.example/ x main"}), "foreign host")
bad(lambda e: e["apt"]["sources"].append({"name": "x", "line": "deb https://nvidia.github.io/x /", "key_url": "http://nvidia.github.io/k"}), "http key")
bad(lambda e: e.update(post=["rm -rf /"]), "unknown post action")
bad(lambda e: e["apt"].update(packages=[]), "no packages")
print("addins validation: all checks passed")
