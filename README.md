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
    diagnostics.py              Self-test phases, command logging and MQTT report
    admin.py                    Administrator configuration editor (web app and HA)
    updater.py                  Release check, self-update, test builds and the HA update entity
    version.py                  Reads VERSION and BUILD_NUMBER
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
| | `admin_token` | Token required for `POST /api/default` and for the *Administrator* configuration editor. Empty means the editor stays locked. |
| **notify** | `enabled` | Enable push notifications (default: `false`). |
| | `url` | ntfy/Gotify server URL (default: `https://ntfy.sh`). |
| | `topic` | Secret topic name for notifications. |
| | `token` | Optional auth token for the notification server. |
| | `priority` | Notification priority (e.g., `high`). |
| | `notify_on_error` | Send notification on job failure (default: `true`). |
| **history** | `enabled` | Enable job history tracking (default: `true`). |
| | `max_entries` | Number of jobs to keep in history (default: `10`). |
| | `file` | Path to the history JSON file (default: `job_history.json`). |
| **update** | `enabled` | Enable the update check (default: `true`). |
| | `repository` | GitHub repository to check, `owner/name` (default: `wolsca/Wols_CA_PrintService`). |
| | `branch` | Branch used for test builds (default: `main`). |
| | `channel` | `release` (default; only published GitHub releases trigger an update) or `branch` (treat the branch head as the version - for a test machine). |
| | `allow_test_builds` | Allow the *Check/Install test build* buttons to use the branch head (default: `true`). |
| | `check_interval_hours` | How often the service checks for a new release (default: `6`). |
| | `auto_update` | Install a new **release** automatically (default: `false`). Never installs a test build. Toggled by the HA switch and the web app. |
| | `source_directory` | Git checkout used for the update (default: `/usr/local/src/wolsca-print-service`). |
| | `update_command` | Optional single command that replaces the default `git fetch/reset` + `install.sh`. |
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
-   `GET /api/diagnostics`: Returns the last self-test report (without the individual steps).
-   `POST /api/diagnostics/run`: Starts a self-test. Optional `?phases=cups,printer` selects phases.
-   `GET /api/update`: Installed version, latest release, whether an update is available, the test build state and the automatic update setting.
-   `POST /api/update/check`: Checks GitHub for a new **release**.
-   `POST /api/update/install`: Installs the latest release (the service restarts).
-   `POST /api/update/check-test`: Checks the branch head for a test build (commit build).
-   `POST /api/update/install-test`: Installs the branch head - for testing only.
-   `POST /api/update/auto`: Toggles automatic updates; `?enabled=1` / `?enabled=0` sets it explicitly.
-   `GET /api/admin/config`: The editable settings with their current values. Requires the admin token (`?token=`, JSON body or `X-Admin-Token`), otherwise `403`.
-   `POST /api/admin/config`: Saves settings. Body: `{"token": "...", "values": {"web.title": "..."}}`. Answers with `saved`, `changed`, `errors` and `restart_required`.
-   `POST /api/admin/restart`: Restarts the service (the *yes* answer to the restart question).
-   `POST /api/admin/reload`: Re-reads the configuration file and drops the pending restart flag.
-   `GET /qr`: Page showing a QR code for the web app URL.
-   `GET /healthz`: Liveness check.

**Example `curl` call:**
```bash
curl -X POST http://print.local:8080/api/resume
```

---

## Self-Test / Diagnostics Report

`diagnostics.py` is a test module that walks through the print chain phase by phase.
Every single Debian command it runs is logged **together with its output**, both to the
service log and to MQTT, and the whole run is aggregated into one report.

### Phases

| Phase | What it checks |
| :--- | :--- |
| `system` | `uname -a`, `/etc/os-release`, `systemctl is-active` for the service, CUPS and Avahi, `df -h /var/spool`, `id -a`. |
| `config` | Config file, drop/temp/error directories and their permissions, `ls -laR` of the drop folder, the resolved printer target and the intake queues. |
| `cups` | `lpstat -t/-d/-o`, the sharing directives in `cupsd.conf`, all `cups-pdf*.conf` output paths, `cups-pdf_log`, `error_log`, and whether every configured queue really exists. |
| `permissions` | Owner, group and mode of the configuration directory and file, of every spool/intake directory from the live configuration and of the history file, and whether the running process can really read and write them. Names `fix-permissions.sh` as the remedy. |
| `admin` | Whether `web.admin_token` is set (the configuration editor stays locked without it), the current value of every editable setting, whether a restart is still pending and whether the configuration file is writable. |
| `update` | The version files (`VERSION`, `BUILD_NUMBER`, commit hash), the `update` configuration, that the channel is release-only, whether GitHub can be reached, whether a newer **release** exists and whether the source checkout needed for the update button is present. |
| `printer` | Ping of the printer, `ipptool get-printer-attributes` against `hardware.printer_uri`, `lpstat -v` and the details of the output queue. |
| `network` | TCP reachability of the MQTT broker, MQTT connection state, the web app port, `avahi-browse -rt _ipp._tcp`, `ss -ltnp`. |
| `chain` | End to end: submits a real job with `lp`, then waits up to 40 s for `cups-pdf` to drop a PDF in the watched folder, plus `lpstat -W completed -o` and the last service log lines. Not part of the default run because it prints. |

