"""Container options in the app form (NVIDIA GPU). Offline: python webui/tests/test_app_options.py"""
import copy, os, sys, tempfile

tmp = tempfile.mkdtemp()
os.environ["HELIX_CONFIG"] = os.path.join(tmp, "config")
os.environ["STATE_DIR"] = os.path.join(tmp, "state")
os.makedirs(os.environ["HELIX_CONFIG"]); os.makedirs(os.environ["STATE_DIR"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from routers import apps  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + name)


def compose(env=None):
    svc = {"image": "plexinc/pms-docker", "ports": ["32400:32400"]}
    if env is not None:
        svc["environment"] = env
    return {"services": {"db": {"image": "postgres"}, "plex": svc}}


on = apps._clean_options({"gpu_nvidia": True})
off = apps._clean_options({"gpu_nvidia": False})

c = apps._apply_options(compose({"TZ": "UTC"}), on)
p = c["services"]["plex"]
check("on: runtime nvidia on the main service", p.get("runtime") == "nvidia")
check("on: NVIDIA env added", p["environment"].get("NVIDIA_VISIBLE_DEVICES") == "all"
      and "video" in p["environment"].get("NVIDIA_DRIVER_CAPABILITIES", ""))
check("on: existing env kept", p["environment"].get("TZ") == "UTC")
check("on: helper service untouched", "runtime" not in c["services"]["db"])
check("on: form reads it back", apps._effective_options(c)["gpu_nvidia"] is True)

c = apps._apply_options(copy.deepcopy(c), off)
p = c["services"]["plex"]
check("off: runtime removed", "runtime" not in p)
check("off: NVIDIA env removed, TZ kept", p["environment"] == {"TZ": "UTC"})
check("off: form reads it back", apps._effective_options(c)["gpu_nvidia"] is False)

c = compose(["NVIDIA_VISIBLE_DEVICES=all", "NVIDIA_DRIVER_CAPABILITIES=all"]); c["services"]["plex"]["runtime"] = "nvidia"
c = apps._apply_options(c, off)
check("list env with only NVIDIA keys + off: runtime and environment dropped",
      "environment" not in c["services"]["plex"] and "runtime" not in c["services"]["plex"])
c = apps._apply_options(compose(["NVIDIA_VISIBLE_DEVICES=all"]), off)
check("template's own NVIDIA env without the runtime is left alone",
      c["services"]["plex"]["environment"] == {"NVIDIA_VISIBLE_DEVICES": "all"})
c = apps._apply_options(compose(), on)
check("no env + on: environment created", c["services"]["plex"]["environment"]["NVIDIA_VISIBLE_DEVICES"] == "all")
check("template runtime nvidia shows as on", apps._effective_options(
    {"services": {"x": {"image": "i", "ports": ["1:1"], "runtime": "nvidia"}}})["gpu_nvidia"] is True)

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
