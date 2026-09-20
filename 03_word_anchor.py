#!/usr/bin/env python3
"""word_anchor.py — Word-anchored timeline IR + compiler for AutoMV / short-drama
(internalized from hypit-ai/hypit SVML, source-level verified 2026-09-20).

THE MECHANISM (from hypit): never hardcode seconds in the authoring layer.
Every visual event (subtitle, broll, title, effect) anchors to WORDS of the
voiceover — hypit does this with `@{~tag}...@{/tag}` spans + whisperx:SemanticTake
word-level timeline, and binds ALL tracks (sound/performance/media/caption) to
that one timeline. Seconds are derived only at compile time.

WHAT THIS TOOL DOES (pure stdlib, no hypit runtime):
  1. parse_script(text)  — extract @{~tag}...@{/tag} spans and || pauses from
     a script text, like hypit's inline anchor markup.
  2. resolve(words, a)   — resolve one anchor to [t0, t1] seconds against a
     word-level transcript (WhisperX format: [{"word","start","end"}, ...]).
     Anchor kinds: tag-span | word-text (nth occurrence) | word-index,
     each optionally +offset_words / +offset_s / -tail_s.
  3. compile(words, spec)— compile a declarative spec (JSON, the .wa.json IR)
     into a concrete per-track cue sheet in absolute seconds.
     SUCCESS / EXPLICIT_FAILURE follows the three-state contract (our
     01_silence_hunter): unknown anchor word, overlapping same-track cues, or
     negative duration are EXPLICIT failures with reasons — never silent.

WHY IT MATTERS: AutoMV/short-drama currently has one-off scripts; this is the
missing reusable IR. Re-timing = swap the transcript, spec unchanged.

CLI:
    python word_anchor.py --selftest
    python word_anchor.py compile <transcript.json> <spec.json> [--out cue.json]
"""
from __future__ import annotations

import json
import os
import re
import sys

# ---------------- three-state contract (mirrors 01_silence_hunter) ----------------

def _success(payload):
    return {"state": "SUCCESS", "payload": payload, "reasons": []}

def _failure(reasons):
    return {"state": "EXPLICIT_FAILURE", "payload": None, "reasons": reasons}

# ---------------- script text parsing (hypit inline markup) ----------------

_SPAN_OPEN = re.compile(r"@\{~([\w\u4e00-\u9fff-]+)\}")
_SPAN_CLOSE = re.compile(r"@\{/([\w\u4e00-\u9fff-]+)\}")
_PAUSE = "||"


def parse_script(text: str) -> dict:
    """Extract tagged spans and pauses from script text.

    Returns {"spans": {tag: {"order": n}}, "pauses": int, "clean": str}
    `clean` is the text with @{~x}/@{/x} markers stripped (pauses kept).
    Unbalanced spans are dropped silently here — compile() is where
    malformed anchors become explicit failures, not the text scanner.
    """
    spans: dict = {}
    open_tags: list = []

    def _open(m):
        tag = m.group(1)
        if tag not in spans:
            spans[tag] = {"order": len(spans)}
        open_tags.append(tag)
        return ""

    def _close(m):
        tag = m.group(1)
        if open_tags and open_tags[-1] == tag:
            open_tags.pop()
        return ""

    clean = _SPAN_OPEN.sub(_open, text)
    clean = _SPAN_CLOSE.sub(_close, clean)
    pauses = clean.count(_PAUSE)
    clean = re.sub(r"\s*\|\|\s*", " ", clean).strip()
    return {"spans": spans, "pauses": pauses, "clean": clean}

# ---------------- anchor resolution ----------------

def _norm(w: str) -> str:
    """Word normalization for matching: lowercase, strip punctuation."""
    return re.sub(r"[\s\W_]+", "", w.lower(), flags=re.UNICODE)


def resolve(words: list, a: dict) -> dict:
    """Resolve one anchor to {"start": s, "end": e} seconds. Raises ValueError
    with an explicit reason on any unresolvable anchor (caller aggregates)."""
    kind = a.get("kind")
    if kind == "tag":
        raise ValueError("tag anchors are resolved by compile() via parsed spans")
    if kind == "index":
        i = a["index"]
        if not (0 <= i < len(words)):
            raise ValueError(f"index {i} out of range (0..{len(words)-1})")
        i0 = i1 = i
    elif kind == "word":
        target = _norm(a["word"])
        if not target:
            raise ValueError("empty word in anchor")
        occ = a.get("occurrence", 1)
        hits = [i for i, w in enumerate(words) if _norm(w["word"]) == target]
        if not hits:
            raise ValueError(f"word '{a['word']}' not found in transcript")
        if occ < 1 or occ > len(hits):
            raise ValueError(
                f"occurrence {occ} of '{a['word']}' not found (only {len(hits)})")
        i0 = i1 = hits[occ - 1]
    else:
        raise ValueError(f"unknown anchor kind: {kind!r}")

    ow = a.get("offset_words", 0)
    i1 = max(0, min(len(words) - 1, i1 + max(0, ow)))  # offset extends right edge
    start = words[i0]["start"] + a.get("offset_s", 0.0)
    end = words[i1]["end"] + a.get("offset_s", 0.0) - a.get("tail_s", 0.0)
    return {"start": round(start, 3), "end": round(end, 3)}

