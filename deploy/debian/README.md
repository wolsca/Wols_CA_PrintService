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

### MQTT Broker Accounts

The default broker account name is `wolsca_mqtt`. When a Debian server and a Home
Assistant add-on share one broker, create **both** accounts in Home Assistant's
Mosquitto (e.g. `wolsca_mqtt` and `wolsca_mqtt_ha`). A Debian-only installation
needs only `wolsca_mqtt`, created either in Home Assistant's Mosquitto or in
EMQX/Mosquitto on the server itself.

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
git pull && sudo ./deploy/debian/install.sh   # re-run, keeps the configuration
sudo ./deploy/debian/uninstall.sh             # remove, keeps config + spool
sudo ./deploy/debian/uninstall.sh --purge     # remove everything
```

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
| No PDF in the drop directory | `journalctl -u cups -e`; verify `Out` in `/etc/cups/cups-pdf.conf` |
| No notification arrives | Check `notify.enabled` and `notify.topic`; verify the server can reach the ntfy URL |
| Progress bar stays at 0% | Real-time progress requires `dispatch: cups` and the `ipptool` command |
| A print "does nothing" and you cannot see why | The job log names the step it stopped at: *Job log* card in the web app, the *Print Job Step* / *Print Job Result* attributes in Home Assistant, or `journalctl -u wolsca-print-service \| grep '^\[Job'` |
| Printed in the wrong mode | The `mode:` line of the job log says whether the mode came from the intake queue or from the web app/configuration |
| Job stuck in `WAITING_FOR_FLIP` | Press **CONTINUE** in the web app, *Resume Print (Flip)* in Home Assistant, or `curl -X POST http://localhost:8080/api/resume` |
| Job cancelled automatically | Stale jobs are cancelled after `hardware.flip_timeout_seconds` (default 30 min) |
| Web app not reachable | `ss -lntp \| grep 8080`; check `web.enabled`/`web.bind_address` and the firewall |
| `<hostname>.local` does not resolve | `systemctl status avahi-daemon`; UDP 5353 must be open and the client on the same subnet |
| `[Zero-Touch] CUPS queue 'WolsCA_...' not found` | The queues were never created - re-run `install.sh` (CUPS is included by default now) or `main.py --install-printer`. The service also creates the missing queues itself at start-up when it runs as root and `lpadmin` is present |
| Printer not offered on the phone | `avahi-browse -rt _ipp._tcp`; the queue must be shared (`lpadmin -p WolsCA_Booklet -o printer-is-shared=true`) |
| Wrong printer used | A personal choice from the web app wins for `personal_choice_ttl_seconds`; check `journalctl` for the `[Printers] Target:` line |
| `Printer refused the connection` | The physical printer must accept raw port 9100 (JetDirect) |
| Self-test: `IPP get-printer-attributes ... exit code 1` | Install `cups-ipp-utils` (`ipptool`). If the retry over `ipp://<host>:631/...` in the same step succeeds, only TLS fails - the printer's certificate or TLS version is rejected; set `hardware.printer_uri` to the `ipp://` address |
| Files land in a per-user folder | Re-run `--install-printer`; cups-pdf `Out`/`AnonDirName` must be the drop directory |
| Permission denied on the drop dir | `sudo /opt/wolsca-print-service/fix-permissions.sh`; the service user must be in group `lp` and the directory mode `2775` |
| `Errno 13` on the configuration file | `sudo /opt/wolsca-print-service/fix-permissions.sh`; `/etc/wolsca` must be `0755` and the JSON `0664` |
| Update button does nothing | `update.source_directory` must be a git checkout of the repository; run `main.py --self-test update` |
| Version reported as `0.0.0` | `VERSION` and `BUILD_NUMBER` are missing in `/opt/wolsca-print-service`; re-run `install.sh` |
| `Active: activating (auto-restart)` with `status=217/USER` | The `User=` of the unit does not exist. The installer deploys everything as `root`, so the unit must say `User=root`; re-run `install.sh`, which now keeps unit and installer in step |
| Settings visible in Home Assistant but not changeable | The service is not running - Home Assistant keeps showing the retained MQTT values. `systemctl status wolsca-print-service`, then `journalctl -u wolsca-print-service -e` |
| Configuration editor in the web app stays read-only | Set `web.admin_token` in `/etc/wolsca/WolsCAPrintService.json` and restart; without a token the editor is locked (the Home Assistant entities do not need the token) |
