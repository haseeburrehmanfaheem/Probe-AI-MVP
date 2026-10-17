"""LLM trace: every request, response and tool call of a session, one JSON line each, for debugging.

Files: data/traces/<project id>.jsonl (next to the saved projects; PROBE_DATA moves both).
Set PROBE_TRACE=0 to turn tracing off. The API key is never written.
A phase prompt is written in full the first time it is used and when it changes; later
requests point to it by hash, so a long session doesn't repeat it on every turn.

Read one:  python backend/llm_trace.py data/traces/<project id>.jsonl [--full]
"""
import hashlib, json, os, sys, time
from pathlib import Path

DIR = Path(os.environ.get("PROBE_DATA", Path(__file__).parent / "data")) / "traces"
ENABLED = os.environ.get("PROBE_TRACE", "1") != "0"


def _plain(obj):
    """OpenAI SDK objects -> JSON-able dicts."""
    if hasattr(obj, "model_dump"): return obj.model_dump(exclude_none=True)
    if isinstance(obj, dict): return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [_plain(v) for v in obj]
    if hasattr(obj, "__dict__"): return {k: _plain(v) for k, v in vars(obj).items()}
    return obj


class Tracer:
    def __init__(self):
        self.path, self.seen = None, set()

    def open(self, project_id):
        if not ENABLED: return
        DIR.mkdir(parents=True, exist_ok=True)
        self.path, self.seen = DIR / f"{project_id}.jsonl", set()
        if self.path.exists():  # reopened project: don't rewrite prompts already in the file
            for line in self.path.read_text().splitlines():
                try: rec = json.loads(line)
                except ValueError: continue
                if rec.get("kind") == "prompt": self.seen.add(rec["hash"])

    def write(self, kind, c=None, **data):
        if not self.path: return
        rec = {"ts": round(time.time(), 3), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
               **({"component": c.id, "name": c.name, "phase": c.phase} if c else {}), **_plain(data)}
        with self.path.open("a") as f: f.write(json.dumps(rec, default=str) + "\n")

    def request(self, c, model, instructions, input, tools, previous_response_id):
        h = hashlib.sha1(instructions.encode()).hexdigest()[:12]
        if h not in self.seen:
            self.seen.add(h); self.write("prompt", c, hash=h, instructions=instructions)
        self.write("request", c, model=model, prompt=h, previous_response_id=previous_response_id,
                   tools=[t["name"] for t in tools], input=input)
        return time.time()

    def response(self, c, t0, resp):
        items = []
        for item in resp.output:
            if item.type == "message":
                items.append({"type": "message", "text": "".join(getattr(p, "text", "") for p in item.content)})
            elif item.type == "function_call":
                items.append({"type": "function_call", "name": item.name, "arguments": item.arguments, "call_id": item.call_id})
            else:
                items.append({"type": item.type})
        self.write("response", c, id=resp.id, seconds=round(time.time() - t0, 2), output=items,
                   usage=getattr(resp, "usage", None))

    def tool(self, c, name, arguments, result, t0):
        self.write("tool", c, name=name, arguments=arguments, seconds=round(time.time() - t0, 2), result=result)

    def error(self, c, where, err):
        self.write("error", c, where=where, error=f"{type(err).__name__}: {err}")


def show(path, full=False):
    """Print a trace as a readable transcript."""
    prompts = {}
    for line in Path(path).read_text().splitlines():
        r = json.loads(line); k = r["kind"]; head = f"{r['time']} {r.get('component', '---')} {r.get('phase', ''):<13}"
        if k == "prompt":
            prompts[r["hash"]] = r["instructions"]
            print(f"{head} PROMPT {r['hash']} ({len(r['instructions'])} chars)")
            if full: print("    " + r["instructions"].replace("\n", "\n    "))
        elif k == "request":
            for i in r["input"]:
                if i.get("role") == "user": print(f"{head} USER   {i['content']}")
        elif k == "response":
            u = r.get("usage") or {}
            print(f"{head} LLM    {r['seconds']}s, tokens in {u.get('input_tokens', '?')} / out {u.get('output_tokens', '?')}")
            for o in r["output"]:
                if o["type"] == "message": print(f"{'':>36}says: {o['text']}")
                elif o["type"] == "function_call": print(f"{'':>36}call: {o['name']}({o['arguments']})")
        elif k == "tool":
            res = json.dumps(r["result"])
            print(f"{head} TOOL   {r['name']} -> {res if full or len(res) < 300 else res[:300] + ' …'}")
        elif k == "error":
            print(f"{head} ERROR  {r['where']}: {r['error']}")
        else:
            print(f"{head} {k.upper():<6} {json.dumps({x: y for x, y in r.items() if x not in ('ts', 'time', 'kind')})}")


if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(__doc__)
    show(sys.argv[1], "--full" in sys.argv)
