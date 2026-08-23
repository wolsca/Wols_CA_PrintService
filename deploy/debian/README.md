# Deploying on Debian or Ubuntu (Proxmox)

This guide moves the print service from the Windows desktop to a small
Debian/Ubuntu server (for example the host that already runs your local DNS)
running under Proxmox VE.

Supported and tested: **Debian 12/13** and **Ubuntu 22.04/24.04** (any apt based
derivative such as Raspberry Pi OS works too). The installer detects the
distribution from `/etc/os-release` and uses the same `apt` packages on all of
them.

A **minimal** installation (netinst without any task selected) is enough: step
1/8 installs the complete dependency set itself - `ca-certificates`, `curl`,
`wget`, the Python tool chain (`python3`, `-venv`, `-pip`, `-setuptools`,
`-dev`, `build-essential`), `avahi-daemon`/`avahi-utils`/`libnss-mdns` for mDNS,
and `iproute2`, `iputils-ping`, `procps`, `psmisc`, `file`, `less`, `tzdata`,
`locales` plus the archive tools for the diagnostics and the updater. Unless
`--without-cups` is given it also installs `cups`, `cups-daemon`, `cups-client`, `cups-bsd`,
`cups-filters`, `cups-ipp-utils`, `printer-driver-cups-pdf`, `ghostscript` and
`poppler-utils`. A package this distribution does not offer is reported and
skipped instead of aborting the installation. Only `git` is a prerequisite -
the checkout comes from it.

For per-device usage (iPhone, Android, Windows, Linux) see
[`../../docs/USER_GUIDE.md`](../../docs/USER_GUIDE.md).

## 1. Sizing and where to run it

The service is I/O bound; PDF imposition with `pypdf` is the only CPU work.

| Resource | Minimum | Comfortable |
| --- | --- | --- |
| vCPU | 1 | 2 |
| RAM | 512 MB | 1 GB |
| Disk | 2 GB | 4 GB (spool + CUPS logs) |

Two options on Proxmox:

* **Install next to the DNS service** on the existing Debian guest. Smallest
  footprint, but CUPS then listens on that host too (port 631).
* **Dedicated LXC container** (recommended if you want to keep the DNS host
  clean): `Debian 12` or `Ubuntu 24.04` template, 1 vCPU / 512 MB RAM / 4 GB disk, **unprivileged**
  is fine. CUPS and cups-pdf work in an unprivileged container because no
  physical printer device is attached — the physical printer is reached over
  TCP port 9100.

Give the guest a static IP (or a DHCP reservation) and a DNS record such as
`print.lan`, because Windows clients will point at that name.

## 2. Prepare the guest

```bash
apt update && apt -y upgrade
apt -y install git
git clone <your-repo-url> /usr/local/src/wolsca-print-service
cd /usr/local/src/wolsca-print-service
```

## 3. Install the service

```bash
chmod +x deploy/debian/*.sh          # only if this script itself is not executable
sudo ./deploy/debian/install.sh
```

CUPS and the intake queues are installed **by default** - without them nothing
can be printed to the service. `--with-cups` is still accepted (and does
nothing); use `--without-cups` only when CUPS is managed elsewhere.

What the installer does:

1. installs `python3`, `python3-venv`, `python3-pip`, `avahi-daemon` and
   `avahi-utils` (Bonjour/mDNS, so the host is reachable as `<hostname>.local`);
2. creates the system user `wolsca` (member of group `lp`);
3. creates `/var/spool/wolsca/{PrintFileDrop,PrintTemp,PrintError}` (setgid `lp`,
   so both cups-pdf and the service can write there);
4. copies the service to `/opt/wolsca-print-service` and the default config to
   `/etc/wolsca/WolsCAPrintService.json`;
5. builds `/opt/wolsca-print-service/venv` and installs `requirements.txt`;
6. installs CUPS, cups-pdf, `cups-ipp-utils` and Avahi (unless `--without-cups`),
   redirects the cups-pdf output to the drop directory, creates the shared queue
   `WolsCA_Booklet` (driverless/AirPrint friendly, `application/pdf` default),
   enables network sharing and advertises the web app over mDNS
   (`/etc/avahi/services/wolsca-print-web.service`);
