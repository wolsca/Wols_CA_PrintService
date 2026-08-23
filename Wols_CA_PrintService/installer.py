import os
import sys
import time
import shutil
import subprocess
import platform
import ssl
import urllib.request
import socket
import config

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

if IS_WINDOWS:
    import ctypes
    import winreg

def intake_queues():
    c = config.get_config()
    section = c.get("intake", {})
    if not section.get("enabled", True):
        return []
    queues = []
    for entry in section.get("queues", []):
        # Lower case, so a capitalised id in the configuration can never split
        # the cups-pdf instances (the backend name derives from it).
        q_id = config.normalize_intake_id(str(entry.get("id") or entry.get("cups_queue") or "").strip())
        if not q_id: continue
        mode = str(entry.get("print_mode", "Booklet"))
        directory = str(entry.get("directory", "")).strip() or os.path.join(config.DROP_DIR, q_id)
        queues.append({
            "id": q_id,
            "cups_queue": entry.get("cups_queue") or f"WolsCA_{q_id.capitalize()}",
            "description": entry.get("description") or mode,
            "print_mode": mode,
            "directory": directory
        })
    return queues

def check_virtual_printer():
    if IS_LINUX:
        check_cups_queue()
        return
    if not IS_WINDOWS:
        return

    printer_name = config.get_config()["virtual_printer"]["name"]
    result = subprocess.run(["powershell", "-Command", f"Get-Printer -Name '{printer_name}' -ErrorAction SilentlyContinue"], capture_output=True, text=True)

    if not result.stdout.strip():
        print(f"[Zero-Touch] Virtual printer '{printer_name}' not found.")
        print("[Zero-Touch] Requesting Administrator privileges for fully automated deployment...")
        script_path = os.path.abspath(sys.argv[0])
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script_path}" --install-printer', None, 1)

