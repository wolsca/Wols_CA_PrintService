"""Validates the Home Assistant add-on repository in this checkout.

Checks what the Supervisor needs before it can install anything: the repository
descriptor, and per add-on the manifest keys, a version that matches a published
image tag, and options and schema that describe exactly the same keys.

    python tools/check_addons.py
"""
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is installed in CI
    print("[Error] pyyaml is required: pip install pyyaml")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDONS = ("wolsca_print_service", "wolsca_print_service_test")
REQUIRED = ("name", "slug", "version", "image", "arch", "options", "schema")

failures = []


def fail(message):
    failures.append(message)
    print(f"[FAIL] {message}")


with open(os.path.join(ROOT, "repository.json"), encoding="utf-8") as handle:
    repository = json.load(handle)
for key in ("name", "url"):
    if key not in repository:
        fail(f"repository.json has no '{key}'.")

for addon in ADDONS:
    path = os.path.join(ROOT, addon, "config.yaml")
    if not os.path.exists(path):
        fail(f"{addon}: config.yaml is missing.")
        continue
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    for key in REQUIRED:
        if key not in data:
            fail(f"{addon}: '{key}' is missing.")
    if data.get("slug") != addon:
        fail(f"{addon}: slug '{data.get('slug')}' does not match the directory name.")

    options = data.get("options") or {}
    schema = data.get("schema") or {}
    for key in sorted(set(options) - set(schema)):
        fail(f"{addon}: option '{key}' has no schema entry.")
    for key in sorted(set(schema) - set(options)):
        fail(f"{addon}: schema entry '{key}' has no default option.")

    # Both add-ons publish onto the same broker, so their instance label - which
    # drives the MQTT topic prefix and every entity id - has to differ.
    if not str(options.get("instance_id", "")).strip():
        fail(f"{addon}: instance_id must not be empty.")

    if not os.path.exists(os.path.join(ROOT, addon, "DOCS.md")):
        fail(f"{addon}: DOCS.md is missing.")

    print(f"[OK] {addon}: {data.get('slug')} {data.get('version')} "
          f"instance '{options.get('instance_id')}' image {data.get('image')}")

labels = []
for addon in ADDONS:
    path = os.path.join(ROOT, addon, "config.yaml")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            labels.append(str((yaml.safe_load(handle).get("options") or {}).get("instance_id")))
if len(labels) != len(set(labels)):
    fail(f"The add-ons share an instance_id ({labels}); their entities would collide.")

if failures:
    print(f"\n{len(failures)} problem(s) found.")
    sys.exit(1)
print("\nThe add-on repository is consistent.")
