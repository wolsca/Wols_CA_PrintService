import socket
import time
import subprocess
import os
import shutil
import config
import job_log
import mqtt_service
import printer_power

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

IPP_MANUAL_DUPLEX_REQUEST = """{
    OPERATION Print-Job
    GROUP operation-attributes-tag
    ATTR charset attributes-charset utf-8
    ATTR naturalLanguage attributes-natural-language en
    ATTR uri printer-uri $uri
    ATTR name requesting-user-name wolsca
    ATTR name job-name $job-name
    ATTR mimeMediaType document-format application/pdf
    GROUP job-attributes-tag
    ATTR integer copies $copies
    ATTR keyword sides $sides
    ATTR integer manual-duplex-sheet-count $sheet-count
    ATTR keyword media $media
    FILE $filename
}
"""

IPP_PRINTER_JOB_REQUEST = """{
    OPERATION Get-Job-Attributes
    GROUP operation-attributes-tag
    ATTR charset attributes-charset utf-8
    ATTR naturalLanguage attributes-natural-language en
    ATTR uri printer-uri $uri
    ATTR integer job-id $job-id
    ATTR keyword requested-attributes job-state,job-state-reasons,job-impressions-completed
}
"""


IPP_MANUAL_TRAY_REQUEST = """{
    OPERATION Print-Job
    GROUP operation-attributes-tag
    ATTR charset attributes-charset utf-8
    ATTR naturalLanguage attributes-natural-language en
    ATTR uri printer-uri $uri
    ATTR name requesting-user-name wolsca
    ATTR name job-name $job-name
    ATTR mimeMediaType document-format application/pdf
    GROUP job-attributes-tag
    ATTR integer copies $copies
    ATTR keyword sides one-sided
    ATTR keyword media $media
    ATTR collection media-col {
        MEMBER keyword media-source $media-source
    }
    FILE $filename
}
"""


def ipp_template_file(name, body):
    """Writes (once) an ipptool request template into the temp directory."""
    path = os.path.join(config.TEMP_DIR, name)
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(body)
    return path


def _tail(*outputs, lines=6):
    """The last lines of stdout/stderr - what a failing command really said."""
    text = "\n".join(part.strip() for part in outputs if part and part.strip())
    return " | ".join(text.splitlines()[-lines:]) or None


def ipp_attribute(output, name):
    """The value of one attribute out of verbose ipptool output."""
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name} ") and "=" in stripped:
            return stripped.split("=", 1)[1].strip()
    return None


def dispatch_via_ipp_manual_duplex(pdf_path, uri, copies, sides, sheet_count,
                                   side, shutdown_event=None):
    """Manual duplex handled by the printer itself, over IPP.

    The printer prints the front sides, asks on its own display to put the stack
    back in the tray and finishes the job when the button on the printer is
    pressed. It is deliberately not routed through CUPS: printers offering this
    report 'sides-supported = one-sided', so the 'everywhere' queue would strip
    the two-sided request.
    """
    if not shutil.which("ipptool"):
        job_log.error("submit", "'ipptool' is missing - install the package 'cups-ipp-utils'")
        raise ValueError("'ipptool' is required for printer confirmed flipping.")

    request = ipp_template_file("print-job-manual-duplex.test", IPP_MANUAL_DUPLEX_REQUEST)
    command = ["ipptool", "-tv",
               "-d", f"copies={copies}",
               "-d", f"sides={sides}",
               "-d", f"sheet-count={sheet_count}",
               "-d", "media=iso_a4_210x297mm",
               "-d", f"job-name={os.path.basename(pdf_path)}",
               "-d", f"filename={pdf_path}",
               uri, request]

    print(f"[Hardware] Submitting to {uri} (sides={sides}, "
          f"manual-duplex-sheet-count={sheet_count}).")
    job_log.step("submit", f"Direct IPP print job to {uri}", sides=sides,
                 sheet_count=sheet_count, copies=copies, command=" ".join(command))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        job_log.error("submit", f"The printer at {uri} did not answer within 120 s")
        raise ValueError(f"The printer at {uri} did not accept the job in time.")

    if result.returncode != 0 or "successful-ok" not in (result.stdout or ""):
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        job_log.error("submit", "The printer refused the job", exit_code=result.returncode,
                      output=_tail(result.stdout, result.stderr))
        raise ValueError(f"The printer refused the job: {detail[-1] if detail else 'no answer'}")

    job_id = ipp_attribute(result.stdout, "job-id")
    if not job_id or not job_id.isdigit():
        print("[Hardware] Job accepted, but the printer returned no job id; not waiting for it.")
        job_log.warn("submit", "The printer accepted the job but returned no job id, "
                               "so its progress cannot be followed")
        return

    job_log.step("submit", f"Accepted by the printer as job {job_id}", job_id=job_id)
    poll_ipp_job(uri, job_id, sheet_count * copies, side, shutdown_event)