Each step gets a status: `PASS`, `FAIL`, `WARN` (optional check), `SKIP` (tool not installed) or `INFO`.

### How to start it

```bash
# command line (default phases)
sudo WOLSCA_CONFIG=/etc/wolsca/WolsCAPrintService.json \
     /opt/wolsca-print-service/venv/bin/python /opt/wolsca-print-service/main.py --self-test

# selected phases, or every phase including the test print
... main.py --self-test cups,printer
... main.py --self-test --all
```

The exit code is `0` when nothing failed, `1` otherwise, so it can be used in a cron job.

- **Web app**: the *Service self-test* card has *Run self-test* and *Run self-test including a test print* buttons and shows the report inline.
- **Home Assistant**: press the *Run Print Service Self-Test* or *Run Print Service Chain Test* button.
- **MQTT**: publish `SELFTEST`, `SELFTEST_CHAIN` or `SELFTEST:cups,printer` to `<topic_prefix>/command`.

### MQTT topics and Home Assistant entities

| Topic | Content |
| :--- | :--- |
| `<prefix>/diagnostics/step` | One JSON message per step: phase, title, the exact command line, exit code, status and (truncated) output. |
| `<prefix>/diagnostics/report` | Retained aggregated report: `result`, `summary`, counters, `failed_steps`, `markdown` and all `steps`. |
| `<prefix>/diagnostics/state` | `running` while a test is in progress, then the result. |

Auto-discovered entities: `sensor.print_service_self_test` (state = `PASS`/`WARN`/`FAIL`, the full
report in its attributes), `sensor.print_service_self_test_summary`,
`sensor.print_service_self_test_failures` and the two run buttons.

A markdown card renders the whole report:

```yaml
type: markdown
content: |
  {{ state_attr('sensor.print_service_self_test', 'markdown') }}
```

---

## Versioning

The version is `x.y.<commit number>`, for example **1.4.381**, and lives in two plain
text files in the repository root:

| File | Meaning | Who changes it |
| :--- | :--- | :--- |
| `VERSION` | The release `x.y`: `x` is the main release, `y` the minor release. | `x` by hand, `y` by the release tool |
| `BUILD_NUMBER` | The commit number, starting at **381**. | Raised by one on every commit (git hook) |

`Wols_CA_PrintService/version.py` reads both files and is the single source of truth:
`mqtt_service.SERVICE_VERSION`, the MQTT status payload, the Home Assistant device
(`sw_version`), the web app and the self-test all use it.

### The commit number

Enable the git hook once per checkout; it raises `BUILD_NUMBER` and stages it with
every commit:

```bash
./tools/install-git-hooks.sh                   # Linux/macOS
powershell -File tools\install-git-hooks.ps1   # Windows
```

### Releasing

```bash
python tools/bump_version.py --show            # 1.4.381
python tools/bump_version.py --release         # 1.4 -> 1.5 (y + 1)
python tools/bump_version.py --release --major # 1.5 -> 2.0 (x + 1, y = 0)
```

The rule: when `x` was raised - by hand in `VERSION` or with `--major` - the minor
release restarts at `0`; otherwise it becomes the current `y + 1`. The release the
minor counter was last bumped for is remembered in `.version-released`.

### Release notes: `changesFixes.md`

Everything that changes or is fixed is written to **`changesFixes.md`** in the
repository root while working. That file therefore always describes exactly the
*unreleased* work, and it is what the release is announced with:

```bash
python tools/release.py --dry-run   # show the section that would be written
python tools/release.py             # cut the release
python tools/release.py --major     # raise x, y back to 0
python tools/release.py --tag       # also create the annotated git tag vX.Y
```