7. installs and starts the `wolsca-print-service` systemd unit and, when `ufw` is
   active, opens TCP 631, TCP 8080 and UDP 5353;
8. runs `fix-permissions.sh` as the last step, so every location has the right
   owner, group and mode - also for what `--install-printer` just created.

With `--without-cups` you can set the queues up later:

```bash
sudo WOLSCA_CONFIG=/etc/wolsca/WolsCAPrintService.json \
     /opt/wolsca-print-service/venv/bin/python \
     /opt/wolsca-print-service/main.py --install-printer
```

### Ownership and permissions

All access rights are owned by one idempotent script, so a wrong mode can always
be repaired without reinstalling:

```bash
sudo ./deploy/debian/fix-permissions.sh             # from the checkout
sudo /opt/wolsca-print-service/fix-permissions.sh   # after an install
```

| Location | Owner:group | Mode |
| --- | --- | --- |
| `/opt/wolsca-print-service` | `root:root` | `0755`, files `0644` |
| `/etc/wolsca` | `root:root` | `0755` (must be traversable by CUPS and the diagnostics) |
| `/etc/wolsca/WolsCAPrintService.json` | `root:root` | `0664` (the installer rewrites the printer target) |
| `/var/spool/wolsca` and every drop/temp/error directory | `root:lp` | `2775` (setgid, so files created by cups-pdf keep group `lp`) |
| Spooled files | `root:lp` | `0664` |
| `/etc/systemd/system/wolsca-print-service.service` | `root:root` | `0644` |
| `/etc/cups/cups-pdf*.conf` | `root:lp` | `0644` |

The directory list is read from the live configuration (`paths.*` and
`intake.queues[].directory`), so custom paths are covered too. The `permissions`
phase of the self-test verifies all of this and reports it to Home Assistant. The
new `notify` phase sends a real test message to the configured ntfy topic:
`main.py --self-test notify`.

## 4. Configure

Edit `/etc/wolsca/WolsCAPrintService.json` (MQTT broker/credentials, the physical printers, the print mode, and notifications) and restart:

```bash
sudo systemctl restart wolsca-print-service
```

### Configuration version and automatic upgrades

The configuration file carries its own version in `config_version` (currently **1.1.b**) - the
version of the *file*, not of the service. At every start-up the service compares it with the version
it expects and runs only the upgrade steps that are still missing, one after another:

* a file **without** `config_version` was written before versioning existed and counts as **1.0**;
* a step may carry a letter, which makes it a sub-step of that version: `1.1` < `1.1.a` < `1.1.b` <
  `1.2`, so a field can be added to a version that already exists without repeating its other work;
* every step is applied exactly once - a jump from 1.0 to a future 1.3 runs everything in between in
  order, and a file that is already up to date is not touched at all;
* an upgrade only **adds** the new fields; a value you set yourself is never overwritten, and the
  file is rewritten only when something really changed (`[Config] Upgrading configuration 1.0 ->
  1.1.b...` in the journal names every field it added).

| Version | What the upgrade does |
| --- | --- |
| 1.0 | The original file, without a version key |
| 1.1.a | Adds `hardware.wake_on_lan`, `hardware.printer_mac`, `hardware.wake_broadcast` and `hardware.wait_for_printer_seconds`, and fills in the MAC address of the printer itself when it can be read from the network |
| 1.1.b | Adds `hardware.printer_mac_wired`, `hardware.printer_mac_wifi`, `hardware.recover_printer_ip` and `hardware.block_on_mac_change` (the known address becomes the wired one), and gives every intake queue its own `printer` |

The MAC address is looked up over the network during that upgrade: the printer is contacted first
(that is what creates the ARP entry) and the neighbour table of the server is then read, so
`hardware.printer_mac` is usually filled in without typing anything. It only works while the printer
is awake and on the same subnet - a sleeping printer has no ARP entry left, which is exactly why the
address has to be in the file *before* it falls asleep. Was it empty after the upgrade, switch the
printer on and restart the service, or fill it in by hand (see below).

`main.py --self-test admin` shows the version of the file that is in use.

### One printer per intake queue

Booklet, double sided and single sided do not have to end up on the same machine. Every intake queue
has its own `printer`, which names one of the ids in `printers.targets`:

