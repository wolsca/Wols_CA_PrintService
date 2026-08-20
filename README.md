# Wols CA Print Service

A Python print service that watches a drop folder for PDFs, imposes them into A5 booklets on A4 paper, and manages the double-sided printing process. It includes a built-in mobile web app and MQTT integration for Home Assistant.

## Features

- **Automated Booklet Imposition**: Converts PDF pages into A5 booklet format (4 pages per A4 sheet).
- **Multiple Intake Queues**: Choose the print mode (Booklet, Duplex, Simplex) directly in your device's print dialog by picking the corresponding virtual printer.
- **Double-Sided Workflow**: Prints the front side, waits for the user to flip the paper, and prints the back side upon confirmation.
- **New Print Modes**: Forced single-sided (Simplex) and double-sided (Duplex) printing for all document types.
- **Push Notifications**: Receive a ping on your phone when paper needs to be flipped or when an error occurs via ntfy/Gotify.
- **Mobile Web App**: Real-time status with page progress, paper flip confirmation, printer/mode/copies selection, and job history.
- **Home Assistant Integration**: MQTT discovery for status sensors, mode/printer selection, and remote control (Resume/Cancel/Reprint).
- **Duplex Printer Support**: Automatic double-sided printing for supported printers (no manual flip required).
- **Multi-Platform**: Primary support for Debian/Ubuntu (CUPS-based), with development support for Windows (PDFCreator-based).
- **Zero-Touch Deployment**: Automated installer for CUPS, cups-pdf, and service dependencies.

---

## Installation (Debian 12+ / Ubuntu 22.04+)

The recommended way to deploy the service is on a dedicated Linux server or LXC container.

### 1. Install Dependencies:
```bash
    sudo apt update
    sudo apt install -y git python3 python3-venv python3-pip
```

2.  **Install Virtual Printer**:
```bash
    - **Linux**: `sudo python3 main.py --install-printer`
    - **Windows**: Run the script with Administrative privileges: `python main.py --install-printer`. This downloads and configures PDFCreator.
```

3.  **Run Service**:
```bash
    python3 main.py
```

The installer performs the following:
- Creates a dedicated `wolsca` system user.
- Sets up spool directories: `/var/spool/wolsca/{PrintFileDrop,PrintTemp,PrintError}`.
- Creates a Python virtual environment in `/opt/wolsca-print-service`.
- Installs the `wolsca-print-service` systemd unit.
- Deploys three virtual CUPS printer queues (`WolsCA_Booklet`, `WolsCA_DoubleSided`, `WolsCA_SingleSided`) shared over the network.
- Configures multiple `cups-pdf` instances to route jobs to specific mode-based drop folders.
- Configures mDNS (Avahi) to advertise the web app as `http://<hostname>.local:8080/`.

---

## Manual Installation / Development

If you prefer to run the service from source or on Windows:

1.  **Prepare Environment**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # venv\Scripts\activate on Windows
    pip install -r requirements.txt
    ```
2.  **Install Virtual Printer**:
    - **Linux**: `sudo python3 Wols_CA_PrintService.py --install-printer`
    - **Windows**: Run the script with Administrative privileges: `python Wols_CA_PrintService.py --install-printer`. This downloads and configures PDFCreator.
3.  **Run Service**:
    ```bash
    python3 Wols_CA_PrintService.py
    ```
## Project Layout
```text
Wols_CA_PrintService/
    main.py                     Core orchestrator and queue worker
    config.py                   Configuration management
    mqtt_service.py             MQTT and Home Assistant logic
    pdf_processor.py            Booklet imposition and PDF handling
    hardware_dispatcher.py      Raw TCP and CUPS dispatch
    file_watcher.py             Directory monitoring
    web_app.py                  Mobile interface hosting
    web_strings.json            UI translations
    WolsCAPrintService.json     Runtime configuration (development)
---

## Choose Mode in the Print Dialog

Instead of one intake queue, there are three shared CUPS queues. The queue a document arrives on decides how it is printed. On iPhone, Android, Windows, macOS, and Linux, simply pick the printer in the normal print dialog:

