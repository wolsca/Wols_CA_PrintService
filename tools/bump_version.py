#!/usr/bin/env python3
"""Version tooling for the Wols CA Print Service.

Two independent counters live in the repository root:

    BUILD_NUMBER   the commit number, raised by one on every commit
    VERSION        the release "x.y" - x by hand, y by this script

Usage:
    python tools/bump_version.py --build           # +1 commit number (git hook)
    python tools/bump_version.py --release         # compute y for a release
    python tools/bump_version.py --release --major # first raise x by one, y = 0
    python tools/bump_version.py --show            # print the current version

Release rule: when x was raised (by hand in VERSION, or with --major) the minor
release restarts at 0, otherwise it becomes the current y + 1. The release the
minor counter was last bumped for is remembered in .version-released.
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")
BUILD_FILE = os.path.join(REPO_ROOT, "BUILD_NUMBER")
RELEASED_FILE = os.path.join(REPO_ROOT, ".version-released")


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return default


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{text}\n")


def split_release(release):
    parts = str(release).split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        major = 0
    try:
        minor = int(parts[1])
    except (ValueError, IndexError):
        minor = 0
    return major, minor


def current():
    release = read_text(VERSION_FILE, "0.0") or "0.0"
    try:
        build = int(read_text(BUILD_FILE, "0").split()[0])
    except (ValueError, IndexError):
        build = 0
    return release, build


def bump_build():
    release, build = current()
    build += 1
    write_text(BUILD_FILE, build)
    print(f"{release}.{build}")
    return build


def bump_release(major_bump=False):
    release, build = current()
    major, minor = split_release(release)
    released_major, _ = split_release(read_text(RELEASED_FILE, release) or release)

    if major_bump:
        major += 1
        minor = 0
    elif major != released_major:
        # x was raised by hand in VERSION: the minor release restarts.
        minor = 0
    else:
        minor += 1

    new_release = f"{major}.{minor}"
    write_text(VERSION_FILE, new_release)
    write_text(RELEASED_FILE, new_release)
    print(f"{new_release}.{build}")
    return new_release


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bump the version of the print service.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="raise the commit number by one")
    group.add_argument("--release", action="store_true", help="compute the release number")
    group.add_argument("--show", action="store_true", help="print the current version")
    parser.add_argument("--major", action="store_true",
                        help="with --release: raise x by one and reset y to 0")
    args = parser.parse_args(argv)

    if args.build:
        bump_build()
    elif args.release:
        bump_release(args.major)
    else:
        release, build = current()
        print(f"{release}.{build}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
