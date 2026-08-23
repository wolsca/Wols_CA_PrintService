import os
import sys
import time
import signal
import threading
import queue
import platform
try:
    import config
    import mqtt_service
    import hardware_dispatcher
    import pdf_processor
    import file_watcher
    import job_log
    import notifier
    import printer_capabilities
    import web_app
    import installer
    import diagnostics
    import updater
    import version
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver
except ImportError as import_error:
    # An incomplete installation dies here, before anything of the service runs,
    # and systemd then only shows 'status=1/FAILURE'. Say which module is
    # missing and where it was looked for, so the journal names the cause.
    print(f"[Fatal] The service cannot start: {import_error}")
    print(f"[Fatal] Directory: {os.path.dirname(os.path.abspath(__file__))}")
    print("[Fatal] A module or dependency is missing from this installation - "
          "re-run deploy/debian/install.sh (it copies every module and verifies "
          "that they import).")
    sys.stdout.flush()
    raise

# Release 'x.y' from the VERSION file plus the commit number from BUILD_NUMBER.
SERVICE_VERSION = version.FULL_VERSION
RESCAN_INTERVAL_SECONDS = 15.0
shutdown_event = threading.Event()
job_queue = queue.Queue()
queue_lock = threading.Lock()
queued_files = []
known_paths = set()

def handle_termination(signum, frame):
    print(f"\n[System] Received signal {signum}. Shutting down gracefully...")
    shutdown_event.set()
    mqtt_service.request_cancel()

def enqueue_print_job(filepath, intake=None):
    """Idempotent: created/moved/closed events and rescans can all report the same file."""
    path = os.path.abspath(filepath)
    name = os.path.basename(path)
    with queue_lock:
        if path in known_paths:
            return
        known_paths.add(path)
        queued_files.append(name)
        waiting = len(queued_files)
    job_queue.put((path, intake))
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    print(f"[Queue] Added '{name}' ({waiting} waiting).")
    mqtt_service.publish_log(f"Queued '{name}' from "
                             f"{(intake or {}).get('cups_queue') or 'the drop folder'} "
                             f"({size} bytes, {waiting} waiting).", "info")

def intake_directories():
    entries = []
    for q_entry in config.get_config().get("intake", {}).get("queues", []):
        directory = q_entry.get("directory")
        if directory:
            entries.append((directory, q_entry))
    return entries

def rescan_worker():
    """Safety net: inotify is unreliable on overlayfs/LXC and bind mounts."""
    while not shutdown_event.wait(RESCAN_INTERVAL_SECONDS):
        file_watcher.scan_directory(config.DROP_DIR, enqueue_print_job, recursive=False)
        for directory, q_entry in intake_directories():
            file_watcher.scan_directory(directory, enqueue_print_job, q_entry)

def wait_for_flip(filename, sheets, instruction, prompt=None):
    job_log.step("flip", "Waiting for the user to put the sheets back and press Continue",
                 sheets=sheets, instruction=instruction or None)
    mqtt_service.waiting_for_user_action = True
    mqtt_service.set_state("WAITING_FOR_FLIP",
                           prompt or f"Front side done. Put the {sheets} sheet(s) back in the tray and press Continue.",
                           side="front", flip_instruction=instruction)
    # The user is standing at the printer, not at the web app: a push message on
    # the phone is what makes the manual flip workable.
    notifier.notify_flip(filename, web_app.public_url())

    while mqtt_service.waiting_for_user_action and not shutdown_event.is_set():
        time.sleep(0.5)

    if shutdown_event.is_set():
        raise hardware_dispatcher.JobCancelled("Service stopped.")
    if mqtt_service.cancel_requested():
        raise hardware_dispatcher.JobCancelled("Cancelled by user.")
    if mqtt_service.take_reprint_request():
        job_log.step("flip", "Reprint of the front side requested")
        return "reprint"
    job_log.step("flip", "Continue confirmed - printing the other side")
    return "resume"