def dispatch_via_ipp_manual_tray(pdf_path, uri, copies, media_source, side,
                                 shutdown_event=None):
    """Sends the document to the manual feed slot of the printer.

    The printer then asks on its own panel to load the paper and prints nothing
    until the button there is pressed - the paper change without wasting a
    sheet. Like the manual duplex flow this goes straight to the printer,
    because the CUPS output queue would drop the media-source.
    """
    if not shutil.which("ipptool"):
        job_log.error("submit", "'ipptool' is missing - install the package 'cups-ipp-utils'")
        raise ValueError("'ipptool' is required for the paper change on the printer.")

    request = ipp_template_file("print-job-manual-tray.test", IPP_MANUAL_TRAY_REQUEST)
    command = ["ipptool", "-tv",
               "-d", f"copies={copies}",
               "-d", "media=iso_a4_210x297mm",
               "-d", f"media-source={media_source}",
               "-d", f"job-name={os.path.basename(pdf_path)}",
               "-d", f"filename={pdf_path}",
               uri, request]

    print(f"[Hardware] Submitting to {uri} (media-source={media_source}); "
          f"the printer asks for the paper on its own panel.")
    job_log.step("submit", f"Direct IPP print job to {uri}, manual feed slot",
                 media_source=media_source, copies=copies, command=" ".join(command))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        job_log.error("submit", f"The printer at {uri} did not answer within 120 s")
        raise ValueError(f"The printer at {uri} did not accept the job in time.")

    if result.returncode != 0 or "successful-ok" not in (result.stdout or ""):
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        job_log.error("submit", "The printer refused the job", exit_code=result.returncode,
                      output=_tail(result.stdout, result.stderr))
        raise ValueError(f"The printer refused the job: {detail[-1] if detail else 'no answer'}")

    job_id = ipp_attribute(result.stdout, "job-id")
    if not job_id or not job_id.isdigit():
        print("[Hardware] Job accepted, but the printer returned no job id; not waiting for it.")
        job_log.warn("submit", "The printer accepted the job but returned no job id, "
                               "so its progress cannot be followed")
        return

    job_log.step("submit", f"Accepted by the printer as job {job_id}", job_id=job_id)

    poll_ipp_job(uri, job_id, copies, side, shutdown_event,
                 waiting_detail="Put the paper in the manual feed slot and press the "
                                "button on the printer.")


def poll_ipp_job(uri, job_id, expected, side, shutdown_event=None, waiting_detail=None):
    """Follows a job on the printer until it leaves the queue.

    While the printer waits for the sheets to be put back, its own panel is in
    charge; the service only reports progress, it never asks for a Continue.
    """
    request = ipp_template_file("get-printer-job-attributes.test", IPP_PRINTER_JOB_REQUEST)
    expected = max(1, expected)
    reported_flip = False

    while True:
        if shutdown_event and shutdown_event.is_set():
            break
        if mqtt_service.cancel_requested():
            raise JobCancelled("Cancelled while the printer was working.")

        try:
            result = subprocess.run(["ipptool", "-tv", "-d", f"job-id={job_id}", uri, request],
                                    capture_output=True, text=True, timeout=20)
        except Exception as e:
            job_log.warn("progress", f"The printer stopped answering, so job {job_id} is no "
                                     f"longer followed", error=str(e))
            break

        state = ipp_attribute(result.stdout, "job-state") or ""
        reasons = ipp_attribute(result.stdout, "job-state-reasons") or ""
        done = ipp_attribute(result.stdout, "job-impressions-completed")
        sheets_done = int("".join(ch for ch in (done or "") if ch.isdigit()) or 0)

        # The printer holds the job while it waits at its panel for the sheets.
        waiting = state.startswith("pending-held") or any(
            reason in reasons for reason in ("resources-are-not-ready", "job-printing-stopped",
                                             "media-needed", "media-empty", "media-jam"))
        if waiting:
            if not reported_flip:
                print("[Hardware] The printer is asking on its own panel to put the sheets back.")
                job_log.step("flip", "The printer asks on its own panel to put the sheets back",
                             job_state=state, reasons=reasons)
                reported_flip = True
            mqtt_service.set_state("WAITING_FOR_FLIP",
                                   waiting_detail or "Put the sheets back in the tray and "
                                                     "press the button on the printer.",
                                   sheets_done=min(sheets_done, expected), side="front",
                                   flip_owner="printer")
        else:
            reported_flip = False
            mqtt_service.set_state("PRINTING", f"Sheet {min(sheets_done, expected)} of {expected}",
                                   sheets_done=min(sheets_done, expected), side=side,
                                   flip_owner="printer")

        if state.startswith(("completed", "canceled", "aborted")):
            level = "info" if state.startswith("completed") else "warning"
            job_log.step("printed", f"The printer reports job {job_id} as {state.strip()}",
                         level, sheets=sheets_done, expected=expected, reasons=reasons or None)
            break
        time.sleep(2)

    print(f"[Hardware] Printer job {job_id} finished.")


