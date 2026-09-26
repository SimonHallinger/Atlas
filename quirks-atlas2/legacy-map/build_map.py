#!/usr/bin/env python3
"""Merge static analysis + history + (optional) reasoning into the system map.

  python3 build_map.py --static data/static.json --history data/history.json \
      --analyses data/analyses --discussions data/discussions.json \
      --source data/quirks.c --out data/system_map.json
"""
import argparse, glob, json, os, re, subprocess, sys
from datetime import datetime, timezone

CLEANUP_RE = re.compile(r"whitespace|dev_printk|printk|typo|spelling|misspell|No functional change|__dev|__init|"
                        r"Use .* helper|Convert .* to use|Remove __dev|checkpatch|quoted strings|fallthrough|PCI_HEADER_TYPE_", re.I)
SEVERE_RE = re.compile(r"\b(hang|lock ?up|lockup|corrupt\w*|data loss|machine check|crash|reset|reboot|freeze|fatal|disk eating)\b", re.I)
PRE_GIT = "1da177e4c3f4"
XREF_PATHS = ["drivers/pci", "include/linux/pci.h"]


def prefetch_xref_blobs(repo):
    """A --filter=blob:none clone has no file contents. Download every blob under
    XREF_PATHS in ONE request, so git grep never falls back to one network
    round-trip per file (which looks like a hang on a fresh clone)."""
    try:
        tree = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "HEAD", "--", *XREF_PATHS],
                              capture_output=True, text=True, check=True, timeout=120).stdout
        oids = [l.split()[2] for l in tree.splitlines() if l.split()[1] == "blob"]
        chk = subprocess.run(["git", "-C", repo, "cat-file", "--batch-check"], input="\n".join(oids) + "\n",
                             capture_output=True, text=True, timeout=120,
                             env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}).stdout
        missing = [l.split()[0] for l in chk.splitlines() if l.endswith("missing")]
        if missing:
            print(f"downloading {len(missing)} files for caller lookup (one batch) ...", file=sys.stderr)
            subprocess.run(["git", "-C", repo, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q", "--no-tags",
                            "--no-write-fetch-head", "--filter=blob:none", "origin", *missing],
                           check=True, timeout=600)
        return True
    except Exception as e:  # never let the optional caller lookup block the pipeline
        print(f"WARNING: skipping cross-file caller lookup ({e})", file=sys.stderr)
        return False


