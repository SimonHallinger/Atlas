#!/usr/bin/env python3
"""LLM reasoning layer.

For each selected function, send the *static facts* and the *evidence pool*
(comments, code lines, commits, linked discussions) to Claude and ask it to
fill the eight SAAB sections. The model may only cite evidence from the pool;
every excerpt it quotes is then checked verbatim by validate.py.

  export ANTHROPIC_API_KEY=...
  python3 reason.py --map data/system_map.json --functions data/selected.txt --out data/analyses

Without an API key, run with --dry-run to write the prompts to data/prompts/
(useful for inspecting exactly what the model sees).
"""
import argparse, json, os, sys, urllib.request

MODEL = os.environ.get("LEGACY_MAP_MODEL", "claude-opus-5-5")

SYSTEM = """You are a firmware/hardware archaeologist helping a NEW engineering team
understand a legacy embedded code base BEFORE they change it. You do not summarise
or rewrite code. You reconstruct INTENT, DEPENDENCIES and RISK from evidence.

Rules (hard):
1. Every claim cites evidence from the provided EVIDENCE POOL by its `ref`,
   with an `excerpt` copied VERBATIM (a contiguous substring, <= 300 chars) from
   that evidence item. Excerpts are machine-checked; paraphrased excerpts are rejected.
2. If a claim is your own inference (no direct evidence says it), set
   "basis": "inference", keep confidence at most "medium", and cite the evidence the
   inference is built on (or none).
3. confidence: high = stated explicitly by a commit message or code comment and
   consistent with the code; medium = strongly implied / corroborated by several
   sources; low = plausible reading, weak or conflicting evidence.
4. Distinguish what the evidence SAYS from what you INFER. Name unknowns explicitly
   ("the history does not explain why 15us was chosen").
5. Commits flagged cleanup=true are refactors; do not treat them as rationale.
   A commit with pre_git_import=true means the code predates git (2005); its
   original rationale is not in this history - say so.
6. Think like a reviewer at a safety-critical company: what breaks, for whom, and
   how would anyone notice, if this code is removed or changed?

Output ONLY JSON matching this schema:
{
 "function": str,
 "one_line_intent": str,
 "analysis": {
   "architecture_rationale": [ENTRY],
   "intentional_vs_accidental": [ENTRY + "classification": "intentional"|"likely_accidental"|"unclear"],
   "hardware_timing_dependencies": [ENTRY],
   "suspected_workarounds": [ENTRY],
   "external_interfaces": [ENTRY],
   "compatibility_certification_sensitive": [ENTRY],
   "criticality": {"level": "high"|"medium"|"low",
                   "category": "safety"|"mission"|"business",
                   "reasoning": ENTRY},
   "historical_lessons": [ENTRY]
 },
 "change_risks": [ {"if_you": str, "then": str, "confidence": str, "evidence": [EVIDENCE]} ],
 "open_questions": [str]
}
ENTRY = {"claim": str, "confidence": "high"|"medium"|"low", "basis": "evidence"|"inference",
         "evidence": [{"type": "comment"|"commit"|"discussion"|"code"|"bug_report", "ref": str, "excerpt": str}]}
Sections may be empty lists when nothing applies - that is better than filler."""


def build_prompt(node, src_lines):
    s = node["static"]
    code = "\n".join(f"{i + 1}: {src_lines[i]}" for i in range(node["loc"]["start"] - 1, node["loc"]["end"]))
    pool = []
    for e in node["evidence_pool"]:
        item = {k: e[k] for k in ("type", "ref", "excerpt") if k in e}
        for k in ("date", "author", "role", "cleanup", "pre_git_import", "trailers"):
            if e.get(k):
                item[k] = e[k]
        if e["type"] == "commit":
            item["message"] = e["message"]
        pool.append(item)
    facts = {
        "function": node["name"], "kind": node["kind"], "lines": node["loc"],
        "fixup_registrations": s["fixups"][:40], "n_fixups": len(s["fixups"]),
        "calls_local": s["calls_local"], "calls_external": s["calls_external"], "called_by": s["called_by"],
        "config_access": s["config_access"], "delays": s["delays"], "timing": s["timing"], "io": s["io"],
        "sets_global_state": s["global_flags"], "sets_device_fields": s["dev_flags_set"],
        "reset_method_tables": s["table_refs"], "non_static": not s["static_storage"],
        "history": {k: node["history"][k] for k in ("n_commits", "first", "last", "origin_pre_git", "n_stable", "fixes_chain")},
    }
    return (f"STATIC FACTS (from tree-sitter + git):\n{json.dumps(facts, indent=1)}\n\n"
            f"SOURCE:\n```c\n{code}\n```\n\nEVIDENCE POOL:\n{json.dumps(pool, indent=1)}\n\n"
            "Produce the JSON analysis now.")


def call_claude(prompt):
    req = urllib.request.Request(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages",
        data=json.dumps({"model": MODEL, "max_tokens": 8000, "system": SYSTEM,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)
    text = "".join(b.get("text", "") for b in out["content"])
    text = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--source", default="data/quirks.c")
    ap.add_argument("--functions", required=True, help="file with comma/newline separated names")
    ap.add_argument("--out", default="data/analyses")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    M = json.load(open(a.map))
    src = open(a.source).read().splitlines()
    wanted = [w.strip() for w in open(a.functions).read().replace("\n", ",").split(",") if w.strip()]
    by = {n["name"]: n for n in M["nodes"] if n["id"].startswith("fn:")}
    os.makedirs(a.out, exist_ok=True)
    for name in wanted:
        prompt = build_prompt(by[name], src)
        if a.dry_run or not os.environ.get("ANTHROPIC_API_KEY"):
            os.makedirs("data/prompts", exist_ok=True)
            open(f"data/prompts/{name}.txt", "w").write(SYSTEM + "\n\n=====\n\n" + prompt)
            print("prompt written", name, file=sys.stderr)
            continue
        res = call_claude(prompt)
        res["function"] = name
        res["generated_by"] = MODEL
        json.dump(res, open(os.path.join(a.out, f"{name}.json"), "w"), indent=1)
        print("analysed", name, file=sys.stderr)


if __name__ == "__main__":
    main()