The tool

1. computes the new release number,
2. prepends `## x.y.<build> - <date>` plus the collected notes to
   **`RELEASE_NOTES.md`** - which is never rewritten, only prepended to, so it
   stays 100% complete - and to `CHANGELOG.md`,
3. writes `VERSION` and `.version-released`,
4. **empties `changesFixes.md`** back to its template.

Use that section as the body of the GitHub release; the update entity in Home
Assistant shows it as the release notes. `tools/release.py` refuses to run while
`changesFixes.md` only holds the `(none yet)` placeholders (override with
`--allow-empty`).

### Check for updates and update

**Only published releases count.** The update check compares the installed version
with the latest **GitHub release** and installs exactly that tag, so an ordinary
commit never makes Home Assistant offer an update. When no release exists yet the
check says so and reports *no* update - it deliberately does not fall back to the
branch.

A commit build can still be installed on purpose, as a **test build**: it is read
from `VERSION`/`BUILD_NUMBER` on `update.branch` and only ever checked or installed
when the corresponding button is pressed (`update.allow_test_builds`, default
`true`, switches the possibility off).

```bash
... main.py --version                # print release, commit number and git hash
... main.py --check-update           # exit code 1 when a newer release exists
... main.py --update                 # install that release (--force reinstalls)
... main.py --check-update --test     # what is on the branch (test build)
... main.py --update --test           # install the branch head - testing only
```

The service also checks by itself every `update.check_interval_hours`, and installs
the new **release** straight away when `update.auto_update` is on. Automatic updates
never install a test build.

- **Web app**: the *Version and updates* card shows the installed and the latest
  version and has *Check for update*, *Update now*, *Automatic updates* plus
  *Check for test build* and *Install test build*.
- **Home Assistant**: `update.print_service_update` (a real update entity with an
  *Install* button and the release notes), `sensor.print_service_version`,
  `sensor.print_service_test_build`, the *Check for Print Service Update*,
  *Install Print Service Update*, *Check for Print Service Test Build* and
  *Install Print Service Test Build* buttons and the *Print Service Automatic
  Update* switch.
- **MQTT**: publish `CHECK_UPDATE`, `INSTALL_UPDATE`, `CHECK_TEST_BUILD`,
  `INSTALL_TEST_BUILD`, `AUTOUPDATE_ON` or `AUTOUPDATE_OFF` to
  `<topic_prefix>/command`; the state is retained on `<topic_prefix>/update/state`
  and `<topic_prefix>/update/auto`.

The update runs `git fetch --all --tags` + `git reset --hard <release tag>` (or
`origin/<branch>` for a test build) in `update.source_directory` and then
`deploy/debian/install.sh`, which reinstalls the files and restarts the unit. So the
server needs a git checkout of the repository - by default
`/usr/local/src/wolsca-print-service` - and the service must run as root (it does)
for `install.sh` to succeed.

---

## Administrator configuration editor

The configuration file stays the source of truth, but the settings that are
actually tuned can be changed without a shell - by the administrator only.

- **Web app**: the *Administrator* card is locked until the token from
  `web.admin_token` is entered. It then shows one field per editable setting,
  *Save configuration*, and asks **"Configuration saved. Restart the service now?"**
  - answer *yes* and the service restarts, *no* and the change waits for the next
  restart. *Discard changes* re-reads the file, *Lock* closes the card again.
  While `web.admin_token` is empty the editor cannot be opened at all.
- **Home Assistant**: a separate device *Wols CA Print Service Admin* carries one
  entity per setting (`text`, `number`, `switch` or `select`, all in the *config*
  category), plus `binary_sensor` *Restart Required* - the same question in HA form -
  and the *Restart Print Service* and *Discard Configuration Changes* buttons.
  Keeping it as its own device means it can be hidden from, or restricted to,
  specific Home Assistant users.
- **MQTT**: values are retained on `<prefix>/admin/value/<key>` and set by publishing
  to `<prefix>/admin/set/<key>` (for example `<prefix>/admin/set/web_title`);
  `RESTART_SERVICE` and `RELOAD_CONFIG` on `<prefix>/command` do the rest.

Every value is validated (type, range, allowed options) before it is written, a
`.bak` of the configuration is kept, and only the whitelisted keys in
`admin.FIELDS` can be edited - paths and intake queues still need the file, which
keeps a typo from breaking the print chain.

---

## Local test container (Docker Desktop)