PHASES = ["early", "header", "final", "enable", "resume", "resume_early", "suspend", "suspend_late"]


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--analyses", default="data/analyses")
    ap.add_argument("--discussions", default="data/discussions.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=None, help="kernel checkout, for cross-file caller lookup")
    a = ap.parse_args()

    S = json.load(open(a.static))
    H = json.load(open(a.history))
    src_lines = open(a.source).read().splitlines()
    commits = H["commits"]
    disc = json.load(open(a.discussions)) if os.path.exists(a.discussions) else {}
    analyses = {}
    for p in glob.glob(os.path.join(a.analyses, "*.json")):
        d = json.load(open(p))
        analyses[d["function"]] = d

    xref_ok = bool(a.repo) and prefetch_xref_blobs(a.repo)
    short_to_full = {c["short"]: h for h, c in commits.items()}
    nodes, edges = [], []
    node_ids = set()

    def add_node(n):
        if n["id"] not in node_ids:
            node_ids.add(n["id"])
            nodes.append(n)

    for ph in PHASES:
        add_node({"id": f"phase:{ph}", "kind": "phase", "name": f"fixup phase: {ph}"})

    core_changes = {}
    for name, f in S["functions"].items():
        hist = H["functions"].get(name, {})
        hs = list(dict.fromkeys(hist.get("log_L", [])[::-1] + [b["hash"] for b in hist.get("blame", [])]))
        hs = sorted([h for h in hs if h in commits], key=lambda h: commits[h]["date"])

        # ---------- evidence pool ----------
        pool = []
        def ev(type_, ref, excerpt, **kw):
            pool.append({"id": f"E{len(pool) + 1}", "type": type_, "ref": ref, "excerpt": excerpt, **kw})

        if f.get("doc_comment"):
            l0, l1 = f["doc_comment_lines"]
            ev("comment", f"quirks.c:L{l0}-L{l1}", f["doc_comment"], role="doc")
        for pc in f.get("preamble_comments", []):
            ev("comment", f"quirks.c:L{pc['lines'][0]}-L{pc['lines'][1]}", pc["text"], role="preamble")
        for ic in f.get("inline_comments", []):
            if len(ic["text"]) > 3:
                ev("comment", f"quirks.c:L{ic['line']}", ic["text"], role="inline")
        seen_site = set()
        for fx in f.get("fixups", []):
            if fx.get("site_comment") and tuple(fx["site_comment_lines"]) not in seen_site:
                seen_site.add(tuple(fx["site_comment_lines"]))
                l0, l1 = fx["site_comment_lines"]
                ev("comment", f"quirks.c:L{l0}-L{l1}", fx["site_comment"], role="registration_site")
            if fx.get("trailing_comment"):
                ev("comment", f"quirks.c:L{fx['line']}", fx["trailing_comment"], role="registration_trailing")
        code_lines = set()
        for x in f.get("config_access", []):
            if x["op"] == "write":
                code_lines.add((x["line"], "config_write"))
        for x in f.get("delays", []):
            code_lines.add((x["line"], "delay"))
        for x in f.get("timing", []):
            code_lines.add((x["line"], "timing"))
        for x in f.get("io", []):
            code_lines.add((x["line"], "port_or_mmio_io"))
        for ln, role in sorted(code_lines):
            ev("code", f"quirks.c:L{ln}", src_lines[ln - 1].strip(), role=role)
        for fx in f.get("fixups", [])[:60]:
            ev("code", f"quirks.c:L{fx['line']}", src_lines[fx["line"] - 1].strip(), role="fixup_registration")
        ext_callers = []
        if xref_ok and (not f["static_storage"] or f.get("exported")):
            try:
                out = subprocess.run(["git", "-C", a.repo, "grep", "-n", "-w", name, "HEAD", "--", *XREF_PATHS],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                                     env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}, timeout=60).stdout
            except subprocess.TimeoutExpired:
                print(f"  caller lookup for {name} timed out; skipping", file=sys.stderr)
                out = ""
            for l in out.splitlines()[:25]:
                parts = l.split(":", 3)
                if len(parts) < 4 or not parts[2].isdigit():
                    continue
                _, path, ln, txt = parts
                ext_callers.append({"file": path, "line": int(ln), "text": txt.strip()})
                ev("code", f"{path}:L{ln}", txt.strip(), role="external_reference")
        links, bugs = [], []
        for h in hs:
            c = commits[h]
            is_cleanup = bool(CLEANUP_RE.search(c["subject"])) and not c["fixes"]
            ev("commit", f"commit:{c['short']}", c["subject"],
               date=c["date"], author=c["author"], message=c["message"][:3000],
               trailers={k: c[k] for k in ("fixes", "links", "bug_refs", "reported_by", "tested_by", "cc_stable") if c.get(k)},
               cleanup=is_cleanup, pre_git_import=c["short"] == PRE_GIT)
            for l in c["links"]:
                if l not in links:
                    links.append(l)
                    d = disc.get(l)
                    ev("discussion", l, d["excerpt"] if d else f"(linked from commit {c['short']}; thread not retrieved)",
                       retrieved=bool(d), via_commit=c["short"])
            for b in c["bug_refs"]:
                if b not in bugs:
                    bugs.append(b)
                    ev("bug_report", b, f"(bug tracker entry referenced by commit {c['short']})", via_commit=c["short"])

        # ---------- history stats ----------
        substantive = [commits[h] for h in hs if not (CLEANUP_RE.search(commits[h]["subject"]) and not commits[h]["fixes"])]
        fixes_chain = []
        for h in hs:
            for fr in commits[h].get("fixes_resolved", []):
                fixes_chain.append({"by": commits[h]["short"], "fixes": fr["raw"][:120], "fixes_hash": (fr["hash"] or "")[:12],
                                    "in_this_function": bool(fr["hash"] and fr["hash"] in hs)})
                if fr["hash"] and fr["hash"] not in hs and fr["hash"] not in commits:
                    core_changes.setdefault(fr["hash"][:12], {"subject": fr["raw"], "quirks": set()})["quirks"].add(name)
        text_blob = " ".join([f.get("doc_comment") or ""] + [c["message"] for c in substantive])
        history = {
            "n_commits": len(hs), "n_substantive": len(substantive),
            "first": commits[hs[0]]["date"] if hs else None, "last": commits[hs[-1]]["date"] if hs else None,
            "origin_pre_git": bool(hs) and commits[hs[0]]["short"] == PRE_GIT,
            "n_stable": sum(1 for h in hs if commits[h]["cc_stable"]),
            "n_links": len(links), "n_bug_refs": len(bugs), "fixes_chain": fixes_chain,
            "severity_terms": sorted({m.lower() for m in SEVERE_RE.findall(text_blob)}),
            "authors": sorted({commits[h]["author"] for h in hs}),
            "timeline": [{"short": commits[h]["short"], "date": commits[h]["date"], "subject": commits[h]["subject"],
                          "cleanup": bool(CLEANUP_RE.search(commits[h]["subject"])) and not commits[h]["fixes"],
                          "fixes": bool(commits[h]["fixes"]), "stable": bool(commits[h]["cc_stable"])} for h in hs],
        }

        # ---------- heuristic criticality (for every node; replaced by reasoning when available) ----------
        score, why = 0, []
        def add(pts, reason):
            nonlocal score
            score += pts; why.append(f"+{pts} {reason}")
        if f.get("global_flags"): add(3, f"sets system-wide state ({', '.join(f['global_flags'])})")
        nw = len([x for x in f["config_access"] if x["op"] == "write"])
        if nw: add(min(nw, 3), f"{nw} config-space write(s)")
        if f.get("io"): add(2, "direct port/MMIO I/O")
        if f["delays"] or f.get("timing"): add(2, "timing/delay assumption")
        if any(fx["phase"].startswith("resume") or fx["phase"].startswith("suspend") for fx in f["fixups"]):
            add(1, "runs on suspend/resume path")
        if f.get("table_refs"): add(2, "registered as device reset method")
        if history["n_stable"]: add(min(history["n_stable"], 3), f"{history['n_stable']} commit(s) sent to stable")
        if fixes_chain: add(min(len(fixes_chain), 3), f"{len(fixes_chain)} Fixes: tag(s) in history")
        sev = [t for t in history["severity_terms"] if t not in ("reset",)]
        if sev: add(2, f"history mentions: {', '.join(sev[:5])}")
        if not f["static_storage"] or f.get("exported"): add(2, "non-static / called from outside quirks.c")
        ndev = len({(fx["vendor"], fx["device"]) for fx in f["fixups"]})
        if ndev >= 10: add(1, f"matches {ndev} device IDs")
        level = "high" if score >= 8 else "medium" if score >= 4 else "low"

        # ---------- node ----------
        kind = "reset_method" if f.get("table_refs") else "fixup_hook" if f["fixups"] else "helper"
        node = {
            "id": f"fn:{name}", "kind": kind, "name": name, "loc": f["loc"], "signature": f["signature"],
            "static": {
                "fixups": [{k: fx.get(k) for k in ("macro", "phase", "vendor", "device", "class", "line", "via_macro")} for fx in f["fixups"]],
                "calls_local": f["calls_local"], "calls_external": f["calls_external"], "called_by": f.get("called_by", []),
                "config_access": f["config_access"], "delays": f["delays"], "timing": f.get("timing", []), "io": f.get("io", []),
                "dev_flags_set": f["dev_flags_set"], "global_flags": f["global_flags"],
                "table_refs": f.get("table_refs", []), "exported": f.get("exported"), "external_callers": ext_callers,
                "static_storage": f["static_storage"], "loc_count": f["loc_count"],
            },
            "history": history,
            "heuristic_criticality": {"level": level, "score": score, "factors": why},
            "evidence_pool": pool,
            "analysis": analyses.get(name, {}).get("analysis"),
            "analysis_meta": {k: v for k, v in analyses.get(name, {}).items() if k not in ("analysis", "function")} or None,
        }
        add_node(node)

        # ---------- edges ----------
        for c in f["calls_local"]:
            edges.append({"source": f"fn:{name}", "target": f"fn:{c}", "type": "calls"})
        for ph in sorted({fx["phase"] for fx in f["fixups"]}):
            add_node({"id": f"phase:{ph}", "kind": "phase", "name": f"fixup phase: {ph}"})
            edges.append({"source": f"fn:{name}", "target": f"phase:{ph}", "type": "runs_in"})
        for fx in f["fixups"]:
            did = f"dev:{fx['vendor']}:{fx['device']}" + (f":{fx['class']}" if fx.get("class") else "")
            add_node({"id": did, "kind": "device", "name": f"{fx['vendor']} / {fx['device']}" + (f" (class {fx['class']})" if fx.get("class") else ""),
                      "vendor": fx["vendor"], "device": fx["device"]})
            edges.append({"source": f"fn:{name}", "target": did, "type": "registers_for", "phase": fx["phase"], "line": fx["line"]})
        for g in f["global_flags"]:
            add_node({"id": f"effect:{g}", "kind": "effect", "name": g, "scope": "system-wide"})
            edges.append({"source": f"fn:{name}", "target": f"effect:{g}", "type": "sets"})
        body = "\n".join(src_lines[f["loc"]["start"] - 1:f["loc"]["end"]])
        for flag in sorted(set(re.findall(r"\b(PCI_DEV_FLAGS_\w+|PCI_BUS_FLAGS_\w+)\b", body))):
            add_node({"id": f"effect:{flag}", "kind": "effect", "name": flag, "scope": "per-device"})
            edges.append({"source": f"fn:{name}", "target": f"effect:{flag}", "type": "sets"})
        for fld in f["dev_flags_set"]:
            if fld in ("dev_flags", "bus_flags"):
                continue
            add_node({"id": f"effect:pci_dev.{fld}", "kind": "effect", "name": f"pci_dev.{fld}", "scope": "per-device"})
            edges.append({"source": f"fn:{name}", "target": f"effect:pci_dev.{fld}", "type": "sets"})
        for t in f.get("table_refs", []):
            add_node({"id": f"table:{t['table']}", "kind": "interface", "name": t["table"], "struct": t["struct"], "line": t["line"]})
            edges.append({"source": f"table:{t['table']}", "target": f"fn:{name}", "type": "dispatches"})
        if not f["static_storage"] or f.get("exported"):
            add_node({"id": "iface:pci_core", "kind": "interface", "name": "PCI core (probe.c / pci.c callers)"})
            edges.append({"source": "iface:pci_core", "target": f"fn:{name}", "type": "calls_into"})

    for h, cc in core_changes.items():
        add_node({"id": f"core:{h}", "kind": "core_change", "name": cc["subject"][:110]})
        for q in sorted(cc["quirks"]):
            edges.append({"source": f"fn:{q}", "target": f"core:{h}", "type": "compensates_for"})

    # drop edges to functions not present (calls to macros etc.)
    edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]
    out = {
        "meta": {
            "file": "drivers/pci/quirks.c", "repo": "https://github.com/torvalds/linux",
            "commit": H["repo_head"], "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pipeline_version": "0.3",
            "counts": {"functions": len(S["functions"]), "fixup_registrations": len(S["fixups"]),
                       "commits_in_history": len(commits), "analysed": sum(1 for n in nodes if n.get("analysis"))},
            "sections": ["architecture_rationale", "intentional_vs_accidental", "hardware_timing_dependencies",
                         "suspected_workarounds", "external_interfaces", "compatibility_certification_sensitive",
                         "criticality", "historical_lessons"],
        },
        "nodes": nodes, "edges": edges,
    }
    json.dump(out, open(a.out, "w"), indent=1)
    kinds = {}
    for n in nodes:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    print("nodes", kinds, "edges", len(edges))


if __name__ == "__main__":
    main()
