# Changes and fixes for the next release

Add one bullet per change while you work. The release tool (`python tools/release.py`) moves
everything below into `RELEASE_NOTES.md` and `CHANGELOG.md` under the new version number and
empties this file again, so it always describes only the *unreleased* work.

## Changes

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
  unchanged, and so are the CUPS queue names and the intake directories. `config.normalize_print_mode()`
  still accepts the old names, and `settings.print_mode` now defaults to `DoubleSided`.
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

## Fixes

- `installer.py` no longer aborts where there is no `apt-get` and no longer reports missing
  `systemctl` as an error, so `--install-printer` also runs inside a container.
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

## Known issue found with the new container

- A job sent to `WolsCA_Booklet` is written by cups-pdf into the **parent** drop folder
  (`/var/spool/wolsca/PrintFileDrop/`) instead of `.../booklet/`, even though
  `/etc/cups/cups-pdf-booklet.conf` has `Out .../booklet` and the backend symlink
  `cups-pdf-booklet` exists. The job still prints (front, flip, back, verified end to end), but it
  is processed with the *default* print mode instead of the mode of the queue - so "choose the mode
  by choosing the printer" does not work. Needs a separate fix round.
