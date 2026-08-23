# Changes and fixes for the next release

Add one bullet per change while you work. The release tool (`python tools/release.py`) moves
everything below into `RELEASE_NOTES.md` and `CHANGELOG.md` under the new version number and
empties this file again, so it always describes only the *unreleased* work.

## Changes

- The configuration file now carries its own version (`config_version`, currently `1.1.b`) and is
  upgraded step by step at start-up. A file without the key is a `1.0` file, the migrations newer
  than the version in the file are run in order (so 1.0 straight to a future 1.3 still runs every
  step in between) and already applied steps are skipped; a step may carry a letter, which makes it
  a sub-step of that version (`1.1` < `1.1.a` < `1.1.b` < `1.2`). Step `1.1.a` adds
  `hardware.wake_on_lan`, `printer_mac`, `wake_broadcast` and `wait_for_printer_seconds` and looks
  the MAC address of the printer up over the network (the printer is contacted first, then the
  neighbour table is read) so it does not have to be typed in by hand; step `1.1.b` adds
  `printer_mac_wired`, `printer_mac_wifi`, `recover_printer_ip` and `block_on_mac_change` and gives
  every intake queue its own `printer`. Values already in the file are never overwritten, the file
  is only rewritten when something really changed, and the self-test reports the version of the
  configuration in its `admin` phase.
- **One MAC address per interface.** A printer has a different MAC address on the cable than on
  Wi-Fi, so one address was not enough: `hardware.printer_mac_wired` and
  `hardware.printer_mac_wifi` hold what is printed on the printer itself and `hardware.printer_mac`
  is the *working* address, the one that answers now. A Wake-on-LAN packet is sent to every known
  address (a sleeping printer does not say which interface will come up), an address that matches
  the other interface simply switches the working one over, and an address that matches **neither**
  is no longer adopted silently: the administrator is warned with *has the printer been changed, or
  is another device using this IP address?*
- **A printer that lost its DHCP address is found again** (`hardware.recover_printer_ip`, on by
  default). Only the MAC address survives a lease that moved, so the neighbour table of the server
  is searched for the known addresses (priming it first with one UDP datagram per address of the
  subnet) and `hardware.printer_uri` plus the host of the target are rewritten to the address that
  carries it. It runs before a job concludes that the printer is off and once a minute while a job
  waits, so a printer with a plain DHCP address no longer needs a reservation.
- **New safety switch `hardware.block_on_mac_change`** (on by default): an IP address does not say
  *which* machine answers on it, so when the MAC address at the printer's address belongs to neither
  of the printer's interfaces the job is stopped instead of printed on an unknown device. The job
  log names the address, and switching the setting off prints anyway.
- **The administrator can pick a printer from a list.** New `printer_discovery.py` asks
  Avahi/Bonjour for `_ipp._tcp` and `_ipps._tcp` and additionally probes port 631 on the local
  subnet (a printer with mDNS switched off announces nothing), and reports every printer with its
  name, address, port and MAC address. The *Administrator* card of the web app has a drop down with
  the found printers, a *Search for printers* and a *Use the selected printer* button - which takes
  over the URI, the host of the target and the MAC address in one go - and the `network` self-test
  phase lists the same printers.
- **A printer per intake queue**: `intake.queues[].printer` sends booklet, double sided and single
  sided each to their own printer, editable as *Printer for ...* in the web app and in Home
  Assistant. Empty means the default printer, a single configured printer is simply used, and a
  personal choice from the web app still wins; the job log says which of the three decided.
- The self-test report can be opened as plain text (*Open report as plain text*,
  `GET /api/diagnostics/report.txt`), so it can be selected and copied on any device - also over
  plain HTTP, where the browser refuses the clipboard API - instead of being screenshotted.
- The MAC address of the printer is kept correct by the service itself instead of being typed in
  once: it is verified at start-up (in a background thread), before every print job for which the
  printer is checked, and in the `printer` self-test phase (*Printer MAC address verified /
  switched to the other interface / detected and saved / corrected / unknown / not verifiable*, plus
  *Another MAC address than configured - has the printer changed?*). An empty `hardware.printer_mac`
  is filled in and an address that no longer matches the printer on the network - a replacement,
  another network card, cable swapped for Wi-Fi - is corrected in the configuration, because a magic
  packet to the old address wakes nothing without reporting anything.
- Updates offered through Home Assistant now only react to published GitHub releases; commit
  builds from the branch are only installed on request, with the *Check for test build* and
  *Install test build* buttons.
