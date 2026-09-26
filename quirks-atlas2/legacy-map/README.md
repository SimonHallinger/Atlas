# Quirks Atlas: reconstructing intent in legacy embedded code

SAAB track, Gothenburg Tech Week × Chalmers.

**Question:** before a new team changes a legacy system, how can it recover what the system is for, what it depends on and where the risk is?

**What this does:** it turns a legacy C file and its git history into a JSON **system map**. Every claim in the map about *why* the code exists points to evidence (a code comment, a line of code, a commit or a linked discussion). A validator then checks each quoted excerpt word for word against git. The tool does not summarise code. It rebuilds rationale from the historical record and marks anything it cannot prove as inference.

Demo target: Linux [`drivers/pci/quirks.c`](https://github.com/torvalds/linux/blob/master/drivers/pci/quirks.c), about 6,400 lines of hardware workarounds going back to 1999.

| | |
|---|---|
| Functions extracted | 218 |
| Fixup registrations (incl. macro-wrapped) | 806, across 690 vendor/device IDs |
| Commits mined (`git log --follow`, 2005 to 2026) | 623 (154 with `Link:`, 51 `Fixes:`, 81 `Cc: stable`) |
| Quirks fully reasoned | 28 |
| Quoted evidence excerpts verified verbatim | 299 / 299 |

## Architecture

```
quirks.c ──► extract.py ──► static.json        tree-sitter: functions, DECLARE_PCI_FIXUP_* (phase, vendor, device),
                                               call graph, config-space reads/writes, delays & timeouts, port/MMIO I/O,
                                               global side effects, reset-method tables, comments (doc/preamble/inline/
                                               registration-site)
git repo ──► history.py ──► history.json       git log --follow, git blame -w, git log -L :func: (full per-function history);
                                               trailers: Fixes:, Link:, Reported-by:, Cc: stable, bugzilla refs
         ──► build_map.py ─► system_map.json   evidence pool per node, graph nodes/edges, heuristic criticality for ALL nodes,
                                               cross-file callers (git grep), "compensates_for" edges from Fixes: tags
                                               that point at core kernel changes
         ──► reason.py ───► analyses/*.json    LLM reasoning: 8 SAAB sections, each claim {claim, confidence, basis, evidence[]}
         ──► validate.py                       every excerpt must appear verbatim in the cited commit / source lines;
                                               otherwise it is flagged, and claims without valid evidence are downgraded
                                               to basis=inference
         ──► export_viewer.py ─► viewer/index.html   interactive map (d3), click a node to see its sections and evidence
```

Division of labour: static analysis and git mining find the **facts**. The LLM only **reasons over a closed evidence pool** it is handed. The validator makes the reasoning **auditable**. For a defence customer, "the AI says so" is worthless. "The AI says so, and here is the commit that proves it" is what matters.

## Output schema (per function node)

```jsonc
{
  "id": "fn:quirk_via_vlink", "kind": "fixup_hook|reset_method|helper", "loc": {...},
  "static":  { "fixups": [...], "config_access": [...], "delays": [...], "timing": [...], "io": [...],
               "global_flags": [...], "dev_flags_set": [...], "external_callers": [...], ... },
  "history": { "n_commits", "first", "last", "origin_pre_git", "n_stable", "fixes_chain": [...], "timeline": [...] },
  "heuristic_criticality": { "level", "score", "factors": [...] },       // every node
  "evidence_pool": [ { "id": "E7", "type": "comment|code|commit|discussion|bug_report", "ref", "excerpt", ... } ],
  "analysis": {                                                          // reasoned nodes
    "architecture_rationale":               [ENTRY],
    "intentional_vs_accidental":            [ENTRY + classification],
    "hardware_timing_dependencies":         [ENTRY],
    "suspected_workarounds":                [ENTRY],
    "external_interfaces":                  [ENTRY],
    "compatibility_certification_sensitive":[ENTRY],
    "criticality": { "level", "category": "safety|mission|business", "reasoning": ENTRY },
    "historical_lessons":                   [ENTRY]
  },
  "analysis_meta": { "one_line_intent", "change_risks": [{if_you, then, confidence, evidence}], "open_questions", "verification" }
}
ENTRY = { "claim", "confidence": "high|medium|low", "basis": "evidence|inference",
          "evidence": [ { "type", "ref": "commit:a89c82249c37 | quirks.c:L48-L94 | drivers/pci/probe.c:L2743 | https://...",
                          "excerpt", "verified": true } ] }
```
Top-level `nodes` also contain `device`, `phase`, `effect` (for example `pci_no_msi`, `PCI_DEV_FLAGS_NO_BUS_RESET`), `interface` and `core_change` nodes. Edge types: `calls`, `runs_in`, `registers_for`, `sets`, `dispatches`, `calls_into`, `compensates_for`.

## Showcases: lines that look redundant until you read the history

1. **`pcie_failed_link_retrain`**: generic-looking link code that runs on every PCIe downstream port. It was written for *one* riser card on a SiFive board, widened on purpose because the root cause was unknown, and has since been patched four times (a `Fixes:` chain with stable backports) because the widening left a 2.5GT/s speed clamp on unrelated devices. For about three months a refactor made the clamp-lifting code dead, and nobody noticed.
2. **`quirk_via_vlink`**: `udelay(15); /* unknown if delay really needed */`. It predates git. Its scoping rules were flipped five times between 2005 and 2007 because each narrowing or widening broke a different VIA board. The tool says plainly that the reason for the delay cannot be recovered.
3. **`delay_250ms_after_flr`**: `msleep(250)` looks arbitrary. It is an empirically chosen delay ("heuristically proven") for Intel P3700 SSDs in VM passthrough. It was later reused for a Solidigm drive because Intel's SSD business became Solidigm, so the device table follows the company rather than the silicon.
4. **`asus_hides_smbus_hostbridge`**: looks like a harmless board whitelist. Two entries were removed because unhiding the SMBus let Linux race with SMM/ACPI firmware running thermal management. The comment above it is a safety rule, and the tool rates this node *safety* critical.

## How to run

```bash
pip install tree-sitter tree-sitter-c
./run.sh                    # partial-clones Linux (--filter=blob:none, ~2 GB, no blobs), runs the whole pipeline
export ANTHROPIC_API_KEY=…   # optional: reason.py runs the LLM layer for data/selected.txt
python3 reason.py --map data/system_map.json --functions data/selected.txt --dry-run   # inspect exact prompts
python3 fetch_discussions.py --history data/history.json --out data/discussions.json   # optional, from a normal machine
open viewer/index.html
```

## Limitations

- **Reasoning in this demo** was produced in-session by Claude (Opus 5.5) following the same prompt contract as `reason.py` (see `data/prompts/`), because the sandbox had no API key. Run `reason.py` to regenerate the analyses. `validate.py` checks the output either way.
- **Mailing-list threads were not retrieved.** lore.kernel.org blocks message pages for crawlers, so `Link:` URLs appear as linked but unretrieved evidence. `fetch_discussions.py` fills them from a normal machine. Kernel maintainers put most of the rationale in commit messages anyway.
- **History starts in April 2005.** Anything older points to the `Linux-2.6.12-rc2` import commit and is flagged `origin_pre_git`. The pre-2005 history tree (history.git) could extend this.
- **The validator checks quotes, not conclusions.** A verified excerpt proves the source says that. It does not prove the inference drawn from it is right. Confidence levels and the evidence/inference split are there so a human reviewer knows where to look.
- **Line-range attribution** uses `git blame` plus `git log -L`. Moved or heavily refactored code can be attributed to the refactor. Cleanup commits are detected and marked, but the detection is heuristic.
- Heuristic criticality for non-reasoned nodes is a transparent scoring rule (factors listed per node), not a judgement.

## Generalising beyond Linux

Nothing here is specific to PCI. You need a C/C++ parser, a registration pattern (here `DECLARE_PCI_FIXUP_*`; elsewhere interrupt vector tables, device trees or linker sections), and version-control history with reasonably good messages. For codebases with weak commit messages, the same evidence pool can take issue-tracker items, requirements IDs or test names.
