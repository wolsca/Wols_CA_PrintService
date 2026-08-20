# Release Notes

## 1.0.0 - 2026-08-20

First public release of the **Wols CA Print Service**.

A Python print service that watches a drop folder for PDFs, imposes them into A5 booklets on
A4 paper and manages the double-sided printing process. It ships with a built-in mobile web
app, push notifications and MQTT integration for Home Assistant.

### Highlights

- **Automated booklet imposition** - PDF pages are reordered into A5 booklet format, 4 pages
  per A4 sheet. If the page count is not a multiple of 4, 1-3 blank A5 sides are added
  automatically.
- **Pick the mode by picking the printer** - three shared CUPS queues are published on the
  network, so no print dialog settings have to be changed:

  | Queue (visible printer) | Drop directory | Mode | Result |
  | --- | --- | --- | --- |
  | `WolsCA_Booklet` | `.../PrintFileDrop/booklet` | Booklet | A5 booklet imposition on A4, flip halfway |
  | `WolsCA_DoubleSided` | `.../PrintFileDrop/duplex` | Duplex | Forced double sided, no imposition |
  | `WolsCA_SingleSided` | `.../PrintFileDrop/simplex` | Simplex | Forced single sided, one page per sheet |

  The intake queue always wins over the mode selected in the web app.
- **Double-sided workflow** - the front side is printed, the service pauses and asks you to
  flip the stack, and prints the back side after confirmation. Default instruction: *"Take the
  whole stack out of the output tray, do NOT rotate it, and put it back in the paper tray
  printed side down, top edge first."*
- **Duplex printer support** - targets with `duplex: true` and `dispatch: cups` are printed as
  a single two-sided job; the manual flip step is skipped entirely.
- **Mobile web app (PWA)** - real-time status, page progress bar, flip illustration and
  instructions, Continue / Reprint Front / Cancel controls, printer / mode / copies selection
  and job history. Available at `http://<server-name>.local:8080/`, with a QR code page at
  `/qr`, and installable on iOS, Android and Windows.
- **Push notifications** - ntfy/Gotify ping when paper needs to be flipped or when a job
  fails, including a *Continue printing* action button in the notification.
- **Home Assistant integration** - MQTT discovery for status sensors, mode/printer selects and
  remote control (Resume / Cancel / Reprint), plus an optional `panel_iframe` sidebar entry.
- **HTTP API** - `GET /api/status`, `/api/queues`, `/api/printers`, `/api/history`,
  `POST /api/resume`, `/api/cancel`, `/api/reprint`, `/api/options`, `/api/printer`,
  `/api/default`, plus `GET /qr` and `GET /healthz`.
- **Drop folder alternative** - copy a PDF into the network share (SMB/NFS) to start a job;
  the `booklet`, `duplex` and `simplex` sub-folders select the mode, the top level uses the
  personal choice from the web app or the admin default.
- **Job queue** - one document is printed at a time, remaining documents wait; the web app
  shows the number of waiting jobs. Jobs waiting for a flip are auto-cancelled after 30
  minutes (`flip_timeout_seconds`, default `1800`).
- **Zero-touch deployment** - `deploy/debian/install.sh --with-cups` creates the `wolsca`
  system user, spool directories, a virtualenv in `/opt/wolsca-print-service`, the
  `wolsca-print-service` systemd unit, the three CUPS queues, the mode-based `cups-pdf`
  instances and the Avahi/mDNS advertisement.
- **Multi-platform** - primary support for Debian 12+ / Ubuntu 22.04+ (CUPS based), with
  development support for Windows (PDFCreator based).
- **Dutch and English UI** - via the `web.language` setting (`en` / `nl`).

### Supported clients

iPhone / iPad (AirPrint), Android (Default or Mopria Print Service), Windows 10 & 11
(shared printer by IPP URL), macOS (Bonjour) and Linux / Raspberry Pi (cups-browsed or manual
IPP URI). See `docs/USER_GUIDE.md` for step-by-step instructions per device.

### Configuration

Configuration lives in `/etc/wolsca/WolsCAPrintService.json` on Linux (or
`WolsCAPrintService.json` in the project root for development) with `mqtt`, `paths`, `intake`,
`hardware`, `printers`, `web`, `notify`, `history` and `settings` sections. Restart the
service after editing:

```bash
sudo systemctl restart wolsca-print-service
```

Options precedence: **intake queue** > **personal choice** (web app, TTL
`printers.personal_choice_ttl_seconds`, default 900 s) > **admin default**.

### Network requirements

| Port | Purpose |
| --- | --- |
| TCP 631 | Incoming IPP print jobs |
| UDP 5353 | mDNS / Bonjour discovery |
| TCP 8080 | Web app |
| TCP 9100 | Outbound to the physical printer (Raw/JetDirect) |
| TCP 1883 | Outbound to the MQTT broker |

### Known limitations

- A booklet needs a multiple of 4 pages; blank A5 sides are appended to fill the last sheet.
- Real-time page progress requires a CUPS-dispatched printer - raw port 9100 transfers only
  report the active sheet.
- Notification action buttons only work when `web.public_url` is set to an address the phone
  can reach.
- An HTTPS Home Assistant instance blocks the HTTP `panel_iframe` (mixed content); use an
  iframe dashboard card or MQTT-based cards instead.
- If the installer is run without `--with-cups`, only one queue is created.
- mDNS (`.local`) is not always reliable over VPN, guest VLANs or on some Android builds; a
  stable name in your own DNS server (for example `print.home.lan`) is recommended.

### Naming change

The project was renamed from **Wols CA Double Sided Print Service** to **Wols CA Print Service**,
because it handles booklet, duplex and simplex printing and not only double-sided output. This
affects file and directory names as well as the configuration file:

| Old | New |
| --- | --- |
| `Wols_CA_Double_Sided_Print_Service/` | `Wols_CA_PrintService/` |
| `Wols_CA_Double_Sided_Print_Service.py` | `Wols_CA_PrintService.py` |
| `Wols_CA_Double_Sided_Print_Service.slnx` / `.pyproj` | `Wols_CA_PrintService.slnx` / `.pyproj` |
| `WolsCADoubleSided.json` | `WolsCAPrintService.json` |
| `WolsCADoubleSided.linux.json` | `WolsCAPrintService.linux.json` |

Upgrading an existing installation: rename `/etc/wolsca/WolsCADoubleSided.json` to
`/etc/wolsca/WolsCAPrintService.json` (or point `WOLSCA_CONFIG` at the old file) before
restarting `wolsca-print-service`. The systemd unit name (`wolsca-print-service`), the CUPS
queue names (`WolsCA_Booklet`, `WolsCA_DoubleSided`, `WolsCA_SingleSided`) and the spool
directories are unchanged, so clients do not have to be reconfigured.

### Documentation

- `README.md` - installation, configuration, API and DNS setup
- `docs/USER_GUIDE.md` - per-device usage guide
- `deploy/debian/` - installer, systemd unit and default Linux configuration

### License

MIT License.