- New admin section: a token-protected configuration editor in the web app and matching Home
  Assistant entities, which asks whether the service should be restarted after saving.
- New local test container (`deploy/docker/`, Debian flavour of the Home Assistant base image) to
  run the installer, the service and the self-test phases on the desktop with Docker Desktop -
  same packages, same queues, same configuration as the server; credentials come from the
  git-ignored `deploy/docker/.env.local`.
- The `chain` self-test phase now watches the whole drop tree and reports in which folder the PDF
  landed, so a cups-pdf instance writing to the wrong directory is named instead of just failing.
- Release notes are now written from `changesFixes.md`; `RELEASE_NOTES.md` keeps the complete
  history.
- New `build_and_release.ps1`: one pipeline that saves the open files, checks the syntax and the
  imports of all Python files, builds the Debian container, prints a real multi-page booklet job to
  a **virtual printer** (PDF into a folder, including the flip), runs all self-test phases and only
  then commits, pushes and publishes the container package; with `-Release` it cuts the release,
  tags `vX.Y`, branches off to `release/vX.Y` and freezes that branch through the GitHub API.
- The test container has a virtual printer (`WOLSCA_VIRTUAL_OUTPUT=1`): the new `wolscafile` CUPS
  backend copies the produced PDF into `WOLSCA_VIRTUAL_OUTPUT_DIR`, and the container refuses to
  start when the output queue is not that printer - so an automated test can never print on paper.
- Test documents live in `TestPrint/` (or `-TestDocument`; otherwise `tools/make_test_pdf.py`
  generates a three-page A4 document); the printed result is stored as
  `TestPrint/Results/<document>-<version>-front|back.pdf`.

- The repository is now a **Home Assistant add-on repository**: `repository.json` plus
  `wolsca_print_service/` (follows the published releases) and `wolsca_print_service_test/`
  (follows every commit build), both using the container image that is already published to GHCR.
  Add the repository URL in the Add-on Store and install it - the add-on version is kept in step
  with the pushed image tag by `build_and_release.ps1`, so Home Assistant only offers an update
  when a release is cut.
- The add-on writes its options into the same `WolsCAPrintService.json` (in `/data/`, the
  persistent location), so the configuration and the way it is used are identical to a Debian
  installation; the broker details of the Mosquitto add-on are picked up through the Supervisor
  mqtt service. There is deliberately no ingress and no sidebar panel: the web app keeps its own
  port (`web_port`).
- New `mqtt.instance_id`: an installation label, empty by default so existing installations keep
  exactly the entities they have. When it is set, every `unique_id`, the discovery node and the
  device become their own, and the add-on prefixes the MQTT topic with `HA_` (idempotently, at
  start-up) - so the add-on and the Debian service can run side by side on one broker and show up
  as two devices in Home Assistant.
- New `tools/check_addons.py` validates the add-on repository (manifest keys, options/schema in
  step, unique instance labels); it runs in CI and in the pipeline.
- `deploy/debian/install.sh` now installs everything a **minimal** Debian server lacks: the
  certificates, download, network, mDNS and diagnostic tools plus the complete CUPS/PDF filter
  chain (`cups-filters`, `ghostscript`, `poppler-utils`), in two explicit package lists. A package
  the distribution does not offer is reported and skipped instead of aborting the install. Only
  `git` stays a prerequisite, since the checkout comes from it.
- `README.md` installation section restored: the step that *gets the repository* (clone into
  `/usr/local/src/wolsca-print-service`, the default `update.source_directory`) had gone missing,
  so the instructions jumped straight to `--install-printer`. It now reads: 1 get the repository,
  2 `deploy/debian/install.sh --with-cups` (plus how to upgrade), 3 verify. The development section
  got its clone step and points at `Wols_CA_PrintService/main.py` instead of the removed
  monolithic script, and the unterminated code fence of the project layout was closed.

- The MQTT credentials moved from `settings.user` / `settings.password` to `mqtt.user` /
  `mqtt.password`, next to the broker address, and the default account name is now `wolsca_mqtt` -
  a *broker* account, not a system user. A configuration that still has them under `settings` keeps
  working, since `config.get_mqtt_credentials()` falls back to that location. The README documents
  which accounts to create where: both `wolsca_mqtt` and `wolsca_mqtt_ha` in Home Assistant's
  Mosquitto when the add-on and a Debian server share one broker, only `wolsca_mqtt` (Mosquitto or
  EMQX on the server) for a Debian-only installation.
