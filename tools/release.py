#!/usr/bin/env python3
"""Release tooling for the Wols CA Print Service.

A release is cut from the notes that were collected in `changesFixes.md` while
working. This script:

    1. computes the new release number (see tools/bump_version.py),
    2. prepends a section for it to RELEASE_NOTES.md, which therefore keeps the
       complete history, and to CHANGELOG.md,
    3. empties changesFixes.md again, leaving only the template header,
    4. optionally creates the annotated git tag `vX.Y` for the release.

Usage:
    python tools/release.py --dry-run          # show what would happen
    python tools/release.py                    # cut the next minor release
    python tools/release.py --major            # raise x by one, y back to 0
    python tools/release.py --tag              # also create the git tag
"""

import argparse
import os
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bump_version  # noqa: E402

REPO_ROOT = bump_version.REPO_ROOT
CHANGES_FILE = os.path.join(REPO_ROOT, "changesFixes.md")
RELEASE_NOTES_FILE = os.path.join(REPO_ROOT, "RELEASE_NOTES.md")
CHANGELOG_FILE = os.path.join(REPO_ROOT, "CHANGELOG.md")

NOTES_HEADING = "# Release Notes"
CHANGELOG_HEADING = "# Changelog"

CHANGES_TEMPLATE = """# Changes and fixes for the next release

Add one bullet per change while you work. The release tool (`python tools/release.py`) moves
everything below into `RELEASE_NOTES.md` and `CHANGELOG.md` under the new version number and
empties this file again, so it always describes only the *unreleased* work.

## Changes

- (none yet)

## Fixes

- (none yet)
"""


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return default


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def collected_notes():
    """The body of changesFixes.md without its header and explanation."""
    lines = read_text(CHANGES_FILE).splitlines()
    body = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            continue
        if stripped.startswith("Add one bullet per change") or stripped.startswith("everything below") \
                or stripped.startswith("empties this file again"):
            continue
        body.append(line.rstrip())

    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def has_content(notes):
    """False when the file only holds the '(none yet)' placeholders."""
    for line in notes.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped != "- (none yet)":
            return True
    return False


def insert_section(text, heading, section):
    """Puts the section above the newest existing one, keeping the intro intact."""
    if not text.strip():
        return f"{heading}\n\n{section}"

    lines = text.splitlines()
    insert_at = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            insert_at = index
            break
    if insert_at is None:
        # No version section yet: append after the intro.
        return text.rstrip() + "\n\n" + section.rstrip() + "\n"

    head = lines[:insert_at]
    while head and not head[-1].strip():
        head.pop()
    return "\n".join(head + ["", section.rstrip(), ""] + lines[insert_at:]).rstrip() + "\n"


def git_tag(release):
    tag = f"v{release}"
    subprocess.run(["git", "-C", REPO_ROOT, "tag", "-a", tag, "-m", f"Release {tag}"], check=True)
    print(f"[release] Created git tag {tag}; push it with: git push origin {tag}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cut a release from changesFixes.md.")
    parser.add_argument("--major", action="store_true", help="raise x by one and reset y to 0")
    parser.add_argument("--dry-run", action="store_true", help="only show the release section")
    parser.add_argument("--tag", action="store_true", help="also create the annotated git tag")
    parser.add_argument("--allow-empty", action="store_true",
                        help="release even when changesFixes.md holds no entries")
    args = parser.parse_args(argv)

    notes = collected_notes()
    if not has_content(notes) and not args.allow_empty:
        print("[release] changesFixes.md holds no entries; add them first "
              "or pass --allow-empty.", file=sys.stderr)
        return 1

    old_release, build = bump_version.current()
    major, minor = bump_version.split_release(old_release)
    released_major, _ = bump_version.split_release(
        bump_version.read_text(bump_version.RELEASED_FILE, old_release) or old_release)
    if args.major:
        major, minor = major + 1, 0
    elif major != released_major:
        minor = 0
    else:
        minor += 1
    new_release = f"{major}.{minor}"
    full_version = f"{new_release}.{build}"

    section = f"## {full_version} - {date.today().isoformat()}\n\n{notes}\n"
    print(section)
    if args.dry_run:
        print(f"[release] Dry run: {old_release} -> {new_release} (build {build}).")
        return 0

    write_text(RELEASE_NOTES_FILE,
               insert_section(read_text(RELEASE_NOTES_FILE), NOTES_HEADING, section))
    write_text(CHANGELOG_FILE,
               insert_section(read_text(CHANGELOG_FILE), CHANGELOG_HEADING, section))
    bump_version.write_text(bump_version.VERSION_FILE, new_release)
    bump_version.write_text(bump_version.RELEASED_FILE, new_release)
    write_text(CHANGES_FILE, CHANGES_TEMPLATE)

    print(f"[release] Release {full_version} written to RELEASE_NOTES.md and CHANGELOG.md; "
          f"changesFixes.md is empty again.")
    if args.tag:
        git_tag(new_release)
    else:
        print(f"[release] Tag and publish it with: git tag -a v{new_release} "
              f"-m 'Release v{new_release}' && git push origin v{new_release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
