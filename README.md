# resilient-agent-toolkit-v2

**Six more zero-dependency Python tools from the "learn it, surpass it" pipeline.**
Sequel to [resilient-agent-toolkit](https://github.com/chenhz01/resilient-agent-toolkit) —
these were internalized from open-source mechanisms (langgraph, strands, memsearch,
hypit, archify, deepeval), rebuilt as standalone stdlib-only tools, each with a
built-in `--selftest`.

```
01 durable_steps ──▶ 02 context_budget ──▶ 03 word_anchor
                                              │
04 ir_render ◀────────────────────────────────┘
05 eval_gate      06 fact_lint
```

Files are numbered in dependency order. Read them in order.

## Components

### 01. `01_durable_steps.py` — checkpoint/resume + human gate
*(from langgraph durable execution & interrupt)*

Every step persists a checkpoint. Crash → rerun → skip completed, resume from
the exact breakpoint. `{"gate": true}` steps pause the pipeline for human
approval. Critical-step failures stop with an explicit state; non-critical
continue.

```bash
python 01_durable_steps.py --selftest
```

### 02. `02_context_budget.py` — context window pressure valve
*(from strands ContextManager)*

When context exceeds budget: offload → summarize → truncate, with an auditable
stash — offloaded blocks are retrievable byte-identical by `stash_id`.

### 03. `03_word_anchor.py` — word-anchored timeline compiler
*(from hypit SVML)*

Never hardcode seconds. Anchor subtitles/B-roll/titles to WORDS of the
voiceover; seconds are derived at compile time. Swap the voiceover, keep the
spec — cues re-time automatically. Unknown anchors, bad occurrences, and
same-track overlaps are explicit failures, never silent.

```bash
python 03_word_anchor.py --selftest
```

### 04. `04_ir_render.py` — model emits IR, program draws the diagram
*(from archify)*

Typed JSON IR → schema validation → deterministic SVG. Same IR in →
byte-identical SVG out. Plus `delta()` between two IRs. Duplicates, dangling
edges, unknown keys are explicit failures.

### 05. `05_eval_gate.py` — mechanical regression gate
*(from deepeval)*

Metric suite (required/forbidden/json/length/sections) + append-only run log +
`compare()` that reports fixed/regressed/new/dropped between two runs. Replace
"it feels better" with numbers.

### 06. `06_fact_lint.py` — facts must be timeless, dated, or pointers
FRESH-1/2/3 violations (undated status claims, bare "now/currently", number+unit
without a date) exit non-zero — CI-friendly.

## Design rules shared by all six

- **Zero dependencies** — pure Python stdlib, each file runs standalone
- **`--selftest` built in** — every file verifies itself in seconds
- **Three-state contract** — SUCCESS / EXPLICIT_FAILURE (with reasons) /
  SILENT_TIMEOUT; silence is treated as failure, never success
- **Numbered in dependency order** — 01 defines primitives, later files build on them

## What is deliberately *not* in this repo

This toolkit was distilled from an internal agent system with 100+ skills, and
the split is intentional: **anything that touches personal memory stores stays
private.** The clearest example is a tool that reads a personal memory log and
proposes skill candidates from it — by our own rule, tools that read, derive
from, or expose the shape of private memory never ship publicly, not even
"anonymized". What ships here is only the generic machinery that runs *without*
needing to know anything about you.

Same logic applies to the private thresholds, prompt recipes, and production
data of our own system — those live behind the collaboration channel below.

## Status

All six pass their selftests; word_anchor additionally ran end-to-end with real
edge-tts audio (real word boundaries → compiled cue sheet → verified re-timing
across two different voices with an unchanged spec).

## Contact

**hcac4735@agent.qq.com**. Chinese and English both welcome.

## License

MIT
