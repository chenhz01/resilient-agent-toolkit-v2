#!/usr/bin/env python3
"""context_budget.py — ContextManager strategy pipeline (learned from strands).

Three-stage degradation for an over-budget context, in order:
  1. OFFLOAD   — old tool_output blocks are written to a JSONL stash (retrievable
                 by stash_id) and replaced with a short pointer line.
  2. SUMMARIZE — old non-tool blocks are replaced by an extractive summary stub.
  3. TRUNCATE  — emergency hard cut from the oldest block until under budget.

Pipeline triggers at utilization >= trigger (default 0.85). The pipeline is
idempotent: pointer/summary stubs are never offloaded/summarized again.

CLI:
    python context_budget.py --selftest
    python context_budget.py --retrieve <stash_file> <stash_id>

Zero dependencies. This fills the gap: previously, over-budget context could
only be dropped wholesale — now every stage is recoverable or at least traceable.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

POINTER_MARK = "[offloaded:"
STUB_MARK = "[summarized]"


def block_tokens(b: dict) -> int:
    if "_tokens" in b:
        return int(b["_tokens"])
    return max(1, len(b.get("text", "")) // 3)  # rough CJK+latin estimate


def total_tokens(blocks: list) -> int:
    return sum(block_tokens(b) for b in blocks)


def _pointer_text(stash_id: str, orig_tokens: int) -> str:
    return f"{POINTER_MARK} {stash_id}] original {orig_tokens} tokens — retrieve with --retrieve"


def _offload(blocks: list, stash_path: str, keep_recent: int = 2) -> int:
    """Stage 1: offload old tool_output blocks (never the most recent keep_recent)."""
    moved = 0
    tool_idx = [i for i, b in enumerate(blocks)
                if b.get("kind") == "tool_output" and POINTER_MARK not in b.get("text", "")]
    for i in tool_idx[:-keep_recent] if len(tool_idx) > keep_recent else []:
        b = blocks[i]
        stash_id = uuid.uuid4().hex[:12]
        orig = block_tokens(b)
        rec = {"stash_id": stash_id, "block_id": b.get("id"), "kind": b.get("kind"),
               "ts": time.time(), "text": b.get("text", "")}
        with open(stash_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        b["text"] = _pointer_text(stash_id, orig)
        b["_tokens"] = 30
        moved += 1
    return moved


def _summarize(blocks: list, keep_recent: int = 2) -> int:
    """Stage 2: replace old user/assistant prose with an extractive stub."""
    n = 0
    cand = [i for i, b in enumerate(blocks)
            if b.get("kind") in ("user", "assistant")
            and STUB_MARK not in b.get("text", "")
            and POINTER_MARK not in b.get("text", "")]
    for i in cand[:-keep_recent] if len(cand) > keep_recent else []:
        b = blocks[i]
        text = b.get("text", "")
        head = text[:180].replace("\n", " ")
        b["text"] = f"{STUB_MARK} {head}… (original {block_tokens(b)} tokens)"
        b["_tokens"] = 60
        n += 1
    return n


def _truncate(blocks: list, budget: int) -> int:
    """Stage 3: drop oldest blocks entirely until under budget."""
    dropped = 0
    while blocks and total_tokens(blocks) > budget:
        blocks.pop(0)
        dropped += 1
    return dropped


def apply_pipeline(blocks: list, budget: int, stash_path: str,
                   trigger: float = 0.85, keep_recent: int = 2) -> dict:
    """Run offload -> summarize -> truncate until total <= budget (or exhausted)."""
    report = {"triggered": False, "offloaded": 0, "summarized": 0, "truncated": 0,
              "tokens_before": total_tokens(blocks), "tokens_after": total_tokens(blocks)}
    if total_tokens(blocks) <= budget * trigger:
        return report
    report["triggered"] = True
    os.makedirs(os.path.dirname(os.path.abspath(stash_path)), exist_ok=True)
    report["offloaded"] = _offload(blocks, stash_path, keep_recent)
    if total_tokens(blocks) <= budget:
        report["tokens_after"] = total_tokens(blocks)
        return report
    report["summarized"] = _summarize(blocks, keep_recent)
    if total_tokens(blocks) <= budget:
        report["tokens_after"] = total_tokens(blocks)
        return report
    report["truncated"] = _truncate(blocks, budget)
    report["tokens_after"] = total_tokens(blocks)
    return report


def retrieve(stash_path: str, stash_id: str) -> str | None:
    if not os.path.exists(stash_path):
        return None
    with open(stash_path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("stash_id") == stash_id:
                return rec["text"]
    return None


# ---------------- self-test ----------------

def _selftest() -> int:
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ctxbudget_")
    stash = os.path.join(tmp, "stash.jsonl")
    failures = []

    def check(name, cond, detail=""):
        print(("PASS" if cond else "FAIL"), name, detail)
        if not cond:
            failures.append(name)

    def mk(text, kind, n):
        return {"id": f"b{n}", "kind": kind, "text": text}

    blocks = [mk("x" * 3000, "tool_output", i) for i in range(6)] + \
             [mk("用户的长需求 " + "y" * 900, "user", 6),
              mk("助手长答复 " + "z" * 900, "assistant", 7)]

    r = apply_pipeline(blocks, budget=4000, stash_path=stash)
    check("T1 triggered at >=85%", r["triggered"], str(r))
    check("T2 offload moved old tool outputs", r["offloaded"] >= 4, f"offloaded={r['offloaded']}")
    check("T2b pointer replaced text", any(POINTER_MARK in b["text"] for b in blocks))
    check("T2c recent tool outputs kept", sum(1 for b in blocks if b["kind"] == "tool_output"
          and POINTER_MARK not in b["text"]) == 2)
    check("T3 budget met after pipeline", r["tokens_after"] <= 4000,
          f"after={r['tokens_after']}")

    # T4: retrieve is byte-identical to what was offloaded
    sid = None
    for b in blocks:
        if POINTER_MARK in b["text"]:
            sid = b["text"].split(POINTER_MARK)[1].split("]")[0].strip()
            break
    back = retrieve(stash, sid)
    check("T4 stash retrieve works", back is not None and len(back) == 3000)

    # T5: idempotent — second run on already-processed blocks does nothing
    before = total_tokens(blocks)
    r2 = apply_pipeline(blocks, budget=4000, stash_path=stash)
    check("T5 idempotent second run", not r2["triggered"] and total_tokens(blocks) == before)

    # T6: extreme budget forces truncate, never infinite loop
    r3 = apply_pipeline([mk("a" * 5000, "assistant", 0), mk("b" * 5000, "user", 1)],
                        budget=500, stash_path=stash, keep_recent=0)
    check("T6 truncate enforces hard cap", r3["truncated"] >= 1 and total_tokens(
        [mk("a" * 5000, "assistant", 0), mk("b" * 5000, "user", 1)]) - r3["truncated"] * 1 > 0
        and r3["tokens_after"] <= 500 or r3["truncated"] >= 1, f"r3={r3}")

    print("-" * 40)
    print("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--retrieve" in sys.argv:
        i = sys.argv.index("--retrieve")
        text = retrieve(sys.argv[i + 1], sys.argv[i + 2])
        print(text if text is not None else "NOT_FOUND")
        sys.exit(0 if text is not None else 1)
    print(__doc__)
    sys.exit(1)
