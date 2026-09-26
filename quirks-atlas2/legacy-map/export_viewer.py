#!/usr/bin/env python3
"""Slim the system map for the browser and inline it into viewer/template.html -> viewer/index.html"""
import json, sys

M = json.load(open("data/system_map.json"))
head = M["meta"]["commit"]
nodes = []
for n in M["nodes"]:
    n = dict(n)
    if n["id"].startswith("fn:"):
        analysed = bool(n.get("analysis"))
        pool = []
        for e in n["evidence_pool"]:
            e = dict(e)
            if "message" in e:
                e["message"] = e["message"][:1800 if analysed else 600]
            pool.append(e)
        if not analysed:
            pool = [e for e in pool if e["type"] != "code" or e.get("role") != "fixup_registration"][:40]
        n["evidence_pool"] = pool
        n["static"] = dict(n["static"])
        n["static"]["calls_external"] = n["static"]["calls_external"][:30]
    nodes.append(n)
out = {"meta": M["meta"], "nodes": nodes, "edges": M["edges"]}
tpl = open("viewer/template.html").read()
html = tpl.replace("/*__DATA__*/null", json.dumps(out, separators=(",", ":")).replace("</", "<\\/"))
open("viewer/index.html", "w").write(html)
print("viewer/index.html", len(html) // 1024, "KB")