# ---------------- compile: .wa.json spec -> cue sheet ----------------

def compile_timeline(words: list, spec: dict, spans: dict | None = None) -> dict:
    """Compile a declarative spec into a per-track cue sheet.

    spec = {
      "tracks": {
        "<track>": [
          {"id": "cue-1", "at": {...anchor...}},                       # point->word duration
          {"id": "cue-2", "span": "tag-name"},                         # parsed script span
          {"id": "cue-3", "from": {...}, "to": {...}}                  # explicit edges
        ], ...
      }
    }
    Anchor dicts: {"kind":"word","word":"creatinine","occurrence":1,
                   "offset_words":2, "offset_s":0.3, "tail_s":0.0}
    """
    spans = spans if spans is not None else {}
    reasons: list = []
    cue_sheet: dict = {}

    for track, cues in (spec.get("tracks") or {}).items():
        resolved: list = []
        for c in cues:
            cid = c.get("id", f"{track}-{len(resolved)+1}")
            try:
                if "span" in c:
                    tag = c["span"]
                    if tag not in spans:
                        raise ValueError(f"span '{tag}' not present in script anchors")
                    t0, t1 = spans[tag]["start"], spans[tag]["end"]
                elif "from" in c and "to" in c:
                    r0 = resolve(words, c["from"])
                    r1 = resolve(words, c["to"])
                    t0, t1 = r0["start"], r1["end"]
                elif "at" in c:
                    r = resolve(words, c["at"])
                    t0, t1 = r["start"], r["end"]
                else:
                    raise ValueError("cue needs one of: span / from+to / at")
            except (ValueError, KeyError) as e:
                reasons.append(f"[{track}/{cid}] {e}")
                continue
            if t1 < t0:
                reasons.append(f"[{track}/{cid}] negative duration ({t0}->{t1})")
                continue
            dur = round(t1 - t0, 3)
            if "min_dur" in c and dur < c["min_dur"]:
                # stretch the tail, never shrink the anchor (keeps lip-sync)
                t1 = round(t0 + c["min_dur"], 3)
            resolved.append({"id": cid, "start": t0, "end": t1,
                             "dur": round(t1 - t0, 3)})

        # overlap check within the same track (visual layers must not stack)
        resolved.sort(key=lambda x: x["start"])
        for p, q in zip(resolved, resolved[1:]):
            if q["start"] < p["end"] - 1e-6:
                reasons.append(
                    f"[{track}] overlap: '{p['id']}'({p['start']}-{p['end']}) "
                    f"vs '{q['id']}'({q['start']}-{q['end']})")
        cue_sheet[track] = resolved

    if reasons:
        return _failure(reasons)
    total = max((w["end"] for w in words), default=0.0)
    return _success({"cue_sheet": cue_sheet, "total_s": round(total, 3),
                     "n_cues": sum(len(v) for v in cue_sheet.values())})

# ---------------- self-test ----------------

