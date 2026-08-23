"""Finding printers on the network - by name, by address and by MAC address.

Two questions have to be answered without anybody reading an IP address from a
printer display:

* **Which printers are out there?** `discover()` asks Avahi/Bonjour for the
  `_ipp._tcp` and `_ipps._tcp` services (that is what every driverless/AirPrint
  printer announces) and, because a printer with mDNS switched off announces
  nothing at all, it also probes port 631 on the local subnet itself. The result
  is the list the administrator picks from in the web app.
* **Where did *our* printer go?** A printer with a DHCP address that is not
  reserved gets a different IP address after a long power-off, and then the
  configured `hardware.printer_uri` points at nothing (or, worse, at another
  device). The MAC address does not change, so `find_by_mac()` looks the printer
  up in the neighbour table of this host and the service recovers the address by
  itself.

The neighbour (ARP) table only holds addresses that were talked to recently, so
it is primed first: a single UDP datagram to every address of the subnet forces
the kernel to resolve the MAC address, which costs no round trip of its own and
does not need root.
"""

import ipaddress
import json
import os
import socket
import subprocess
import threading
import time

import config

IPP_PORT = 631
IPPS_PORT = 443
RAW_PORT = 9100
# A /24 is 254 probes and takes about a second with the pool below; a bigger
# subnet is skipped instead of hammering thousands of addresses.
MAX_SUBNET_HOSTS = 512
PROBE_TIMEOUT = 0.4
WORKERS = 64
ARP_SETTLE = 1.0

# The last discovery, so the web app can show it again without scanning anew.
state = {
    "printers": [],
    "scanned": None,
    "detail": "",
    "running": False,
    # The printers of this scan that were never seen before.
    "new": []
}
_lock = threading.Lock()

# Which printers have been seen before, so a printer that appears on the network
# can be reported instead of silently ending up in the list. It is not a
# configuration setting, so it lives next to the job history.
KNOWN_FILE = os.path.join(config.TEMP_DIR, "known-printers.json")


def normalize_mac(value):
    """'00-1B-A9-12-34-56' / '001ba9123456' -> '00:1b:a9:12:34:56'; '' when invalid."""
    cleaned = "".join(ch for ch in str(value or "") if ch.isalnum()).lower()
    if len(cleaned) != 12 or any(ch not in "0123456789abcdef" for ch in cleaned):
        return ""
    if cleaned == "000000000000":
        return ""
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


