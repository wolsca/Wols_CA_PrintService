# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
