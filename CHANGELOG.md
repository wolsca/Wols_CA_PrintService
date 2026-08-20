# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Unreleased changes are collected in `changesFixes.md`; `tools/release.py` moves them into
`RELEASE_NOTES.md` and this file when a release is cut, and empties that file again.

### Added
- Release notes workflow: `changesFixes.md` in the repository root collects every change and fix
  while working, and `tools/release.py` cuts the release from it - it computes the new `x.y`,
  prepends `## x.y.<build> - <date>` plus the collected notes to `RELEASE_NOTES.md` (only ever
  prepended, so the history stays 100% complete) and to `CHANGELOG.md`, writes `VERSION` /
  `.version-released`, empties `changesFixes.md` and can create the `vX.Y` git tag (`--tag`,
  `--major`, `--dry-run`, `--allow-empty`).
- Administrator configuration editor (`Wols_CA_PrintService/admin.py`):
  - Web app: a token-protected *Administrator* card (`web.admin_token`; while it is empty the
    editor cannot be opened) with one field per editable setting, which asks
    "Configuration saved. Restart the service now?" after saving, plus *Restart service*,
    *Discard changes* and *Lock*.
  - Home Assistant: its own MQTT device *Wols CA Print Service Admin* with a `text`, `number`,
    `switch` or `select` entity per setting, a `Restart required` binary sensor and the
    *Restart Print Service* / *Discard Configuration Changes* buttons - so the admin entities can
    be hidden from normal users.
  - Values are validated (type, range, options) against the whitelist in `admin.FIELDS`, a `.bak`
    of the configuration is kept, and MQTT topics `<prefix>/admin/value/<key>` (retained) and
    `<prefix>/admin/set/<key>` plus the commands `RESTART_SERVICE` and `RELOAD_CONFIG` are added.
  - New endpoints `GET/POST /api/admin/config`, `POST /api/admin/restart` and
    `POST /api/admin/reload`, all rejecting requests without the token with `403`.
  - Self-test: new `admin` phase (part of the default run).
- Local test container (`deploy/docker/`): `Dockerfile.debian` on the Debian flavour of the Home
  Assistant base images, `entrypoint.sh`, `docker-compose.yml` and `tools/run-local-test.ps1` run
  the service on Docker Desktop with the same packages, the same `installer.py`, the same CUPS
  intake queues, the same `/etc/wolsca/WolsCAPrintService.json` and the same spool directories as
  the Debian server - so the self-test phases can be run locally, and the same image can later be
  published as a Home Assistant add-on.

### Changed
- Updates only react to **published GitHub releases**: the latest release tag is compared with the
  installed version and installed with `git reset --hard <tag>`, so a commit never makes Home
  Assistant offer an update, and the branch is no longer used as a fallback when no release exists.
- Commit builds are now an explicit *test build* path: `CHECK_TEST_BUILD` / `INSTALL_TEST_BUILD`,
  the *Check/Install test build* buttons in Home Assistant and the web app,
  `main.py --check-update --test` / `--update --test`, `sensor.print_service_test_build` and the
  new `update.allow_test_builds` setting. Automatic updates never install a test build.
- `installer.py` also runs where there is no systemd or `apt-get` (the test container): package
  installation is skipped when the packages are already present and `systemctl` calls are skipped
  instead of reported as errors; everything else is the same code path as on the server.

### Added
- Version numbering `x.y.<commit number>` (currently **1.4.381**), carried by two plain text
  files in the repository root and read by the new `Wols_CA_PrintService/version.py`:
  - `VERSION` holds the release `x.y` - `x` (main release) is raised by hand, `y` (minor release)
    by `tools/bump_version.py --release`: when `x` was raised, `y` restarts at `0`, otherwise it
    becomes the current `y + 1` (the last released `x.y` is remembered in `.version-released`).
  - `BUILD_NUMBER` holds the commit number, starting at **381**, raised by one on every commit by
    `tools/git-hooks/pre-commit` (enable once with `tools/install-git-hooks.sh` / `.ps1`).
  - `mqtt_service.SERVICE_VERSION`, the MQTT status payload, the Home Assistant device
    (`sw_version`), the startup banner, the web app and the self-test all use this single source;
    the hardcoded `"1.4.2"` and `"2.1.0-Hybrid"` strings are gone. `install.sh` ships both files
    to `/opt/wolsca-print-service/` and prints the installed version.
  - New command line switches: `main.py --version`, `--check-update` and `--update [--force]`.
