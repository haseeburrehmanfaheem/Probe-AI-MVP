"""Projects: named bring-up sessions whose UI events are saved to disk, so they can be
reopened from history (replayed into the UI) and continued with new components.

One JSON file per project under data/projects/ (override the root with PROBE_DATA).
Only the events needed to redraw the UI are kept (no typing indicators); live readings
are kept so test charts replay in full.
"""
import json, os, re, time, uuid
from pathlib import Path

DIR = Path(os.environ.get("PROBE_DATA", Path(__file__).parent / "data")) / "projects"
LOGGED = {"component", "phase", "message", "schematic", "wiring_step", "wiring_confirmed", "board_io",
          "config", "test_step", "test_status", "reading", "test_result", "error", "done"}
ID_RE = re.compile(r"^[0-9a-f]{10}$")


def _path(pid):
    if not ID_RE.match(str(pid)): raise ValueError(f"bad project id {pid!r}")
    return DIR / f"{pid}.json"


class Project:
    def __init__(self, data): self.data = data

    @classmethod
    def create(cls, name):
        now = time.time()
        p = cls({"id": uuid.uuid4().hex[:10], "name": name, "created": now, "updated": now,
                 "pinned": False, "components": {}, "events": []})
        p.save(); return p

    @classmethod
    def load(cls, pid): return cls(json.loads(_path(pid).read_text()))

    @property
    def id(self): return self.data["id"]

    def record(self, msg):
        comps, cid = self.data["components"], msg.get("component_id")
        if msg["type"] == "component":
            comps.setdefault(cid, {"name": msg["name"], "phases": {}, "done": False})["name"] = msg["name"]
        elif msg["type"] == "phase" and cid in comps:
            comps[cid]["phases"][msg["phase"]] = msg["status"]
        elif msg["type"] == "done" and cid in comps:
            comps[cid]["done"] = True
        self.data["events"].append(msg); self.data["updated"] = time.time()

    def save(self):
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = DIR / f".{self.id}.tmp"
        tmp.write_text(json.dumps(self.data)); tmp.replace(_path(self.id))

    def summary(self):
        d = self.data
        return {"id": d["id"], "name": d["name"], "created": d["created"], "updated": d["updated"], "pinned": d["pinned"],
                "components": [{"id": k, **v} for k, v in d["components"].items()]}


def list_projects():
    out = []
    for f in DIR.glob("*.json"):
        try: out.append(Project(json.loads(f.read_text())).summary())
        except (OSError, ValueError, KeyError): continue
    return sorted(out, key=lambda p: p["updated"], reverse=True)


def set_pinned(pid, pinned):
    p = Project.load(pid); p.data["pinned"] = bool(pinned); p.save()  # pinning doesn't bump "updated"


def delete(pid): _path(pid).unlink(missing_ok=True)
