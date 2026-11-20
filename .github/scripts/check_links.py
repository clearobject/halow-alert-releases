#!/usr/bin/env python3
"""Check the relative links in this repository's Markdown files.

    python3 .github/scripts/check_links.py [ROOT]      (default: the current directory)

For every inline link, image and reference definition in every *.md file under ROOT:
  - a relative path must point to a file or folder that exists inside ROOT;
  - an anchor (#section, or file.md#section) must match a heading in that Markdown file
    (GitHub's anchor rules) or an explicit id/name;
  - a link to a github.com/clearobject repository other than this one is an error, because
    readers of this public repository may not be able to open it.
Web links (http, https, mailto) are not fetched. Links inside code blocks and code spans
are ignored. Exit code 1 lists every broken link as file:line.
"""
import os
import re
import sys
from urllib.parse import unquote

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "dist", "__pycache__", ".venv", "venv"}
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
INLINE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
HTML_ATTR = re.compile(r"""<(?:a|img)\b[^>]*?\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
EXPLICIT_ID = re.compile(r"""<a\b[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.I)
CLEAROBJECT_REPO = re.compile(r"github\.com/clearobject/([\w-]+)", re.I)
PUBLIC_REPOS = {"halow-alert-releases"}   # ClearObject repositories readers can open


def strip_code(lines):
    """Lines with fenced code blocks blanked and code spans removed (line numbers kept)."""
    out, fence = [], None
    for line in lines:
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append("")
            continue
        if m:
            fence = m.group(1)
            out.append("")
            continue
        out.append(CODE_SPAN.sub("", line))
    return out


def slug(text):
    """GitHub's heading anchor: rendered text, lower case, punctuation dropped, spaces to '-'."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)      # links and images -> their text
    text = re.sub(r"<[^>]+>", "", text)                          # inline HTML
    text = text.replace("`", "").replace("*", "")
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)               # _emphasis_, not snake_case
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


_anchor_cache = {}


def anchors(path):
    if path not in _anchor_cache:
        with open(path, encoding="utf-8") as f:
            raw = f.read().splitlines()
        found, counts = set(), {}
        fence = None
        for line in raw:
            m = FENCE.match(line)
            if fence:
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                    fence = None
                continue
            if m:
                fence = m.group(1)
                continue
            for i in EXPLICIT_ID.findall(line):
                found.add(i)
            h = HEADING.match(line)
            if h:
                s = slug(h.group(2))
                n = counts.get(s, 0)
                counts[s] = n + 1
                found.add(s if n == 0 else f"{s}-{n}")
        _anchor_cache[path] = found
    return _anchor_cache[path]


def check_target(root, md_path, target):
    """An error message for one link target, or None when it is fine."""
    m = CLEAROBJECT_REPO.search(target)
    if m and m.group(1).lower() not in PUBLIC_REPOS:
        return ("links to a ClearObject repository that readers of this public repository may not be "
                "able to open; link to this repository or the image instead")
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("//"):
        return None                                              # http:, https:, mailto:, ...
    path_part, _, frag = target.partition("#")
    path_part = unquote(path_part.split("?", 1)[0])
    if path_part:
        if path_part.startswith("/"):
            return "absolute path; use a path relative to this file"
        dest = os.path.normpath(os.path.join(os.path.dirname(md_path), path_part))
        if os.path.relpath(dest, root).startswith(".."):
            return "points outside the repository"
        if not os.path.exists(dest):
            return "no such file or folder"
    else:
        dest = md_path
    if frag and dest.lower().endswith(".md") and os.path.isfile(dest):
        if unquote(frag).lower() not in {a.lower() for a in anchors(dest)}:
            return f"no heading for #{frag} in {os.path.relpath(dest, root)}"
    return None


def main(argv):
    root = os.path.abspath(argv[1] if len(argv) > 1 else ".")
    errors, checked, files = [], 0, 0
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for name in sorted(names):
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(d, name)
            files += 1
            with open(path, encoding="utf-8") as f:
                lines = strip_code(f.read().splitlines())
            for no, line in enumerate(lines, 1):
                targets = [m.group(1) for m in INLINE.finditer(line)]
                targets += HTML_ATTR.findall(line)
                m = REFDEF.match(line)
                if m:
                    targets.append(m.group(1))
                for t in targets:
                    if not t:
                        continue
                    checked += 1
                    why = check_target(root, path, t)
                    if why:
                        errors.append(f"{os.path.relpath(path, root)}:{no}: {t}: {why}")
    for e in errors:
        print(e)
    print(f"check_links: {checked} links in {files} Markdown files, {len(errors)} broken")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
