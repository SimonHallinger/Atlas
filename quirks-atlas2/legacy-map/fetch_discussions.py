#!/usr/bin/env python3
"""Optional: enrich evidence with mailing-list text from lore.kernel.org.

lore blocks message pages for crawlers, so this was not run in the hackathon
sandbox. From a normal machine it fetches each Link: URL's raw message and keeps
the first paragraphs of the body as the excerpt.

  python3 fetch_discussions.py --history data/history.json --out data/discussions.json
"""
import argparse, json, re, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--history", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--limit", type=int, default=200)
a = ap.parse_args()
H = json.load(open(a.history))
urls = sorted({l for c in H["commits"].values() for l in c["links"] if "lore.kernel.org" in l})[:a.limit]
out = {}
for u in urls:
    m = re.search(r"lore\.kernel\.org/(?:r|all|[\w-]+)/([^/\s]+)", u)
    if not m:
        continue
    raw = f"https://lore.kernel.org/all/{m.group(1)}/raw"
    try:
        txt = urllib.request.urlopen(urllib.request.Request(raw, headers={"User-Agent": "legacy-map"}), timeout=20).read().decode("utf8", "replace")
    except Exception as e:
        print("skip", u, e); continue
    body = txt.split("\n\n", 1)[1] if "\n\n" in txt else txt
    body = body.split("\n---\n")[0]
    out[u] = {"raw_url": raw, "excerpt": body.strip()[:1500]}
    time.sleep(1)
json.dump(out, open(a.out, "w"), indent=1)
print(len(out), "discussions")