```json
"queues": [
    { "id": "booklet", "cups_queue": "WolsCA_Booklet", "print_mode": "Booklet", "printer": "office" },
    { "id": "doublesided", "cups_queue": "WolsCA_DoubleSided", "print_mode": "DoubleSided", "printer": "" }
]
```

It is a choice only when there is something to choose: **empty** means the default printer, and with
a single configured printer that printer is simply used. A personal choice made in the web app still
wins for `personal_choice_ttl_seconds`. The configuration editor of the web app (and Home Assistant)
shows one *Printer for ...* drop down per queue, and the `printer:` line of the job log says which
of the three decided - `personal`, `intake queue 'booklet'` or `default`.

### Finding the printers on the network

The *Administrator* card of the web app has a drop down of the printers that support IPP, filled by
**Search for printers**:

* everything that announces itself over mDNS/Bonjour as `_ipp._tcp` or `_ipps._tcp` (every
  driverless/AirPrint printer does), and
* everything that answers on port 631 in the subnet of the server, because a printer with mDNS
  switched off announces nothing at all.

Each entry shows the name, the address, the port and the MAC address that belongs to it. **Use the
selected printer** takes all of that over at once - `hardware.printer_uri`, the host of the target
and the MAC address - so a new or replaced printer is configured without typing an address.

Every search writes its result to the journal - one line per printer with the name, the address, the
port, the MAC address, how it was found and whether port 9100 is open:

```bash
journalctl -u wolsca-print-service | grep '\[Discovery\]'
```

A printer that was **not** on the network during an earlier search is reported: in the journal, as a
warning in the *Administrator* card (*New printer found: ...*), over MQTT and as an ntfy message.
That is either the new or replaced machine that still has to be chosen, or a device answering on IPP
that has no business doing so. Which printers are already known is remembered in
`<temp_directory>/known-printers.json` - by MAC address, so a changed IP address is not a new printer.
The very first search only records what is there, and deleting the file makes everything new again.
The self-test has the same check: *No new printer on the network* (`main.py --self-test network`).


### Keeping the printer MAC address correct

The MAC address is not filled in once and then forgotten - it is checked whenever the printer is
found on the network, because a replaced printer, a new network card or a move from cable to Wi-Fi
gives a different address, and a magic packet to the old one wakes nothing without complaining:

* at **start-up**, in the background, so a sleeping printer never delays the service;
* **before every print job** for which the printer is checked;
* in the **self-test**: `main.py --self-test printer` reports *Printer MAC address verified*,
  *... switched to the other interface*, *... detected and saved*, *... corrected*, *... unknown* or
  *... not verifiable*, and *Another MAC address than configured - has the printer changed?* when it
  belongs to neither interface.

There are three addresses, and each has its own job:

| Address | Where it comes from | What it is for |
| --- | --- | --- |
| `hardware.printer_mac_wired` | The printer itself (network page, or the sticker) | The cable interface |
| `hardware.printer_mac_wifi` | The printer itself | The Wi-Fi interface - a *different* address |
| `hardware.printer_mac` | The service, from the network | The address that answers **now**: it is woken, and it is what the printer is recognised by |

Filling in both means the printer stays known when it moves between cable and Wi-Fi (the working
address is switched over silently) and that it can be found again after its DHCP address changed. An
address that matches neither is the interesting case: then something else is answering where the
printer used to be, so it is reported and - with `hardware.block_on_mac_change` on - the job is
stopped rather than printed on an unknown machine.

A missing address is filled in and a changed address is corrected in
`/etc/wolsca/WolsCAPrintService.json` on the spot; `journalctl -u wolsca-print-service | grep
'\[Power\]'` shows what happened. The address can only be read while the printer is awake **and** on
the same subnet - with a router in between the neighbour table stays empty and the address has to
come from the printer's own network page.

### MQTT Broker Accounts

The default broker account name is `wolsca_mqtt`. When a Debian server and a Home
Assistant add-on share one broker, create **both** accounts in Home Assistant's
Mosquitto (e.g. `wolsca_mqtt` and `wolsca_mqtt_ha`). A Debian-only installation
needs only `wolsca_mqtt`, created either in Home Assistant's Mosquitto or in
EMQX/Mosquitto on the server itself.

### The printer is switched off or asleep

