#!/usr/bin/env python3
"""
Validate every internal Markdown link and heading anchor in the repository.

Documentation is a load-bearing artefact here -- the specification, the known
issues, and both test READMEs cross-reference each other constantly -- so a
broken anchor silently strands a reader. This check exists because that happened
repeatedly while the doc set was being written.

Anchor slugs follow GitHub's algorithm: lowercase, strip punctuation except word
characters, hyphens and underscores, then replace each space with a hyphen. Note
that GitHub does NOT collapse runs of spaces, so a heading containing an em dash
yields a double hyphen ("## Q1 — Foo" -> "#q1--foo").

Exit status 0 when every link resolves, 1 otherwise.
"""

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Only skip directories that cannot contain hand-written prose. `corpus/` and
# `baselines/` hold generated JSON and snapshots, but may carry a README that
# must be validated like any other, so they are NOT skipped.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".results"}
LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def slug(heading):
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.replace(" ", "-")


def markdown_files():
    for path in sorted(REPO.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        yield path


def headings(path):
    """Headings outside fenced code blocks."""
    found, in_fence = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING.match(line)
        if m:
            found.add(slug(m.group(2)))
    return found


def main():
    files = list(markdown_files())
    anchors = {p: headings(p) for p in files}

    problems, checked = [], 0
    for path in files:
        base = path.parent
        for label, target in LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#!")):
                continue
            checked += 1
            file_part, _, anchor = target.partition("#")
            resolved = (base / file_part).resolve() if file_part else path
            rel = path.relative_to(REPO)

            if file_part and not resolved.exists():
                problems.append(f"{rel}: [{label}] -> {target}  (path does not exist)")
                continue
            if anchor and resolved in anchors and anchor not in anchors[resolved]:
                problems.append(f"{rel}: [{label}] -> {target}  (no such heading)")

    print(f"checked {checked} internal links across {len(files)} markdown files")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        return 1
    print("all internal links and anchors resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