def remove_quietly(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try: os.remove(path)
            except OSError: pass

def process_print_job(filepath, intake=None):
    if not os.path.exists(filepath): return
    filename = os.path.basename(filepath)
    print(f"\n--- NEW PRINT JOB DETECTED ---")
    print(f"[File] {filename}")

    # Everything this job does is written to one timeline (journal, MQTT for
    # Home Assistant, the web app and the history file), so a job that goes
    # wrong shows exactly which step it was.
    job_log.start(filename,
                  source=(intake or {}).get("cups_queue") or "drop folder",
                  path=filepath)

    # The intake queue may have a printer of its own, so booklet, double sided
    # and single sided can each go to a different machine.
    target, printer_source = web_app.resolve_target_printer(intake)
    options = web_app.resolve_job_options()
    web_app.consume_pending_options()
    mqtt_service.reset_job_control()

    copies = options["copies"]
    mode_source = "intake queue" if intake and "print_mode" in intake else "web app / configuration"
    print_mode = intake["print_mode"] if intake and "print_mode" in intake else options["print_mode"]
    print_mode = config.normalize_print_mode(print_mode)
    instruction = target.get("flip_instruction") or config.get_config()["hardware"].get("flip_instruction", "")
    duplex = bool(target.get("duplex")) and target.get("dispatch") == "cups"

    # When the printer can ask for the flip on its own panel, it owns the whole
    # job: no Continue button in the web app or in Home Assistant (see
    # printer_capabilities.flip_owner).
    printer_flip = not duplex and printer_capabilities.flip_owner(target) == "printer"
    mqtt_service.set_flip_owner("printer" if printer_flip else "service")

    job_log.field(print_mode=print_mode, printer=target["name"], copies=copies)
    job_log.step("mode", f"Print mode '{print_mode}' from the {mode_source}",
                 copies=copies, requested=options["print_mode"])
    job_log.step("printer", f"Target '{target['name']}' ({printer_source})",
                 dispatch=target.get("dispatch"), host=target.get("host"),
                 port=target.get("port"), queue=target.get("cups_queue"),
                 duplex_unit=duplex, flip_owner="printer" if printer_flip else "service")

    mqtt_service.set_state("PROCESSING", f"Analyzing {filename}", filename=filename,
                           copies=copies, print_mode=print_mode, printer_id=target["id"])

    front_pdf = back_pdf = duplex_pdf = blank_pdf = None
    pages = 0

    try:
        pages = pdf_processor.validate_pdf(filepath)
        job_log.field(pages=pages)
        job_log.step("analyse", f"PDF is readable, {pages} page(s)",
                     bytes=os.path.getsize(filepath))

        if print_mode == "Booklet":
            front_pdf, back_pdf, pages = pdf_processor.generate_booklet_pdfs(filepath)
            sheets = ((pages + 3) // 4)
            job_log.field(sheets=sheets)
            job_log.step("impose", f"Booklet imposition done: {sheets} sheet(s) of A4, "
                                   f"two A5 pages per side", pages=pages,
                         front=os.path.basename(front_pdf), back=os.path.basename(back_pdf))
            if duplex or printer_flip:
                duplex_pdf = pdf_processor.generate_duplex_booklet_pdf(front_pdf, back_pdf, filename)
                job_log.step("impose", "Front and back interleaved into one duplex document",
                             reason="duplex unit" if duplex else "the printer asks for the flip itself")
                hardware_dispatcher.dispatch_to_printer_ipp(duplex_pdf, "Booklet-Duplex", target, side="both", copies=copies, duplex=duplex, total_sheets=sheets * 2, sides="two-sided-short-edge" if printer_flip else None, shutdown_event=shutdown_event, manual_duplex_sheets=sheets if printer_flip else 0)
            else:
                job_log.step("plan", "Manual flip: the front sides are printed first, "
                                     "then Continue prints the back sides", sheets=sheets)
                while True:
                    hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "Booklet-Front", target, side="front", copies=copies, total_sheets=sheets, shutdown_event=shutdown_event)
                    if wait_for_flip(filename, sheets, instruction) == "resume": break
                hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "Booklet-Back", target, side="back", copies=copies, total_sheets=sheets, shutdown_event=shutdown_event)

        elif print_mode == "DoubleSided":
            sheets = (pages + 1) // 2
            job_log.field(sheets=sheets)

            # A single page can be given a paper change of its own, so special
            # paper can be loaded for exactly this page without another job
            # using it (hardware.single_page_paper_change):
            #   printer - send it to the manual feed slot, so the printer asks
            #             on its own panel and prints nothing until OK, no waste
            #   pause   - ask in the web app / Home Assistant first, no waste
            #   blank   - print a blank front, so the printer asks on its panel
            #             and the page ends up on the back of that sheet
            paper_change = "off"
            if pages == 1:
                paper_change = str(config.get_config()["hardware"].get(
                    "single_page_paper_change", "off") or "off").strip().lower()
                job_log.step("plan", f"Single page in DoubleSided mode, paper change '{paper_change}'")

            if paper_change == "printer":
                uri = config.get_config()["hardware"].get("printer_uri", "")
                mqtt_service.set_flip_owner("printer")
                job_log.step("plan", "Straight to the printer over IPP, manual feed slot", uri=uri)
                hardware_dispatcher.dispatch_via_ipp_manual_tray(
                    filepath, uri, copies,
                    config.get_config()["hardware"].get("single_page_media_source") or "manual",
                    "back", shutdown_event)
            elif paper_change == "pause":
                mqtt_service.set_flip_owner("service")
                wait_for_flip(filename, 1, instruction,
                              prompt="Put the paper for this page in the tray and press Continue.")
                hardware_dispatcher.dispatch_to_printer_ipp(filepath, "DoubleSided-SinglePage", target, side="back", copies=copies, total_sheets=1, sides="one-sided", shutdown_event=shutdown_event)
            elif paper_change == "blank":
                blank_pdf = pdf_processor.generate_blank_front_pdf(filepath)
                job_log.step("impose", "Blank front side added, so the page lands on the back")
                if printer_flip:
                    hardware_dispatcher.dispatch_to_printer_ipp(blank_pdf, "DoubleSided-BlankFront", target, side="both", copies=copies, total_sheets=1, sides="two-sided-long-edge", shutdown_event=shutdown_event, manual_duplex_sheets=1)
                else:
                    front_pdf, back_pdf, _ = pdf_processor.generate_two_sided_pdfs(blank_pdf)
                    while True:
                        hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "DoubleSided-BlankFront", target, side="front", copies=copies, total_sheets=1, sides="one-sided", shutdown_event=shutdown_event)
                        if wait_for_flip(filename, 1, instruction) == "resume": break
                    hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "DoubleSided-Back", target, side="back", copies=copies, total_sheets=1, sides="one-sided", shutdown_event=shutdown_event)
            elif duplex or printer_flip or pages < 2:
                # Bugfix: Bypass mechanical hardware flip for single-page documents
                actual_sides = "two-sided-long-edge" if ((duplex or printer_flip) and pages > 1) else "one-sided"
                job_log.step("plan", f"One job with sides={actual_sides}",
                             reason="duplex unit" if duplex else
                                    ("the printer asks for the flip itself" if printer_flip
                                     else "single page"))
                hardware_dispatcher.dispatch_to_printer_ipp(filepath, "DoubleSided", target, side="both", copies=copies, duplex=duplex, total_sheets=pages, sides=actual_sides, shutdown_event=shutdown_event, manual_duplex_sheets=sheets if (printer_flip and pages > 1) else 0)
            else:
                front_pdf, back_pdf, pages = pdf_processor.generate_two_sided_pdfs(filepath)
                job_log.step("impose", f"Split into odd and even pages: {sheets} sheet(s), "
                                       f"manual flip in between", pages=pages)
                while True:
                    hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "Duplex-Front", target, side="front", copies=copies, total_sheets=sheets, sides="one-sided", shutdown_event=shutdown_event)
                    if wait_for_flip(filename, sheets, instruction) == "resume": break
                hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "Duplex-Back", target, side="back", copies=copies, total_sheets=sheets, sides="one-sided", shutdown_event=shutdown_event)

        elif print_mode == "SingleSided":
            job_log.field(sheets=pages)
            job_log.step("plan", "One job, no imposition, sides=one-sided", pages=pages)
            hardware_dispatcher.dispatch_to_printer_ipp(filepath, "SingleSided", target, copies=copies, total_sheets=pages, sides="one-sided", shutdown_event=shutdown_event)
        else:
            job_log.warn("plan", f"Unknown print mode '{print_mode}' - sent to the printer unchanged",
                         pages=pages)
            hardware_dispatcher.dispatch_to_printer_ipp(filepath, print_mode, target, copies=copies, total_sheets=pages, shutdown_event=shutdown_event)

        mqtt_service.set_state("COMPLETED", f"Processed {filename}", side=None)
        notifier.notify_completed(filename, pages)
        if os.path.exists(filepath): os.remove(filepath)
        job_log.finish("COMPLETED", f"Processed {filename} ({pages} page(s))")

    except hardware_dispatcher.JobCancelled as jc:
        mqtt_service.set_state("CANCELLED", str(jc), side=None)
        remove_quietly(filepath)
        job_log.finish("CANCELLED", str(jc))
    except Exception as e:
        mqtt_service.set_state("ERROR", str(e), side=None)
        notifier.notify_error(str(e), filename)
        # The document itself is deliberately kept, so the job can be retried
        # once the cause named in the timeline has been repaired.
        job_log.error("failed", f"{type(e).__name__}: {e}", exception=e, file=filepath)
        job_log.finish("ERROR", str(e))

    finally:
        # Nothing may leave the timeline open, otherwise the next job would
        # append its steps to this one.
        job_log.finish("ERROR", "The job ended without a result.")
        remove_quietly(front_pdf, back_pdf, duplex_pdf, blank_pdf)
        mqtt_service.set_flip_owner("service")
        mqtt_service.waiting_for_user_action = False
        mqtt_service.reset_job_control()
        with queue_lock:
            if len(queued_files) == 0:
                mqtt_service.set_state("IDLE", "Waiting for the next print job", side=None)