| Queue (visible printer) | Drop directory | Print mode | Result |
| :--- | :--- | :--- | :--- |
| **WolsCA_Booklet** | `.../PrintFileDrop/booklet` | Booklet | A5 booklet imposition on A4, flip halfway |
| **WolsCA_DoubleSided** | `.../PrintFileDrop/duplex` | Duplex | Forced double sided, no imposition |
| **WolsCA_SingleSided** | `.../PrintFileDrop/simplex` | Simplex | Forced single sided, one page per sheet |

---

## Configuration

The configuration is stored in `/etc/wolsca/WolsCAPrintService.json` (Linux) or `WolsCAPrintService.json` (root folder). **Restart the service after editing.**

```bash
sudo systemctl restart wolsca-print-service
```

### Configuration Keys

| Section | Key | Description |
| :--- | :--- | :--- |
| **mqtt** | `broker_ip` | IP address of your MQTT broker. |
| | `topic_prefix` | Base topic for MQTT messages (default: `wolsca/printer`). |
| **paths** | `drop_directory` | Folder watched for new PDF files. |
| **intake** | `enabled` | Enable triple-queue intake (default: `true`). |
| | `queues` | Array of intake queues: `[{id, cups_queue, description, print_mode, directory}]`. |
| **hardware** | `printer_uri` | URI of the physical printer (e.g. `ipps://192.168.1.10:443/ipp/print`). The installer creates the output queue from it. |
| | `cups_queue_name` | Name of the CUPS output queue for the physical printer (default: `WolsCA_Output`). |
| | `flip_instruction` | Global override for the paper flip instruction text. |
| | `flip_timeout_seconds` | Auto-cancel job if no flip confirmation (default: `1800`, 0 to disable). |
| **printers** | `default` | ID of the system-wide default printer. |
| | `targets` | Array of available printers: `[{id, name, host, port, duplex, dispatch, cups_queue, flip_instruction}]`. |
| | `personal_choice_ttl_seconds` | How long a user's choice lasts (default: `900`). |
| **web** | `enabled` | Enable/disable the built-in web app. |
| | `port` | Port for the web app (default: `8080`). |
| | `language` | UI language: `en` (English) or `nl` (Dutch). |
| | `public_url` | External URL for notification actions (e.g. `http://print.local:8080`). |
| | `admin_token` | Token required for `POST /api/default` admin actions. |
| **notify** | `enabled` | Enable push notifications (default: `false`). |
| | `url` | ntfy/Gotify server URL (default: `https://ntfy.sh`). |
| | `topic` | Secret topic name for notifications. |
| | `token` | Optional auth token for the notification server. |
| | `priority` | Notification priority (e.g., `high`). |
| | `notify_on_error` | Send notification on job failure (default: `true`). |
| **history** | `enabled` | Enable job history tracking (default: `true`). |
| | `max_entries` | Number of jobs to keep in history (default: `10`). |
| | `file` | Path to the history JSON file (default: `job_history.json`). |
| **settings** | `print_mode` | `Bypass`, `Standard`, `Simplex`, `Duplex`, or `Booklet`. |
| | `password` | **Change this**: Password for the MQTT broker. |

---

## Web App & HTTP API

The web app is available at `http://<server>.local:8080/`. It uses mDNS/Bonjour for easy discovery on mobile devices.

### Printer Selection & Options Precedence
1.  **Intake Queue**: The queue the document arrived on (e.g. `WolsCA_SingleSided` is always single sided).
2.  **Personal Choice**: A user can select options in the web app. This choice applies to documents dropped in the root drop folder or when intake is disabled. TTL: `printers.personal_choice_ttl_seconds`.
3.  **Admin Default**: If no personal choice or specific intake queue is active, the global settings are used.

### HTTP API Endpoints

