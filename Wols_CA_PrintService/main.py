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
from watchdog.observers import Observer

SERVICE_VERSION = "1.4.3"
shutdown_event = threading.Event()
job_queue = queue.Queue()
queue_lock = threading.Lock()
queued_files = []

def handle_termination(signum, frame):
    print(f"\n[System] Received signal {signum}. Shutting down gracefully...")
    shutdown_event.set()
    mqtt_service.request_cancel() # Wake up any waiting sleeps

def enqueue_print_job(filepath, intake=None):
    name = os.path.basename(filepath)
    with queue_lock:
        queued_files.append(name)
    job_queue.put((filepath, intake))
    print(f"[Queue] Added '{name}' ({len(queued_files)} waiting).")

def wait_for_flip(filename, sheets, instruction):
    mqtt_service.waiting_for_user_action = True
    mqtt_service.set_state("WAITING_FOR_FLIP",
                           f"Front side done. Put the {sheets} sheet(s) back in the tray and press Continue.",
                           side="front", flip_instruction=instruction)
    mqtt_service.publish_log(f"Waiting for manual flip for document '{filename}'.", "info")

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
            try:
                os.remove(path)
            except OSError:
                pass

def process_print_job(filepath, intake=None):
    if not os.path.exists(filepath):
        return

    start_time = time.time()
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
                           pages=0, sheets=0, copies=copies, print_mode=print_mode,
                           printer_id=target["id"], printer_name=target["name"])

    front_pdf = back_pdf = duplex_pdf = None

    try:
        pages = pdf_processor.validate_pdf(filepath)

        if print_mode == "Booklet":
            print("[Logic] Booklet Mode. Generating imposed PDFs...")
            front_pdf, back_pdf, pages = pdf_processor.generate_booklet_pdfs(filepath)
            sheets = ((pages + 3) // 4)
            mqtt_service.set_state("PROCESSING", f"{pages} pages become {sheets} sheet(s)", pages=pages, sheets=sheets)

            if duplex:
                duplex_pdf = pdf_processor.generate_duplex_booklet_pdf(front_pdf, back_pdf, filename)
                hardware_dispatcher.dispatch_to_printer_ipp(duplex_pdf, "Booklet-Duplex", target, side="both",
                                                            copies=copies, duplex=True, total_sheets=sheets * 2, shutdown_event=shutdown_event)
            else:
                while True:
                    print("[Job 1] Dispatching Front Pages...")
                    hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "Booklet-Front", target, side="front",
                                                                copies=copies, total_sheets=sheets, shutdown_event=shutdown_event)
                    if wait_for_flip(filename, sheets, instruction) == "resume":
                        break

                print("[Job 2] Dispatching Back Pages...")
                hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "Booklet-Back", target, side="back",
                                                            copies=copies, total_sheets=sheets, shutdown_event=shutdown_event)

            mqtt_service.publish_log(f"Successfully completed printing '{filename}' in Booklet mode.", "success")

        elif print_mode == "Duplex":
            sheets = (pages + 1) // 2
            print("[Logic] Duplex Mode (forced double sided, no imposition).")
            mqtt_service.set_state("PROCESSING", f"{pages} pages become {sheets} sheet(s)", pages=pages, sheets=sheets)

            if duplex or pages < 2:
                # Bugfix: Prevent mechanical hardware flip for single-page documents
                actual_sides = "two-sided-long-edge" if (duplex and pages > 1) else "one-sided"

                hardware_dispatcher.dispatch_to_printer_ipp(filepath, "Duplex", target, side="both",
                                                            copies=copies, duplex=duplex, total_sheets=pages,
                                                            sides=actual_sides, shutdown_event=shutdown_event)
            else:
                front_pdf, back_pdf, pages = pdf_processor.generate_two_sided_pdfs(filepath)
                while True:
                    print("[Job 1] Dispatching the odd pages...")
                    hardware_dispatcher.dispatch_to_printer_ipp(front_pdf, "Duplex-Front", target, side="front",
                                                                copies=copies, total_sheets=sheets,
                                                                sides="one-sided", shutdown_event=shutdown_event)
                    if wait_for_flip(filename, sheets, instruction) == "resume":
                        break

                print("[Job 2] Dispatching the even pages...")
                hardware_dispatcher.dispatch_to_printer_ipp(back_pdf, "Duplex-Back", target, side="back",
                                                            copies=copies, total_sheets=sheets,
                                                            sides="one-sided", shutdown_event=shutdown_event)

            mqtt_service.publish_log(f"Successfully completed printing '{filename}' in Duplex mode.", "success")

        elif print_mode == "Simplex":
            print("[Logic] Simplex Mode.")
            hardware_dispatcher.dispatch_to_printer_ipp(filepath, "Simplex", target, copies=copies, total_sheets=pages, sides="one-sided", shutdown_event=shutdown_event)
            mqtt_service.publish_log(f"Successfully printed '{filename}' in Simplex.", "success")

        else:
            hardware_dispatcher.dispatch_to_printer_ipp(filepath, print_mode, target, copies=copies, total_sheets=pages, shutdown_event=shutdown_event)

        mqtt_service.set_state("COMPLETED", f"Processed {filename}", side=None)

        if os.path.exists(filepath):
            os.remove(filepath)

    except hardware_dispatcher.JobCancelled as jc:
        print(f"[System] Job cancelled: {jc}")
        mqtt_service.set_state("CANCELLED", str(jc), side=None)
        mqtt_service.publish_log(f"Print job for '{filename}' was cancelled.", "warning")
        remove_quietly(filepath)
    except Exception as e:
        print(f"[Error] Fatal workflow exception: {e}")
        mqtt_service.set_state("ERROR", str(e), side=None)
        mqtt_service.publish_log(f"Error processing '{filename}': {e}", "error")

    finally:
        remove_quietly(front_pdf, back_pdf, duplex_pdf)
        mqtt_service.waiting_for_user_action = False
        mqtt_service.reset_job_control()

        with queue_lock:
            pending = len(queued_files)
        if pending == 0:
            mqtt_service.set_state("IDLE", "Waiting for the next print job",
                                   filename=None, pages=0, sheets=0, sheets_done=0, side=None)

