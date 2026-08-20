"""Update check and self-update for the Wols CA Print Service.

The installed version comes from the VERSION / BUILD_NUMBER files (see
version.py).

The normal (Home Assistant) update path only reacts to *published GitHub
releases*: the latest release tag is compared with the installed version, and
an update is installed by checking out exactly that tag. Commit builds on the
branch never show up as an update, so a commit does not trigger Home Assistant.

For testing there is a second, explicit path: the *test build*, read from the
VERSION and BUILD_NUMBER files on the configured branch. It is only checked and
installed when the 'Check for test build' / 'Install test build' button is
pressed (in Home Assistant or the web app), never automatically.

    python main.py --check-update             # releases only
    python main.py --check-update --test      # branch (test build)
    python main.py --update                   # install the latest release
    python main.py --update --test            # install the branch head
"""

import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import config
import mqtt_service
import version

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
HTTP_TIMEOUT = 15
MAX_LOG_CHARS = 4000

install_lock = threading.Lock()

state = {
    "installed_version": version.FULL_VERSION,
    "latest_version": version.FULL_VERSION,
    "update_available": False,
    "auto_update": False,
    "checked": None,
    "checking": False,
    "installing": False,
    "title": "Wols CA Print Service",
    "release_url": "",
    "release_notes": "",
    "release_tag": "",
    "last_result": "",
    "last_log": "",
    "check_ok": True,
    # The test build (branch head); only filled on explicit request.
    "test_version": "",
    "test_available": False,
    "test_checked": None,
    "test_result": ""
}


# --- configuration ------------------------------------------------------

def update_config():
    """The 'update' section, with the defaults applied."""
    section = dict(config.get_config().get("update", {}) or {})
    section.setdefault("enabled", True)
    section.setdefault("repository", "wolsca/Wols_CA_PrintService")
    section.setdefault("branch", "main")
    # 'release' (default) reacts to published releases only; 'branch' is the
    # test channel and has to be selected on purpose.
    section.setdefault("channel", "release")
    section.setdefault("allow_test_builds", True)
    section.setdefault("check_interval_hours", 6)
    section.setdefault("auto_update", False)
    section.setdefault("source_directory", "/usr/local/src/wolsca-print-service")
    section.setdefault("update_command", "")
    return section


def topic(suffix):
    return f"{mqtt_service.PREFIX}/update/{suffix}"


# --- version comparison -------------------------------------------------

def parse_version(text):
    """'v1.4.381' -> (1, 4, 381); missing parts become 0."""
    numbers = [int(n) for n in re.findall(r"\d+", str(text or ""))][:3]
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def is_newer(candidate, installed):
    return parse_version(candidate) > parse_version(installed)


# --- remote lookup ------------------------------------------------------

def fetch_text(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "WolsCA-Print-Service",
        "Accept": "application/vnd.github+json"
    })
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def latest_from_release(repo):
    """The latest GitHub release; returns (version, tag, title, url, notes)."""
    payload = json.loads(fetch_text(GITHUB_API.format(repo=repo)))
    tag = payload.get("tag_name") or payload.get("name") or ""
    return (tag.lstrip("vV"),
            tag,
            payload.get("name") or tag,
            payload.get("html_url", ""),
            payload.get("body") or "")


def latest_from_branch(repo, branch):
    """Reads VERSION and BUILD_NUMBER straight from the branch (test build)."""
    release = fetch_text(GITHUB_RAW.format(repo=repo, branch=branch, path="VERSION")).strip()
    build = fetch_text(GITHUB_RAW.format(repo=repo, branch=branch, path="BUILD_NUMBER")).strip()
    latest = f"{release.splitlines()[0].strip()}.{build.splitlines()[0].strip()}"
    notes = ""
    try:
        notes = fetch_text(GITHUB_RAW.format(repo=repo, branch=branch,
                                             path="changesFixes.md"))
    except Exception:
        pass
    return (latest, f"origin/{branch}", f"{branch} branch",
            f"https://github.com/{repo}/tree/{branch}", notes)