def dispatch_via_socket(pdf_path, target, copies):
    """Raw JetDirect transfer on port 9100 (no feedback from the printer)."""
    printer_ip = target["host"]
    port = int(target.get("port", 9100))

    # Security & Protocol Guard: Prevent sending raw bytes to HTTPS/IPP port.
    if port in (443, 631):
        print(f"[Hardware] Warning: Port {port} is for IPP/IPPS, but dispatch mode is 'raw'. Overriding to port 9100 for JetDirect raw socket transfer.")
        job_log.warn("submit", f"Port {port} is an IPP port but the dispatch mode is 'raw' - "
                               f"using port 9100 instead")
        port = 9100

    print(f"[Hardware] Direct TCP Socket transfer initiating to {printer_ip}:{port}")

    with open(pdf_path, 'rb') as f:
        data = f.read()

    # Raw JetDirect gives no feedback at all: the printer never reports whether
    # it printed the document, so what was sent is all the log can show.
    job_log.step("submit", f"Raw JetDirect transfer to {printer_ip}:{port}",
                 file=os.path.basename(pdf_path), bytes=len(data), copies=copies,
                 feedback="none - the printer reports nothing back over port 9100")

    for copy_index in range(copies):
        if mqtt_service.cancel_requested():
            raise JobCancelled("Cancelled before the remaining copies were sent.")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(15.0)
                s.connect((printer_ip, port))
                s.sendall(data)
        except socket.timeout:
            job_log.error("submit", f"No answer from {printer_ip}:{port} within 15 s")
            raise ValueError(f"Connection to printer ({printer_ip}) timed out.")
        except ConnectionRefusedError:
            job_log.error("submit", f"{printer_ip} refused the connection on port {port} - "
                                    f"raw printing (JetDirect) must be enabled on the printer")
            raise ValueError(f"Printer ({printer_ip}) refused the connection on port {port}.")
        except Exception as e:
            job_log.error("submit", f"Raw transfer to {printer_ip}:{port} failed: {e}", exception=e)
            raise ValueError(f"Hardware dispatch failed: {str(e)}")
        if copies > 1:
            print(f"[Hardware] Copy {copy_index + 1}/{copies} sent.")
        job_log.step("printed", f"Copy {copy_index + 1} of {copies} handed over to the printer",
                     bytes=len(data))
        time.sleep(2)