-   `GET /api/status`: Returns job state, progress, and intake queue info.
-   `GET /api/queues`: List all configured intake queues.
-   `GET /api/printers`: List all configured target printers.
-   `GET /api/history`: Returns the list of recently completed jobs.
-   `POST /api/resume`: Confirms paper re-insertion (same as the web button).
-   `POST /api/cancel`: Cancels the current printing job.
-   `POST /api/reprint`: Reprints the front side of the current job.
-   `POST /api/options`: Set personal job options. Body: `{"printer": "<id>", "print_mode": "Booklet", "copies": 2}`.
-   `POST /api/printer`: Set personal printer choice only. Body: `{"printer": "<id>"}`.
-   `POST /api/default`: Set admin default printer. Body: `{"printer": "<id>", "token": "<admin_token>"}`.
-   `GET /qr`: Page showing a QR code for the web app URL.
-   `GET /healthz`: Liveness check.

**Example `curl` call:**
```bash
curl -X POST http://print.local:8080/api/resume
```

---

## Network & Firewall

-   **TCP 631 (IPP)**: Incoming print jobs from clients.
-   **UDP 5353 (mDNS)**: Service discovery (Bonjour/Avahi).
-   **TCP 8080**: Web app access.
-   **TCP 9100**: Outbound to physical printer (Raw/JetDirect).
-   **TCP 1883**: Outbound to MQTT broker.

---

## Adding the Service to your Local DNS (Pi-hole, dnsmasq, Unbound, router)

mDNS (`<hostname>.local`) works out of the box, but it depends on Bonjour/Avahi
and is not always reliable over VPN, guest VLANs or on some Android builds. If
you run your own DNS server, give the print server a stable name such as
`print.home.lan` and use that everywhere (web app URL, `web.public_url`, the
Home Assistant `panel_iframe` and the IPP printer URLs).

Pick the name first, then add it in your DNS server:

### Pi-hole (v5 and v6)

Pi-hole has a built-in "Local DNS" feature; nothing has to be edited by hand:

1. Open the Pi-hole admin interface.
2. Go to **Settings -> Local DNS Records** (Pi-hole v6: **Settings -> Local DNS**).
3. Add a record:
   - **Domain**: `print.home.lan`
   - **IP address**: the IP of the print server, e.g. `192.168.101.10`
4. Click **Add**. The change is active immediately, no restart needed.

