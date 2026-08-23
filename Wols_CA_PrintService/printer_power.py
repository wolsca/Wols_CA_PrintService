"""Waking the printer and waiting for it to come back.

A printer that is switched off or in deep sleep answers nothing at all: the
self-test then showed `ipptool: Unable to connect ... Host is down` and the job
failed with 'the printer refused the job', although the observation itself was
completely correct - there simply was no printer on the network yet.

Two things are possible over the network:

* **Wake-on-LAN** - a magic packet on UDP port 9 wakes a printer that has WOL
  enabled. It needs the MAC address of the printer (`hardware.printer_mac`),
  because the packet carries nothing else; an IP address cannot be used for it.
  A printer whose power switch is off can never be woken this way.
* **Waiting** - most network printers keep their network card alive in sleep
  mode and wake up on the first connection, and a printer that is switched on by
  hand appears after a few seconds. So when waking is impossible the service
  simply waits (`hardware.wait_for_printer_seconds`) until the printer answers
  instead of throwing the job away.

Reachability is decided with a plain TCP connect, not with ICMP: ping is often
blocked while IPP answers, and it is the port that has to be open anyway.
"""

import socket
import subprocess
import time

import config

# The ports a print job can possibly use, in the order they are tried.
FALLBACK_PORTS = (631, 443, 9100, 80)
WOL_PORT = 9
PROBE_TIMEOUT = 3.0
POLL_INTERVAL = 5.0


def settings():
    return config.get_config().get("hardware", {}) or {}


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def wait_seconds():
    """How long a job may wait for a sleeping printer. 0 switches it off."""
    return max(0, _int(settings().get("wait_for_printer_seconds", 900), 900))


def wol_enabled():
    return bool(settings().get("wake_on_lan", True))


def normalize_mac(value):
    """'00-1B-A9-12-34-56' -> '00:1b:a9:12:34:56'; '' when it is not a MAC."""
    import printer_discovery
    return printer_discovery.normalize_mac(value)


def printer_mac():
    """The *working* MAC address: the one the printer answers with right now."""
    return normalize_mac(settings().get("printer_mac"))


def wired_mac():
    """The MAC address of the cable interface, as printed on the printer."""
    return normalize_mac(settings().get("printer_mac_wired"))


def wifi_mac():
    """The MAC address of the Wi-Fi interface - a *different* address."""
    return normalize_mac(settings().get("printer_mac_wifi"))


def known_macs():
    """Every address that may belong to this printer, working one first.

    A printer has one MAC address per interface, so a machine that is moved from
    the cable to Wi-Fi keeps being the same printer under another address. Both
    are configured, and the working one says which of them is in use now - that
    is also the set searched for when the DHCP address changed.
    """
    macs = []
    for mac in (printer_mac(), wired_mac(), wifi_mac()):
        if mac and mac not in macs:
            macs.append(mac)
    return macs


def mac_role(mac):
    """'wired' / 'wifi' / 'working' / '' - which configured address this is."""
    mac = normalize_mac(mac)
    if not mac:
        return ""
    if mac == wired_mac():
        return "wired"
    if mac == wifi_mac():
        return "wifi"
    if mac == printer_mac():
        return "working"
    return ""


def recovery_enabled():
    """Whether a printer that moved to another IP address may be looked up."""
    return bool(settings().get("recover_printer_ip", True))


def block_on_mac_change():
    """Whether a job is stopped when an unknown MAC address answers."""
    return bool(settings().get("block_on_mac_change", True))


def uri_host_port(uri):
    """('ipps://host:443/ipp/print') -> ('host', 443). ('', None) when unusable."""
    text = str(uri or "")
    if "://" not in text:
        return "", None
    scheme, _, rest = text.partition("://")
    authority = rest.partition("/")[0].split("@")[-1]
    port = None
    if authority.startswith("["):                       # IPv6 literal
        host, _, tail = authority.partition("]")
        host += "]"
        if tail.startswith(":"):
            port = _int(tail[1:], None)
    elif ":" in authority:
        host, _, tail = authority.rpartition(":")
        port = _int(tail, None)
    else:
        host = authority
    if port is None:
        port = {"ipps": 443, "https": 443, "ipp": 631, "http": 631, "socket": 9100}.get(scheme)
    return host, port


