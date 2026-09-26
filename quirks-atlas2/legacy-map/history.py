#!/usr/bin/env python3
"""History/evidence layer: mine git history for a file and map it onto the
functions found by extract.py.

  - git log --follow  -> every commit touching the file, with parsed trailers
  - git blame -w -C   -> which commit last touched each line of each function
  - git log -L        -> full per-function history (introducing commit + all
                         modifications) for a selected subset of functions

Usage:
  python3 history.py --repo ../work/linux --path drivers/pci/quirks.c \
      --static data/static.json --out data/history.json [--deep fn1,fn2,...]
"""
import argparse, json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

SEP = "\x1e"
FMT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%s%x1f%B" + SEP

TRAILER_RE = re.compile(
    r"^(Fixes|Link|Closes|Reported-by|Tested-by|Reviewed-by|Acked-by|Signed-off-by|Suggested-by|Cc|BugLink|Bugzilla|Reported-and-tested-by)\s*:\s*(.+)$",
    re.I | re.M)
BUGZILLA_RE = re.compile(r"https?://bugzilla\.[\w./-]+\S*|https?://bugs\.[\w./-]+\S*|bugzilla\S*id=\d+")
LORE_RE = re.compile(r"https?://(?:lore\.kernel\.org|lkml\.kernel\.org|patchwork\.[\w.]+|marc\.info|lkml\.org)/\S+")
ERRATA_RE = re.compile(r"\b(errat(?:um|a)|spec(?:ification)? update|hardware bug|silicon bug|BIOS bug|firmware bug|workaround|work around|broken|hang|lock ?up|data corruption|corrupt|machine check|MCE|freeze)\b", re.I)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[:500]}")
    return r.stdout


def parse_commit(chunk):
    parts = chunk.strip("\n").split("\x1f")
    if len(parts) < 7:
        return None
    h, an, ae, ad, cd, subj, body = parts[:7]
    trailers = {}
    for k, v in TRAILER_RE.findall(body):
        trailers.setdefault(k.lower(), []).append(v.strip())
    cc = trailers.get("cc", [])
    stable = [c for c in cc if "stable@" in c]
    links = sorted(set(LORE_RE.findall(body)))
    bugs = sorted(set(BUGZILLA_RE.findall(body)))
    body_wo_trailers = "\n".join(l for l in body.splitlines() if not TRAILER_RE.match(l)).strip()
    return {
        "hash": h, "short": h[:12], "author": an, "author_email": ae,
        "date": ad[:10], "commit_date": cd[:10], "subject": subj,
        "message": body_wo_trailers,
        "raw": body,
        "fixes": trailers.get("fixes", []),
        "links": links,
        "bug_refs": bugs,
        "reported_by": trailers.get("reported-by", []) + trailers.get("reported-and-tested-by", []),
        "tested_by": trailers.get("tested-by", []),
        "reviewed_by": trailers.get("reviewed-by", []) + trailers.get("acked-by", []),
        "cc_stable": stable,
        "errata_terms": sorted({m.lower() for m in ERRATA_RE.findall(body_wo_trailers)}),
    }


def log_file(repo, path):
    out = git(repo, "log", "--follow", "--no-merges", f"--format={FMT}", "--", path)
    commits = [c for c in (parse_commit(x) for x in out.split(SEP)) if c]
    return commits