If you prefer files (or want a CNAME to the server's existing hostname), create
a drop-in and restart the resolver:

```bash
# Pi-hole v5
sudo tee /etc/dnsmasq.d/99-wolsca-print.conf >/dev/null <<'EOF'
address=/print.home.lan/192.168.101.10
EOF
sudo pihole restartdns

# Pi-hole v6 (dnsmasq config directory)
sudo tee /etc/pihole/dnsmasq.d/99-wolsca-print.conf >/dev/null <<'EOF'
address=/print.home.lan/192.168.101.10
EOF
sudo systemctl restart pihole-FTL
```

A CNAME alias instead of an A record (Pi-hole: **Local CNAME Records**):

```
cname=print.home.lan,printsrv.home.lan
```

### Plain dnsmasq

```bash
sudo tee /etc/dnsmasq.d/wolsca-print.conf >/dev/null <<'EOF'
address=/print.home.lan/192.168.101.10
EOF
sudo systemctl restart dnsmasq
```

### Unbound

```
# /etc/unbound/unbound.conf.d/wolsca-print.conf
server:
    local-zone: "home.lan." transparent
    local-data: "print.home.lan. IN A 192.168.101.10"
```

```bash
sudo systemctl restart unbound
```

### BIND9

```
; zone file for home.lan
print   IN  A   192.168.101.10
```

Bump the zone serial and run `sudo rndc reload`.

### AdGuard Home

**Filters -> DNS rewrites -> Add DNS rewrite**: domain `print.home.lan`,
answer `192.168.101.10`.

### Consumer routers (OPNsense/pfSense, OpenWrt, Fritz!Box, UniFi)

Look for *Host overrides*, *Static DNS entries*, *Local DNS* or *DHCP static
lease with hostname*. Assigning a **static DHCP lease** to the print server is
recommended in all cases, so the IP behind the name never changes.

### After adding the record

```bash
# from any client
nslookup print.home.lan
curl -I http://print.home.lan:8080/healthz
```

Then use the stable name everywhere:

| Where | Value |
| :--- | :--- |
| Web app | `http://print.home.lan:8080/` |
| Config `web.public_url` | `http://print.home.lan:8080` (makes the push notification button work) |
| Home Assistant `panel_iframe` | `http://print.home.lan:8080/` |
| Booklet printer (IPP) | `ipp://print.home.lan:631/printers/WolsCA_Booklet` |
| Double sided printer | `ipp://print.home.lan:631/printers/WolsCA_DoubleSided` |
| Single sided printer | `ipp://print.home.lan:631/printers/WolsCA_SingleSided` |

Notes:

-   Use a domain you control, for example `home.lan`, `home.arpa` (reserved for
    exactly this purpose) or a subdomain of a domain you own. Do **not** invent
    names under `.local` - that suffix belongs to mDNS.
-   Restart the service after changing `web.public_url`:
    `sudo systemctl restart wolsca-print-service`.
-   iPhones and iPads only use your DNS server when they are on the LAN Wi-Fi
    (or VPN) and "Private Wi-Fi Address"/encrypted DNS is not bypassing it. If a
    device ignores the record, `.local` via Bonjour keeps working as a fallback.
-   Adding the DNS name does not replace AirPrint discovery: phones still find
    the three queues over mDNS, the DNS name is mainly for the web app and for
    adding printers by URL.

---

## Technical Notes

-   **Booklet Imposition**: The service uses `pypdf` to reorder pages. If the page count is not a multiple of 4, the service automatically adds 1-3 blank A5 sides to the end of the booklet.
-   **Duplex Printing**: If a printer target has `duplex: true` and `dispatch: cups`, the service sends the job as a single two-sided task, skipping the manual flip step.
-   **Page Progress**: When using `dispatch: cups`, the service uses `ipptool` to poll the printer for real-time page progress.
-   **Job Queue**: Only one document is printed at a time; further documents wait in a queue. The web app shows the number of waiting jobs.
-   **Home Assistant**: Entities are discovered automatically. You can add the web app to your sidebar by adding this to your `configuration.yaml`:
    ```yaml
    panel_iframe:
      booklet_printer:
        title: "Booklet Printer"
        url: "http://print.local:8080/"
        icon: mdi:printer
        require_admin: false
    ```
    *Note: An HTTPS Home Assistant instance will block an HTTP iframe (mixed-content). In that case, use an iframe dashboard card or MQTT-based cards.*
-   **License**: MIT License.

## Project Layout

```
CMakeLists.txt                                  CLion/CMake project model (no compilation)
requirements.txt                                Python dependencies (paho-mqtt, watchdog, pypdf)
LICENSE                                         MIT license
.run/                                           Shared CLion run configurations
docs/USER_GUIDE.md                              Per-device usage guide
deploy/debian/                                  Debian/Ubuntu deployment (systemd, CUPS, Avahi)
    install.sh / uninstall.sh                   Installer and remover
    wolsca-print-service.service                systemd unit
    WolsCAPrintService.linux.json                Default Linux configuration
Wols_CA_PrintService/
    Wols_CA_PrintService.py       The service (watcher, imposition, web app)
    WolsCAPrintService.json                      Runtime configuration (development)
```

## Development in CLion

1. `File | Open` and select the project directory; CLion picks up the root
   `CMakeLists.txt`, which declares `LANGUAGES NONE` so no C/C++ toolchain is
   required.
2. Install the **Python** plugin for code insight and debugging.
3. Use the shared run configurations in `.run/` (`Install Requirements`,
   `Run Print Service`, `Install Virtual Printer`) or the equivalent CMake
   targets:

   ```powershell
   cmake -S . -B cmake-build-debug
   cmake --build cmake-build-debug --target install_requirements
   cmake --build cmake-build-debug --target run_service
   ```

   Override the interpreter with `-DPYTHON_EXECUTABLE_NAME=<path-to-python>`.

## Documentation
- [User Guide](docs/USER_GUIDE.md): Per-device setup (iPhone, Android, Windows, etc.)
- [Debian/Ubuntu Deployment Details](deploy/debian/README.md): Installer, systemd and troubleshooting.