def default_target():
    """The configured default printer, so a caller does not have to look it up."""
    printers = config.get_config().get("printers", {}) or {}
    targets = printers.get("targets") or []
    return next((t for t in targets if t.get("id") == printers.get("default")),
                targets[0] if targets else {})


def candidates(target=None, uri=None):
    """The (host, port) pairs that say whether the printer is on the network."""
    if uri is None:
        uri = settings().get("printer_uri", "")
    hosts = []
    ports = []
    uri_host, uri_port = uri_host_port(uri)
    if uri_host:
        hosts.append(uri_host)
        if uri_port:
            ports.append(uri_port)
    if target:
        if target.get("host"):
            hosts.append(str(target["host"]))
        target_port = _int(target.get("port"), None)
        if target_port:
            ports.append(target_port)
    ports.extend(FALLBACK_PORTS)

    pairs = []
    for host in dict.fromkeys(hosts):
        for port in dict.fromkeys(ports):
            pairs.append((host, port))
    return pairs


def probe(host, port, timeout=PROBE_TIMEOUT):
    """True when something accepts a TCP connection on that port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def reachable(target=None, uri=None, timeout=PROBE_TIMEOUT):
    """Returns (True, 'host:port') as soon as one port answers."""
    pairs = candidates(target, uri)
    for host, port in pairs:
        if probe(host, port, timeout):
            return True, f"{host}:{port}"
    tried = ", ".join(f"{host}:{port}" for host, port in pairs) or "no address configured"
    return False, tried


def magic_packet(mac):
    """The 102 byte Wake-on-LAN payload for this MAC address."""
    cleaned = "".join(ch for ch in str(mac) if ch.isalnum())
    if len(cleaned) != 12:
        raise ValueError(f"'{mac}' is not a MAC address (expected 12 hex digits)")
    address = bytes.fromhex(cleaned)
    return b"\xff" * 6 + address * 16


def send_wol(mac=None, broadcast=None):
    """Sends the magic packet. Returns (sent, detail) - never raises.

    Without an explicit address every known MAC address is woken: a printer that
    is asleep does not say which of its interfaces will come up, and a packet to
    the wrong one costs nothing but a single UDP datagram.
    """
    macs = [normalize_mac(mac)] if mac else known_macs()
    macs = [m for m in macs if m]
    if not macs:
        return False, ("no MAC address configured (hardware.printer_mac, "
                       "printer_mac_wired or printer_mac_wifi), so no Wake-on-LAN packet "
                       "can be built - a magic packet carries nothing but a MAC address")
    broadcast = str(broadcast or settings().get("wake_broadcast") or "255.255.255.255")
    sent = []
    problems = []
    for one in macs:
        try:
            payload = magic_packet(one)
        except ValueError as e:
            problems.append(str(e))
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(payload, (broadcast, WOL_PORT))
            sent.append(one)
        except OSError as e:
            problems.append(f"could not send the Wake-on-LAN packet to {broadcast}: {e}")
    if not sent:
        return False, "; ".join(problems) or "no Wake-on-LAN packet could be sent"
    return True, (f"Wake-on-LAN packet for {', '.join(sent)} sent to "
                  f"{broadcast}:{WOL_PORT}"
                  + (f" ({'; '.join(problems)})" if problems else ""))


def detect_mac(host=None, target=None, uri=None):
    """The MAC address the host currently has in its ARP/neighbour table.

    Only meant to *help filling in* `hardware.printer_mac`: the entry is there
    only while the printer is (or recently was) awake and on the same subnet, so
    it can never replace the configured value - a switched off printer has no
    ARP entry, which is exactly when the MAC address is needed.
    """
    if not host:
        if uri is None:
            uri = settings().get("printer_uri", "")
        host = uri_host_port(uri)[0] or (target or {}).get("host")
    if not host:
        return None
    host = str(host).strip("[]")
    for argv in (["ip", "neigh", "show", host], ["arp", "-n", host]):
        try:
            completed = subprocess.run(argv, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            # Only the line of this address counts: some 'arp' implementations
            # print the whole table when the host is unknown, and then the MAC of
            # a completely different device would be reported.
            if host not in line:
                continue
            for word in line.replace("\t", " ").split():
                cleaned = word.replace("-", ":")
                parts = cleaned.split(":")
                if len(parts) == 6 and all(len(p) == 2 and all(
                        ch in "0123456789abcdefABCDEF" for ch in p) for p in parts):
                    if cleaned.lower() != "00:00:00:00:00:00":
                        return cleaned.lower()
    return None


def store_mac(mac, key="printer_mac"):
    """Writes a MAC address into `hardware.<key>`. True when it was saved."""
    try:
        config.get_config().setdefault("hardware", {})[key] = normalize_mac(mac) or ""
        config.save_config()
        return True
    except Exception as e:
        print(f"[Power] Could not save the printer MAC {mac}: {e}")
        return False


def learn_mac(target=None, uri=None):
    """Fills in the working MAC address from the network when it is still empty.

    Called whenever the printer has just been found on the network: that is the
    only moment the neighbour table holds its MAC address. Saving it there and
    then means the address is available later, when the printer is asleep and
    only a magic packet can wake it. Returns the address it stored, or None.
    """
    if printer_mac():
        return None
    detected = detect_mac(target=target, uri=uri)
    if not detected or not store_mac(detected):
        return None
    # The interface it was found on is unknown, so it is only the working
    # address; the wired and Wi-Fi addresses come from the printer itself.
    if not wired_mac() and not wifi_mac():
        store_mac(detected, "printer_mac_wired")
    print(f"[Power] Printer MAC {detected} detected on the network and saved in "
          f"hardware.printer_mac; the printer can be woken from now on.")
    return detected


def recover_address(target=None, uri=None, save=True):
    """Finds the printer again after its IP address changed.

    The printer does not answer on the configured address any more. That is
    exactly what a DHCP lease without a reservation does after a long power-off,
    and the MAC address is what survives it: whichever address on the network
    carries one of the known MAC addresses *is* the printer, so
    `hardware.printer_uri` and the host of the target are rewritten to it.

    Returns (host, detail); host is '' when nothing was found.
    """
    import printer_discovery

    macs = known_macs()
    if not macs:
        return "", ("no MAC address is configured, so the printer cannot be looked up by "
                    "MAC address - fill in hardware.printer_mac_wired and "
                    "hardware.printer_mac_wifi (they are on the printer itself)")
    if not recovery_enabled():
        return "", "looking the printer up by MAC address is switched off " \
                   "(hardware.recover_printer_ip)"

    host, mac = printer_discovery.find_by_mac(macs)
    if not host:
        return "", (f"none of the known MAC addresses ({', '.join(macs)}) is on the "
                    f"network, so the printer is really switched off or on another subnet")

    old_uri = str(settings().get("printer_uri") or "")
    old_host = uri_host_port(old_uri)[0]
    if old_host == host:
        return host, f"the printer is still on {host}"

    role = mac_role(mac) or "unknown interface"
    detail = (f"the printer with MAC {mac} ({role}) now has IP address {host} instead of "
              f"{old_host or 'the configured address'}")
    if not save:
        return host, detail

    try:
        hardware = config.get_config().setdefault("hardware", {})
        if old_host and old_uri:
            hardware["printer_uri"] = old_uri.replace(old_host, host, 1)
        for entry in config.get_config().get("printers", {}).get("targets") or []:
            if not old_host or str(entry.get("host") or "") == old_host:
                entry["host"] = host
        if mac != printer_mac():
            hardware["printer_mac"] = mac
        config.save_config()
        detail += " - the configuration has been corrected"
    except Exception as e:
        detail += f" - but the configuration could not be saved: {e}"
    print(f"[Power] {detail}")
    return host, detail


def verify_mac(target=None, uri=None, save=True):
    """Checks the configured MAC address against the network and corrects it.

    A MAC address is not forever: a replaced printer, a swapped network card or
    a printer that moved from cable to Wi-Fi all give a different address, and a
    magic packet to the old one silently wakes nothing. So whenever the printer
    is awake the address is compared with what the network says.

    Two addresses are configured on purpose - the wired and the Wi-Fi address of
    the printer, which are never the same - plus the *working* one that says
    which of them answers now. An address that matches neither is not adopted
    silently: the administrator is warned, because it usually means another
    device is answering on that IP address, or the printer really was replaced.

    Returns (status, detail) with status one of 'ok', 'switched', 'detected',
    'corrected', 'unexpected', 'unknown' or 'offline'.
    """
    configured = printer_mac()
    awake, where = reachable(target, uri)
    if not awake:
        if configured:
            return "offline", (f"the printer does not answer ({where}), so the configured "
                               f"MAC {configured} cannot be verified now")
        return "offline", (f"the printer does not answer ({where}) and no MAC address is "
                           f"configured, so the service can only wait for it")

    detected = detect_mac(target=target, uri=uri)
    if not detected:
        if configured:
            return "unknown", (f"the printer answers on {where} but has no entry in the "
                               f"neighbour table (another subnet or a router in between), "
                               f"so {configured} could not be verified")
        return "unknown", (f"the printer answers on {where} but its MAC address is not in "
                           f"the neighbour table - read it from the network page of the "
                           f"printer and fill it in")
    detected = normalize_mac(detected)
    if not configured:
        stored = save and store_mac(detected)
        return "detected", (f"MAC {detected} read from the network and "
                            + ("saved as the working address in hardware.printer_mac"
                               if stored else
                               "not saved - fill it in as hardware.printer_mac"))
    if detected == configured:
        return "ok", f"MAC {configured} matches the address on the network"

    role = mac_role(detected)
    if role in ("wired", "wifi"):
        # Not a different printer at all: the same printer over its other
        # interface. Exactly what the two configured addresses are for, so the
        # working address simply follows along without bothering anybody.
        stored = save and store_mac(detected)
        detail = (f"the printer answers over its {role} interface ({detected}) instead of "
                  f"{configured}"
                  + (" - the working address has been switched over" if stored
                     else " - set hardware.printer_mac to it"))
        print(f"[Power] {detail}")
        return "switched", detail

    if wired_mac() or wifi_mac():
        # Neither of the addresses that were typed in from the printer itself:
        # this is a warning, not something to correct behind the administrator's
        # back - the address may belong to a completely different device that
        # took over the printer's IP address from DHCP.
        detail = (f"MAC {detected} answers on {where}, but that is neither the wired "
                  f"({wired_mac() or 'not configured'}) nor the Wi-Fi "
                  f"({wifi_mac() or 'not configured'}) address of the printer - has the "
                  f"printer been changed, or is another device using this IP address? "
                  f"Check it and fill in the right address")
        print(f"[Power] Warning: {detail}")
        return "unexpected", detail

    stored = save and store_mac(detected)
    detail = (f"the printer on the network has MAC {detected}, the configuration said "
              f"{configured}"
              + (" - the working address has been corrected" if stored
                 else " - correct hardware.printer_mac"))
    print(f"[Power] {detail}")
    return "corrected", detail


def security_check(target=None, uri=None):
    """May this job be sent to whatever is answering there?

    An IP address says nothing about *which* machine answers on it: a DHCP lease
    that moved, a device someone plugged in, or simply another printer. The MAC
    address does identify it, so when the answering address is neither the wired
    nor the Wi-Fi address of the configured printer, the document may be about
    to be printed somewhere it does not belong. With
    `hardware.block_on_mac_change` on, the job is stopped instead.

    Returns (allowed, status, detail).
    """
    status, detail = verify_mac(target, uri)
    if status != "unexpected":
        return True, status, detail
    if not block_on_mac_change():
        return True, status, (f"{detail}. The job continues because "
                              f"hardware.block_on_mac_change is off")
    return False, status, (f"{detail}. The job is stopped for safety "
                           f"(hardware.block_on_mac_change): check the printer and, when it "
                           f"is the right one, put its address in "
                           f"hardware.printer_mac_wired or hardware.printer_mac_wifi")


def describe(target=None):
    """One line for the report: how the printer would be woken."""
    macs = known_macs()
    if not wol_enabled():
        return "waking is switched off (hardware.wake_on_lan is false)"
    if not macs:
        detected = detect_mac(target=target)
        hint = f" - the printer currently answers from MAC {detected}" if detected else ""
        return ("no Wake-on-LAN possible - no MAC address is configured, so the service "
                f"only waits for the printer{hint}")
    return (f"Wake-on-LAN to {', '.join(macs)} "
            f"(broadcast {settings().get('wake_broadcast') or '255.255.255.255'}; "
            f"working {printer_mac() or '-'}, wired {wired_mac() or '-'}, "
            f"Wi-Fi {wifi_mac() or '-'})")


def wait_until_reachable(target=None, uri=None, timeout=None, on_wait=None,
                         should_stop=None, interval=POLL_INTERVAL):
    """Waits until the printer answers, waking it first when that is possible.

    `on_wait(elapsed, remaining, detail)` is called on every poll, so the caller
    can keep the job log, the web app and Home Assistant informed. `should_stop`
    aborts the wait (shutdown or a cancelled job).

    Returns (ready, detail).
    """
    ok, where = reachable(target, uri)
    if ok:
        return True, where

    # Before concluding that the printer is off: it may simply have another IP
    # address than the one in the configuration (DHCP without a reservation).
    # The MAC address does not change, so the printer is looked up by it.
    host, recovered = recover_address(target, uri)
    if host:
        ok, where = reachable(target, uri)
        if ok:
            print(f"[Power] {recovered}")
            return True, where

    limit = wait_seconds() if timeout is None else max(0, int(timeout))
    woken = None
    if wol_enabled():
        sent, woken = send_wol()
        print(f"[Power] {woken}")
        if not sent and limit == 0:
            return False, f"the printer does not answer ({where}); {woken}"

    if limit == 0:
        return False, (f"the printer does not answer ({where}) and waiting is switched off "
                       f"(hardware.wait_for_printer_seconds is 0)")

    started = time.time()
    last_wol = started
    while True:
        if should_stop is not None and should_stop():
            return False, "the wait was stopped"
        elapsed = time.time() - started
        remaining = limit - elapsed
        if remaining <= 0:
            return False, (f"the printer did not come back within {limit} s ({where})"
                           + (f"; {woken}" if woken else ""))
        if on_wait is not None:
            on_wait(int(elapsed), int(remaining), woken)
        time.sleep(min(interval, max(1.0, remaining)))

        ok, where = reachable(target, uri)
        if ok:
            return True, where
        # Once a minute, not on every poll: a printer that ignored the first
        # packet (or was still shutting down) gets another one, and a printer
        # that came back on a different IP address is looked up by its MAC -
        # that search walks the whole subnet, so it may not run every 5 seconds.
        if known_macs() and time.time() - last_wol >= 60:
            last_wol = time.time()
            if wol_enabled():
                send_wol()
            if recover_address(target, uri)[0]:
                ok, where = reachable(target, uri)
                if ok:
                    return True, where
