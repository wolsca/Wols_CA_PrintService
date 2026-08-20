# Deploying on Debian or Ubuntu (Proxmox)

This guide moves the print service from the Windows desktop to a small
Debian/Ubuntu server (for example the host that already runs your local DNS)
running under Proxmox VE.

Supported and tested: **Debian 12/13** and **Ubuntu 22.04/24.04** (any apt based
derivative such as Raspberry Pi OS works too). The installer detects the
distribution from `/etc/os-release` and uses the same `apt` packages on all of
them.

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
chmod +x deploy/debian/*.sh          # only needed if the bit was lost on transfer
sudo ./deploy/debian/install.sh --with-cups
```

What the installer does:

1. installs `python3`, `python3-venv`, `python3-pip`, `avahi-daemon` and
   `avahi-utils` (Bonjour/mDNS, so the host is reachable as `<hostname>.local`);
2. creates the system user `wolsca` (member of group `lp`);
3. creates `/var/spool/wolsca/{PrintFileDrop,PrintTemp,PrintError}` (setgid `lp`,
   so both cups-pdf and the service can write there);
4. copies the service to `/opt/wolsca-print-service` and the default config to
   `/etc/wolsca/WolsCAPrintService.json`;
5. builds `/opt/wolsca-print-service/venv` and installs `requirements.txt`;
6. with `--with-cups`: installs CUPS, cups-pdf, `cups-ipp-utils` and Avahi,
   redirects the cups-pdf output to the drop directory, creates the shared queue
   `WolsCA_Booklet` (driverless/AirPrint friendly, `application/pdf` default),
   enables network sharing and advertises the web app over mDNS
   (`/etc/avahi/services/wolsca-print-web.service`);
7. installs and starts the `wolsca-print-service` systemd unit and, when `ufw` is
   active, opens TCP 631, TCP 8080 and UDP 5353.

Omit `--with-cups` if you want to set the queue up later:

```bash
sudo WOLSCA_CONFIG=/etc/wolsca/WolsCAPrintService.json \
     /opt/wolsca-print-service/venv/bin/python \
     /opt/wolsca-print-service/main.py --install-printer
```

## 4. Configure

Edit `/etc/wolsca/WolsCAPrintService.json` (MQTT broker/credentials, the physical printers, the print mode, and notifications) and restart:

```bash
sudo systemctl restart wolsca-print-service
```

### Key Configuration Sections

- **`printers`**: Lists physical printers, duplex support, and dispatch method (`raw` or `cups`).
- **`notify`**: Push notifications via ntfy/Gotify (enabled, url, topic, priority).
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

The Home Assistant entities (status, last error, *Resume Print (Flip)* button, *Cancel Print Job*, *Reprint Front Side*, the *Print Mode* select and the *Target Printer* select) appear automatically via MQTT discovery once the broker connection succeeds.

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

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No PDF in the drop directory | `journalctl -u cups -e`; verify `Out` in `/etc/cups/cups-pdf.conf` |
| No notification arrives | Check `notify.enabled` and `notify.topic`; verify the server can reach the ntfy URL |
| Progress bar stays at 0% | Real-time progress requires `dispatch: cups` and the `ipptool` command |
| Job stuck in `WAITING_FOR_FLIP` | Press **CONTINUE** in the web app, *Resume Print (Flip)* in Home Assistant, or `curl -X POST http://localhost:8080/api/resume` |
| Job cancelled automatically | Stale jobs are cancelled after `hardware.flip_timeout_seconds` (default 30 min) |
| Web app not reachable | `ss -lntp \| grep 8080`; check `web.enabled`/`web.bind_address` and the firewall |
| `<hostname>.local` does not resolve | `systemctl status avahi-daemon`; UDP 5353 must be open and the client on the same subnet |
| Printer not offered on the phone | `avahi-browse -rt _ipp._tcp`; the queue must be shared (`lpadmin -p WolsCA_Booklet -o printer-is-shared=true`) |
| Wrong printer used | A personal choice from the web app wins for `personal_choice_ttl_seconds`; check `journalctl` for the `[Printers] Target:` line |
| `Printer refused the connection` | The physical printer must accept raw port 9100 (JetDirect) |
| Files land in a per-user folder | Re-run `--install-printer`; cups-pdf `Out`/`AnonDirName` must be the drop directory |
| Permission denied on the drop dir | The service user must be in group `lp` and the directory mode `2775` |
