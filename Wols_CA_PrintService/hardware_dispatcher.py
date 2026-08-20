import socket
import time
import subprocess
import os
import shutil
import config
import mqtt_service

class JobCancelled(Exception):
    """Raised when the running job is cancelled by the user or a timeout."""
    pass

IPP_JOB_REQUEST = """{
    OPERATION Get-Job-Attributes
    GROUP operation-attributes-tag
    ATTR charset attributes-charset utf-8
    ATTR naturalLanguage attributes-natural-language en
    ATTR uri printer-uri $uri
    ATTR integer job-id $job-id
    ATTR keyword requested-attributes job-state,job-impressions-completed
}
"""

def ipp_request_file():
    """Creates a temporary file required for CUPS ipptool queries."""
    path = os.path.join(config.TEMP_DIR, "get-job-attributes.test")
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(IPP_JOB_REQUEST)
    return path

def cups_job_impressions(queue_name, job_id):
    """Real page level progress: asks CUPS how many sides it already printed."""
    if not shutil.which("ipptool"):
        return None
    try:
        result = subprocess.run(
            ["ipptool", "-d", f"job-id={job_id}", "-t",
             f"ipp://localhost/printers/{queue_name}", ipp_request_file()],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None

    for line in result.stdout.splitlines():
        if "job-impressions-completed" in line:
            digits = "".join(ch for ch in line.split("=")[-1] if ch.isdigit())
            if digits:
                return int(digits)
    return None

def dispatch_via_socket(pdf_path, target, copies):
    """Raw JetDirect transfer on port 9100 (no feedback from the printer)."""
    printer_ip = target["host"]
    port = int(target.get("port", 9100))

    # Security & Protocol Guard: Prevent sending raw bytes to HTTPS/IPP port.
    if port in (443, 631):
        print(f"[Hardware] Warning: Port {port} is for IPP/IPPS, but dispatch mode is 'raw'. Overriding to port 9100 for JetDirect raw socket transfer.")
        port = 9100

    print(f"[Hardware] Direct TCP Socket transfer initiating to {printer_ip}:{port}")

    with open(pdf_path, 'rb') as f:
        data = f.read()

    for copy_index in range(copies):
        if mqtt_service.cancel_requested():
            raise JobCancelled("Cancelled before the remaining copies were sent.")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(15.0)
                s.connect((printer_ip, port))
                s.sendall(data)
        except socket.timeout:
            raise ValueError(f"Connection to printer ({printer_ip}) timed out.")
        except ConnectionRefusedError:
            raise ValueError(f"Printer ({printer_ip}) refused the connection on port {port}.")
        except Exception as e:
            raise ValueError(f"Hardware dispatch failed: {str(e)}")
        if copies > 1:
            print(f"[Hardware] Copy {copy_index + 1}/{copies} sent.")
        time.sleep(2)

def dispatch_via_cups(pdf_path, target, copies, duplex, total_sheets, side, sides=None, shutdown_event=None):
    """Sends the job through a local CUPS queue, providing page progress."""
    queue_name = target.get("cups_queue") or target["id"]
    command = ["lp", "-d", queue_name, "-n", str(copies)]

    # Force sides if applicable
    if not sides and duplex:
        sides = "two-sided-short-edge"
    if sides:
        command += ["-o", f"sides={sides}"]
    command += ["-o", "media=A4", pdf_path]

    print(f"[Hardware] Submitting to CUPS queue '{queue_name}' (sides={sides or 'default'}).")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise ValueError("CUPS command 'lp' is not available on this host.")
    except subprocess.CalledProcessError as e:
        raise ValueError(f"CUPS refused the job: {(e.stderr or '').strip() or e}")

    job_id = None
    for word in (result.stdout or "").split():
        if "-" in word and word.rsplit("-", 1)[-1].isdigit():
            job_id = word.rsplit("-", 1)[-1]
            break

    if not job_id:
        print("[Hardware] Job submitted, but CUPS returned no job id; no progress available.")
        return

    expected = max(1, total_sheets * copies)

    # Loop and monitor status
    is_shutting_down = False
    while not is_shutting_down:
        if shutdown_event and shutdown_event.is_set():
            is_shutting_down = True

        if mqtt_service.cancel_requested():
            subprocess.run(["cancel", f"{queue_name}-{job_id}"], capture_output=True)
            raise JobCancelled("Cancelled while the printer was working.")

        done = cups_job_impressions(queue_name, job_id)
        if done is not None:
            mqtt_service.set_state("PRINTING",
                                   f"Sheet {min(done, expected)} of {expected}",
                                   sheets_done=min(done, expected), side=side)

        active = subprocess.run(["lpstat", "-o", queue_name], capture_output=True, text=True)
        if f"{queue_name}-{job_id}" not in (active.stdout or ""):
            break
        time.sleep(2)

    print(f"[Hardware] CUPS job {queue_name}-{job_id} finished.")

def dispatch_to_printer_ipp(pdf_path, print_mode_name, target, side=None,
                            copies=1, duplex=False, total_sheets=0, sides=None, shutdown_event=None):
    """
    Submits the document to the physical printer. Routes via CUPS or Raw TCP.
    """
    if mqtt_service.cancel_requested():
        raise JobCancelled("Cancelled before printing started.")

    mqtt_service.set_state("PRINTING", f"Dispatching {os.path.basename(pdf_path)} to {target['name']}",
                           side=side, sheets_done=0, printer_id=target["id"], printer_name=target["name"])

    if target.get("dispatch") == "cups" and shutil.which("lp"):
        dispatch_via_cups(pdf_path, target, copies, duplex, total_sheets, side, sides, shutdown_event)
    else:
        dispatch_via_socket(pdf_path, target, copies)

    print(f"[Hardware] Transfer complete for {print_mode_name}.")