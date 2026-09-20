#!/usr/bin/env python3
"""fact_lint.py — The Three Legal Forms of a Fact (learned from
eugeniughelbur/obsidian-second-brain, source-level verified 2026-09-20).

Rule: every factual claim written into memory must be one of —

  (a) TIMELESS  — explicitly marked with the [timeless] tag (rules, definitions,
                  conventions; things that do not rot)
  (b) DATED     — carries a date token (YYYY-MM-DD / 2026年 / "今天"), or lives
                  in a file whose name carries the date (daily logs: auto-dated)
  (c) POINTER   — a pointer, not a claim: contains "见 " / "→" / "(#" links /
                  "指针" / "详见" — it routes, it does not assert

Anything that LOOKS like a claim (status verbs / numbers+units / 现在-words)
without one of the three forms is a violation:

  FRESH-1  undated present-tense fact (status verb, no date, no tag, no pointer)
  FRESH-2  "现在/目前/当前" — rot-prone wording without an attached date
  FRESH-3  numeric claim (number + unit) without a date anchor

Exit code: 0 = clean, 1 = violations found (CI-friendly).

CLI:
    python fact_lint.py --selftest
    python fact_lint.py <file_or_dir> [--strict] [--quiet]
"""
from __future__ import annotations

import os
import re
import sys

DATE_RE = re.compile(r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?|\d{4}-\d{2}|\d{4}年\d{1,2}月")
TIMELESS_RE = re.compile(r"\[\s*timeless\s*\]|（约定）|\(convention\)|【规则】|【定义】")
POINTER_RE = re.compile(r"→|->|见\s|详见|指针|参见|http://|https://|\]\(#|\]\(\.\/")
NOW_RE = re.compile(r"现在|目前|当前|latest|最新")
STATUS_RE = re.compile(
    r"完成|完毕|失败|通过|上线|发布|崩溃|修复|部署|发送|发出|已推|待拍板|达成|亏损|盈利|收入|突破|落地")
NUM_RE = re.compile(r"\d+(\.\d+)?\s*(%|元|万|亿|张|条|人|次|天|小时|分钟|GB|MB|KB|tokens?|颗|个)")
HEADING_RE = re.compile(r"^#{1,6}\s")
LIST_RE = re.compile(r"^\s*[-*·]\s")
CODE_FENCE = "```"


def lint_text(text: str, auto_dated: bool = False, source: str = "<text>") -> list:
    """Return violations: [{rule, line_no, excerpt}]"""
    out = []
    in_code = False
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if line.strip().startswith(CODE_FENCE):
            in_code = not in_code
            continue
        if in_code or not line.strip():
            continue
        if HEADING_RE.match(line) or line.strip().startswith("|"):
            continue  # headings/tables are structure, not claims
        if TIMELESS_RE.search(line) or POINTER_RE.search(line):
            continue  # legal form (a) / (c)
        dated = bool(DATE_RE.search(line)) or auto_dated  # daily-log file name
        if dated:
            continue  # legal form (b)
        if NOW_RE.search(line):
            out.append({"rule": "FRESH-2", "line_no": no, "excerpt": line.strip()[:70],
                        "file": source}); continue
        if NUM_RE.search(line):
            out.append({"rule": "FRESH-3", "line_no": no, "excerpt": line.strip()[:70],
                        "file": source}); continue
        if STATUS_RE.search(line) and (LIST_RE.match(line) or len(line.strip()) > 12):
            out.append({"rule": "FRESH-1", "line_no": no, "excerpt": line.strip()[:70],
                        "file": source})
    return out


def lint_path(path: str, strict: bool = False) -> list:
    """Lint a file or every .md file under a dir. Daily-log filenames
    (YYYY-MM-DD.md) grant auto-dated status to their content."""
    results = []
    if os.path.isdir(path):
        files = []
        for root, _, names in os.walk(path):
            files += [os.path.join(root, n) for n in names if n.endswith(".md")]
    else:
        files = [path]
    for fp in files:
        auto = bool(re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(fp)))
        try:
            text = open(fp, encoding="utf-8").read()
        except Exception:
            continue
        results += lint_text(text, auto_dated=auto, source=fp)
    return results


# ---------------- self-test ----------------

def _selftest() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(("PASS" if cond else "FAIL"), name, detail)
        if not cond:
            failures.append(name)

    # FRESH-1: undated status claim
    v = lint_text("- 已完成沉默失败猎手的开发并部署到生产环境")
    check("T1 FRESH-1 catches undated claim", any(x["rule"] == "FRESH-1" for x in v))

    # legal (b): dated
    v = lint_text("- 2026-09-20 完成了第三版重构")
    check("T2 dated claim passes", not v)

    # legal (a): timeless tag
    v = lint_text("- [timeless] 所有发布操作必须先经人工确认")
    check("T3 timeless tag passes", not v)

    # legal (c): pointer
    v = lint_text("- 详见 docs/INDEX.md → 发布检查清单")
    check("T4 pointer passes", not v)

    # FRESH-2: rot-prone wording
    v = lint_text("目前收入已经稳定在每月 2 万元")
    check("T5 FRESH-2 catches 现在-wording", any(x["rule"] == "FRESH-2" for x in v))

    # FRESH-3: numeric claim without date
    v = lint_text("- 候选池覆盖 50 人")
    check("T6 FRESH-3 catches bare numbers", any(x["rule"] == "FRESH-3" for x in v))

    # auto-date: daily log filename
    v = lint_text("- 已完成沉默失败猎手部署", auto_dated=True)
    check("T7 daily-log filename auto-dates", not v)

    # structure ignored: headings, tables, code fences
    v = lint_text("# 标题\n| a | b |\n```python\n已完成 x = 1\n```\n正文见 README → ")
    check("T8 headings/tables/code skipped", not v)

    # now-word WITH date is fine
    v = lint_text("当前（2026-09-20）共 244 张卡")
    check("T9 now-word + date passes", not v)

    print("-" * 40)
    print("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    a = [x for x in sys.argv[1:]]
    if "--selftest" in a:
        sys.exit(_selftest())
    target = next((x for x in a if not x.startswith("--")), None)
    if not target:
        print(__doc__); sys.exit(1)
    strict = "--strict" in a
    v = lint_path(target, strict)
    for x in v:
        print(f"{x['rule']}  {x['file']}:{x['line_no']}  {x['excerpt']}")
    print(f"\n{len(v)} violation(s)")
    sys.exit(1 if v else 0)