def job_worker():
    while not shutdown_event.is_set():
        try: filepath, intake = job_queue.get(timeout=1.0)
        except queue.Empty: continue
        name = os.path.basename(filepath)
        with queue_lock:
            if name in queued_files: queued_files.remove(name)
        try: process_print_job(filepath, intake)
        except Exception as e: print(f"[Error] {e}")
        finally:
            with queue_lock:
                known_paths.discard(os.path.abspath(filepath))
            job_queue.task_done()

def verify_printer_mac():
    """Checks the MAC address of the printer shortly after start-up.

    A MAC address only changes when the printer does (a replacement, another
    network card, cable swapped for Wi-Fi), and a magic packet to the old
    address wakes nothing at all without saying so. Runs in its own thread,
    because a printer that is asleep has to time out first.
    """
    try:
        import printer_power
    except Exception:
        return
    try:
        status, detail = printer_power.verify_mac(printer_power.default_target())
    except Exception as e:
        print(f"[Power] Could not verify the printer MAC: {e}")
        return
    if status == "ok":
        return                                   # nothing to report, all correct
    print(f"[Power] Printer MAC check: {detail}")


def start_service():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(sig, handle_termination)
        except (ValueError, OSError): pass

    print(f"\n===================================================")
    print(f"  Wols CA Print Service {SERVICE_VERSION} started!")
    print(f"  Release {version.RELEASE}, commit number {version.BUILD}")
    print(f"  Hybrid Architecture - CUPS Intake Active")
    print(f"===================================================\n")

    job_log.load_history()
    threading.Thread(target=verify_printer_mac, daemon=True).start()
    threading.Thread(target=installer.check_virtual_printer, daemon=True).start()
    mqtt_service.start_mqtt()
    httpd = web_app.start_web_app()

    threading.Thread(target=job_worker, daemon=True).start()

    file_watcher.scan_directory(config.DROP_DIR, enqueue_print_job, recursive=False)
    for directory, q_entry in intake_directories():
        file_watcher.scan_directory(directory, enqueue_print_job, q_entry)

    use_polling = str(os.environ.get("WOLSCA_POLL_WATCHER", "")).lower() in ("1", "true", "yes")
    observer = PollingObserver(timeout=2.0) if use_polling else Observer()
    if use_polling:
        print("[System] Using the polling file observer (WOLSCA_POLL_WATCHER).")

    observer.schedule(file_watcher.PrintFolderWatcher(shutdown_event, enqueue_print_job), config.DROP_DIR, recursive=False)
    print(f"[System] Watching {config.DROP_DIR} (drop folder).")
    for directory, q_entry in intake_directories():
        os.makedirs(directory, exist_ok=True)
        print(f"[System] Watching {directory} for queue "
              f"'{q_entry.get('cups_queue')}' ({q_entry.get('print_mode')}).")
        # recursive: cups-pdf writes into a per-user subfolder for known accounts.
        observer.schedule(file_watcher.PrintFolderWatcher(shutdown_event, enqueue_print_job, q_entry), directory, recursive=True)

    observer.start()

    # Being ready does not depend on the broker: the state used to be announced
    # only from the MQTT on_connect callback, so a refused login left the service
    # in STARTING forever even though printing worked.
    if mqtt_service.job_state.get("state") == "STARTING":
        mqtt_service.set_state("IDLE", "Waiting for the next print job", side=None)

    threading.Thread(target=rescan_worker, daemon=True).start()
    updater.start_watcher(shutdown_event)
    shutdown_event.wait()
    if httpd: httpd.shutdown()
    observer.stop()
    observer.join()
    mqtt_service.stop_mqtt()

