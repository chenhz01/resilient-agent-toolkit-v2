#!/usr/bin/env python3
"""ir_render.py — Typed IR -> validated -> deterministic diagram compile
(internalized from tt-a1i/archify, source-level verified 2026-09-20, L2 card).

THE MECHANISM (from archify): the agent NEVER draws. It emits a typed JSON IR;
a schema validates it; a compiler renders deterministically. Same IR in ->
byte-identical SVG out, forever. Plus delta(): compare two IRs and show
added/removed/changed instead of eyeballing two images.

WHY IT MATTERS: hand-drawn or model-drawn architecture diagrams are
unverifiable and unstable. This tool makes the diagram a pure function of the IR.

IR SCHEMA (nodes+edges, lane-based layout):
  {"title": str,
   "lanes": ["lane-A", "lane-B", ...],              # ordered columns
   "nodes": [{"id", "label", "lane", "row"?}],      # row optional (auto-topo)
   "edges": [{"from": id, "to": id, "label"?}]}
Validation failures (dup id, bad lane, dangling edge, unknown key) are
EXPLICIT_FAILURE with per-item reasons — a broken IR never renders silently.

CLI:
    python ir_render.py --selftest
    python ir_render.py render <ir.json> [--out out.svg]
    python ir_render.py delta <old.json> <new.json>
"""
from __future__ import annotations

import json
import os
import re
import sys

_NODE_KEYS = {"id", "label", "lane", "row"}
_EDGE_KEYS = {"from", "to", "label"}


def validate(ir: dict) -> dict:
    """Schema-validate the IR. SUCCESS payload = {"ir": ir}; else EXPLICIT_FAILURE."""
    reasons = []
    if not isinstance(ir, dict):
        return {"state": "EXPLICIT_FAILURE", "payload": None,
                "reasons": ["IR must be an object"]}
    if not isinstance(ir.get("lanes"), list) or not ir.get("lanes"):
        reasons.append("lanes must be a non-empty array")
    lanes = ir.get("lanes") or []
    nodes = ir.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        reasons.append("nodes must be a non-empty array")
        nodes = []
    ids = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            reasons.append(f"node missing id: {n!r}")
            continue
        if nid in ids:
            reasons.append(f"duplicate node id: {nid}")
        ids.add(nid)
        if n.get("lane") not in lanes:
            reasons.append(f"node '{nid}' lane '{n.get('lane')}' not in lanes")
        extra = set(n) - _NODE_KEYS
        if extra:
            reasons.append(f"node '{nid}' unknown keys: {sorted(extra)}")
    for e in ir.get("edges") or []:
        if e.get("from") not in ids:
            reasons.append(f"edge from '{e.get('from')}' references unknown node")
        if e.get("to") not in ids:
            reasons.append(f"edge to '{e.get('to')}' references unknown node")
        extra = set(e) - _EDGE_KEYS
        if extra:
            reasons.append(f"edge {e.get('from')}->{e.get('to')} unknown keys: {sorted(extra)}")
    if reasons:
        return {"state": "EXPLICIT_FAILURE", "payload": None, "reasons": reasons}
    return {"state": "SUCCESS", "payload": {"ir": ir}, "reasons": []}


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def compile_svg(ir: dict) -> str:
    """Deterministic SVG compile. Layout: lane -> x column (declared order),
    node -> y by (row if given, else declaration order). Stable sort everywhere."""
    lanes = ir["lanes"]
    nodes = sorted(ir["nodes"], key=lambda n: (lanes.index(n["lane"]),
                                               n.get("row", 10**9), n["id"]))
    col_x = {ln: 60 + i * 240 for i, ln in enumerate(lanes)}
    col_count = {ln: 0 for ln in lanes}
    pos = {}
    for n in nodes:
        pos[n["id"]] = (col_x[n["lane"]], 60 + col_count[n["lane"]] * 90)
        col_count[n["lane"]] += 1
    W = 60 * 2 + (len(lanes)) * 240
    H = 60 * 2 + max(col_count.values(), default=1) * 90
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'width="{W}" height="{H}" font-family="sans-serif">',
             _esc(ir.get("title", "")) and
             f'<text x="{W//2}" y="28" text-anchor="middle" font-size="16" fill="#888">{_esc(ir.get("title", ""))}</text>']
    # edges first (under nodes), deterministic order
    node_set = {n["id"] for n in nodes}
    for e in sorted(ir.get("edges") or [], key=lambda e: (e["from"], e["to"])):
        if e["from"] not in node_set or e["to"] not in node_set:
            continue
        x0, y0 = pos[e["from"]]; x1, y1 = pos[e["to"]]
        mx = (x0 + 130 + x1) / 2
        parts.append(f'<path d="M {x0+130} {y0+25} C {mx} {y0+25}, {mx} {y1+25}, '
                     f'{x1} {y1+25}" fill="none" stroke="#666" stroke-width="1.5" '
                     f'marker-end="url(#arr)"/>')
        if e.get("label"):
            parts.append(f'<text x="{mx}" y="{(y0+y1)//2+20}" text-anchor="middle" '
                         f'font-size="11" fill="#999">{_esc(e["label"])}</text>')
    for n in nodes:
        x, y = pos[n["id"]]
        parts.append(f'<rect x="{x}" y="{y}" width="130" height="50" rx="8" '
                     f'fill="#161c2c" stroke="#4a9eff" stroke-width="1.5"/>')
        parts.append(f'<text x="{x+65}" y="{y+29}" text-anchor="middle" '
                     f'font-size="13" fill="#e8ecf4">{_esc(n["label"])}</text>')
    parts.append('<defs><marker id="arr" markerWidth="8" markerHeight="8" '
                 'refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" '
                 'fill="none" stroke="#666"/></marker></defs>')
    parts.append("</svg>")
    return "\n".join(parts)


