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
        q_id = str(entry.get("id") or entry.get("cups_queue") or "").strip()
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

    suffix = queue_entry["id"]
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
        run_root_command(["systemctl", "enable", "--now", "avahi-daemon"], "Enabling Avahi daemon")
        run_root_command(["systemctl", "reload-or-restart", "avahi-daemon"], "Publishing web app over mDNS")
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
    print("[Admin] 1/5: Installing CUPS, cups-pdf and Avahi (Debian/Ubuntu)...")
    try:
        subprocess.run(["apt-get", "update"], check=True, env=env)
        subprocess.run(["apt-get", "install", "-y", "cups", "printer-driver-cups-pdf",
                        "cups-ipp-utils", "avahi-daemon", "avahi-utils"],
                       check=True, env=env)
    except Exception as e:
        print(f"[Error] Package installation failed: {e}")
        sys.exit(1)

    print("[Admin] 2/5: Configuring cups-pdf output directory...")
    os.makedirs(drop_dir, exist_ok=True)
    os.chmod(drop_dir, 0o2775)
    configure_cups_pdf(drop_dir)

    queues = intake_queues()
    if queues:
        print("[Admin] 3/5: Creating one visible queue per print mode...")
        for queue_entry in queues:
            create_intake_queue(queue_entry, share)
    else:
        print("[Admin] 3/5: Creating the single intake queue...")
        create_intake_queue({"id": "booklet", "cups_queue": queue,
                             "description": "Wols CA Booklet Intake",
                             "print_mode": "Booklet", "directory": drop_dir}, share)

    if share:
        print("[Admin] 4/5: Publishing the queue on the local network...")
        run_root_command(["cupsctl", "--share-printers", "--remote-any"], "Enabling network sharing")
        run_root_command(["systemctl", "enable", "--now", "avahi-daemon"], "Enabling Avahi announcements")
        run_root_command(["systemctl", "restart", "cups"], "Restarting CUPS")

    print("[Admin] 5/5: Advertising the web app over mDNS...")
    advertise_web_app_over_mdns()

    host = socket.gethostname()
    web_port = int(c.get("web", {}).get("port", 8080))
    print(f"\n[Admin] Deployment complete.")
    for queue_entry in (queues or [{"cups_queue": queue, "print_mode": "Booklet"}]):
        print(f"  Print to : ipp://{host}.local:631/printers/{queue_entry['cups_queue']} ({queue_entry['print_mode']})")
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