def run_self_test(argv):
    """Runs the diagnostics phases and reports them to MQTT for Home Assistant."""
    index = argv.index("--self-test")
    selected = None
    if len(argv) > index + 1 and not argv[index + 1].startswith("-"):
        selected = [p.strip() for p in argv[index + 1].split(",") if p.strip()]
    elif "--all" in argv:
        selected = list(diagnostics.PHASES.keys())

    mqtt_service.start_mqtt()
    time.sleep(2.0)  # give the broker connection a chance before the first publish
    report = diagnostics.run(selected)
    time.sleep(1.0)  # let the retained report leave the client buffer
    mqtt_service.stop_mqtt()
    sys.exit(0 if report.get("failed", 1) == 0 else 1)

def run_update_check(argv):
    """Prints the installed and the latest version; exit code 1 when outdated.

    Without --test only published releases count, exactly like the Home
    Assistant update entity; with --test the branch head is reported.
    """
    if "--test" in argv:
        result = updater.check_test_build(publish=False)
        print(f"Installed : {result['installed_version']}")
        print(f"Test build: {result['test_version'] or '-'}")
        print(result["test_result"])
        sys.exit(1 if result["test_available"] else 0)

    result = updater.check(publish=False)
    print(f"Installed: {result['installed_version']}")
    print(f"Release  : {result['latest_version']}")
    print(result["last_result"])
    sys.exit(1 if result["update_available"] else 0)


def run_update(argv):
    """Installs the latest release, or the branch head with --test."""
    if "--test" in argv:
        result = updater.install_test_build(publish=False)
        sys.exit(0 if "Updated" in result["last_result"] else 1)

    updater.check(publish=False)
    if not updater.state["update_available"] and "--force" not in argv:
        print("[Update] Already up to date; use --force to reinstall anyway.")
        sys.exit(0)
    result = updater.install(publish=False)
    sys.exit(0 if "Updated" in result["last_result"] else 1)


def main(argv):
    if "--version" in argv:
        for key, value in version.version_info().items():
            print(f"{key}: {value}")
    elif "--install-printer" in argv:
        installer.perform_cups_printer_install()
    elif "--self-test" in argv:
        run_self_test(argv)
    elif "--check-update" in argv:
        run_update_check(argv)
    elif "--update" in argv:
        run_update(argv)
    else:
        start_service()


if __name__ == "__main__":
    try:
        main(sys.argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception:
        # systemd only shows 'status=1/FAILURE'; without this the reason of a
        # crash during start-up was nowhere to be found. The traceback goes to
        # the journal, so 'journalctl -u wolsca-print-service -e' names the line.
        import traceback
        print("[Fatal] The service stopped because of an unhandled error:")
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(1)