def delta(old: dict, new: dict) -> dict:
    """IR-level diff: added/removed/changed nodes and edges."""
    on = {n["id"]: n for n in old.get("nodes", [])}
    nn = {n["id"]: n for n in new.get("nodes", [])}
    oe = {(e["from"], e["to"], e.get("label")) for e in old.get("edges", [])}
    ne = {(e["from"], e["to"], e.get("label")) for e in new.get("edges", [])}
    return {
        "nodes_added": sorted(set(nn) - set(on)),
        "nodes_removed": sorted(set(on) - set(nn)),
        "nodes_changed": sorted(k for k in set(on) & set(nn) if on[k] != nn[k]),
        "edges_added": [list(e) for e in sorted(ne - oe)],
        "edges_removed": [list(e) for e in sorted(oe - ne)],
    }


def render(ir: dict) -> dict:
    v = validate(ir)
    if v["state"] != "SUCCESS":
        return v
    return {"state": "SUCCESS", "payload": {"svg": compile_svg(ir)}, "reasons": []}


# ---------------- self-test ----------------

def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name, detail)
        ok = ok and cond

    ir = {"title": "skill-graph", "lanes": ["core", "infra"],
          "nodes": [{"id": "a", "label": "router", "lane": "core"},
                    {"id": "b", "label": "pruner", "lane": "core", "row": 1},
                    {"id": "c", "label": "git", "lane": "infra"}],
          "edges": [{"from": "a", "to": "b", "label": "calls"},
                    {"from": "b", "to": "c"}]}

    v = validate(ir)
    check("T1 valid IR", v["state"] == "SUCCESS")

    s1 = compile_svg(ir)
    s2 = compile_svg(ir)
    check("T2 deterministic byte-identical", s1 == s2)

    bad = {"title": "x", "lanes": ["core"],
           "nodes": [{"id": "a", "label": "a", "lane": "core"},
                     {"id": "a", "label": "dup", "lane": "nowhere", "bogus": 1}],
           "edges": [{"from": "a", "to": "ghost"}]}
    v = validate(bad)
    check("T3 explicit failures", v["state"] == "EXPLICIT_FAILURE"
          and len(v["reasons"]) >= 3
          and any("duplicate" in r for r in v["reasons"])
          and any("unknown node" in r for r in v["reasons"]), str(v["reasons"]))

    d = delta(ir, {**ir, "nodes": ir["nodes"] + [{"id": "d", "label": "new", "lane": "infra"}],
                   "edges": ir["edges"] + [{"from": "c", "to": "d"}]})
    d2 = delta(ir, {**ir, "edges": ir["edges"][:-1]})  # edge removed in new
    check("T4 delta added", d["nodes_added"] == ["d"]
          and d["edges_added"] == [["c", "d", None]]
          and d2["edges_removed"] == [["b", "c", None]])
    check("T5 no false change", d["nodes_changed"] == [] and d["nodes_removed"] == [])

    check("T6 svg is svg", s1.startswith("<svg") and s1.endswith("</svg>")
          and "router" in s1 and "marker" in s1)

    print("-" * 40)
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a:
        sys.exit(_selftest())
    if len(a) >= 2 and a[0] == "render":
        ir = json.load(open(a[1], encoding="utf-8"))
        out = render(ir)
        if out["state"] == "SUCCESS" and "--out" in a:
            with open(a[a.index("--out") + 1], "w", encoding="utf-8", newline="\n") as f:
                f.write(out["payload"]["svg"])
            out["payload"]["svg"] = f"({len(out['payload']['svg'])} chars written)"
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(0 if out["state"] == "SUCCESS" else 1)
    if len(a) >= 3 and a[0] == "delta":
        print(json.dumps(delta(json.load(open(a[1], encoding="utf-8")),
                               json.load(open(a[2], encoding="utf-8"))),
                         ensure_ascii=False, indent=1))
        sys.exit(0)
    print(__doc__)
    sys.exit(1)