def check(publish=True, channel=None):
    """Asks GitHub for the latest *release* and updates the state.

    Only published releases are considered, so a plain commit never makes Home
    Assistant offer an update. Pass channel='branch' (or use check_test_build)
    to look at the branch head instead.
    """
    section = update_config()
    state["installed_version"] = version.FULL_VERSION
    state["auto_update"] = bool(section.get("auto_update"))

    if not section.get("enabled", True):
        state["last_result"] = "Update checking is disabled in the configuration."
        if publish:
            publish_state()
        return state

    state["checking"] = True
    if publish:
        publish_state()

    repo = section.get("repository")
    branch = section.get("branch", "main")
    channel = str(channel or section.get("channel", "release")).lower()
    try:
        try:
            if channel == "branch":
                latest, tag, title, url, notes = latest_from_branch(repo, branch)
            else:
                latest, tag, title, url, notes = latest_from_release(repo)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # No release published yet. That is not an error of this
            # installation, and it deliberately does not fall back to the
            # branch: commits must never look like an update.
            state["latest_version"] = version.FULL_VERSION
            state["update_available"] = False
            state["check_ok"] = True
            state["release_tag"] = ""
            state["last_result"] = (f"No release published yet for {repo}; use the test "
                                    f"build button to try the {branch} branch.")
            print(f"[Update] {state['last_result']}")
            return state
        state["check_ok"] = True
        state["latest_version"] = latest or version.FULL_VERSION
        state["release_tag"] = tag or ""
        state["title"] = title or "Wols CA Print Service"
        state["release_url"] = url
        state["release_notes"] = notes[:MAX_LOG_CHARS]
        state["update_available"] = is_newer(state["latest_version"], version.FULL_VERSION)
        state["last_result"] = ("Release {0} available.".format(state["latest_version"])
                                if state["update_available"] else "Up to date.")
    except Exception as e:
        state["latest_version"] = version.FULL_VERSION
        state["update_available"] = False
        state["check_ok"] = False
        state["last_result"] = f"Update check failed: {e}"
        print(f"[Update] Check failed: {e}")
    finally:
        state["checked"] = datetime.now().isoformat(timespec="seconds")
        state["checking"] = False
        if publish:
            publish_state()

    print(f"[Update] Installed {state['installed_version']}, "
          f"latest {state['latest_version']} - {state['last_result']}")
    if publish:
        mqtt_service.publish_log(state["last_result"], "info")
    return state


# --- installation -------------------------------------------------------

def install_commands(section, ref=None):
    """The commands that bring the checkout and the installation up to date.

    `ref` is the git reference to install: the release tag (default) or
    origin/<branch> for a test build.
    """
    custom = str(section.get("update_command") or "").strip()
    source = section.get("source_directory") or "/usr/local/src/wolsca-print-service"
    if custom:
        return [custom]
    target = ref or state.get("release_tag") or f"origin/{section.get('branch', 'main')}"
    return [
        f"git -C {source} fetch --all --tags --prune",
        f"git -C {source} reset --hard {target}",
        f"bash {source}/deploy/debian/install.sh"
    ]


