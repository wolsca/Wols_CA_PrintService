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

## Fixes

- `installer.py` no longer aborts where there is no `apt-get` and no longer reports missing
  `systemctl` as an error, so `--install-printer` also runs inside a container.
- The output queue is only created driverless (`-m everywhere`) for IPP targets; a `socket:` or
  file backend now gets a raw queue, so the PDF is passed through unchanged instead of being
  rejected.
- The `printer` self-test phase skips the IPP query when the output does not go to an IPP target,
  instead of failing.

## Known issue found with the new container

- A job sent to `WolsCA_Booklet` is written by cups-pdf into the **parent** drop folder
  (`/var/spool/wolsca/PrintFileDrop/`) instead of `.../booklet/`, even though
  `/etc/cups/cups-pdf-booklet.conf` has `Out .../booklet` and the backend symlink
  `cups-pdf-booklet` exists. The job still prints (front, flip, back, verified end to end), but it
  is processed with the *default* print mode instead of the mode of the queue - so "choose the mode
  by choosing the printer" does not work. Needs a separate fix round.