def queue_status(queue_name):
    """The one line 'lpstat -p' gives about a queue - disabled, paused, idle."""
    try:
        result = subprocess.run(["lpstat", "-p", queue_name], capture_output=True,
                                text=True, timeout=10)
    except Exception:
        return None
    return (result.stdout or result.stderr or "").strip().splitlines()[0] \
        if (result.stdout or result.stderr).strip() else None


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
    job_log.step("submit", f"Handing the document to the CUPS queue '{queue_name}'",
                 sides=sides or "default", copies=copies, sheets=total_sheets,
                 command=" ".join(command))
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        job_log.error("submit", "The command 'lp' is missing - install the package 'cups-client'")
        raise ValueError("CUPS command 'lp' is not available on this host.")
    except subprocess.CalledProcessError as e:
        job_log.error("submit", f"CUPS refused the job on queue '{queue_name}'",
                      exit_code=e.returncode, output=_tail(e.stdout, e.stderr))
        raise ValueError(f"CUPS refused the job: {(e.stderr or '').strip() or e}")

    job_id = None
    for word in (result.stdout or "").split():
        if "-" in word and word.rsplit("-", 1)[-1].isdigit():
            job_id = word.rsplit("-", 1)[-1]
            break

    if not job_id:
        print("[Hardware] Job submitted, but CUPS returned no job id; no progress available.")
        job_log.warn("submit", "CUPS accepted the job but returned no job id, so its "
                               "progress cannot be followed", output=_tail(result.stdout))
        return

    expected = max(1, total_sheets * copies)
    job_log.step("submit", f"Accepted by CUPS as job {queue_name}-{job_id}",
                 job_id=job_id, expected_sheets=expected)

    # Loop and monitor status
    last_reported = -1
    reported_no_progress = False
    is_shutting_down = False
    while not is_shutting_down:
        if shutdown_event and shutdown_event.is_set():
            is_shutting_down = True

        if mqtt_service.cancel_requested():
            subprocess.run(["cancel", f"{queue_name}-{job_id}"], capture_output=True)
            job_log.warn("cancel", f"CUPS job {queue_name}-{job_id} cancelled on request")
            raise JobCancelled("Cancelled while the printer was working.")

        done = cups_job_impressions(queue_name, job_id)
        if done is not None:
            if done != last_reported:
                job_log.step("progress", f"Sheet {min(done, expected)} of {expected} printed")
                last_reported = done
            mqtt_service.set_state("PRINTING",
                                   f"Sheet {min(done, expected)} of {expected}",
                                   sheets_done=min(done, expected), side=side)
        elif not reported_no_progress:
            job_log.warn("progress", "CUPS reports no page progress for this job "
                                     "('ipptool' from cups-ipp-utils is needed for that)")
            reported_no_progress = True

        active = subprocess.run(["lpstat", "-o", queue_name], capture_output=True, text=True)
        if f"{queue_name}-{job_id}" not in (active.stdout or ""):
            break
        time.sleep(2)

    print(f"[Hardware] CUPS job {queue_name}-{job_id} finished.")
    # 'Left the queue' is not the same as 'printed': CUPS also drops a job it
    # could not deliver, and its reason is in the CUPS log.
    reason = queue_status(queue_name)
    job_log.step("printed", f"CUPS job {queue_name}-{job_id} left the queue after "
                            f"{last_reported if last_reported >= 0 else 0} of {expected} sheet(s)",
                 queue_state=reason)

def ensure_printer_available(target, side=None, shutdown_event=None):
    """Waits (and wakes) until the printer is on the network.

    A printer that is asleep or switched off is not a failure of the service, so
    the job is not thrown away: the state becomes WAITING_FOR_PRINTER, a
    Wake-on-LAN packet is sent when the MAC address is known, and the job
    continues by itself as soon as the printer answers.
    """
    uri = str(config.get_config().get("hardware", {}).get("printer_uri", "")).strip()
    network_uri = uri.startswith(("ipp://", "ipps://", "socket://", "http://", "https://"))
    if not network_uri and target.get("dispatch") != "raw":
        # The virtual printer of the test container writes to a file backend, so
        # the physical printer is not part of the path and probing it says
        # nothing about this job.
        return

    ready, where = printer_power.reachable(target)
    if ready:
        job_log.step("printer_ready", f"The printer answers on {where}")
        check_printer_identity(target)
        return

    job_log.warn("printer_offline", "The printer does not answer, so the job waits for it "
                                    "instead of failing",
                 tried=where, wake=printer_power.describe(target),
                 wait_seconds=printer_power.wait_seconds())
    mqtt_service.set_state("WAITING_FOR_PRINTER",
                           "The printer is switched off or asleep - waiting for it.",
                           side=side)
    try:
        import notifier
        notifier.send("The printer does not answer. The job waits until the printer is "
                      "back on the network.", title="Printer offline")
    except Exception:
        pass

    def stop():
        if shutdown_event is not None and shutdown_event.is_set():
            return True
        return mqtt_service.cancel_requested()

    def announce(elapsed, remaining, wake_detail):
        detail = (f"Waiting for the printer ({elapsed // 60} min, "
                  f"{remaining // 60} min left)")
        mqtt_service.set_state("WAITING_FOR_PRINTER", detail, side=side)
        if elapsed and elapsed % 60 < printer_power.POLL_INTERVAL:
            job_log.step("printer_offline", detail, wake=wake_detail)

    ready, detail = printer_power.wait_until_reachable(target, on_wait=announce,
                                                       should_stop=stop)
    if mqtt_service.cancel_requested():
        raise JobCancelled("Cancelled while waiting for the printer.")
    if not ready:
        job_log.error("printer_offline", f"The printer stayed unreachable: {detail}")
        raise ValueError(f"The printer is not on the network: {detail}")

    job_log.step("printer_ready", f"The printer is back on the network ({detail})")
    check_printer_identity(target)