def install(publish=True, ref=None):
    """Pulls the new version and reinstalls; the systemd unit is restarted."""
    section = update_config()
    if not install_lock.acquire(blocking=False):
        print("[Update] An installation is already running.")
        return state

    state["installing"] = True
    state["last_log"] = ""
    if publish:
        publish_state()
        mqtt_service.publish_log(f"Update to {state['latest_version']} started.", "info")

    log_lines = []
    try:
        for command in install_commands(section, ref):
            print(f"[Update] $ {command}")
            log_lines.append(f"$ {command}")
            completed = subprocess.run(command, shell=True, timeout=1800,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            output = completed.stdout.decode("utf-8", "replace").strip()
            if output:
                log_lines.append(output)
                for line in output.splitlines()[-20:]:
                    print(f"[Update] | {line}")
            if completed.returncode != 0:
                state["last_result"] = f"Update failed (exit code {completed.returncode}): {command}"
                raise RuntimeError(state["last_result"])
        state["last_result"] = f"Updated to {state['latest_version']}. The service restarts."
        print(f"[Update] {state['last_result']}")
    except Exception as e:
        state["last_result"] = state["last_result"] or f"Update failed: {e}"
        print(f"[Error] {state['last_result']}")
    finally:
        state["last_log"] = "\n".join(log_lines)[-MAX_LOG_CHARS:]
        state["installing"] = False
        install_lock.release()
        if publish:
            publish_state()
            mqtt_service.publish_log(state["last_result"],
                                     "info" if "Updated" in state["last_result"] else "error")
    return state


def install_async(ref=None):
    threading.Thread(target=install, kwargs={"ref": ref}, daemon=True).start()


def check_async():
    threading.Thread(target=check, daemon=True).start()


# --- test builds (branch head, on request only) --------------------------

def check_test_build(publish=True):
    """Reads the version on the branch; never marks a normal update."""
    section = update_config()
    repo = section.get("repository")
    branch = section.get("branch", "main")
    if not section.get("allow_test_builds", True):
        state["test_result"] = "Test builds are disabled in the configuration."
    else:
        try:
            latest, _, _, _, notes = latest_from_branch(repo, branch)
            state["test_version"] = latest
            state["test_available"] = is_newer(latest, version.FULL_VERSION)
            state["test_result"] = (f"Test build {latest} available on {branch}."
                                    if state["test_available"]
                                    else f"No newer test build on {branch} ({latest}).")
            state["release_notes"] = (notes or state["release_notes"])[:MAX_LOG_CHARS]
        except Exception as e:
            state["test_version"] = ""
            state["test_available"] = False
            state["test_result"] = f"Test build check failed: {e}"
    state["test_checked"] = datetime.now().isoformat(timespec="seconds")
    print(f"[Update] {state['test_result']}")
    if publish:
        publish_state()
        mqtt_service.publish_log(state["test_result"], "info")
    return state


def install_test_build(publish=True):
    """Installs the branch head, whatever its version - for testing only."""
    section = update_config()
    if not section.get("allow_test_builds", True):
        state["last_result"] = "Test builds are disabled in the configuration."
        print(f"[Update] {state['last_result']}")
        if publish:
            publish_state()
        return state
    branch = section.get("branch", "main")
    print(f"[Update] Installing the test build from origin/{branch}.")
    return install(publish=publish, ref=f"origin/{branch}")


def check_test_build_async():
    threading.Thread(target=check_test_build, daemon=True).start()


def install_test_build_async():
    threading.Thread(target=install_test_build, daemon=True).start()


# --- automatic updates --------------------------------------------------

def set_auto_update(enabled):
    """Persists the automatic update switch in the configuration."""
    c = config.get_config()
    c.setdefault("update", {})["auto_update"] = bool(enabled)
    state["auto_update"] = bool(enabled)
    config.save_config()
    print(f"[Update] Automatic updates {'enabled' if enabled else 'disabled'}.")
    publish_state()
    return state["auto_update"]


def watcher(shutdown_event=None):
    """Checks periodically for releases and installs when auto update is on."""
    section = update_config()
    if not section.get("enabled", True):
        print("[Update] Update checking is disabled.")
        return

    interval = max(1.0, float(section.get("check_interval_hours", 6))) * 3600.0
    while True:
        try:
            check()
            if state["update_available"] and update_config().get("auto_update"):
                print("[Update] Automatic update triggered.")
                install()
        except Exception as e:
            print(f"[Update] Watcher error: {e}")
        if shutdown_event is not None:
            if shutdown_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_watcher(shutdown_event=None):
    threading.Thread(target=watcher, args=(shutdown_event,), daemon=True).start()


# --- MQTT ---------------------------------------------------------------

def payload():
    """The MQTT update payload; the keys are the ones Home Assistant expects."""
    return {
        "installed_version": state["installed_version"],
        "latest_version": state["latest_version"],
        "title": state["title"],
        "release_url": state["release_url"],
        "release_summary": (state["release_notes"] or state["last_result"])[:255],
        "in_progress": bool(state["installing"]),
        "update_available": bool(state["update_available"]),
        "auto_update": bool(state["auto_update"]),
        "checked": state["checked"],
        "check_ok": bool(state["check_ok"]),
        "last_result": state["last_result"],
        "release_tag": state["release_tag"],
        "channel": str(update_config().get("channel", "release")).lower(),
        "test_version": state["test_version"],
        "test_available": bool(state["test_available"]),
        "test_checked": state["test_checked"],
        "test_result": state["test_result"]
    }


def publish_state():
    try:
        mqtt_service.mqtt_client.publish(topic("state"), json.dumps(payload()), retain=True)
        mqtt_service.mqtt_client.publish(topic("auto"),
                                         "ON" if state["auto_update"] else "OFF", retain=True)
    except Exception as e:
        print(f"[Error] Could not publish the update state: {e}")


def publish_ha_discovery():
    """The update entity, the check button and the automatic update switch."""
    device = mqtt_service.DEVICE_INFO
    state["auto_update"] = bool(update_config().get("auto_update"))

    entity = {
        "name": mqtt_service.entity_name("Print Service Update"),
        "state_topic": topic("state"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_install": "INSTALL_UPDATE",
        "device_class": "firmware",
        "icon": "mdi:package-up",
        "unique_id": mqtt_service.uid("wolsca_print_update"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/update/{mqtt_service.NODE_ID}/update/config",
        json.dumps(entity), retain=True)

    sensor = {
        "name": mqtt_service.entity_name("Print Service Version"),
        "state_topic": topic("state"),
        "value_template": "{{ value_json.installed_version }}",
        "json_attributes_topic": topic("state"),
        "icon": "mdi:tag-outline",
        "entity_category": "diagnostic",
        "unique_id": mqtt_service.uid("wolsca_print_version"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/version/config",
        json.dumps(sensor), retain=True)

    check_button = {
        "name": mqtt_service.entity_name("Check for Print Service Update"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "CHECK_UPDATE",
        "icon": "mdi:cloud-search-outline",
        "unique_id": mqtt_service.uid("wolsca_print_update_check"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/{mqtt_service.NODE_ID}/update_check/config",
        json.dumps(check_button), retain=True)

    install_button = {
        "name": mqtt_service.entity_name("Install Print Service Update"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "INSTALL_UPDATE",
        "icon": "mdi:download-box-outline",
        "unique_id": mqtt_service.uid("wolsca_print_update_install"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/{mqtt_service.NODE_ID}/update_install/config",
        json.dumps(install_button), retain=True)

    test_check_button = {
        "name": mqtt_service.entity_name("Check for Print Service Test Build"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "CHECK_TEST_BUILD",
        "icon": "mdi:flask-outline",
        "entity_category": "config",
        "unique_id": mqtt_service.uid("wolsca_print_testbuild_check"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/{mqtt_service.NODE_ID}/testbuild_check/config",
        json.dumps(test_check_button), retain=True)

    test_install_button = {
        "name": mqtt_service.entity_name("Install Print Service Test Build"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "INSTALL_TEST_BUILD",
        "icon": "mdi:flask",
        "entity_category": "config",
        "unique_id": mqtt_service.uid("wolsca_print_testbuild_install"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/{mqtt_service.NODE_ID}/testbuild_install/config",
        json.dumps(test_install_button), retain=True)

    test_sensor = {
        "name": mqtt_service.entity_name("Print Service Test Build"),
        "state_topic": topic("state"),
        "value_template": "{{ value_json.test_version | default('-') }}",
        "json_attributes_topic": topic("state"),
        "icon": "mdi:flask-outline",
        "entity_category": "diagnostic",
        "unique_id": mqtt_service.uid("wolsca_print_testbuild"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/testbuild/config",
        json.dumps(test_sensor), retain=True)

    auto_switch = {
        "name": mqtt_service.entity_name("Print Service Automatic Update"),
        "state_topic": topic("auto"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_on": "AUTOUPDATE_ON",
        "payload_off": "AUTOUPDATE_OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "icon": "mdi:update",
        "unique_id": mqtt_service.uid("wolsca_print_update_auto"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/switch/{mqtt_service.NODE_ID}/update_auto/config",
        json.dumps(auto_switch), retain=True)

    publish_state()
