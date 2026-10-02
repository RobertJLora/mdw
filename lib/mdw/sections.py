#!/usr/bin/env python3
"""Section lister and extractor for mdw's t, c and e keys.
list mode:  sections.py list <file>          -> "idx<TAB>level<TAB>srcline<TAB>title" lines
copy mode:  sections.py copy <file> <idx>    -> copies cleaned section text to the clipboard
map mode:   sections.py rendermap <file> <rendered> -> "idx<TAB>level<TAB>renderline<TAB>srcline<TAB>title"
            (renderline found by matching heading titles against the ANSI-stripped render,
            monotonically forward; 0 when a heading could not be located. The styled render
            drops "##" markers, so scanning the render alone would miss every H2 and below)
Cleaning: heading line dropped, "> " quote prefixes stripped, ** markers removed,
internal blank-quote separators become blank lines. Source lines are already
unwrapped paragraphs, so the paste is clean prose."""
import os
import re, subprocess, sys


def headings(lines):
    out = []
    fence = False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', ln)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def main():
    mode, path = sys.argv[1], sys.argv[2]
    lines = open(path, encoding="utf-8").read().split("\n")
    heads = headings(lines)
    if mode == "list":
        for n, (line, lvl, title) in enumerate(heads, 1):
            print(f"{n}\t{lvl}\t{line + 1}\t{title}")
        return
    if mode == "rendermap":
        rendered = open(sys.argv[3], encoding="utf-8", errors="replace").read().split("\n")
        strip = [re.sub(r'\x1b\[[0-9;]*m', '', l).strip() for l in rendered]
        # The render shows emoji where the source has shortcodes (":tada:"),
        # so match headings against the same conversion render.py applies.
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "mdw_render", os.path.join(os.path.dirname(os.path.abspath(__file__)), "render.py"))
            mdr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mdr)
            conv = (lambda s: mdr.convert_shortcodes(s)[0]) if mdr.EMOJI_ENABLED else (lambda s: s)
        except Exception:
            conv = lambda s: s
        j = 0
        for n, (line, lvl, title) in enumerate(heads, 1):
            t = conv(title.strip())
            found = 0
            for k in range(j, len(strip)):
                core = strip[k].lstrip('▌').strip()
                if not core:
                    continue
                if core == t or (t.startswith(core) and len(core) >= min(12, len(t))):
                    found = k + 1
                    j = k + 1
                    break
            print(f"{n}\t{lvl}\t{found}\t{line + 1}\t{title}")
        return
    idx = int(sys.argv[3]) - 1
    if not 0 <= idx < len(heads):
        sys.exit(1)
    start, lvl, _ = heads[idx]
    end = len(lines)
    for j, l2, _t in heads[idx + 1:]:
        if l2 <= lvl:
            end = j
            break
    body = lines[start + 1:end]
    cleaned = []
    for ln in body:
        ln = re.sub(r'^\s*>\s?', '', ln)
        ln = ln.replace('**', '')
        cleaned.append(ln)
    text = "\n".join(cleaned).strip() + "\n"
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            print(f"{len(text)} chars")
            return
        except (OSError, subprocess.CalledProcessError):
            continue
    sys.exit("no clipboard tool found (pbcopy, wl-copy or xclip)")


main()
