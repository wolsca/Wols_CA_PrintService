import os
import shutil
import subprocess
import threading
import time
import config

# How long a capability probe stays valid. The answer only changes when the
# printer is replaced or its firmware is updated.
CACHE_SECONDS = 900.0

_lock = threading.Lock()
_cache = {}

ATTRIBUTES_REQUEST = """{
    OPERATION Get-Printer-Attributes
    GROUP operation-attributes-tag
    ATTR charset attributes-charset utf-8
    ATTR naturalLanguage attributes-natural-language en
    ATTR uri printer-uri $uri
    ATTR keyword requested-attributes all
}
"""


def _request_file():
    """The ipptool request used to ask the printer what it can do."""
    path = os.path.join(config.TEMP_DIR, "get-printer-attributes.test")
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(ATTRIBUTES_REQUEST)
    return path


def is_ipp_uri(uri):
    return str(uri or "").startswith(("ipp://", "ipps://"))


def probe(uri, force=False):
    """Returns the printer attributes as {name: value}, or {} when unavailable."""
    uri = str(uri or "")
    if not is_ipp_uri(uri) or not shutil.which("ipptool"):
        return {}

    with _lock:
        cached = _cache.get(uri)
        if cached and not force and (time.time() - cached["at"]) < CACHE_SECONDS:
            return cached["attributes"]

    attributes = {}
    try:
        result = subprocess.run(["ipptool", "-tv", uri, _request_file()],
                                capture_output=True, text=True, timeout=20)
        for line in (result.stdout or "").splitlines():
            if "=" not in line or "(" not in line:
                continue
            name = line.strip().split("(", 1)[0].strip()
            value = line.split("=", 1)[1].strip()
            if name and name not in attributes:
                attributes[name] = value
    except Exception as e:
        print(f"[Printer] Could not read the capabilities of {uri}: {e}")

    with _lock:
        _cache[uri] = {"at": time.time(), "attributes": attributes}
    return attributes


def supports_manual_duplex(uri, force=False):
    """True when the printer prompts on its own panel to reload the sheets.

    That is the AirPrint/Mopria manual duplex flow: the printer prints the front
    sides, asks on its display to put the stack back and continues after the
    button on the printer is pressed. Note that such printers report
    'sides-supported = one-sided', so this can never run through the CUPS
    'everywhere' queue - the job has to go straight to the printer.
    """
    attributes = probe(uri, force)
    if not attributes:
        return False
    manual = attributes.get("manual-duplex-supported", "")
    creation = attributes.get("job-creation-attributes-supported", "")
    return "true" in manual.lower() and "manual-duplex-sheet-count" in creation


def flip_owner(target=None):
    """Who confirms the flip halfway through a job: 'printer' or 'service'.

    'printer' means the button on the printer itself; the Continue button in the
    web app and in Home Assistant is then taken away, so there is never a second
    button competing with the printer panel.
    """
    hardware = config.get_config().get("hardware", {})
    mode = str(hardware.get("flip_confirmation", "auto") or "auto").strip().lower()
    if mode == "service":
        return "service"

    uri = hardware.get("printer_uri", "")
    if mode == "printer":
        # Explicitly requested: only honoured when the printer can be reached
        # over IPP, otherwise the job would stall forever.
        return "printer" if is_ipp_uri(uri) and shutil.which("ipptool") else "service"

    return "printer" if supports_manual_duplex(uri) else "service"


def describe(target=None):
    """One line for the self-test and the logs."""
    hardware = config.get_config().get("hardware", {})
    uri = hardware.get("printer_uri", "")
    owner = flip_owner(target)
    if owner == "printer":
        return f"the printer confirms the flip on its own panel ({uri})"
    if not is_ipp_uri(uri):
        return f"the service asks for the flip ({uri} is not an IPP target)"
    if not shutil.which("ipptool"):
        return "the service asks for the flip ('ipptool' is not installed)"
    return f"the service asks for the flip ({uri} does not offer manual duplex)"