def check_printer_identity(target):
    """Is the machine that answers really our printer?

    The printer is awake, so this is the only moment its MAC address can be read
    from the network: an empty address is filled in (then a sleeping printer can
    be woken later without anyone typing it in) and an address that belongs to
    neither of the printer's interfaces stops the job when
    `hardware.block_on_mac_change` is on - an IP address alone does not say
    which device is listening on it.
    """
    learned = printer_power.learn_mac(target)
    if learned:
        job_log.step("printer_mac", f"Printer MAC {learned} detected and saved in "
                                    f"hardware.printer_mac")
        return
    allowed, status, detail = printer_power.security_check(target)
    if not allowed:
        job_log.error("printer_mac", f"Unknown printer on the network: {detail}")
        raise ValueError(f"The printer was not recognised: {detail}")
    if status in ("unexpected", "switched", "corrected"):
        job_log.warn("printer_mac", detail)


def dispatch_to_printer_ipp(pdf_path, print_mode_name, target, side=None,
                            copies=1, duplex=False, total_sheets=0, sides=None, shutdown_event=None,
                            manual_duplex_sheets=0):
    """
    Submits the document to the physical printer. Routes via CUPS or Raw TCP.

    With manual_duplex_sheets the job goes straight to the printer over IPP, so
    the printer itself asks for the flip on its panel (see
    dispatch_via_ipp_manual_duplex).
    """
    if mqtt_service.cancel_requested():
        raise JobCancelled("Cancelled before printing started.")

    # A printer that is off or asleep is waited for (and woken) first, so the job
    # is never lost just because nobody switched the printer on yet.
    ensure_printer_available(target, side, shutdown_event)

    mqtt_service.set_state("PRINTING", f"Dispatching {os.path.basename(pdf_path)} to {target['name']}",
                           side=side, sheets_done=0, printer_id=target["id"], printer_name=target["name"])

    job_log.step("dispatch", f"{print_mode_name}: sending "
                             f"'{os.path.basename(pdf_path)}' to {target['name']}",
                 side=side or "-", copies=copies, sheets=total_sheets,
                 sides=sides or ("two-sided-short-edge" if duplex else "printer default"),
                 route="printer over IPP" if manual_duplex_sheets > 0 else
                       ("CUPS" if target.get("dispatch") == "cups" and shutil.which("lp")
                        else "raw port 9100"))

    if manual_duplex_sheets > 0:
        uri = config.get_config()["hardware"].get("printer_uri", "")
        dispatch_via_ipp_manual_duplex(pdf_path, uri, copies,
                                       sides or "two-sided-long-edge",
                                       manual_duplex_sheets, side, shutdown_event)
    elif target.get("dispatch") == "cups" and shutil.which("lp"):
        dispatch_via_cups(pdf_path, target, copies, duplex, total_sheets, side, sides, shutdown_event)
    else:
        if target.get("dispatch") == "cups":
            # Configured for CUPS but 'lp' is missing: without this line the job
            # silently went out over raw port 9100 instead, losing the duplex
            # and progress information.
            job_log.warn("dispatch", "'lp' is not installed although the printer is configured "
                                     "for CUPS - falling back to raw port 9100, so there is no "
                                     "duplex control and no progress")
        dispatch_via_socket(pdf_path, target, copies)

    print(f"[Hardware] Transfer complete for {print_mode_name}.")
    job_log.step("dispatch", f"{print_mode_name} handed over completely")