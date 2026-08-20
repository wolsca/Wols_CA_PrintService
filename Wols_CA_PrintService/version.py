"""Single source of truth for the version of this service.

Three plain text files in the repository root carry the version information:

    VERSION           the release, "x.y" - x is the main release (bumped by
                      hand), y the minor release (bumped by the release tool)
    BUILD_NUMBER      the commit number, incremented on every commit
    .version-released the release the minor counter was last bumped for, used
                      by tools/bump_version.py to decide whether y restarts

Both files are copied next to the modules by deploy/debian/install.sh, so the
lookup checks the module directory first and the repository root second.

    from version import FULL_VERSION      # "1.4.381"
    version.version_info()               # dict for MQTT / the web app
"""

import os
import subprocess

DEFAULT_RELEASE = "0.0"
DEFAULT_BUILD = 0

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(MODULE_DIR)


def _read(filename):
    """Reads a version file from the module directory or the repository root."""
    for directory in (MODULE_DIR, REPO_ROOT):
        path = os.path.join(directory, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
            if text:
                return text
        except OSError:
            continue
    return ""


def read_release():
    """The hand-maintained 'x.y' release, e.g. '1.4'."""
    text = _read("VERSION").splitlines()[0].strip() if _read("VERSION") else ""
    return text or DEFAULT_RELEASE


def read_build():
    """The commit number, e.g. 381."""
    text = _read("BUILD_NUMBER")
    try:
        return int(str(text).split()[0])
    except (ValueError, IndexError):
        return DEFAULT_BUILD


def split_release(release=None):
    """Returns (major, minor) as integers, tolerating a malformed file."""
    parts = str(release or read_release()).split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        major = 0
    try:
        minor = int(parts[1])
    except (ValueError, IndexError):
        minor = 0
    return major, minor


def git_commit():
    """Short commit hash when the code runs from a checkout, else ''."""
    try:
        completed = subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if completed.returncode == 0:
            return completed.stdout.decode("utf-8", "replace").strip()
    except Exception:
        pass
    return ""


RELEASE = read_release()
BUILD = read_build()
MAJOR, MINOR = split_release(RELEASE)

# "1.4.381" - release plus commit number; this is what the service reports.
FULL_VERSION = f"{RELEASE}.{BUILD}"


def version_info():
    """Everything the MQTT payloads, Home Assistant and the web app need."""
    return {
        "version": FULL_VERSION,
        "release": RELEASE,
        "major": MAJOR,
        "minor": MINOR,
        "build": BUILD,
        "commit": git_commit()
    }


if __name__ == "__main__":
    for key, value in version_info().items():
        print(f"{key}: {value}")