def _run(argv, timeout=6):
    """Runs a helper and returns its output; '' when the tool is not there."""
    try:
        completed = subprocess.run(argv, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.decode("utf-8", "replace")


# --- the neighbour table -------------------------------------------------

def neighbours():
    """The whole ARP/neighbour table as {ip: mac}."""
    table = {}
    for argv in (["ip", "neigh", "show"], ["arp", "-a"], ["arp", "-n", "-a"]):
        text = _run(argv)
        if not text:
            continue
        for line in text.splitlines():
            ip = ""
            mac = ""
            for word in line.replace("(", " ").replace(")", " ").replace("\t", " ").split():
                candidate = normalize_mac(word) if ("-" in word or ":" in word) else ""
                if candidate and not mac:
                    mac = candidate
                    continue
                if not ip:
                    try:
                        ip = str(ipaddress.ip_address(word))
                    except ValueError:
                        ip = ""
            if ip and mac:
                table.setdefault(ip, mac)
        if table:
            break
    return table


def local_networks():
    """The IPv4 networks this host is in, small enough to be scanned."""
    networks = []
    text = _run(["ip", "-o", "-4", "addr", "show"])
    for line in text.splitlines():
        for word in line.split():
            if "/" not in word:
                continue
            try:
                interface = ipaddress.ip_interface(word)
            except ValueError:
                continue
            network = interface.network
            if network.is_loopback or network.num_addresses <= 2:
                continue
            if network.num_addresses - 2 > MAX_SUBNET_HOSTS:
                continue
            if network not in networks:
                networks.append(network)
    return networks


def prime_arp(networks=None, port=9):
    """Makes the kernel resolve the MAC address of every host in the subnet.

    One UDP datagram per address: nothing has to answer it, the ARP request the
    kernel sends first is what fills the neighbour table. Returns how many
    addresses were touched.
    """
    touched = 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return 0
    with sock:
        for network in (networks if networks is not None else local_networks()):
            for address in network.hosts():
                try:
                    sock.sendto(b"\x00", (str(address), port))
                    touched += 1
                except OSError:
                    continue
    if touched:
        time.sleep(ARP_SETTLE)          # give the ARP replies time to arrive
    return touched


def find_by_mac(macs, prime=True):
    """The current IP address of one of these MAC addresses, or ''.

    This is the recovery from a changed DHCP address: the MAC address of the
    printer stays the same, so whichever address in the neighbour table carries
    it *is* the printer. Returns (ip, mac).
    """
    wanted = [normalize_mac(m) for m in (macs or []) if normalize_mac(m)]
    if not wanted:
        return "", ""
    for attempt in (0, 1):
        table = neighbours()
        for ip, mac in table.items():
            if mac in wanted:
                return ip, mac
        if attempt == 0 and prime:
            prime_arp()
    return "", ""


# --- discovery -----------------------------------------------------------

def avahi_printers():
    """Everything that announces itself as an IPP printer over mDNS."""
    found = {}
    for service in ("_ipp._tcp", "_ipps._tcp"):
        text = _run(["avahi-browse", "-rtp", service], timeout=12)
        for line in text.splitlines():
            parts = line.split(";")
            # '=' is a resolved record: =;iface;IPv4;name;type;domain;host;ip;port;txt
            if len(parts) < 9 or parts[0] != "=" or parts[2] != "IPv4":
                continue
            host = parts[7].strip()
            if not host:
                continue
            try:
                port = int(parts[8])
            except ValueError:
                port = IPPS_PORT if service == "_ipps._tcp" else IPP_PORT
            name = parts[3].replace("\\032", " ").strip() or host
            entry = found.setdefault(host, {"host": host, "name": name,
                                            "hostname": parts[6].strip(),
                                            "port": port, "source": "mDNS",
                                            "secure": service == "_ipps._tcp"})
            # A printer that offers both: prefer the plain IPP port, that is the
            # one that works without a certificate the server has to trust.
            if service == "_ipp._tcp":
                entry["port"] = port
                entry["secure"] = False
    return list(found.values())


def _probe(host, port, timeout=PROBE_TIMEOUT):
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet(networks=None):
    """Probes port 631 on the whole subnet, for printers without mDNS."""
    networks = local_networks() if networks is None else networks
    addresses = [str(a) for n in networks for a in n.hosts()]
    if not addresses:
        return []

    found = []
    found_lock = threading.Lock()
    index = {"next": 0}

    def worker():
        while True:
            with found_lock:
                position = index["next"]
                index["next"] += 1
            if position >= len(addresses):
                return
            host = addresses[position]
            if _probe(host, IPP_PORT):
                with found_lock:
                    found.append({"host": host, "name": "", "hostname": "",
                                  "port": IPP_PORT, "source": "port 631",
                                  "secure": False})

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(WORKERS, len(addresses)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return sorted(found, key=lambda e: ipaddress.ip_address(e["host"]))


def reverse_name(host):
    try:
        return socket.gethostbyaddr(host)[0]
    except (OSError, socket.herror):
        return ""


def uri_for(entry):
    scheme = "ipps" if entry.get("secure") else "ipp"
    return f"{scheme}://{entry['host']}:{entry.get('port') or IPP_PORT}/ipp/print"


def printer_key(entry):
    """What identifies a printer: its MAC address, or its address without one."""
    return normalize_mac(entry.get("mac")) or f"host:{entry.get('host')}"


def load_known():
    """The printers seen during an earlier scan, as {key: label}."""
    try:
        with open(KNOWN_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_known(known):
    try:
        os.makedirs(os.path.dirname(KNOWN_FILE), exist_ok=True)
        with open(KNOWN_FILE, "w", encoding="utf-8") as handle:
            json.dump(known, handle, indent=4)
    except OSError as e:
        print(f"[Discovery] Could not remember the printers found: {e}")


def report_new_printers(result):
    """Warns the administrator about a printer that was never seen before.

    A printer appearing on the network is worth knowing: it is a new or replaced
    machine to pick in the web app - or something answering on IPP that has no
    business doing so. The very first scan only records what is there, because
    then everything would be 'new'.
    """
    known = load_known()
    first_scan = not known
    fresh = [e for e in result if printer_key(e) not in known]
    for entry in result:
        known[printer_key(entry)] = entry["label"]
    save_known(known)

    with _lock:
        # On the very first search nothing is 'new' - there was simply nothing
        # to compare with yet, and a warning about every printer helps nobody.
        state["new"] = [] if first_scan else [e["label"] for e in fresh]
    if not fresh or first_scan:
        if first_scan and result:
            print(f"[Discovery] {len(result)} printer(s) recorded as known; from now on a "
                  f"printer that appears on the network is reported.")
        return

    message = ("New printer on the network: "
               + "; ".join(e["label"] for e in fresh)
               + ". Choose it in the web app when it has to be printed on, or check "
                 "which device this is.")
    print(f"[Discovery] {message}")
    try:
        import mqtt_service
        mqtt_service.publish_log(message, "warning")
    except Exception:
        pass
    try:
        import notifier
        notifier.send(message, title="New printer found")
    except Exception:
        pass


def discover(include_scan=True):
    """The list of IPP printers on the network, mDNS first, then the port scan.

    Every entry gets its MAC address from the neighbour table, because that is
    exactly what has to be stored for Wake-on-LAN and for finding the printer
    again after a DHCP change.
    """
    with _lock:
        if state["running"]:
            return list(state["printers"])
        state["running"] = True
    try:
        entries = {}
        for entry in avahi_printers():
            entries[entry["host"]] = entry
        if include_scan:
            for entry in scan_subnet():
                if entry["host"] not in entries:
                    entries[entry["host"]] = entry

        prime_arp()
        table = neighbours()
        current = str(config.get_config().get("hardware", {}).get("printer_uri") or "")
        result = []
        for host, entry in entries.items():
            entry["mac"] = table.get(host, "")
            entry["name"] = entry.get("name") or entry.get("hostname") \
                or reverse_name(host) or host
            entry["uri"] = uri_for(entry)
            entry["raw_port_open"] = _probe(host, RAW_PORT)
            entry["configured"] = host in current
            entry["label"] = (f"{entry['name']} - {host}:{entry['port']}"
                              f"{' (' + entry['mac'] + ')' if entry['mac'] else ''}"
                              f"{' - in use' if entry['configured'] else ''}")
            result.append(entry)
        result.sort(key=lambda e: (not e["configured"], e["name"].lower()))

        with _lock:
            state["printers"] = result
            state["scanned"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            state["detail"] = (f"{len(result)} printer(s) with IPP support found"
                               if result else
                               "no printer with IPP support found - is it switched on and "
                               "on this subnet?")
        print(f"[Discovery] {state['detail']}")
        # Every printer in the journal, one line each: this is the only place
        # where 'which machines on this network speak IPP' is written down, and
        # it is what a wrong printer or a double address is recognised by.
        for entry in result:
            print(f"[Discovery] {entry['label']} "
                  f"({entry['source']}, {entry['uri']}"
                  f"{', port 9100 open' if entry['raw_port_open'] else ''})")
        report_new_printers(result)
        return result
    finally:
        with _lock:
            state["running"] = False


def payload():
    """What the web app needs: the list plus when it was made."""
    with _lock:
        return {"printers": list(state["printers"]), "scanned": state["scanned"],
                "detail": state["detail"], "running": state["running"],
                "new": list(state["new"])}
