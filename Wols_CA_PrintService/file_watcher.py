import os
import time
from watchdog.events import FileSystemEventHandler
import mqtt_service

def wait_until_file_is_complete(filepath, shutdown_event, timeout=120.0):
    """Waits until the file size stops growing (spooler is still writing)."""
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline and not shutdown_event.is_set():
        try:
            size = os.path.getsize(filepath)
        except OSError:
            return False
        if size > 0 and size == last_size:
            return True
        last_size = size
        time.sleep(1.0)
    return os.path.exists(filepath)

class PrintFolderWatcher(FileSystemEventHandler):
    """Watches a specific drop directory and triggers the callback for PDFs."""
    def __init__(self, shutdown_event, enqueue_callback, intake=None):
        super().__init__()
        self.shutdown_event = shutdown_event
        self.enqueue_callback = enqueue_callback
        self.intake = intake

    def on_created(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext == ".pdf":
                if wait_until_file_is_complete(event.src_path, self.shutdown_event):
                    self.enqueue_callback(event.src_path, self.intake)
            elif ext in [".prn", ".ps", ".pcl"]:
                filename = os.path.basename(event.src_path)
                print(f"[Warning] Ignored {ext} file: {filename}. Service requires PDF files.")
                mqtt_service.publish_log(f"Ignored unsupported file format '{filename}'. Please ensure the Windows queue uses a PDF driver.", "warning")

def scan_directory(directory, enqueue_callback, intake=None):
    """Scans for existing files on startup."""
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        print(f"[Warning] Could not scan {directory}: {e}")
        return

    for filename in names:
        path = os.path.join(directory, filename)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext == ".pdf":
            enqueue_callback(path, intake)
        elif ext in [".prn", ".ps", ".pcl"]:
            print(f"[Warning] Ignored {ext} file: {filename}. Service requires PDF files.")