def _selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name, detail)
        ok = ok and cond

    # fixture: WhisperX-style word transcript for a Chinese MV verse
    words = [
        {"word": "夜色", "start": 0.00, "end": 0.40},
        {"word": "落在", "start": 0.40, "end": 0.72},
        {"word": "琴键", "start": 0.72, "end": 1.10},
        {"word": "你的", "start": 1.30, "end": 1.55},
        {"word": "名字", "start": 1.55, "end": 1.95},
        {"word": "落在", "start": 1.95, "end": 2.27},
        {"word": "窗前", "start": 2.27, "end": 2.70},
    ]

    # T1 script parsing: spans + pauses + clean text
    sc = parse_script("@{~hook}夜色 落在 琴键 || 你的 @{~name}名字@{/name} @{/hook}落在 窗前")
    check("T1 span+pause parse", list(sc["spans"]) == ["hook", "name"]
          and sc["pauses"] == 1 and "||" not in sc["clean"]
          and "@{" not in sc["clean"], sc["clean"])

    # T2 word anchor with occurrence (second 落在)
    r = resolve(words, {"kind": "word", "word": "落在", "occurrence": 2})
    check("T2 occurrence anchor", r == {"start": 1.95, "end": 2.27}, str(r))

    # T3 offsets extend right edge + tail: 琴键(idx2)+2词=名字(idx4, end 1.95)
    r = resolve(words, {"kind": "word", "word": "琴键", "offset_words": 2,
                        "offset_s": 0.1, "tail_s": 0.05})
    check("T3 offsets", r == {"start": 0.82, "end": 2.0}, str(r))

    # T4 span edge from index anchors
    r = resolve(words, {"kind": "index", "index": 3})
    check("T4 index anchor", r == {"start": 1.30, "end": 1.55}, str(r))

    # T5 full compile: subtitle/broll/title from words + spans, zero hardcoded t
    spec = {"tracks": {
        "subtitle": [
            {"id": "sub-1", "from": {"kind": "word", "word": "夜色"},
             "to": {"kind": "word", "word": "琴键"}, "min_dur": 0.6},
            {"id": "sub-2", "from": {"kind": "word", "word": "你的"},
             "to": {"kind": "word", "word": "窗前"}},
        ],
        "broll": [
            {"id": "broll-keys", "span": "hook", "min_dur": 1.0},
        ],
        "title": [
            {"id": "title", "at": {"kind": "word", "word": "名字", "offset_s": -0.2}},
        ],
    }}
    out = compile_timeline(words, spec, spans={
        "hook": {"start": 0.0, "end": 1.10}, "name": {"start": 1.55, "end": 1.95}})
    cs = out["payload"]["cue_sheet"]
    check("T5 compile SUCCESS", out["state"] == "SUCCESS"
          and out["payload"]["n_cues"] == 4, str(out.get("reasons")))
    check("T5b min_dur stretch", cs["subtitle"][0]["dur"] >= 0.6,
          str(cs["subtitle"][0]))
    check("T5c span cue window", cs["broll"][0]["start"] == 0.0
          and cs["broll"][0]["end"] >= 1.0, str(cs["broll"][0]))
    check("T5d title anchored", abs(cs["title"][0]["start"] - 1.35) < 1e-6,
          str(cs["title"][0]))

    # T6 explicit failures: unknown word / bad occurrence / bad span / overlap
    out = compile_timeline(words, {"tracks": {"a": [
        {"id": "x", "at": {"kind": "word", "word": "不存在的词"}},
        {"id": "y", "span": "no-tag"},
        {"id": "z", "at": {"kind": "word", "word": "落在", "occurrence": 9}},
    ]}})
    check("T6 explicit failure", out["state"] == "EXPLICIT_FAILURE"
          and len(out["reasons"]) == 3, str(out["reasons"]))
    out = compile_timeline(words, {"tracks": {"a": [
        {"id": "p", "from": {"kind": "word", "word": "夜色"},
         "to": {"kind": "word", "word": "窗前"}},
        {"id": "q", "at": {"kind": "word", "word": "名字"}},
    ]}})
    check("T6b overlap caught", out["state"] == "EXPLICIT_FAILURE"
          and any("overlap" in r for r in out["reasons"]), str(out["reasons"]))

    # T7 idempotence: same inputs -> byte-identical payload
    a = compile_timeline(words, spec, spans={
        "hook": {"start": 0.0, "end": 1.10}, "name": {"start": 1.55, "end": 1.95}})
    b = compile_timeline(words, spec, spans={
        "hook": {"start": 0.0, "end": 1.10}, "name": {"start": 1.55, "end": 1.95}})
    check("T7 deterministic", a["payload"] == b["payload"])

    print("-" * 40)
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a:
        sys.exit(_selftest())
    if len(a) >= 3 and a[0] == "compile":
        words = json.load(open(a[1], encoding="utf-8"))
        if isinstance(words, dict):          # allow {"words": [...]} wrapper
            words = words["words"]
        spec = json.load(open(a[2], encoding="utf-8"))
        text = spec.pop("script_text", None)
        spans = {t: {"start": None, "end": None}
                 for t in parse_script(text)["spans"]} if text else {}
        if spans:                            # span windows need real timing:
            raise SystemExit("span anchors in CLI mode require pre-parsed timing "
                             "— use from/to word anchors instead")
        out = compile_timeline(words, spec)
        js = json.dumps(out, ensure_ascii=False, indent=1)
        if "--out" in a:
            with open(a[a.index("--out") + 1], "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(js)
        print(js)
        sys.exit(0 if out["state"] == "SUCCESS" else 1)
    print(__doc__)
    sys.exit(1)
