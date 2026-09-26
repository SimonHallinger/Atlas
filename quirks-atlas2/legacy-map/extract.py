#!/usr/bin/env python3
"""Static analysis layer: parse a legacy C file (Linux drivers/pci/quirks.c) and
extract functions, PCI fixup registrations, call graph, config-space accesses,
delays, exported symbols and attached comments.

Usage: python3 extract.py quirks.c > data/static.json
"""
import json, re, sys
import tree_sitter_c as tsc
from tree_sitter import Language, Parser

C = Language(tsc.language())
parser = Parser(C)

FIXUP_RE = re.compile(
    r"(DECLARE_PCI_FIXUP_(CLASS_)?([A-Z_]+?))\s*\(\s*(?P<args>[^;]*?)\)\s*;", re.S)
CONFIG_RE = re.compile(r"^pci_(?:bus_)?(read|write)_config_(byte|word|dword)$|^pcie_capability_(read|write|set|clear|clear_and_set)_(word|dword)$|^pci_(read|write)_vpd")
DELAY_FNS = {"msleep", "mdelay", "udelay", "usleep_range", "ssleep", "ndelay", "msleep_interruptible", "fsleep"}
IO_FNS_RE = re.compile(r"^(inb|inw|inl|outb|outw|outl|readl|readw|readb|writel|writew|writeb|ioread\d+|iowrite\d+|pci_iomap|ioremap\w*|iounmap|pci_iounmap)$")
# Functions that change kernel-wide behaviour flags (global side effects)
GLOBAL_FLAG_RE = re.compile(r"\b(isa_dma_bridge_buggy|pci_pci_problems|pci_no_msi|pcie_aspm_\w+|pci_pm_d3hot_delay|no_ext_tags)\b")
DEV_FLAG_RE = re.compile(r"(dev|pdev|bridge)->(\w+)\s*(\|?=)")


def text(n, src):
    return src[n.start_byte:n.end_byte].decode("utf8", "replace")


def walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def split_args(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [re.sub(r"\s+", " ", a) for a in out]


def lineno(src_text, idx):
    return src_text.count("\n", 0, idx) + 1


def preceding_comment(lines, start_line):
    """Collect the comment block immediately above line start_line (1-based)."""
    i = start_line - 2
    while i >= 0 and lines[i].strip() == "":
        i -= 1
    if i < 0:
        return None, None
    s = lines[i].strip()
    if s.endswith("*/"):
        j = i
        while j >= 0 and "/*" not in lines[j]:
            j -= 1
        block = "\n".join(lines[j:i + 1])
        return clean_comment(block), (j + 1, i + 1)
    if s.startswith("//"):
        j = i
        while j >= 0 and lines[j].strip().startswith("//"):
            j -= 1
        return clean_comment("\n".join(lines[j + 1:i + 1])), (j + 2, i + 1)
    return None, None


def clean_comment(c):
    c = re.sub(r"^\s*/\*+|\*+/\s*$", "", c.strip())
    c = "\n".join(re.sub(r"^\s*(\*|//)\s?", "", l) for l in c.splitlines())
    return c.strip()


def main(path):
    raw = open(path, "rb").read()
    src_text = raw.decode("utf8", "replace")
    lines = src_text.splitlines()
    tree = parser.parse(raw)

    functions = {}
    for node in walk(tree.root_node):
        if node.type != "function_definition":
            continue
        decl = node.child_by_field_name("declarator")
        name = None
        for d in walk(decl):
            if d.type == "function_declarator":
                ident = d.child_by_field_name("declarator")
                if ident is not None:
                    name = text(ident, raw)
                break
        if not name or name in functions:
            continue
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        body = node.child_by_field_name("body")
        calls, config, delays, io, inline_comments = [], [], [], [], []
        dev_flags, globals_ = set(), set()
        for x in walk(body) if body else []:
            if x.type == "call_expression":
                fn = x.child_by_field_name("function")
                fname = text(fn, raw)
                args = x.child_by_field_name("arguments")
                argv = split_args(text(args, raw)[1:-1]) if args else []
                ln = x.start_point[0] + 1
                calls.append({"fn": fname, "line": ln})
                m = CONFIG_RE.match(fname)
                if m:
                    op = "read" if "read" in fname else "write"
                    config.append({"op": op, "api": fname,
                                   "offset": argv[1] if len(argv) > 1 else None,
                                   "value": argv[2] if op == "write" and len(argv) > 2 else None,
                                   "line": ln})
                if fname in DELAY_FNS:
                    delays.append({"fn": fname, "value": ", ".join(argv), "line": ln})
                if IO_FNS_RE.match(fname):
                    io.append({"fn": fname, "args": argv, "line": ln})
            elif x.type == "comment":
                inline_comments.append({"line": x.start_point[0] + 1,
                                        "text": clean_comment(text(x, raw))})
        btxt = text(node, raw)
        timing = []
        for tm in re.finditer(r"(\w*(?:d3hot_delay|d3cold_delay|_delay|timeout))\s*=\s*([^;]+);", btxt):
            timing.append({"kind": "assignment", "target": tm.group(1), "value": tm.group(2).strip(),
                           "line": start + btxt.count("\n", 0, tm.start())})
        for tm in re.finditer(r"\b(quirk_d3hot_delay|msecs_to_jiffies|time_before|time_after|ktime_\w+|read_poll_timeout\w*|jiffies)\b\s*(\([^;]*\))?", btxt):
            timing.append({"kind": "timeout_or_delay_api", "target": tm.group(1), "value": (tm.group(2) or "").strip("()"),
                           "line": start + btxt.count("\n", 0, tm.start())})
        for m in DEV_FLAG_RE.finditer(btxt):
            dev_flags.add(m.group(2))
        for m in GLOBAL_FLAG_RE.finditer(btxt):
            globals_.add(m.group(1))
        doc, doc_lines = preceding_comment(lines, start)
        sig = text(node, raw).split("{", 1)[0].strip()
        functions[name] = {
            "name": name, "loc": {"start": start, "end": end},
            "signature": re.sub(r"\s+", " ", sig),
            "static_storage": sig.startswith("static"),
            "init_section": "__init" in sig,
            "doc_comment": doc, "doc_comment_lines": doc_lines,
            "inline_comments": inline_comments,
            "calls": calls, "config_access": config, "delays": delays, "timing": timing, "io": io,
            "dev_flags_set": sorted(dev_flags), "global_flags": sorted(globals_),
            "fixups": [], "exported": None, "loc_count": end - start + 1,
        }

    # Preamble comments: every comment block between the previous function and this one
    # (catches rationale attached to static variables / tables that sit above a function)
    order = sorted(functions.values(), key=lambda f: f["loc"]["start"])
    prev_end = 0
    for f in order:
        region = "\n".join(lines[prev_end:f["loc"]["start"] - 1])
        pre = []
        for cm in re.finditer(r"/\*.*?\*/", region, re.S):
            a = prev_end + region.count("\n", 0, cm.start()) + 1
            b = prev_end + region.count("\n", 0, cm.end()) + 1
            if f.get("doc_comment_lines") and a == f["doc_comment_lines"][0]:
                continue
            txt = clean_comment(cm.group(0))
            if len(txt) > 40:
                pre.append({"lines": [a, b], "text": txt})
        f["preamble_comments"] = pre
        prev_end = f["loc"]["end"]

    # Fixup registrations (regex: macros at file scope, sometimes inside #ifdef)
    fixups = []
    for m in FIXUP_RE.finditer(src_text):
        macro = m.group(1)
        is_class = bool(m.group(2))
        phase = m.group(3).lower()
        args = split_args(re.sub(r"/\*.*?\*/", " ", m.group("args"), flags=re.S))
        ln = lineno(src_text, m.start())
        if len(args) < 3 or not re.fullmatch(r"\w+", args[-1]) or "\\" in m.group(0):
            continue  # skip macro definitions / wrapper macros
        f = {"macro": macro, "phase": phase, "class_match": is_class,
             "vendor": args[0], "device": args[1], "hook": args[-1], "line": ln}
        if is_class and len(args) >= 5:
            f["class"], f["class_shift"] = args[2], args[3]
        fixups.append(f)
        f["hook_defined_here"] = f["hook"] in functions
        c, cl = preceding_comment(lines, ln)
        if c and cl and cl[1] >= ln - 3 and "DECLARE_PCI_FIXUP" not in c:
            f["site_comment"], f["site_comment_lines"] = c, cl
        elif len(fixups) > 1 and fixups[-2]["hook"] == f["hook"] and fixups[-2]["line"] >= ln - 2 and fixups[-2].get("site_comment"):
            # consecutive registrations share the comment block above the group
            f["site_comment"], f["site_comment_lines"] = fixups[-2]["site_comment"], fixups[-2]["site_comment_lines"]
        tm = re.search(r"/\*(.*?)\*/\s*$", lines[lineno(src_text, m.end()) - 1])
        if tm:
            f["trailing_comment"] = tm.group(1).strip()
        if f["hook"] in functions:
            functions[f["hook"]]["fixups"].append(f)

    # Wrapper macros: "#define FOO(vid) DECLARE_PCI_FIXUP_X(VENDOR, vid, ..., hook)"
    for dm in re.finditer(r"#define\s+(\w+)\((\w+)\)\s*\\\s*\n\s*(DECLARE_PCI_FIXUP_(CLASS_)?([A-Z_]+?))\s*\(([^;]*?)\)", src_text, re.S):
        wname, param, macro, is_class, phase, body = dm.groups()
        wargs = split_args(re.sub(r"[\\\n]", " ", body))
        hook = wargs[-1]
        for im in re.finditer(r"^%s\((\w+)\);" % re.escape(wname), src_text, re.M):
            f = {"macro": macro, "phase": phase.lower(), "class_match": bool(is_class),
                 "vendor": wargs[0], "device": im.group(1) if wargs[1] == param else wargs[1],
                 "hook": hook, "line": lineno(src_text, im.start()), "via_macro": wname,
                 "hook_defined_here": hook in functions}
            if is_class and len(wargs) >= 5:
                f["class"], f["class_shift"] = wargs[2], wargs[3]
            fixups.append(f)
            if hook in functions:
                functions[hook]["fixups"].append(f)

    # Exports
    for m in re.finditer(r"EXPORT_SYMBOL(_GPL)?(_NS)?\s*\(\s*(\w+)", src_text):
        if m.group(3) in functions:
            functions[m.group(3)]["exported"] = "EXPORT_SYMBOL" + (m.group(1) or "")

    # Call-graph resolution: local vs external kernel API
    local = set(functions)
    for f in functions.values():
        f["calls_local"] = sorted({c["fn"] for c in f["calls"] if c["fn"] in local and c["fn"] != f["name"]})
        f["calls_external"] = sorted({c["fn"] for c in f["calls"] if c["fn"] not in local})
    called_by = {n: set() for n in functions}
    for f in functions.values():
        for c in f["calls_local"]:
            called_by[c].add(f["name"])
    for n, s in called_by.items():
        functions[n]["called_by"] = sorted(s)
        # a hook may also be referenced via function pointer tables (e.g. reset/ACS tables)

    # Function-pointer tables (e.g. pci_dev_reset_methods, pci_dev_acs_enabled)
    table_refs = {}
    for tm in re.finditer(r"static const struct (\w+)\s+(\w+)\[\]\s*=\s*\{(.*?)\n\};", src_text, re.S):
        for fn in local:
            if re.search(r"\b%s\b" % re.escape(fn), tm.group(3)):
                table_refs.setdefault(fn, []).append({"table": tm.group(2), "struct": tm.group(1),
                                                      "line": lineno(src_text, tm.start())})
    for fn, refs in table_refs.items():
        functions[fn]["table_refs"] = refs

    json.dump({"file": path, "line_count": len(lines),
               "functions": functions, "fixups": fixups}, sys.stdout, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
