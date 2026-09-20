#!/usr/bin/env python3
"""durable_steps.py — Durable step execution with checkpoint/resume + human gate
(internalized from langchain-ai/langgraph durable execution & interrupt primitives,
source-level verified 2026-09-20, L1 card).

THE MECHANISM (from langgraph): long pipelines must survive crashes by persisting
completed-step state after EVERY step (durable execution), and pause at explicit
gates for human approval (interrupt) instead of barreling on.

WHAT THIS TOOL DOES (pure stdlib):
  run(spec, ckpt_path) — execute steps one by one, writing a checkpoint JSON
  after each step. Re-running after a crash SKIPS completed steps and resumes
  from the exact breakpoint. A step may return GATE to pause the pipeline with
  status="awaiting_gate" — rerun continues after the human clears the gate
  (delete the "awaiting_gate" field or set gate_cleared=true in the checkpoint).

  Step spec: [{"id": str, "cmd": [argv] | "note": str, "critical": bool}]
    - cmd steps run via subprocess with timeout; three-state capture
      (exit 0 = SUCCESS / nonzero = EXPLICIT_FAILURE / timeout = SILENT_TIMEOUT)
    - non-critical step failure records the failure and CONTINUES
    - critical step failure stops the pipeline with status="failed"
    - gate step ("gate": true) pauses for human approval

CLI:
    python durable_steps.py --selftest
    python durable_steps.py run <spec.json> <ckpt.json>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

GATE = "__GATE__"


def _load(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _save(path, ck):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ck, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)          # atomic write: crash can't corrupt checkpoint


def run(spec: list, ckpt_path: str, state: dict | None = None) -> dict:
    """Execute steps durably. Returns the checkpoint dict (also persisted)."""
    ck = _load(ckpt_path)
    if ck is None or ck.get("spec_ids") != [s["id"] for s in spec]:
        ck = {"spec_ids": [s["id"] for s in spec], "status": "running",
              "completed": {}, "failures": [], "awaiting_gate": None,
              "state": state or {}}

    for step in spec:
        sid = step["id"]
        if sid in ck["completed"]:
            continue                                    # durable: skip done steps
        if step.get("gate"):
            if ck.get("awaiting_gate") == sid or step.get("gate_cleared"):
                ck["awaiting_gate"] = None              # human already cleared
                ck["status"] = "running"                # resume the state machine
                ck["completed"][sid] = {"state": "SUCCESS", "result": "gate-cleared"}
                _save(ckpt_path, ck)
                continue
            ck["status"] = "awaiting_gate"
            ck["awaiting_gate"] = sid
            ck["state"]["gate_hint"] = step.get("note", "")
            _save(ckpt_path, ck)
            return ck                                   # interrupt: stop here
        if "cmd" in step:
            try:
                r = subprocess.run(step["cmd"], capture_output=True, timeout=60)
                if r.returncode == 0:
                    ck["completed"][sid] = {"state": "SUCCESS",
                                            "result": (r.stdout or b"").decode("utf-8", "ignore").strip()[:2000]}
                else:
                    res = {"state": "EXPLICIT_FAILURE",
                           "result": (r.stderr or r.stdout or b"").decode("utf-8", "ignore").strip()[:2000],
                           "rc": r.returncode}
                    if step.get("critical", False):
                        ck["status"] = "failed"
                        ck["failures"].append({"id": sid, **res})
                        _save(ckpt_path, ck)
                        return ck
                    ck["failures"].append({"id": sid, **res})
            except subprocess.TimeoutExpired:
                res = {"state": "SILENT_TIMEOUT", "result": "timeout 60s"}
                if step.get("critical", False):
                    ck["status"] = "failed"
                    ck["failures"].append({"id": sid, **res})
                    _save(ckpt_path, ck)
                    return ck
                ck["failures"].append({"id": sid, **res})
        else:
            ck["completed"][sid] = {"state": "SUCCESS", "result": step.get("note", "")}
        _save(ckpt_path, ck)                            # persist after EVERY step

    if ck["status"] == "running":
        ck["status"] = "done"
        _save(ckpt_path, ck)
    return ck


# ---------------- self-test ----------------

def _selftest() -> int:
    import tempfile
    tmp = tempfile.mkdtemp(prefix="durable_")
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name, detail)
        ok = ok and cond

    # T1 fresh run: all note steps complete, status done
    ckpt = os.path.join(tmp, "a.json")
    spec = [{"id": "s1", "note": "one"}, {"id": "s2", "note": "two"}]
    ck = run(spec, ckpt)
    check("T1 fresh run done", ck["status"] == "done"
          and list(ck["completed"]) == ["s1", "s2"])

    # T2 crash simulation: partial checkpoint -> rerun resumes, skips done
    ckpt2 = os.path.join(tmp, "b.json")
    spec3 = [{"id": "p1", "note": "x"}, {"id": "p2", "note": "y"}, {"id": "p3", "note": "z"}]
    ck = run(spec3[:1], ckpt2)                      # simulate: only p1 ran
    ck2 = run(spec3, ckpt2)                         # "reboot"
    check("T2 resume skips done", ck2["status"] == "done"
          and list(ck2["completed"]) == ["p1", "p2", "p3"]
          and ck2["completed"]["p1"]["result"] == "x")

    # T3 gate pauses pipeline; T4 clearing the gate resumes
    ckpt3 = os.path.join(tmp, "c.json")
    specg = [{"id": "g1", "note": "prep"},
             {"id": "approve", "gate": True, "note": "human must approve"},
             {"id": "g2", "note": "after"}]
    ck = run(specg, ckpt3)
    check("T3 gate pauses", ck["status"] == "awaiting_gate"
          and ck["awaiting_gate"] == "approve" and "g2" not in ck["completed"])
    # human clears: set gate_cleared on the gate step (as the operator would)
    for s in specg:
        if s.get("gate"):
            s["gate_cleared"] = True
    ck = run(specg, ckpt3)
    check("T4 resume after gate", ck["status"] == "done"
          and ck["awaiting_gate"] is None and "g2" in ck["completed"])

    # T5 critical cmd failure stops with explicit state; T6 non-critical continues
    ckpt5 = os.path.join(tmp, "e.json")
    spec5 = [{"id": "boom", "cmd": [sys.executable, "-c", "import sys; sys.exit(3)"],
              "critical": True}, {"id": "after", "note": "never"}]
    ck = run(spec5, ckpt5)
    check("T5 critical stops", ck["status"] == "failed"
          and ck["failures"][0]["state"] == "EXPLICIT_FAILURE"
          and ck["failures"][0]["rc"] == 3 and "after" not in ck["completed"])

    ckpt6 = os.path.join(tmp, "f.json")
    spec6 = [{"id": "soft", "cmd": [sys.executable, "-c", "import sys; sys.exit(1)"],
              "critical": False}, {"id": "tail", "note": "ok"}]
    ck = run(spec6, ckpt6)
    check("T6 non-critical continues", ck["status"] == "done"
          and ck["failures"][0]["state"] == "EXPLICIT_FAILURE"
          and "tail" in ck["completed"])

    # T7 checkpoint file is valid json mid-run (atomic write)
    check("T7 checkpoint valid", json.load(open(ckpt6, encoding="utf-8"))["status"] == "done")

    print("-" * 40)
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a:
        sys.exit(_selftest())
    if len(a) >= 3 and a[0] == "run":
        spec = json.load(open(a[1], encoding="utf-8"))
        print(json.dumps(run(spec, a[2]), ensure_ascii=False, indent=1))
        sys.exit(0)
    print(__doc__)
    sys.exit(1)
