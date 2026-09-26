#!/usr/bin/env python3
"""Evidence validator: 'the AI says so' is worthless, 'here is the proof' is gold.

For every claim in every analysis:
  * each evidence ref must resolve (commit exists in git history, line range
    exists in the source file, discussion URL was linked by a commit)
  * each excerpt must appear VERBATIM (whitespace-normalised) in what it cites
  * a claim with no valid evidence is downgraded to basis=inference, confidence<=medium
  * high confidence requires at least one verified commit or comment excerpt

  python3 validate.py --history data/history.json --source data/quirks.c \
      --analyses data/analyses [--fix]
Writes a verification block into each analysis ("verified": true/false per evidence,
plus a summary) when --fix is given; exits non-zero if hard errors remain.
"""
import argparse, glob, json, re, sys

SECTIONS = ["architecture_rationale", "intentional_vs_accidental", "hardware_timing_dependencies",
            "suspected_workarounds", "external_interfaces", "compatibility_certification_sensitive",
            "historical_lessons"]


def wsnorm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def norm(s):
    s = re.sub(r"^\s*(\*|//)\s?", "", s, flags=re.M)
    s = s.replace("/*", " ").replace("*/", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--analyses", required=True)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--repo", default=None)
    a = ap.parse_args()
    H = json.load(open(a.history))
    lines = open(a.source).read().splitlines()
    by_short = {}
    for h, c in H["commits"].items():
        by_short[h[:12]] = c
    links = {l for c in H["commits"].values() for l in c["links"] + c["bug_refs"]}

    def resolve(ev):
        t, ref, ex = ev.get("type"), ev.get("ref", ""), ev.get("excerpt", "")
        if t == "commit":
            m = re.match(r"commit:([0-9a-f]{7,40})", ref)
            if not m:
                return False, "bad commit ref"
            c = by_short.get(m.group(1)[:12]) or next((v for k, v in H["commits"].items() if k.startswith(m.group(1))), None)
            if not c:
                return False, "commit not in history"
            hay = wsnorm(c["subject"] + "\n" + c.get("raw", c["message"]))
            return (wsnorm(ex) in hay, "excerpt not found in commit message")
        if t in ("comment", "code") and not ref.startswith("quirks.c:") and a.repo:
            m = re.match(r"([\w./-]+):L(\d+)", ref)
            if not m:
                return False, "bad ref"
            import subprocess
            txt = subprocess.run(["git", "-C", a.repo, "show", f"HEAD:{m.group(1)}"], capture_output=True, text=True).stdout.splitlines()
            ln = int(m.group(2))
            return (0 < ln <= len(txt) and norm(ex) in norm(txt[ln - 1]), "excerpt not found in external file")
        if t in ("comment", "code"):
            m = re.match(r"quirks\.c:L(\d+)(?:-L?(\d+))?", ref)
            if not m:
                return False, "bad line ref"
            l0 = int(m.group(1)); l1 = int(m.group(2) or l0)
            if not (1 <= l0 <= l1 <= len(lines)):
                return False, "line range outside file"
            hay = norm("\n".join(lines[l0 - 1:l1]))
            return (norm(ex) in hay, "excerpt not found at those lines")
        if t in ("discussion", "bug_report"):
            return (ref in links, "URL not linked from any commit")
        return False, f"unknown evidence type {t}"

    total = ok = 0
    errors = []
    for p in sorted(glob.glob(f"{a.analyses}/*.json")):
        d = json.load(open(p))
        an = d["analysis"]
        entries = []
        for s in SECTIONS:
            entries += [(s, e) for e in an.get(s, [])]
        entries.append(("criticality", an["criticality"]["reasoning"]))
        for r in d.get("change_risks", []):
            entries.append(("change_risks", r))
        fn_ok = fn_total = 0
        for sec, e in entries:
            good = 0
            for ev in e.get("evidence", []):
                total += 1; fn_total += 1
                v, why = resolve(ev)
                ev["verified"] = v
                if v:
                    ok += 1; fn_ok += 1; good += 1
                else:
                    errors.append(f"{d['function']}/{sec}: {why}: {ev.get('ref')} :: {ev.get('excerpt', '')[:80]}")
            if "confidence" in e and "claim" in e:
                if good == 0 and e.get("basis") != "inference":
                    e["basis"] = "inference"
                    if e["confidence"] == "high":
                        e["confidence"] = "medium"
                if e["confidence"] == "high" and not any(ev.get("verified") and ev["type"] in ("commit", "comment")
                                                         for ev in e.get("evidence", [])):
                    e["confidence"] = "medium"
        d["verification"] = {"evidence_items": fn_total, "verified": fn_ok,
                             "validator": "validate.py: verbatim excerpt match against git history + source"}
        if a.fix:
            json.dump(d, open(p, "w"), indent=1)
    print(f"evidence items: {total}, verified: {ok}, failed: {total - ok}")
    for e in errors:
        print("  FAIL", e)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
