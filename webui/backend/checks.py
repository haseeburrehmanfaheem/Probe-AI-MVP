"""Test limits evaluated in code, so pass/fail never depends on the LLM comparing numbers.

A test in sensor.json lists "checks"; each one picks values from a run_test result and compares them:
  {"fields": "^G[XYZ]$", "stat": "abs_max", "op": "<", "value": 3, "mode": "all", "label": "..."}
source "fields" (default): per-field statistics; stat is n, min, max, mean, std, abs_max or range (max - min)
source "link" / "decoder": link counter deltas / decoder stats, matched by the "fields" regex
source "result": a top-level number of the result (seq_gaps, garbled_lines, rate_hz, samples); missing = 0
mode "all" (default) or "any" of the matched values must pass.
"""
import operator, re

OPS = {"<": operator.lt, ">": operator.gt, "<=": operator.le, ">=": operator.ge}


def _values(rule, result):
    src, pat = rule.get("source", "fields"), rule["fields"]
    if src in ("link", "decoder"):
        data = result.get(src) or {}
        vals = {k: v for k, v in data.items() if re.search(pat, k)}
        return vals or ({pat: 0} if src == "decoder" and re.fullmatch(r"\w+", pat) else {})  # absent counter = 0
    if src == "result":
        return {pat: result.get(pat, 0)}
    out = {}
    for k, st in (result.get("fields") or {}).items():
        if re.search(pat, k):
            out[k] = st["max"] - st["min"] if rule.get("stat") == "range" else st[rule.get("stat", "mean")]
    return out


def label(rule):
    if rule.get("label"): return rule["label"]
    what = rule["fields"].strip("^$")
    stat = "" if rule.get("source") in ("link", "decoder", "result") else f" {rule.get('stat', 'mean')}"
    return f"{what}{stat} {rule['op']} {rule['value']}"


def rules_for(test):
    return test.get("checks") or ([test["offline_check"]] if test.get("offline_check") else [])


def evaluate(test, result):
    """-> list of {"check", "values", "pass"} for the test's rules (empty if it has none)."""
    out = []
    for rule in rules_for(test):
        vals = _values(rule, result)
        if not vals:
            out.append({"check": label(rule), "values": {}, "pass": False, "note": "no matching data"}); continue
        agg = all if rule.get("mode", "all") == "all" else any
        out.append({"check": label(rule), "values": {k: round(v, 4) for k, v in vals.items()},
                    "pass": agg(OPS[rule["op"]](v, rule["value"]) for v in vals.values())})
    return out