def job_worker():
    while not shutdown_event.is_set():
        try:
            filepath, intake = job_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        name = os.path.basename(filepath)
        with queue_lock:
            if name in queued_files:
                queued_files.remove(name)

        try:
            process_print_job(filepath, intake)
        except Exception as e:
            print(f"[Error] Unhandled error: {e}")
        finally:
            job_queue.task_done()

def start_service():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(sig, handle_termination)
        except (ValueError, OSError): pass

    print(f"\n===================================================")
    print(f"  Wols CA Print Service {SERVICE_VERSION} started!")
    print(f"  Modular Architecture - Core orchestrator online")
    print(f"===================================================\n")

    threading.Thread(target=installer.check_virtual_printer, daemon=True).start()

    mqtt_service.start_mqtt()
    httpd = web_app.start_web_app()

    worker = threading.Thread(target=job_worker, daemon=True)
    worker.start()

    file_watcher.scan_directory(config.DROP_DIR, enqueue_print_job)
    for q_entry in config.get_config().get("intake", {}).get("queues", []):
        d = q_entry.get("directory")
        if d: file_watcher.scan_directory(d, enqueue_print_job, q_entry)

    observer = Observer()
    observer.schedule(file_watcher.PrintFolderWatcher(shutdown_event, enqueue_print_job), config.DROP_DIR, recursive=False)
    for q_entry in config.get_config().get("intake", {}).get("queues", []):
        d = q_entry.get("directory")
        if d:
            observer.schedule(file_watcher.PrintFolderWatcher(shutdown_event, enqueue_print_job, q_entry), d, recursive=False)

    observer.start()
    shutdown_event.wait()

    mqtt_service.set_state("OFFLINE", "Service intentionally stopped.")
    if httpd: httpd.shutdown()
    observer.stop()
    observer.join()
    mqtt_service.stop_mqtt()
    print("[System] Service successfully shut down.")

if __name__ == "__main__":
    if "--install-printer" in sys.argv:
        if platform.system() == "Linux":
            installer.perform_cups_printer_install()
        elif platform.system() == "Windows":
            installer.perform_admin_printer_install()
        else:
            print(f"[Error] Printer deployment is not supported on {platform.system()}.")
            sys.exit(1)
    else:
        start_service()