def prefetch_blobs(repo, path):
    """In a --filter=blob:none clone, fetch every historical blob of `path`
    in batches instead of one lazy round-trip per blob."""
    out = git(repo, "log", "--follow", "--format=", "--raw", "--no-abbrev", "--", path)
    oids = set()
    for line in out.splitlines():
        if line.startswith(":"):
            f = line.split()
            for oid in (f[2], f[3]):
                if set(oid) != {"0"}:
                    oids.add(oid)
    missing = []
    chk = subprocess.run(["git", "-C", repo, "cat-file", "--batch-check"], input="\n".join(oids),
                         capture_output=True, text=True, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    for line in chk.stdout.splitlines():
        if line.endswith("missing"):
            missing.append(line.split()[0])
    oids = sorted(missing)
    print(f"prefetching {len(oids)} blobs", file=sys.stderr)
    for i in range(0, len(oids), 500):
        batch = oids[i:i + 500]
        subprocess.run(["git", "-C", repo, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q",
                        "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no",
                        "--filter=blob:none", "origin", *batch], capture_output=True, text=True)
    return len(oids)


def blame(repo, path):
    out = git(repo, "blame", "-w", "--line-porcelain", "HEAD", "--", path)
    lines, cur = [], None
    for l in out.splitlines():
        if re.match(r"^[0-9a-f]{40} \d+ \d+", l):
            cur = {"hash": l.split()[0]}
        elif l.startswith("author-time "):
            cur["time"] = int(l.split()[1])
        elif l.startswith("\t"):
            lines.append(cur)
    return lines  # index i -> line i+1


def func_log(repo, path, fn):
    """Full history of one function via git log -L :fn:path (oldest last)."""
    out = git(repo, "log", "-L", f":{fn}:{path}", "--no-patch", f"--format={FMT}", check=False)
    if not out.strip():
        # -L with --no-patch may be rejected by older git; retry with patch and strip
        out = git(repo, "log", "-L", f":{fn}:{path}", f"--format={FMT}", check=False)
        out = SEP.join(chunk.split("\ndiff --git")[0] for chunk in out.split(SEP))
    return [c["hash"] for c in (parse_commit(x) for x in out.split(SEP)) if c]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--path", default="drivers/pci/quirks.c")
    ap.add_argument("--static", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--deep", default="", help="comma-separated functions for git log -L")
    ap.add_argument("--no-prefetch", action="store_true")
    a = ap.parse_args()

    static = json.load(open(a.static))
    funcs = static["functions"]
    if not a.no_prefetch:
        prefetch_blobs(a.repo, a.path)

    print("git log --follow ...", file=sys.stderr)
    commits = log_file(a.repo, a.path)
    by_hash = {c["hash"]: c for c in commits}
    print(f"{len(commits)} commits", file=sys.stderr)

    print("git blame ...", file=sys.stderr)
    bl = blame(a.repo, a.path)
    head = git(a.repo, "rev-parse", "HEAD").strip()

    per_fn = {}
    for name, f in funcs.items():
        s, e = f["loc"]["start"], f["loc"]["end"]
        rng = list(range(s, e + 1))
        if f.get("doc_comment_lines"):
            rng = list(range(f["doc_comment_lines"][0], s)) + rng
        for fx in f.get("fixups", []):
            rng.append(fx["line"])
        counts = {}
        for ln in rng:
            if 0 < ln <= len(bl):
                h = bl[ln - 1]["hash"]
                counts.setdefault(h, {"lines": 0, "time": bl[ln - 1]["time"]})
                counts[h]["lines"] += 1
        per_fn[name] = {"blame": sorted(({"hash": h, **v} for h, v in counts.items()), key=lambda x: x["time"])}

    deep = [d for d in a.deep.split(",") if d]
    if deep:
        print(f"git log -L for {len(deep)} functions ...", file=sys.stderr)
        with ThreadPoolExecutor(8) as ex:
            for fn, hs in zip(deep, ex.map(lambda fn: func_log(a.repo, a.path, fn), deep)):
                if fn in per_fn:
                    per_fn[fn]["log_L"] = hs  # newest first
                    for h in hs:
                        if h not in by_hash:
                            c = parse_commit(git(a.repo, "show", "-s", f"--format={FMT}", h).split(SEP)[0])
                            if c:
                                by_hash[h] = c

    # make sure every blamed commit has metadata (blame -C can cite commits from other files)
    need = {b["hash"] for v in per_fn.values() for b in v["blame"]} - set(by_hash)
    for h in need:
        c = parse_commit(git(a.repo, "show", "-s", f"--format={FMT}", h).split(SEP)[0])
        if c:
            c["from_other_file"] = True
            by_hash[h] = c

    # Resolve Fixes: tags to commit hashes when possible
    for c in by_hash.values():
        res = []
        for fx in c["fixes"]:
            m = re.match(r"([0-9a-f]{8,40})", fx)
            if m:
                full = git(a.repo, "rev-parse", "--verify", "-q", m.group(1) + "^{commit}", check=False).strip()
                res.append({"raw": fx, "hash": full or None})
        c["fixes_resolved"] = res

    json.dump({"repo_head": head, "path": a.path, "commits": by_hash, "functions": per_fn},
              open(a.out, "w"), indent=1)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
