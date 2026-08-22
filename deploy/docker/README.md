# Local test container (Debian)

A container that runs the print service the way the Debian server does, so
changes can be tried on the desktop with Docker Desktop before they are
deployed - including the self-test phases and real CUPS intake queues.

The base image is the **Debian** flavour of the Home Assistant base images
(`ghcr.io/home-assistant/amd64-base-debian:bookworm`). Home Assistant add-ons do
not have to be Alpine, and Debian is what matters here: it ships the real
`printer-driver-cups-pdf` package, so the container installs the very same
packages, runs the very same `installer.py`, creates the same CUPS queues and
reads the same `/etc/wolsca/WolsCAPrintService.json`. **Nothing in the
configuration differs from an installation on Debian or Ubuntu**, which is the
whole point: what passes here behaves the same on the server, and the same image
is also what the Home Assistant add-on runs.

The add-on packaging lives in the repository root: `repository.json` plus
`wolsca_print_service/` (releases) and `wolsca_print_service_test/` (commit
builds). Inside Home Assistant the entrypoint finds `/data/options.json`, keeps
the configuration in `/data/WolsCAPrintService.json` and writes the add-on
options into it - see the add-on `DOCS.md` and the *Home Assistant add-on*
section of the main `README.md`.

## Run it

First, once, the local settings - the file is git-ignored, so the credentials stay
out of GitHub:

```powershell
Copy-Item deploy/docker/.env.local.example deploy/docker/.env.local
# then fill in WOLSCA_MQTT_PASSWORD and WOLSCA_ADMIN_TOKEN (WOLSCA_MQTT_USER defaults to wolsca_mqtt)
```

Only the broker address, the MQTT credentials (`mqtt.user`, `mqtt.password`),
the topic prefix and the admin token are taken from that file; everything else
stays as in `WolsCAPrintService.json`.

Then, from the repository root:

```powershell
.\tools\run-local-test.ps1                      # start the service, web app on :8080
.\tools\run-local-test.ps1 -SelfTest            # run all self-test phases and exit
.\tools\run-local-test.ps1 -Shell               # shell inside, CUPS running
.\tools\run-local-test.ps1 -Broker 192.168.1.5  # point MQTT at another broker
```

or with compose:

```powershell
docker compose -f deploy/docker/docker-compose.yml up --build
docker compose -f deploy/docker/docker-compose.yml --profile test run --rm self-test
```

| Address | What |
| --- | --- |
| <http://localhost:8080/> | web app, including the *Administrator* card |
| <http://localhost:6631/> | CUPS of the container (631 inside) |
| `ipp://localhost:6631/printers/WolsCA_Booklet` | intake queue, printable from the host |

## What the entrypoint does

`entrypoint.sh` mirrors the systemd setup of the server:

1. copies the shipped default configuration to `/etc/wolsca/` on first start and
   applies `WOLSCA_MQTT_BROKER` (only the broker address, nothing else),
2. runs `fix-permissions.sh`,
3. starts D-Bus, Avahi and `cupsd` - supervised, because `cupsd` deliberately
   exits whenever its configuration changes and systemd is not there to restart
   it,
4. runs `installer.perform_cups_printer_install()`, which creates the three
   intake queues, the `cups-pdf` instances and the `WolsCA_Output` queue,
5. starts `main.py` (or the self-test).

Because there is no systemd inside the container, the installer skips its
`systemctl` calls and its `apt-get` step (the packages are in the image) - see
`run_service_command()` in `installer.py`. Everything else is the same code path
as on the server.

## Notes

- `WOLSCA_POLL_WATCHER=1` is set: inotify is unreliable on bind mounts from
  Windows, so the polling observer is used.
- The configuration lives in `deploy/docker/config/` on the host, so it can be
  edited by hand or through the administrator card in the web app.
- With `.env.local` in place the whole default self-test passes locally
  (50 checks, one warning about the missing git checkout, which only the update
  button needs).
- The `chain` phase prints for real: it submits a job to the first intake queue,
  which the running service then sends to `WolsCA_Output` - so the physical
  printer produces a page (and asks for the flip).
- The spool directories are a named docker volume, so print jobs survive a
  restart of the container but not `docker compose down -v`.