- The default MQTT topic prefix is now `wols_ca/print_service` (test add-on
  `wols_ca/print_service_test`); the stray fallback `wols_ca/printer_servic` in `mqtt_service.py`
  is gone.
- The print modes are named after the result: `Duplex` became **`DoubleSided`** and `Simplex`
  became **`SingleSided`** ("duplex" means two directions, the mode means two sides). `Booklet` is
  unchanged, and so are the CUPS queue names. `config.normalize_print_mode()` still accepts the old
  names, and `settings.print_mode` now defaults to `DoubleSided`.
- The last places still called `duplex`/`simplex` are renamed as well: the intake queue **ids** and
  the drop directories are now `booklet`, `doublesided` and `singlesided` (so also the cups-pdf
  instances `/etc/cups/cups-pdf-doublesided.conf` and `-singlesided.conf`). New
  `config.normalize_intake_id()` migrates an existing configuration once - id, print mode and, only
  when it still carries an old name of that queue, the directory - and saves the result, so the file
  no longer keeps showing the old names. A path chosen by hand is left alone, `installer.py`
  lower-cases the id when it derives the backend name (a capitalised id could otherwise split the
  cups-pdf instances), and `uninstall.sh --purge` removes the old and the new `cups-pdf-*.conf`.
- **Push notifications are implemented at last** and are on by default: the `notify` section
  existed but nothing ever sent a message. New `Wols_CA_PrintService/notifier.py` (standard library
  only, ntfy) pings the phone when the front side is printed and the paper has to be flipped - with
  a click link to the web app from `web.public_url` - when a job fails and when a document is
  finished. An empty `notify.topic` makes the service generate a unique
  `wolsca_print_service_<random>` at first use and write it back, because on the public ntfy server
  the topic name is the only secret. New self-test phase `notify` (part of the default run) sends a
  real test message, and the add-on gained `notify_enabled`, `notify_url` and `notify_topic`.
- The Debian test container is now simply called `wolsca` (compose and `tools/run-local-test.ps1`).
- The `config` self-test phase now also checks that the broker account is filled in and that the
  default print mode is a known one.
- **The flip can be confirmed on the printer itself.** When the printer offers AirPrint/Mopria
  manual duplex (`manual-duplex-supported`, as the HP M282nw does), the job is sent straight to the
  printer over IPP with `sides=two-sided-*` plus `manual-duplex-sheet-count`: the printer prints the
  front sides, asks on its own display to put the stack back and finishes after the button *there*
  is pressed. It deliberately bypasses the CUPS output queue, because such printers report
  `sides-supported = one-sided` and the `everywhere` queue would strip the two-sided request.
  New `Wols_CA_PrintService/printer_capabilities.py` probes this once (cached) and new
  `hardware.flip_confirmation` (`auto` / `printer` / `service`) decides who asks.
- **Only one Continue exists at a time.** The new `flip_owner` in the job state is published with
  the status, and while the printer owns the flip the web app hides its Continue and Reprint buttons
  (showing the printer instruction instead) and the Home Assistant *Resume Print (Flip)* button goes
  *unavailable* through a new availability topic. Both `RESUME` over MQTT and `POST /api/resume` are
  refused and logged in that case, so the two can never be pressed against each other.
- New `hardware.single_page_paper_change` for a **single page** in DoubleSided mode, so special
  paper can be loaded for exactly that page without another job using it: `printer` sends the page
  to the manual feed slot (`media-col`/`media-source`, default `manual` via
  `hardware.single_page_media_source`), so the printer asks for the paper on its own panel and
  prints nothing until OK - no sheet is wasted; `pause` asks in the web app / Home Assistant first;
  `blank` prints a blank front so the printer asks and the page lands on the back of that sheet;
  `off` (default) keeps the current behaviour.
- The `printer` self-test phase reports who confirms the flip and why, and the administrator editor
  offers the three new settings.
- The self-test card in the web app has a **Copy report** button, so the whole report can be pasted
  somewhere else instead of being selected by hand while the page refreshes. It uses the clipboard
  API on a secure origin and falls back to a hidden textarea (and, as a last resort, selecting the
  report and asking for Ctrl+C), because the web app is normally reached over plain `http`. The
  report text is also only rewritten when it really changed, so a selection survives the polling.

- CUPS is no longer optional: `deploy/debian/install.sh` installs the printing tool chain and
  creates the intake queues **by default**. `--with-cups` is still accepted and does nothing, and
  the rare "CUPS is managed elsewhere" case is now `--without-cups`.