`deploy/docker/` builds a container that runs the service exactly as the Debian
server does - same packages, same `installer.py`, same CUPS intake queues, same
`/etc/wolsca/WolsCAPrintService.json`, same spool directories. The base image is the
Debian flavour of the Home Assistant base images, so the same image can later be
published as a Home Assistant add-on.

```powershell
.\tools\run-local-test.ps1            # start it, web app on http://localhost:8080/
.\tools\run-local-test.ps1 -SelfTest  # run every self-test phase and exit
.\tools\run-local-test.ps1 -Shell     # shell inside, CUPS running
```

See `deploy/docker/README.md` for the details.

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
VERSION                                         Release 'x.y' (x by hand, y by the release tool)
BUILD_NUMBER                                    Commit number, raised on every commit
build_and_release.ps1                           One-shot pipeline: check, build, print test, tests, commit, package, release
tools/make_test_pdf.py                          Generates the multi-page test document for the print test
TestPrint/                                      Test documents; the printed result lands in TestPrint/Results/
tools/bump_version.py                           Raises the commit number and the release number
tools/git-hooks/pre-commit                      Raises BUILD_NUMBER on every commit
tools/install-git-hooks.sh / .ps1               Enables the hooks (core.hooksPath)
LICENSE                                         MIT license
.run/                                           Shared CLion run configurations
docs/USER_GUIDE.md                              Per-device usage guide
deploy/debian/                                  Debian/Ubuntu deployment (systemd, CUPS, Avahi)
    install.sh / uninstall.sh                   Installer and remover
    fix-permissions.sh                          Repairs owner/group/mode of all locations
    wolsca-print-service.service                systemd unit
    WolsCAPrintService.linux.json                Default Linux configuration
Wols_CA_PrintService/
    Wols_CA_PrintService.py       The service (watcher, imposition, web app)
    WolsCAPrintService.json                      Runtime configuration (development)
```

## Build, test and release pipeline (`build_and_release.ps1`)

One PowerShell script does everything from a saved working tree to a published
release, on Docker Desktop. Every step must pass before the next one starts, so
nothing is committed or pushed when a test fails:

| Step | What happens |
| :--- | :--- |
| 0 | Asks the IDE (CLion/Visual Studio) to save all open files |
| 1 | Preconditions: Python 3, Docker daemon, git working tree, registry login |
| 2 | Syntax check of every Python file, import check of all modules, validation of the shipped JSON |
| 3 | Builds the Debian container image (`deploy/docker/Dockerfile.debian`) |
| 4 | Print test to a **virtual printer**: a real job through intake queue, cups-pdf, watcher, imposition and the flip - written as PDF files instead of paper |
| 5 | All self-test phases inside the container (`chain` excluded: step 4 covers it better) |
| 6 | Raises `BUILD_NUMBER`, commits and pushes |
| 7 | Pushes the container package (`build-<version>`, `test`; on a release also `<version>` and `latest`) |
| 8 | With `-Release`: cuts the release from `changesFixes.md`, tags `vX.Y`, branches off to `release/vX.Y` and freezes that branch |

```powershell
.\build_and_release.ps1 -SkipGit -SkipPush            # only verify locally
.\build_and_release.ps1 -CommitMessage "Watcher fix"   # commit + test package
.\build_and_release.ps1 -Release -CommitMessage "..."  # release, tag and freeze
```

The print test uses the newest PDF in `TestPrint/` (or `-TestDocument <file>`;
without either, `tools/make_test_pdf.py` generates a three-page A4 document).
Three pages means two sheets, so front side, flip and back side are all
exercised. At the flip prompt the script waits `-FlipWaitSeconds` (default 30)
before pressing Continue, so the state can be followed in Home Assistant and the
web app. The result is stored as
`TestPrint/Results/<document>-<version>-front|back.pdf`, and the container logs
land in `build/logs/`.

The virtual printer is `WOLSCA_VIRTUAL_OUTPUT=1`: the container points
`hardware.printer_uri` at `wolscafile:<dir>` before the installer runs, so the
output queue uses the `wolscafile` CUPS backend, which copies the produced PDF
into `WOLSCA_VIRTUAL_OUTPUT_DIR`. The container refuses to start when the output
queue is not that virtual printer, so an automated test can never print on the
real printer.

Freezing the release branch uses the GitHub API and needs `$env:GITHUB_TOKEN`
(or a git-ignored `.github_token`) with administration rights; without a token
the branch is pushed but has to be locked by hand under *Settings > Branches*.

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