- Update checking and self-update (`Wols_CA_PrintService/updater.py`) with a new `update`
  configuration section (`enabled`, `repository`, `branch`, `channel`, `check_interval_hours`,
  `auto_update`, `source_directory`, `update_command`):
  - Reads the latest version from the newest GitHub release tag, or from the `VERSION` /
    `BUILD_NUMBER` files on the branch when `channel` is `branch` (also the fallback when no
    release exists yet); the state is published retained to `<prefix>/update/state`.
  - Installing runs `git fetch` + `git reset --hard origin/<branch>` in `update.source_directory`
    and then `deploy/debian/install.sh`, with the whole command output logged and published.
  - The service checks every `check_interval_hours` and installs automatically when
    `auto_update` is on; the switch is persisted in the configuration.
  - Home Assistant: a real `update` entity (installed/latest version, *Install* button, release
    notes), `sensor.print_service_version`, a *Check for Print Service Update* button, an
    *Install Print Service Update* button and a *Print Service Automatic Update* switch;
    MQTT commands `CHECK_UPDATE`, `INSTALL_UPDATE`, `AUTOUPDATE_ON` and `AUTOUPDATE_OFF`.
  - Web app: new *Version and updates* card, plus `GET /api/update`, `POST /api/update/check`,
    `POST /api/update/install` and `POST /api/update/auto`.
  - Self-test: new `update` phase (part of the default run) reporting the version files, the
    update configuration, the reachability of GitHub, whether a newer version exists and whether
    the source checkout needed for the update button is present.
- `deploy/debian/fix-permissions.sh`: one idempotent script that applies the intended owner,
  group and mode to every location the service uses - `/opt/wolsca-print-service` (root:root,
  `0755`/`0644`), `/etc/wolsca` (`0755`) and its JSON (`0664`), the spool root and all drop, temp
  and error directories taken from the live configuration (`root:lp`, setgid `2775`, files `0664`),
  the history file, the systemd unit and the `cups-pdf*.conf` files. `install.sh` runs it as its
  new last step (8/8), so it also covers what `--install-printer` just created, and installs it to
  `/opt/wolsca-print-service/fix-permissions.sh` for later repairs.
- Self-test: new `permissions` phase (part of the default run) verifying owner, group, mode and
  the real read/write access of the configuration and of every spool directory, and naming
  `fix-permissions.sh` as the remedy.
- Self-test / diagnostics module (`diagnostics.py`): runs the print chain phase by phase
  (`system`, `config`, `permissions`, `update`, `cups`, `printer`, `network`, and the optional `chain` test print) and logs
  every Debian command together with its exit code and output.
  - Each step is published to `<prefix>/diagnostics/step`; the aggregated report (counters,
    failed steps and a ready-made markdown summary) is published retained to
    `<prefix>/diagnostics/report`, with the run state on `<prefix>/diagnostics/state`.
  - Home Assistant auto-discovery for the result, summary and failure-count sensors plus two
    buttons ("Run Print Service Self-Test" and "Run Print Service Chain Test").
  - Can be started from the web app ("Service self-test" card), from Home Assistant, over MQTT
    (`SELFTEST`, `SELFTEST_CHAIN`, `SELFTEST:cups,printer`) or from the command line
    (`main.py --self-test [phases|--all]`, exit code 1 when a step failed).
  - New endpoints `GET /api/diagnostics` and `POST /api/diagnostics/run`.

### Changed
- `install.sh` no longer makes `/etc/wolsca/WolsCAPrintService.json` world-writable (`0666`);
  it is now `root:root 0664` and `/etc/wolsca` is `0755` instead of `0750`, so CUPS and the
  diagnostics can traverse it while the service can still rewrite the printer target.
- Refactored the monolithic Wols_CA_PrintService.py script into a highly readable, modular architecture (main.py, pdf_processor.py, etc.) for improved maintainability.

### Fixed
- CI: the workflow still byte-compiled the removed monolithic `Wols_CA_PrintService.py`, so every
  push failed. It now compiles the whole package, imports all modules and runs the offline
  self-test phases.

### Fixed
- Jobs disappeared silently ("printers visible, nothing prints"):
  - The folder watcher now reacts to moved and closed files as well, not only created ones.
    `cups-pdf` renders into its own spool folder and *moves* the finished PDF into the drop
    directory, which inotify reports as a move, so nothing was ever queued.
  - Intake directories are watched recursively and rescanned every 15 seconds as a safety net
    (inotify is unreliable on LXC/overlayfs and bind mounts). Set `WOLSCA_POLL_WATCHER=1`
    to force the polling observer.
  - Enqueueing is now idempotent, so the same file cannot be queued twice.
- Installer: `cupsctl --share-printers --remote-any` returns "Not Implemented" on some CUPS 2.4
  builds. The installer now falls back to writing `Listen *:631`, `Browsing On`,
  `BrowseLocalProtocols dnssd`, `DefaultShared Yes` and `Allow @LOCAL` into `/etc/cups/cupsd.conf`.
- Physical output: raw PDF bytes sent to TCP 9100 are silently discarded by printers without
  native PDF direct print. The installer now creates a driverless output queue
  (`hardware.cups_queue_name`, default `WolsCA_Output`) from `hardware.printer_uri` and switches
  the default printer target to `dispatch: cups`, which also enables real page progress and duplex.