- **Every print job now has a step by step log.** A job used to leave almost no trace - "NEW PRINT
  JOB DETECTED", a state change and, at best, one error line - so there was no way to see *where* a
  booklet, a double sided or a single sided job went wrong. New
  `Wols_CA_PrintService/job_log.py` keeps one timeline per job and forwards it to all consumers at
  once: the journal (one line per step, `[Job 12] dispatch: ...`), MQTT for Home Assistant, the web
  app and a rolling history file (`history.enabled` / `history.max_entries` / `history.file` are in
  use at last, default `<temp>/job-history.json`, restored at start-up).
  Recorded are: the intake queue the file came from, the print mode **and where it came from**
  (intake queue or web app/configuration), the target printer with its dispatch route, host, port,
  CUPS queue and who confirms the flip, the PDF analysis, every imposition (booklet, duplex
  interleave, blank front, odd/even split), the chosen plan per branch, the exact `lp`/`ipptool`
  command line, the job id CUPS or the printer returned, page progress, the flip wait and who
  pressed Continue (web app, Home Assistant or the printer panel), and the result - with the
  exception type, message and traceback when it failed.
- **Home Assistant can answer "where did it go wrong?"**: three new sensors - *Print Job Step*
  (state = the step name, attributes = the complete timeline of the running job), *Print Job Detail*
  (`level: message` of the last step, usable as an automation trigger) and *Print Job Result* (the
  result of the last finished job with its whole timeline as attributes). They are published on
  `<prefix>/job/step` and `<prefix>/job/last`, both retained.
- The web app has a **Job log** card next to the self-test: the last step of the current job, the
  full timeline behind *Show job log* and a *Copy job log* button (the clipboard code is now shared
  with the self-test report). New endpoint `GET /api/joblog`.
- Things that silently swallowed a job are now reported: a PDF that never stops growing (the spooler
  never finished writing it), a `.prn`/`.ps`/`.pcl` file found by the rescan, a printer configured
  for CUPS while `lp` is missing (the job would quietly leave over raw port 9100 without duplex or
  progress), a missing `ipptool`, and CUPS dropping a job - the state of the queue is queried and
  logged when the job leaves it. At start-up every watched directory is logged with the queue and
  the print mode it belongs to.

- **A printer that is switched off or asleep is no longer an error.** New
  `Wols_CA_PrintService/printer_power.py` decides with a plain TCP connect (to the port of
  `hardware.printer_uri`, the port of the target and 631/443/9100/80) whether the printer is on the
  network at all. Before every dispatch the job waits for it - state `WAITING_FOR_PRINTER` in the
  web app and Home Assistant, one job-log line per minute and one ntfy message - and continues by
  itself as soon as the printer answers, up to `hardware.wait_for_printer_seconds` (default 900,
  `0` switches the waiting off). Cancel and shutdown end the wait immediately.
- **The printer can be woken over the network**: when `hardware.printer_mac` is filled in and the
  printer has Wake-on-LAN enabled, a magic packet is sent to UDP port 9 (`hardware.wake_broadcast`,
  default `255.255.255.255`) before waiting, and again every minute for as long as the job waits.
  `hardware.wake_on_lan` switches this off. The MAC address is required because a Wake-on-LAN
  packet carries nothing but the MAC - an IP address cannot be used for it, and a sleeping printer
  no longer has an ARP entry to look it up from; a printer switched off at its power switch can
  never be woken this way.
- The `printer` self-test phase has a new **Printer answers on the network** step and a *Waking the
  printer* line that explains what will happen; when `hardware.printer_mac` is empty it also prints
  the MAC address it finds in the ARP table (`printer_power.detect_mac()` via `ip neigh`/`arp`)
  while the printer is awake, ready to be pasted into the setting. `hardware.wake_on_lan`,
  `hardware.printer_mac` and `hardware.wait_for_printer_seconds` are editable in the web app and in
  Home Assistant.

## Fixes

- `installer.py` no longer aborts where there is no `apt-get` and no longer reports missing
  `systemctl` as an error, so `--install-printer` also runs inside a container.
- The service no longer starts without its print queues: an installation done without the old
  `--with-cups` flag left the log repeating `[Zero-Touch] CUPS queue 'WolsCA_Booklet' not found. Run
  'sudo <python> main.py --install-printer' to create it.` on every start, and nothing could be
  printed. CUPS is now part of the default installation, and `installer.check_cups_queue()` creates
  the missing intake queues itself (the same `create_intake_queue()` path as `--install-printer`,
  plus network sharing and the physical output queue) whenever the service runs as root and
  `lpadmin` is present. The old hint is only printed when creating them is not possible.
