#!/usr/bin/env python3
"""Local markdown link lister for mdw's `l` key.

list mode:  mdw_links.py list <file>
  -> one "idx<TAB>status<TAB>abspath<TAB>label" line per local .md reference,
     in document order, deduped by resolved path. status is ok or missing
     (missing lines carry the raw reference in the abspath column).

What counts as a reference (fenced code blocks are skipped):
  * markdown links and autolinks whose target ends in .md: [text](x.md), <x.md>
  * inline code spans holding a .md path: `notes/2026-09-28_Plan.md`
  * bare path tokens ending in .md in plain text
Notes often cite files relative to some project root rather than to the note
itself, so resolution is a cascade:
  1. absolute or ~ path
  2. relative to the doc's folder, then to each ancestor folder in turn
  3. only when MDW_LINK_ROOT is set: path-suffix match over every .md under
     that folder (one find, run only when something is still unresolved); the
     hit closest to the doc's folder wins, newest mtime breaks ties
"""
import os
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = os.path.expanduser(os.environ.get("MDW_LINK_ROOT", ""))
PRUNE = ("Archive", "node_modules", ".git", ".venv")

MD_LINK = re.compile(r'\[([^\]]*)\]\(\s*<?([^)>\s]+\.md)(?:#[^)\s>]*)?>?(?:\s+"[^"]*")?\s*\)', re.I)
AUTOLINK = re.compile(r'<([^<>\s]+\.md)>', re.I)
CODE = re.compile(r'`([^`\n]+\.md)`', re.I)
BARE = re.compile(r'(?<![\w/.~-])((?:~|\.{1,2})?/?[\w.-]+(?:/[\w.-]+)*\.md)\b', re.I)


def refs(text):
    """(ref, label) pairs in document order."""
    out = []
    fence = False
    for ln in text.splitlines():
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        hits = []
        taken = []
        for m in MD_LINK.finditer(ln):
            hits.append((m.start(), m.group(2), m.group(1).strip()))
            taken.append(m.span())
        for rx in (AUTOLINK, CODE):
            for m in rx.finditer(ln):
                if any(a <= m.start() < b for a, b in taken):
                    continue
                hits.append((m.start(), m.group(1), ""))
                taken.append(m.span())
        for m in BARE.finditer(ln):
            if any(a <= m.start() < b for a, b in taken):
                continue
            hits.append((m.start(), m.group(1), ""))
        for _, ref, label in sorted(hits, key=lambda h: h[0]):
            ref = unquote(ref.strip())
            if re.match(r'^[a-z][a-z0-9+.-]*://', ref, re.I) and not ref.startswith("file://"):
                continue
            if ref.startswith("file://"):
                ref = ref[7:]
            out.append((ref, label))
    return out


def by_ancestors(ref, docdir):
    if ref.startswith("~"):
        p = os.path.expanduser(ref)
        return p if os.path.isfile(p) else None
    if os.path.isabs(ref):
        return ref if os.path.isfile(ref) else None
    d = docdir
    while True:
        p = os.path.normpath(os.path.join(d, ref))
        if os.path.isfile(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


_index = None


def index():
    global _index
    if _index is None:
        cmd = ["find", ROOT, "("]
        for i, name in enumerate(PRUNE):
            if i:
                cmd.append("-o")
            cmd += ["-name", name]
        cmd += [")", "-prune", "-o", "-name", "*.md", "-type", "f", "-print"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            _index = res.stdout.splitlines()
        except Exception:
            _index = []
    return _index


def by_suffix(ref, docdir):
    if not ROOT or not os.path.isdir(ROOT):
        return None
    tail = "/" + ref.lstrip("./")
    hits = [p for p in index() if p.endswith(tail)]
    if not hits:
        return None

    def score(p):
        common = os.path.commonpath([p, docdir]) if p.startswith("/") else ""
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        return (len(common), mt)

    return max(hits, key=score)


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "list":
        print(__doc__, file=sys.stderr)
        return 2
    doc = os.path.abspath(sys.argv[2])
    docdir = os.path.dirname(doc)
    try:
        text = open(doc, encoding="utf-8", errors="replace").read()
    except OSError:
        return 1
    seen = set()
    rows = []
    for ref, label in refs(text):
        path = by_ancestors(ref, docdir) or by_suffix(ref, docdir)
        key = path or ref
        if key in seen or (path and os.path.abspath(path) == doc):
            continue
        seen.add(key)
        rows.append(("ok" if path else "missing", path or ref, label or ref))
    for i, (st, p, lab) in enumerate(rows, 1):
        print(f"{i}\t{st}\t{p}\t{lab}".replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