### Changed
Added: Extracted hard-coded UI texts into web_strings.json to facilitate easy translations.



### Changed
- Renamed the project from "Wols CA Double Sided Print Service" to "Wols CA Print Service":
  - Source folder and script: `Wols_CA_PrintService/Wols_CA_PrintService.py_old`.
  - Project files: `Wols_CA_PrintService.slnx` / `.pyproj`.
  - Configuration files: `WolsCADoubleSided.json` -> `WolsCAPrintService.json` and
    `WolsCADoubleSided.linux.json` -> `WolsCAPrintService.linux.json`.
  - Upgrade note: rename `/etc/wolsca/WolsCADoubleSided.json` to `/etc/wolsca/WolsCAPrintService.json`
    (or set `WOLSCA_CONFIG`) before restarting the service.
  - The systemd unit name, CUPS queue names and spool directories are unchanged.

## [1.4.0] - 2026-08-20
### Added
- "Choose the print mode in the print dialog" workflow:
  - Support for multiple intake CUPS queues, each mapped to a specific print mode.
  - Three default queues created by the installer: `WolsCA_Booklet`, `WolsCA_DoubleSided`, and `WolsCA_SingleSided`.
  - New "intake" configuration section to manage these queues and their target directories.
  - Automatic upgrade of old configuration files to the new intake format.
- Two new print modes:
  - `Duplex`: Forced double-sided printing. Uses native CUPS duplexing or manual flip logic for raw printers.
  - `Simplex`: Forced single-sided printing.
- Web app updates:
  - New card "Printers you can choose in the print dialog" listing available queues.
  - New print modes added to the mode selection.
  - New endpoint `GET /api/queues`.
- Installer improvements:
  - Support for multiple `cups-pdf` backend instances and configurations.
  - Automatic creation of per-mode sub-directories in the drop folder.
  - `uninstall.sh --purge` removes all intake queues, the `cups-pdf-*` backend instances and their configuration files.
- Documentation:
  - How to publish a stable name (e.g. `print.home.lan`) from a local DNS server: Pi-hole (v5/v6), dnsmasq, Unbound, BIND9, AdGuard Home and consumer routers.

### Changed
- Mode precedence: Intake queue choice > Web app personal choice > Global settings.
- `GET /api/status` and MQTT status payload now include intake queue details.
- Version bumped to 1.4.0 across the application.

## [1.3.0] - 2026-08-20
### Added
- Push notifications via ntfy/Gotify:
  - New `notify` config section.
  - Automatic notification when paper needs to be flipped or an error occurs.
  - "Continue printing" action button in the notification.
- Flip help:
  - Visual illustration and instruction text shown in the web app during the flip step.
  - Overridable global (`hardware.flip_instruction`) and per-printer (`flip_instruction` in `printers.targets`).
- Cancel and reprint:
  - New "Cancel Print Job" and "Reprint Front Side" buttons in the web app.
  - Corresponding MQTT buttons and API endpoints (`POST /api/cancel`, `POST /api/reprint`).
- Flip timeout:
  - Automatic cancellation of stale jobs after a timeout (`hardware.flip_timeout_seconds`, default 1800s).
- Job history:
  - Last jobs shown in the web app and available via `GET /api/history`.
  - Configurable `history` section and storage file.
- Per-job options in the web app:
  - Select printer, print mode (Booklet / Standard / Bypass), and copies (1-10) for the next job.
  - Options apply for a TTL period (`printers.personal_choice_ttl_seconds`).
- Duplex printer support:
  - `duplex: true` and `dispatch: cups` support for printers that handle double-sided printing natively.
  - Skips the manual flip step for these printers.
- Real-time page progress:
  - Shows progress bar and "Sheet X of Y" when using `dispatch: cups`.
  - Requires `ipptool` (from `cups-ipp-utils` or `cups-client` packages).
- Job queueing:
  - Serialized printing with a waiting list visible in the web app.
- QR code page:
  - New `GET /qr` page showing a QR code for the web app URL (requires `segno` package).
- UI localization:
  - Dutch (nl) translation and `web.language` setting.
- Home Assistant improvements:
  - New entities for Cancel, Reprint, Print Mode, and Target Printer.
  - Status payload expanded with more job details.

### Changed
- Improved error messages for common issues (encrypted PDF, unreachable printer, etc.).
- Encrypted or empty PDFs now fail immediately and are moved to the error directory.
- `GET /api/status` and MQTT status payload now include version, copies, duplex, and queue info.

## [1.2.0]
### Added
- Booklet imposition (4 pages per A4 sheet).
- MQTT/Home Assistant integration.
- Mobile web app with printer picker.
- Debian/Ubuntu installer with CUPS, cups-pdf, and Avahi.
