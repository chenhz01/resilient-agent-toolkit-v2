#!/usr/bin/env python3
"""eval_gate.py — Mechanical regression gate for skill/output changes
(internalized from confident-ai/deepeval, L1 card, 2026-09-20).

THE MECHANISM (from deepeval): pytest-style, metric-based evaluation with
deterministic scoring — replace "I feel it got better" with before/after
metric numbers. The valuable half for Zhengming is the REGRESSION GATE:
every skill/output change runs the same metric suite; a run log (JSONL,
append-only) makes before/after comparison mechanical.

Mechanical metrics (no LLM judge — fully reproducible):
  required    — all required substrings present
  forbidden   — no forbidden substrings (PII, placeholder, banned words)
  json_valid  — output parses as JSON (when expected)
  length      — min/max bounds
  sections    — required section headings present
Gate verdict: PASS only if every case passes; FAIL lists per-case reasons.
Run log: every run appends to runs.jsonl; compare_runs() diffs the two runs.

CLI:
    python eval_gate.py --selftest
    python eval_gate.py run <cases.json> [--log runs.jsonl]
    python eval_gate.py compare <runs.jsonl>
"""
from __future__ import annotations

import json
import os
import sys
import time


def score_case(case: dict, output: str) -> dict:
    """Score one case. case = {"id", "required": [...], "forbidden": [...],
    "json": bool, "min_len"/"max_len": int, "sections": [...]}."""
    reasons = []
    for s in case.get("required", []):
        if s not in output:
            reasons.append(f"missing required: {s!r}")
    for s in case.get("forbidden", []):
        if s in output:
            reasons.append(f"forbidden present: {s!r}")
    if case.get("json"):
        try:
            json.loads(output)
        except json.JSONDecodeError as e:
            reasons.append(f"not valid json: {e.msg} at pos {e.pos}")
    n = len(output)
    if "min_len" in case and n < case["min_len"]:
        reasons.append(f"too short: {n} < {case['min_len']}")
    if "max_len" in case and n > case["max_len"]:
        reasons.append(f"too long: {n} > {case['max_len']}")
    for sec in case.get("sections", []):
        if sec not in output:
            reasons.append(f"missing section: {sec!r}")
    return {"id": case.get("id", "case"), "pass": not reasons,
            "reasons": reasons, "len": n}


def run(cases: list, outputs: dict, log_path: str | None = None) -> dict:
    """Run the suite. outputs = {case_id: output_text}."""
    results = []
    for c in cases:
        cid = c.get("id", "case")
        if cid not in outputs:
            results.append({"id": cid, "pass": False,
                            "reasons": ["no output provided"], "len": 0})
            continue
        results.append(score_case(c, outputs[cid]))
    verdict = "PASS" if all(r["pass"] for r in results) else "FAIL"
    out = {"verdict": verdict,
           "n_pass": sum(r["pass"] for r in results),
           "n_total": len(results), "results": results}
    if log_path:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **out}
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return out


def compare(log_path: str) -> dict:
    """Diff the last two runs in the append-only log: which cases flipped."""
    if not os.path.exists(log_path):
        return {"state": "EXPLICIT_FAILURE", "payload": None,
                "reasons": [f"no run log at {log_path}"]}
    with open(log_path, encoding="utf-8") as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    if len(entries) < 2:
        return {"state": "EXPLICIT_FAILURE", "payload": None,
                "reasons": [f"need >=2 runs to compare, have {len(entries)}"]}
    prev, curr = entries[-2], entries[-1]
    p = {r["id"]: r["pass"] for r in prev["results"]}
    c = {r["id"]: r["pass"] for r in curr["results"]}
    return {"state": "SUCCESS", "payload": {
        "prev_verdict": prev["verdict"], "curr_verdict": curr["verdict"],
        "fixed": sorted(k for k in p if p[k] is False and c.get(k) is True),
        "regressed": sorted(k for k in p if p[k] is True and c.get(k) is False),
        "new": sorted(k for k in c if k not in p),
        "dropped": sorted(k for k in p if k not in c)}, "reasons": []}


# ---------------- self-test ----------------

def _selftest() -> int:
    import tempfile
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name, detail)
        ok = ok and cond

    tmp = tempfile.mkdtemp(prefix="evalgate_")
    cases = [
        {"id": "c1", "required": ["结论", "证据"], "forbidden": ["TODO", "占位"],
         "min_len": 10, "max_len": 200, "sections": ["## 结论"]},
        {"id": "c2", "json": True},
    ]
    good = {"c1": "## 结论\n先说结论：方案可行。\n证据：实勘 3/3。\n更多细节。",
            "c2": '{"ok": true}'}
    bad = {"c1": "TODO 占位符还没填",
           "c2": '{"ok": }\n\nbroken'}

    r = run(cases, good)
    check("T1 good passes", r["verdict"] == "PASS" and r["n_pass"] == 2)

    r = run(cases, bad)
    c1 = next(x for x in r["results"] if x["id"] == "c1")
    c2 = next(x for x in r["results"] if x["id"] == "c2")
    check("T2 violations caught", r["verdict"] == "FAIL"
          and any("missing required" in x for x in c1["reasons"])
          and any("not valid json" in x for x in c2["reasons"]),
          str(c1["reasons"] + c2["reasons"]))

    log = os.path.join(tmp, "runs.jsonl")
    run(cases, bad, log)            # run 1: FAIL
    run(cases, good, log)           # run 2: PASS
    d = compare(log)
    check("T3 compare log", d["state"] == "SUCCESS"
          and d["payload"]["fixed"] == ["c1", "c2"]
          and d["payload"]["regressed"] == []
          and d["payload"]["curr_verdict"] == "PASS", str(d["payload"]))

    run(cases, good, log)           # run 3
    cases2 = [dict(c) for c in cases]
    cases2[0]["required"] = ["结论", "证据", "新增要求"]
    run(cases2, good, log)          # run 4: c1 regresses
    d = compare(log)
    check("T4 regression detected", d["payload"]["regressed"] == ["c1"]
          and d["payload"]["curr_verdict"] == "FAIL", str(d["payload"]))

    d = compare(os.path.join(tmp, "none.jsonl"))
    check("T5 explicit failure no log", d["state"] == "EXPLICIT_FAILURE")

    print("-" * 40)
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a:
        sys.exit(_selftest())
    if len(a) >= 3 and a[0] == "run":
        cases = json.load(open(a[1], encoding="utf-8"))
        outputs = json.load(open(a[2], encoding="utf-8"))
        log = a[a.index("--log") + 1] if "--log" in a else None
        out = run(cases, outputs, log)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(0 if out["verdict"] == "PASS" else 1)
    if len(a) >= 2 and a[0] == "compare":
        print(json.dumps(compare(a[1]), ensure_ascii=False, indent=1))
        sys.exit(0)
    print(__doc__)
    sys.exit(1)
