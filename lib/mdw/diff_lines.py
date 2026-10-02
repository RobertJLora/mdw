#!/usr/bin/env python3
"""Emit the line numbers (1-based, new file) that changed between two rendered
buffers, for mdw's changed-line markers after a live reload. Deletions mark the
line now sitting where the deletion happened. Space-separated, capped at 500."""
import sys, difflib
old = open(sys.argv[1], errors="replace").read().split("\n")
new = open(sys.argv[2], errors="replace").read().split("\n")
sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
out = []
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag in ("replace", "insert"):
        out.extend(range(j1 + 1, j2 + 1))
    elif tag == "delete":
        out.append(min(j1 + 1, len(new)))
print(" ".join(map(str, out[:500])))