- `install.sh` makes every script in `deploy/debian` executable itself (`chmod +x .../*.sh`, right
  after it resolved the repository root), so a checkout that lost the executable bit - Windows, a ZIP
  download, a copy over SMB - no longer stops at "Permission denied" on `fix-permissions.sh` or
  `uninstall.sh`, and the manual `chmod +x deploy/debian/*.sh` step is no longer needed.
- The output queue is only created driverless (`-m everywhere`) for IPP targets; a `socket:` or
  file backend now gets a raw queue, so the PDF is passed through unchanged instead of being
  rejected.
- The `printer` self-test phase skips the IPP query when the output does not go to an IPP target,
  instead of failing.
- The `printer` phase no longer *fails* on a ping: the configured printer host is only pinged when
  the job really leaves over the network, so with the virtual printer of the test container the
  check is reported as skipped (it says nothing about that path). Where the host does matter, an
  unanswered ping is a warning instead of a failure, because ICMP is often blocked while IPP works
  fine. Without this the whole build pipeline stopped on `printer: Ping <printer ip>` even though
  the print test to the virtual printer had just succeeded.
- The service could never start: `wolsca-print-service.service` was shipped with `User=wolsca`
  while `install.sh` installs everything as `root` (`SERVICE_USER="root"`) and therefore never
  creates that account, so systemd stopped with `status=217/USER` before Python was started and
  the unit ended up in a restart loop. Home Assistant then still showed the retained MQTT values,
  which looks like "I can see all settings but cannot change them". The unit now runs as
  `root:root` with `SupplementaryGroups=lp`, and `install.sh` rewrites `User=`/`Group=` when it is
  installed with a different service user, so unit and installer can no longer drift apart.
- `install.sh` printed `systemctl status wolsca-print-serviceexit`: a stray `exit` was glued to the
  closing `echo`.
- Being ready no longer depends on the MQTT broker: `IDLE` was only announced from the MQTT
  `on_connect` callback, so a refused login (`Connection failed, return code Not authorized`) left
  the service in `STARTING` forever - printing worked, but the web app and the build pipeline never
  saw a ready service ("The service did not reach IDLE inside the container"). The state is now set
  as soon as the folder watchers run. The rejection message and the `network` self-test also name
  the account that was used, so a renamed broker account is immediately obvious.

- A rejected MQTT login no longer blocks the build pipeline: in the test container
  (`WOLSCA_VIRTUAL_OUTPUT=1`) the `network` phase reports "MQTT client connected" as a warning,
  because the broker is not part of what is being released. On a real installation it stays a
  failure, since Home Assistant would get nothing.

- The `printer` self-test could never pass its IPP query: it called
  `ipptool -t <uri> get-printer-attributes.test` with a *bare* file name, which ipptool looks for in
  the current working directory - so the step always ended with `exit code 1` before the printer was
  even contacted (`FAIL IPP get-printer-attributes on ipps://...`). The phase now passes the absolute
  path of the request file the service writes itself (`printer_capabilities.request_file()`, the same
  one the capability probe uses) and gives ipptool an operation timeout (`-T 10`). When the `ipps://`
  request fails, the same query is retried over `ipp://<host>:631/...`; if the printer answers there,
  the step is a **warning** naming the TLS connection as the only problem instead of a failure, and
  the command lines and output of both attempts are in the report.
- The same step no longer *fails* when the printer is simply not there. `ipptool: Unable to connect
  to "..." on port 443 - Host is down` (with a ping that loses every packet) means the printer is
  switched off or asleep, which is a correct observation and not a broken installation: the step is
  now a **warning** stating that a print job waits for the printer instead of failing.

## Known issue found with the new container

- A job sent to `WolsCA_Booklet` is written by cups-pdf into the **parent** drop folder
  (`/var/spool/wolsca/PrintFileDrop/`) instead of `.../booklet/`, even though
  `/etc/cups/cups-pdf-booklet.conf` has `Out .../booklet` and the backend symlink
  `cups-pdf-booklet` exists. The job still prints (front, flip, back, verified end to end), but it
  is processed with the *default* print mode instead of the mode of the queue - so "choose the mode
  by choosing the printer" does not work. Needs a separate fix round.
