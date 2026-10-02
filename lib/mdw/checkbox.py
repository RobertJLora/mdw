#!/usr/bin/env python3
"""Checkbox helper for mdw's x key.

  mdw_checkbox.py list FILE      -> one line per task item: N<TAB>state<TAB>label
                                    (state is x or space; fenced code skipped)
  mdw_checkbox.py toggle FILE N  -> flip item N's [ ] <-> [x] in place

The write keeps the same inode (truncate + write), so the file watcher sees an
ordinary change and mdw live-reloads the view, holding position.
"""
import re
import sys

BOX = re.compile(r"^(\s*[-*+]\s+\[)([ xX])(\]\s)")


def items(lines):
    """Line indices of task items, skipping fenced code."""
    out, fence = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if not fence and BOX.match(line):
            out.append(i)
    return out


def label(line):
    t = BOX.sub("", line, count=1).strip()
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)  # [text](url) -> text
    t = t.replace("**", "").replace("`", "").replace("*", "")
    return " ".join(t.split())


def main():
    cmd, path = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    idx = items(lines)
    if cmd == "list":
        for n, i in enumerate(idx, 1):
            state = "x" if BOX.match(lines[i]).group(2) in "xX" else " "
            print(f"{n}\t{state}\t{label(lines[i])}")
    elif cmd == "toggle":
        n = int(sys.argv[3])
        if not 1 <= n <= len(idx):
            sys.exit(1)
        i = idx[n - 1]
        m = BOX.match(lines[i])
        new = " " if m.group(2) in "xX" else "x"
        lines[i] = m.group(1) + new + m.group(3) + lines[i][m.end():]
        with open(path, "r+", encoding="utf-8") as fh:
            fh.seek(0)
            fh.write("\n".join(lines))
            fh.truncate()


if __name__ == "__main__":
    main()