A printer that sleeps or is switched off answers on no port at all - the self-test then shows
`ipptool: Unable to connect ... Host is down` and a ping that loses every packet. That is a correct
observation, not a broken installation, so it is only a **warning** and a print job is never thrown
away because of it: before the job is handed over the service checks whether the printer is on the
network, otherwise the job goes to *Waiting for the printer to come online* and continues by itself
as soon as the printer answers.

| Setting | Meaning |
| --- | --- |
| `hardware.wait_for_printer_seconds` | How long a job waits for the printer (default 900 s; `0` = do not wait, fail immediately) |
| `hardware.printer_mac` | The MAC address that is **in use** now, e.g. `00:1b:a9:12:34:56`. Filled in and kept correct by the service itself |
| `hardware.printer_mac_wired` | The MAC address of the **cable** interface of the printer, as printed on the printer itself |
| `hardware.printer_mac_wifi` | The MAC address of the **Wi-Fi** interface - a different address than the cable one |
| `hardware.recover_printer_ip` | Look the printer up by MAC address when it does not answer on the configured address any more (on by default) |
| `hardware.block_on_mac_change` | Stop a job when the machine answering at the printer's address has an unknown MAC address (on by default) |
| `hardware.wake_on_lan` | Send a Wake-on-LAN packet before waiting (on by default; it does nothing while `printer_mac` is empty) |
| `hardware.wake_broadcast` | Where the packet is sent, default `255.255.255.255` (use the broadcast address of the printer's subnet when the server is elsewhere) |

#### Why the MAC address is needed

A Wake-on-LAN packet is a *magic packet*: an Ethernet frame that contains nothing but the MAC
address of the machine that has to wake up. There is no IP address in it - a sleeping printer has
switched off its IP stack and no longer answers ARP, so its IP address cannot be translated into a
MAC address at that moment either. The MAC address therefore has to be known **beforehand**, and
that is why it belongs in the configuration. Wake-on-LAN also has to be enabled in the printer's
own network settings, and a printer whose power switch is off can never be woken this way - then
only waiting helps.

The address is on the network/status page of the printer, and the self-test hands it to you while
the printer is awake:

```bash
ip neigh show 192.168.101.251        # 192.168.101.251 dev eth0 lladdr 00:1b:a9:12:34:56 REACHABLE
... main.py --self-test printer       # the 'Waking the printer' line reports the detected MAC
```

Fill it in as *Printer MAC address* in the configuration editor of the web app (or in Home
Assistant) and the service wakes the printer at the start of every job it has to wait for, and
again every minute for as long as it waits.

### Key Configuration Sections

- **`mqtt`**: Broker address, port and credentials (`user`, `password`).
- **`printers`**: Lists physical printers, duplex support, and dispatch method (`raw` or `cups`).
- **`notify`**: Push notifications via ntfy (enabled by default, default server `https://ntfy.sh`). If `topic` is empty, a unique one is generated and saved on first use.
- **`history`**: Job history settings (enabled, max_entries).
- **`web`**: Controls the built-in web app (port, language, public_url).

Example `targets` with duplex and CUPS dispatch:
```json
"targets": [
    { 
      "id": "office", 
      "name": "Office Duplex", 
      "host": "192.168.1.50", 
      "duplex": true, 
      "dispatch": "cups", 
      "cups_queue": "HP_LaserJet" 
    }
]
```

A user who picks a printer in the web app overrides the default for the next job for `personal_choice_ttl_seconds`.

## 5. Verify

```bash
systemctl status wolsca-print-service
journalctl -u wolsca-print-service -f
lpstat -p WolsCA_Booklet
curl -s http://localhost:8080/api/status | python3 -m json.tool
avahi-browse -rt _http._tcp        # the web app should be listed
avahi-browse -rt _ipp._tcp         # the print queue should be listed
# end-to-end test: print a PDF through the queue
lp -d WolsCA_Booklet /usr/share/doc/*/*.pdf
ls -l /var/spool/wolsca/PrintFileDrop
```

The web app is reachable at `http://<hostname>.local:8080/` (or via your own DNS record). It shows the job progress, the printer in use, **Continue**, **Cancel** and **Reprint** buttons, and the personal printer/options picker.

The Home Assistant entities (status, last error, *Print Job Step*, *Print Job Detail*, *Print Job
Result*, *Resume Print (Flip)* button, *Cancel Print Job*, *Reprint Front Side*, the *Print Mode*
select and the *Target Printer* select) appear automatically via MQTT discovery once the broker
connection succeeds.

### Following a print job

Every job writes a complete timeline - which queue it came from, which print mode and why, which
printer and over which route, the imposition, the exact `lp`/`ipptool` command, the job id, the page
progress, who pressed Continue and the result including the traceback of a failure:

```bash
journalctl -u wolsca-print-service -f | grep '^\[Job'   # one line per step
curl -s http://localhost:8080/api/joblog | python3 -c "import json,sys;print(json.load(sys.stdin)['text'])"
```

The same timeline is in the **Job log** card of the web app (with a *Copy job log* button) and in
Home Assistant: *Print Job Step* has the whole timeline of the running job in its attributes,
*Print Job Result* that of the last finished job. The finished jobs are kept in
`history.file` (default `<temp_directory>/job-history.json`, `history.max_entries` of them) and are
reloaded when the service restarts; `history.enabled: false` switches the file off.

## 6. Point the clients at the server

* **Windows 10/11**: *Settings → Printers & scanners → Add printer → The printer
  that I want isn't listed → Select a shared printer by name* and enter
  `http://print.lan:631/printers/WolsCA_Booklet`. Choose a generic PostScript or
  the *Generic / MS Publisher Imagesetter* driver.
* **macOS / Linux**: the queue is announced over IPP; add it as
  `ipp://print.lan:631/printers/WolsCA_Booklet`.
* **Manual drop**: copying a PDF into `/var/spool/wolsca/PrintFileDrop` (e.g. over
  an SMB/NFS share) also triggers a job.

Firewall: allow TCP 631 (IPP), TCP 8080 (web app) and UDP 5353 (mDNS/Bonjour)
inbound, and TCP 9100 outbound to the physical printer. If you also use MQTT,
allow the broker port (1883) outbound. The installer does this automatically when
`ufw` is active.

## 7. Upgrading and removal

```bash
sudo ./deploy/debian/update.sh                # fetch, reset, install, status
git pull && sudo ./deploy/debian/install.sh    # or by hand, keeps the configuration
sudo ./deploy/debian/uninstall.sh             # remove, keeps config + spool
sudo ./deploy/debian/uninstall.sh --purge     # remove everything
```

`update.sh` is the manual counterpart of the *Update now* button and runs exactly
what the service runs itself:

1. `git fetch --all --tags --prune` and `git reset --hard origin/<branch>` in the
   checkout (the branch of the checkout, or `--branch <name>`);
2. `chmod +x deploy/debian/*.sh` - the executable bit is lost by a Windows
   checkout or a ZIP download, and `git reset --hard` does not restore it;
3. `deploy/debian/install.sh` - every option you give `update.sh` other than
   `--branch` is passed on to it (`--without-cups`, and `--with-cups` is still
   accepted and does nothing);
4. `systemctl status wolsca-print-service`.

The whole script is one function that is called on its last line, because
`git reset --hard` rewrites `update.sh` while it is running - otherwise bash
would read the new file halfway through and stop somewhere unpredictable. Note
that the reset **throws away local changes in the checkout**; the configuration
in `/etc/wolsca` is not part of it and is kept.

### Updating from Home Assistant or the web app

The service reports its version (`x.y.<commit number>`, from the `VERSION` and
`BUILD_NUMBER` files) and can update itself:

```bash
sudo /opt/wolsca-print-service/venv/bin/python /opt/wolsca-print-service/main.py --version
... main.py --check-update    # exit code 1 when a newer version exists
... main.py --update          # git fetch/reset plus install.sh
```

The update runs `git fetch` and `git reset --hard origin/<branch>` in
`update.source_directory` (default `/usr/local/src/wolsca-print-service`) and then
`deploy/debian/install.sh`, which restarts the unit. So keep the git checkout on the
server - it is what the *Update now* button uses.

- **Home Assistant**: the `update.print_service_update` entity shows the installed and
  the latest version and has an *Install* button; the *Check for Print Service Update*
  button and the *Print Service Automatic Update* switch are next to it.
- **Web app**: the *Version and updates* card has the same three controls.
- The service checks by itself every `update.check_interval_hours` and, when
  `update.auto_update` is on, installs a new release immediately.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Self-test or job log: `has the printer been changed, or is another device using this IP address?` | The MAC address answering at the printer's address is neither `hardware.printer_mac_wired` nor `..._wifi`. With `hardware.block_on_mac_change` on (the default) the job is stopped instead of printed on an unknown device: check which machine is on that address and, when it really is the printer, fill in its address |
| Printing stops with `The printer was not recognised` | Same cause as above. Correct the wired/Wi-Fi address, or switch `hardware.block_on_mac_change` off to print anyway |
| The printer got another IP address (DHCP) | Nothing to do: with `hardware.recover_printer_ip` on the printer is looked up by its MAC address and `hardware.printer_uri` plus the target host are corrected. It only works with a MAC address filled in and the printer on the same subnet |
| The printer was moved from cable to Wi-Fi | Fill in both `hardware.printer_mac_wired` and `hardware.printer_mac_wifi`; the service then only switches `hardware.printer_mac` over (self-test: *Printer MAC address switched to the other interface*) instead of warning |
| *New printer found* / self-test: `No new printer on the network` | A printer that was not on the network during an earlier search answered now. When it is the new or replaced machine, choose it with *Use the selected printer*; otherwise check which device is answering on IPP there. Known printers are remembered in `<temp_directory>/known-printers.json` |
| Which printers were found | One `[Discovery]` line per printer in the journal: `journalctl -u wolsca-print-service \| grep '\[Discovery\]'`, or `main.py --self-test network` |
| No printer in the *Search for printers* drop down | The printer must be switched on and in the subnet of the server. Check `avahi-browse -rt _ipp._tcp` and whether port 631 answers (`nc -z <printer-ip> 631`); a printer behind a router is not found |
| A queue prints on the wrong printer | `intake.queues[].printer` (*Printer for ...* in the web app) wins over the default printer, and a personal choice in the web app wins over that; the `printer:` line of the job log names the source |
| The self-test report can only be screenshotted | Use *Copy report*, or *Open report as plain text* (`http://<host>:8080/api/diagnostics/report.txt`) and select everything there |
| `hardware.printer_mac` is still empty after the upgrade | The printer was asleep or on another subnet, so it had no ARP entry. Switch the printer on and restart the service (the address is looked up again at start-up and before every job), or read it from `ip neigh show <printer-ip>` or the printer's network page |
| Self-test: `Printer MAC address corrected` | The printer on the network has another MAC address than the configuration; the file has already been corrected. Expected after a printer replacement or a switch between cable and Wi-Fi |
| Self-test: `Printer MAC address unknown` | The printer answers but is not in the neighbour table - usually a router in between. Read the address from the printer's network page and fill it in by hand |
| Configuration keys of a new version are missing | `main.py --self-test admin` reports the version of the file, and `journalctl -u wolsca-print-service \| grep '\[Config\]'` shows what the upgrade did. A `config_version` that stays behind means a migration failed - the reason is on the `[Config] Upgrade to ... failed` line |
| No PDF in the drop directory | `journalctl -u cups -e`; verify `Out` in `/etc/cups/cups-pdf.conf` |
| No notification arrives | Check `notify.enabled` and `notify.topic`; verify the server can reach the ntfy URL |
| Progress bar stays at 0% | Real-time progress requires `dispatch: cups` and the `ipptool` command |
| A print "does nothing" and you cannot see why | The job log names the step it stopped at: *Job log* card in the web app, the *Print Job Step* / *Print Job Result* attributes in Home Assistant, or `journalctl -u wolsca-print-service \| grep '^\[Job'` |
| Printed in the wrong mode | The `mode:` line of the job log says whether the mode came from the intake queue or from the web app/configuration |
| Self-test: `Default print mode known` fails | Only the first letter counts and case does not matter: the mode must start with **b** (Booklet), **d** (DoubleSided) or **s** (SingleSided) - `booklet`, `BOOKLET`, `b` and `duplex` are all accepted, anything else is not |
| `Active: activating (auto-restart)` with `status=1/FAILURE` right after start | The reason is now in the journal: `journalctl -u wolsca-print-service -e` shows `[Fatal] The service stopped because of an unhandled error:` with the traceback. A configuration file missing a whole section (such as `paths`) no longer causes this |
| `[Fatal] The service cannot start: No module named ...` | The installed copy in `/opt/wolsca-print-service` is incomplete - a module that the checkout has was never copied. Re-run `sudo ./deploy/debian/install.sh`: it copies **every** `*.py` of the source directory (no hand kept list any more), removes a stale `__pycache__` and verifies that the service imports before it registers the unit |
| `Failed to read JSON config` / the configuration file is 0 bytes | The service falls back to the defaults and keeps running, but the file has to be repaired: the configuration is now saved to `<file>.new` and moved into place atomically, so it cannot be emptied by an interrupted save any more |
| Job stuck in `WAITING_FOR_FLIP` | Press **CONTINUE** in the web app, *Resume Print (Flip)* in Home Assistant, or `curl -X POST http://localhost:8080/api/resume` |
| Job cancelled automatically | Stale jobs are cancelled after `hardware.flip_timeout_seconds` (default 30 min) |
| Web app not reachable | `ss -lntp \| grep 8080`; check `web.enabled`/`web.bind_address` and the firewall |
| `<hostname>.local` does not resolve | `systemctl status avahi-daemon`; UDP 5353 must be open and the client on the same subnet |
| `[Zero-Touch] CUPS queue 'WolsCA_...' not found` | The queues were never created - re-run `install.sh` (CUPS is included by default now) or `main.py --install-printer`. The service also creates the missing queues itself at start-up when it runs as root and `lpadmin` is present |
| Printer not offered on the phone | `avahi-browse -rt _ipp._tcp`; the queue must be shared (`lpadmin -p WolsCA_Booklet -o printer-is-shared=true`) |
| Wrong printer used | A personal choice from the web app wins for `personal_choice_ttl_seconds`; check `journalctl` for the `[Printers] Target:` line |
| `Printer refused the connection` | The physical printer must accept raw port 9100 (JetDirect) |
| Self-test: `Host is down` / `Printer answers on the network` warning | The printer is switched off or in deep sleep. This is only a warning: a print job waits for it (`hardware.wait_for_printer_seconds`) instead of failing. Fill in `hardware.printer_mac` to have it woken |
| A job stays in *Waiting for the printer to come online* | Switch the printer on, or fill in `hardware.printer_mac` and enable Wake-on-LAN on the printer. After `hardware.wait_for_printer_seconds` (default 900) the job does fail |
| Wake-on-LAN does not wake the printer | Enable WOL in the printer's network settings; check `hardware.printer_mac`, and set `hardware.wake_broadcast` to the broadcast address of the printer's subnet when the server is on another subnet. A printer switched off at its power switch cannot be woken |
| Self-test: `IPP get-printer-attributes ... exit code 1` | Install `cups-ipp-utils` (`ipptool`). If the retry over `ipp://<host>:631/...` in the same step succeeds, only TLS fails - the printer's certificate or TLS version is rejected; set `hardware.printer_uri` to the `ipp://` address |
| Files land in a per-user folder | Re-run `--install-printer`; cups-pdf `Out`/`AnonDirName` must be the drop directory |
| Permission denied on the drop dir | `sudo /opt/wolsca-print-service/fix-permissions.sh`; the service user must be in group `lp` and the directory mode `2775` |
| `Errno 13` on the configuration file | `sudo /opt/wolsca-print-service/fix-permissions.sh`; `/etc/wolsca` must be `0755` and the JSON `0664` |
| Update button does nothing | `update.source_directory` must be a git checkout of the repository; run `main.py --self-test update` |
| Version reported as `0.0.0` | `VERSION` and `BUILD_NUMBER` are missing in `/opt/wolsca-print-service`; re-run `install.sh` |
| `Active: activating (auto-restart)` with `status=217/USER` | The `User=` of the unit does not exist. The installer deploys everything as `root`, so the unit must say `User=root`; re-run `install.sh`, which now keeps unit and installer in step |
| Settings visible in Home Assistant but not changeable | The service is not running - Home Assistant keeps showing the retained MQTT values. `systemctl status wolsca-print-service`, then `journalctl -u wolsca-print-service -e` |
| Configuration editor in the web app stays read-only | Set `web.admin_token` in `/etc/wolsca/WolsCAPrintService.json` and restart; without a token the editor is locked (the Home Assistant entities do not need the token) |
