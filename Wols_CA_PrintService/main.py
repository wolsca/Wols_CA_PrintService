import os
import sys
import time
import signal
import threading
import queue
import platform
import config
import mqtt_service
import hardware_dispatcher
import pdf_processor
import file_watcher
import web_app
import installer
import diagnostics
import updater
import version
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

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
    print(f"[Queue] Added '{name}' ({waiting} waiting).")

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

def wait_for_flip(filename, sheets, instruction):
    mqtt_service.waiting_for_user_action = True
    mqtt_service.set_state("WAITING_FOR_FLIP",
                           f"Front side done. Put the {sheets} sheet(s) back in the tray and press Continue.",
                           side="front", flip_instruction=instruction)

    while mqtt_service.waiting_for_user_action and not shutdown_event.is_set():
        time.sleep(0.5)

    if shutdown_event.is_set():
        raise hardware_dispatcher.JobCancelled("Service stopped.")
    if mqtt_service.cancel_requested():
        raise hardware_dispatcher.JobCancelled("Cancelled by user.")
    if mqtt_service.take_reprint_request():
        return "reprint"
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

    target, _ = web_app.resolve_target_printer()
    options = web_app.resolve_job_options()
    web_app.consume_pending_options()
    mqtt_service.reset_job_control()

    copies = options["copies"]
    print_mode = intake["print_mode"] if intake and "print_mode" in intake else options["print_mode"]
    instruction = target.get("flip_instruction") or config.get_config()["hardware"].get("flip_instruction", "")
    duplex = bool(target.get("duplex")) and target.get("dispatch") == "cups"

    mqtt_service.set_state("PROCESSING", f"Analyzing {filename}", filename=filename,
                           copies=copies, print_mode=print_mode, printer_id=target["id"])

    front_pdf = back_pdf = duplex_pdf = None

    try:
        pages = pdf_processor.validate_pdf(filepath)

        if print_mode == "Booklet":
            front_pdf, back_pdf, pages = pdf_processor.generate_booklet_pdfs(filepath)
            sheets = ((pages + 3) // 4)
            if duplex:
                duplex_pdf = pdf_processor.generate_duplex_booklet_pdf(front_pdf, back_pdf, filename)
                hardware_dispatcher.dispatch_to_printer_ipp(duplex_pdf, "Booklet-Duplex", target, side="both", copies=copies, duplex=True, total_sheets=sheets * 2, shutdown_event=shutdown_event)
            else:
                while True:
                    hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "Booklet-Front", target, side="front", copies=copies, total_sheets=sheets, shutdown_event=shutdown_event)
                    if wait_for_flip(filename, sheets, instruction) == "resume": break
                hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "Booklet-Back", target, side="back", copies=copies, total_sheets=sheets, shutdown_event=shutdown_event)

        elif print_mode == "Duplex":
            sheets = (pages + 1) // 2
            if duplex or pages < 2:
                # Bugfix: Bypass mechanical hardware flip for single-page documents
                actual_sides = "two-sided-long-edge" if (duplex and pages > 1) else "one-sided"
                hardware_dispatcher.dispatch_to_printer_ipp(filepath, "Duplex", target, side="both", copies=copies, duplex=duplex, total_sheets=pages, sides=actual_sides, shutdown_event=shutdown_event)
            else:
                front_pdf, back_pdf, pages = pdf_processor.generate_two_sided_pdfs(filepath)
                while True:
                    hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "Duplex-Front", target, side="front", copies=copies, total_sheets=sheets, sides="one-sided", shutdown_event=shutdown_event)
                    if wait_for_flip(filename, sheets, instruction) == "resume": break
                hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "Duplex-Back", target, side="back", copies=copies, total_sheets=sheets, sides="one-sided", shutdown_event=shutdown_event)

        elif print_mode == "Simplex":
            hardware_dispatcher.dispatch_to_printer_ipp(filepath, "Simplex", target, copies=copies, total_sheets=pages, sides="one-sided", shutdown_event=shutdown_event)
        else:
            hardware_dispatcher.dispatch_to_printer_ipp(filepath, print_mode, target, copies=copies, total_sheets=pages, shutdown_event=shutdown_event)

        mqtt_service.set_state("COMPLETED", f"Processed {filename}", side=None)
        if os.path.exists(filepath): os.remove(filepath)

    except hardware_dispatcher.JobCancelled as jc:
        mqtt_service.set_state("CANCELLED", str(jc), side=None)
        remove_quietly(filepath)
    except Exception as e:
        mqtt_service.set_state("ERROR", str(e), side=None)

    finally:
        remove_quietly(front_pdf, back_pdf, duplex_pdf)
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

def start_service():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(sig, handle_termination)
        except (ValueError, OSError): pass

    print(f"\n===================================================")
    print(f"  Wols CA Print Service {SERVICE_VERSION} started!")
    print(f"  Release {version.RELEASE}, commit number {version.BUILD}")
    print(f"  Hybrid Architecture - CUPS Intake Active")
    print(f"===================================================\n")

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
    for directory, q_entry in intake_directories():
        os.makedirs(directory, exist_ok=True)
        # recursive: cups-pdf writes into a per-user subfolder for known accounts.
        observer.schedule(file_watcher.PrintFolderWatcher(shutdown_event, enqueue_print_job, q_entry), directory, recursive=True)

    observer.start()
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


if __name__ == "__main__":
    if "--version" in sys.argv:
        for key, value in version.version_info().items():
            print(f"{key}: {value}")
    elif "--install-printer" in sys.argv:
        installer.perform_cups_printer_install()
    elif "--self-test" in sys.argv:
        run_self_test(sys.argv)
    elif "--check-update" in sys.argv:
        run_update_check(sys.argv)
    elif "--update" in sys.argv:
        run_update(sys.argv)
    else:
        start_service()