def check_cups_queue():
    c = config.get_config()
    queue = c["virtual_printer"].get("cups_queue_name", "WolsCA_Booklet")

    if not shutil.which("lpstat"):
        print("[Zero-Touch] CUPS is not installed. Run the service once with '--install-printer' as root to deploy cups-pdf.")
        return

    expected = [entry["cups_queue"] for entry in intake_queues()] or [queue]
    for queue_name in expected:
        result = subprocess.run(["lpstat", "-p", queue_name], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[Zero-Touch] CUPS queue '{queue_name}' not found.")
            print("[Zero-Touch] Run 'sudo <python> main.py --install-printer' to create it.")
        else:
            print(f"[Zero-Touch] CUPS queue '{queue_name}' is available.")

def run_root_command(args, description):
    print(f"[Admin] {description}")
    try:
        subprocess.run(args, check=True)
        return True
    except FileNotFoundError:
        print(f"[Error] Command not found: {args[0]}")
    except subprocess.CalledProcessError as e:
        print(f"[Error] {description} failed (exit code {e.returncode}).")
    return False

def run_service_command(args, description):
    """Like run_root_command, but silently skipped without systemd.

    The Alpine test container runs cupsd and avahi-daemon directly, so there is
    no systemctl; the configuration itself is identical to Debian.
    """
    if args and args[0] == "systemctl" and not shutil.which("systemctl"):
        print(f"[Admin] {description}: skipped, no systemd in this environment.")
        return True
    return run_root_command(args, description)

def configure_cups_pdf(drop_dir, conf_path="/etc/cups/cups-pdf.conf", template=None):
    if not os.path.exists(conf_path):
        source = template or "/etc/cups/cups-pdf.conf"
        if os.path.exists(source):
            shutil.copyfile(source, conf_path)
        else:
            print(f"[Warning] {conf_path} not found, skipping cups-pdf configuration.")
            return

    overrides = {
        "Out": drop_dir,
        "AnonDirName": drop_dir,
        # A plain Out path (no ${USER}) keeps every job in one watched folder.
        "AnonUser": "nobody",
        "Grp": "lp",
        "UserUMask": "0000",
        "Truncate": "64",
        "PostProcessing": ""
    }

    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        cleaned = []
        for line in lines:
            key = line.strip().lstrip("#").split(" ")[0] if line.strip() else ""
            if key in overrides and not line.strip().startswith("#"):
                continue
            cleaned.append(line)

        cleaned.append("\n### Wols CA Print Service ###\n")
        for key, value in overrides.items():
            cleaned.append(f"{key} {value}\n" if value else f"{key}\n")

        with open(conf_path, 'w') as f:
            f.writelines(cleaned)
        print(f"[Admin] cups-pdf now writes into {drop_dir}.")
    except Exception as e:
        print(f"[Warning] Could not update {conf_path}: {e}")

def find_cups_pdf_ppd():
    candidates = [
        "/usr/share/ppd/cups-pdf/CUPS-PDF_opt.ppd",
        "/usr/share/ppd/cups-pdf/CUPS-PDF_noopt.ppd",
        "/usr/share/cups/model/CUPS-PDF_opt.ppd",
        "/usr/share/cups/model/CUPS-PDF_noopt.ppd",
        "/usr/share/ppd/cups-pdf/CUPS-PDF.ppd",
        "/usr/share/cups/model/CUPS-PDF.ppd"
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    print("[Warning] No cups-pdf PPD found; falling back to driverless 'everywhere'.")
    return None

def cups_backend_dir():
    for path in ("/usr/lib/cups/backend", "/usr/libexec/cups/backend"):
        if os.path.isdir(path):
            return path
    return None

def ensure_cups_pdf_instance(queue_entry):
    backend_dir = cups_backend_dir()
    base_backend = os.path.join(backend_dir, "cups-pdf") if backend_dir else None
    directory = queue_entry["directory"]

    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o2775)

    if not base_backend or not os.path.exists(base_backend):
        print("[Warning] cups-pdf backend not found; using shared output directory.")
        return "cups-pdf:/"

    suffix = queue_entry["id"].lower()
    instance = os.path.join(backend_dir, f"cups-pdf-{suffix}")
    try:
        if not os.path.exists(instance):
            os.symlink("cups-pdf", instance)
        configure_cups_pdf(directory, f"/etc/cups/cups-pdf-{suffix}.conf")
        print(f"[Admin] cups-pdf instance '{suffix}' writes into {directory}.")
        return f"cups-pdf-{suffix}:/"
    except Exception as e:
        print(f"[Warning] Could not create cups-pdf instance '{suffix}': {e}")
        return "cups-pdf:/"

def create_intake_queue(queue_entry, share):
    queue_name = queue_entry["cups_queue"]
    device_uri = ensure_cups_pdf_instance(queue_entry)
    ppd = find_cups_pdf_ppd()

    lpadmin_args = ["lpadmin", "-p", queue_name, "-v", device_uri, "-E"]
    lpadmin_args += ["-P", ppd] if ppd else ["-m", "everywhere"]
    lpadmin_args += [
        "-o", "printer-is-shared=" + ("true" if share else "false"),
        "-o", "document-format-default=application/pdf",
        "-D", queue_entry["description"],
        "-L", "Wols CA Print Service"
    ]
    run_root_command(lpadmin_args, f"Creating CUPS queue '{queue_name}'")
    run_root_command(["cupsenable", queue_name], f"Enabling queue '{queue_name}'")
    run_root_command(["cupsaccept", queue_name], f"Accepting jobs on '{queue_name}'")

CUPSD_CONF = "/etc/cups/cupsd.conf"

def patch_cupsd_conf(conf_path=CUPSD_CONF):
    """Fallback for 'cupsctl: Not Implemented': write the sharing settings directly."""
    if not os.path.exists(conf_path):
        print(f"[Warning] {conf_path} not found; cannot enable sharing manually.")
        return False

    directives = {
        "Browsing": "On",
        "BrowseLocalProtocols": "dnssd",
        "DefaultShared": "Yes",
        "Listen": None  # handled separately
    }

    try:
        with open(conf_path, "r") as f:
            lines = f.readlines()

        result = []
        depth = 0
        in_root_location = False
        root_location_has_allow = False
        listens = []

        for line in lines:
            stripped = line.strip()
            lowered = stripped.lower()
            key = stripped.split(" ")[0] if stripped else ""

            if lowered.startswith("<location /"):
                depth += 1
                in_root_location = lowered in ("<location />", "<location>")
            elif lowered.startswith("<location"):
                depth += 1
            elif lowered.startswith("</location"):
                if in_root_location and not root_location_has_allow:
                    result.append("  Allow @LOCAL\n")
                depth -= 1
                in_root_location = False
                root_location_has_allow = False
            elif in_root_location and lowered.startswith("allow "):
                root_location_has_allow = True

            if depth == 0 and key in directives and not stripped.startswith("#"):
                if key == "Listen":
                    listens.append(stripped)
                continue

            result.append(line)

        keep_listen = [l for l in listens if "/var/run/cups/cups.sock" in l or "/run/cups/cups.sock" in l]
        block = ["\n### Wols CA Print Service ###\n"]
        block += [l + "\n" for l in keep_listen] or ["Listen /run/cups/cups.sock\n"]
        block += ["Listen *:631\n", "Browsing On\n", "BrowseLocalProtocols dnssd\n", "DefaultShared Yes\n"]
        result += block

        with open(conf_path, "w") as f:
            f.writelines(result)
        print(f"[Admin] {conf_path} updated: sharing, DNS-SD browsing and port 631 enabled.")
        return True
    except Exception as e:
        print(f"[Warning] Could not update {conf_path}: {e}")
        return False

def enable_network_sharing():
    """CUPS 2.4 answers 'Not Implemented' to cupsctl on some builds; fall back to the config file."""
    if run_root_command(["cupsctl", "--share-printers", "--remote-any"], "Enabling network sharing"):
        return
    print("[Admin] cupsctl refused the request; patching cupsd.conf instead.")
    patch_cupsd_conf()

def physical_queue_name(c):
    return str(c.get("hardware", {}).get("cups_queue_name") or "WolsCA_Output").strip()

def ensure_physical_queue():
    """Creates the real output queue so jobs no longer go to a raw 9100 socket."""
    c = config.get_config()
    uri = str(c.get("hardware", {}).get("printer_uri", "")).strip()
    if not uri:
        print("[Admin] No hardware.printer_uri configured; skipping the output queue.")
        return None

    queue_name = physical_queue_name(c)
    lpadmin_args = ["lpadmin", "-p", queue_name, "-v", uri, "-E",
                    "-o", "printer-is-shared=false",
                    "-D", "Wols CA physical output printer",
                    "-L", "Wols CA Print Service"]
    # Driverless only makes sense for IPP targets; a socket or file backend gets a
    # raw queue, so the PDF this service produced is passed through unchanged.
    if uri.startswith(("ipp://", "ipps://", "http://", "https://", "dnssd://")):
        lpadmin_args += ["-m", "everywhere"]
    ok = run_root_command(lpadmin_args,
                          f"Creating output queue '{queue_name}' for {uri}")
    if not ok:
        print("[Warning] Could not create the output queue; the raw dispatch stays in place.")
        return None

    run_root_command(["cupsenable", queue_name], f"Enabling queue '{queue_name}'")
    run_root_command(["cupsaccept", queue_name], f"Accepting jobs on '{queue_name}'")

    targets = c.get("printers", {}).get("targets", [])
    default_id = c.get("printers", {}).get("default")
    changed = False
    for target in targets:
        if len(targets) == 1 or target.get("id") == default_id:
            if target.get("dispatch") != "cups" or target.get("cups_queue") != queue_name:
                target["dispatch"] = "cups"
                target["cups_queue"] = queue_name
                changed = True
    if changed:
        config.save_config()
        print(f"[Admin] Printer target now dispatches through CUPS queue '{queue_name}'.")
    return queue_name

def advertise_web_app_over_mdns():
    if not shutil.which("avahi-daemon"):
        return

    port = int(config.get_config().get("web", {}).get("port", 8080))
    service_file = "/etc/avahi/services/wolsca-print-web.service"
    content = f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Wols CA Booklet Printer on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>{port}</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
"""
    try:
        os.makedirs(os.path.dirname(service_file), exist_ok=True)
        with open(service_file, "w") as f:
            f.write(content)
        run_service_command(["systemctl", "enable", "--now", "avahi-daemon"], "Enabling Avahi daemon")
        run_service_command(["systemctl", "reload-or-restart", "avahi-daemon"], "Publishing web app over mDNS")
    except Exception as e:
        print(f"[Warning] Could not write {service_file}: {e}")

def perform_cups_printer_install():
    print("\n===================================================")
    print("  Wols CA CUPS Intake Printer Deployment Started    ")
    print("===================================================\n")

    if os.geteuid() != 0:
        print("[Error] Root privileges are required. Re-run with sudo.")
        sys.exit(1)

    c = config.get_config()
    queue = c["virtual_printer"].get("cups_queue_name", "WolsCA_Booklet")
    share = c["virtual_printer"].get("cups_share_on_network", True)
    drop_dir = config.DROP_DIR

    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if shutil.which("apt-get"):
        print("[Admin] 1/6: Installing CUPS, cups-pdf and Avahi (Debian/Ubuntu)...")
        try:
            subprocess.run(["apt-get", "update"], check=True, env=env)
            subprocess.run(["apt-get", "install", "-y", "cups", "printer-driver-cups-pdf",
                            "cups-ipp-utils", "avahi-daemon", "avahi-utils"],
                           check=True, env=env)
        except Exception as e:
            print(f"[Error] Package installation failed: {e}")
            sys.exit(1)
    else:
        # Alpine test container: the packages are part of the image already.
        print("[Admin] 1/6: No apt-get here, assuming CUPS and cups-pdf are installed.")
        if not shutil.which("lpadmin"):
            print("[Error] CUPS is not installed and cannot be installed automatically.")
            sys.exit(1)

    print("[Admin] 2/6: Configuring cups-pdf output directory...")
    os.makedirs(drop_dir, exist_ok=True)
    os.chmod(drop_dir, 0o2775)
    configure_cups_pdf(drop_dir)

    queues = intake_queues()
    if queues:
        print("[Admin] 3/6: Creating one visible queue per print mode...")
        for queue_entry in queues:
            create_intake_queue(queue_entry, share)
    else:
        print("[Admin] 3/6: Creating the single intake queue...")
        create_intake_queue({"id": "booklet", "cups_queue": queue,
                             "description": "Wols CA Booklet Intake",
                             "print_mode": "Booklet", "directory": drop_dir}, share)

    print("[Admin] 4/6: Creating the physical output queue...")
    output_queue = ensure_physical_queue()

    if share:
        print("[Admin] 5/6: Publishing the queues on the local network...")
        enable_network_sharing()
        run_service_command(["systemctl", "enable", "--now", "avahi-daemon"], "Enabling Avahi announcements")
        run_service_command(["systemctl", "restart", "cups"], "Restarting CUPS")

    print("[Admin] 6/6: Advertising the web app over mDNS...")
    advertise_web_app_over_mdns()

    host = socket.gethostname()
    web_port = int(c.get("web", {}).get("port", 8080))
    print(f"\n[Admin] Deployment complete.")
    for queue_entry in (queues or [{"cups_queue": queue, "print_mode": "Booklet"}]):
        print(f"  Print to : ipp://{host}.local:631/printers/{queue_entry['cups_queue']} ({queue_entry['print_mode']})")
    if output_queue:
        print(f"  Output   : CUPS queue '{output_queue}' -> {c['hardware'].get('printer_uri')}")
    print(f"  Web app  : http://{host}.local:{web_port}/")
    sys.exit(0)

def download_installer(url, dest_path):
    print(f"[Network] Downloading dependencies from: {url}")
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=ctx) as response, open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        return True
    except Exception as e:
        print(f"[Error] Auto-download failed: {e}")
        return False

def perform_admin_printer_install():
    print("\n===================================================")
    print("  Wols CA Zero-Touch Printer Deployment Started    ")
    print("===================================================\n")

    c = config.get_config()
    installer_path = c["virtual_printer"]["installer_path"]
    download_url = c["virtual_printer"].get("download_url", "")
    drop_dir = config.DROP_DIR

    if not os.path.exists(installer_path):
        if not download_url:
            print("[Error] No download URL configured in JSON. Aborting.")
            sys.exit(1)
        if not download_installer(download_url, installer_path):
            sys.exit(1)

    print(f"[Admin] 1/2: Installing Virtual Printer silently...")
    try:
        subprocess.run([installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", '/COMPONENTS="program"'], check=True)
    except Exception as e:
        print(f"[Error] Failed to install spooler: {e}")

    print("[Admin] 2/2: Injecting Registry Keys for silent Auto-Save...")
    try:
        key_path = r"Software\pdfforge\PDFCreator\Settings\ConversionProfiles\0"
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValueEx(key, "Enabled", 0, winreg.REG_SZ, "True")
        winreg.SetValueEx(key, "AutoSaveEnabled", 0, winreg.REG_SZ, "True")
        winreg.SetValueEx(key, "AutoSaveDirectory", 0, winreg.REG_SZ, drop_dir)
        winreg.SetValueEx(key, "AutoSaveFilename", 0, winreg.REG_SZ, "<DateTime>_<JobId>_WolsPrintJob")
        winreg.SetValueEx(key, "ShowProgress", 0, winreg.REG_SZ, "False")
        winreg.SetValueEx(key, "OpenViewer", 0, winreg.REG_SZ, "False")
        winreg.CloseKey(key)
        print(f"[Admin] Registry injected. Files will drop to: {drop_dir}")
    except Exception as e:
        print(f"[Warning] Registry injection failed: {e}")

    print("\n[Admin] Deployment complete.")
    time.sleep(5)
    sys.exit(0)