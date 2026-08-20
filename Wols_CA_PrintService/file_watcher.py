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

    def handle_path(self, path):
        """Common entry point for created, moved and closed events."""
        if not path or os.path.isdir(path):
            return
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            if wait_until_file_is_complete(path, self.shutdown_event):
                self.enqueue_callback(path, self.intake)
        elif ext in [".prn", ".ps", ".pcl"]:
            filename = os.path.basename(path)
            print(f"[Warning] Ignored {ext} file: {filename}. Service requires PDF files.")
            mqtt_service.publish_log(f"Ignored unsupported file format '{filename}'. Please ensure the Windows queue uses a PDF driver.", "warning")

    def on_created(self, event):
        if not event.is_directory:
            self.handle_path(event.src_path)

    def on_moved(self, event):
        # cups-pdf renders into its own spool dir and *moves* the PDF into the
        # drop folder, which inotify reports as IN_MOVED_TO, not IN_CREATE.
        if not event.is_directory:
            self.handle_path(getattr(event, "dest_path", None) or event.src_path)

    def on_closed(self, event):
        # inotify IN_CLOSE_WRITE: the writer finished with the file.
        if not event.is_directory:
            self.handle_path(event.src_path)

def scan_directory(directory, enqueue_callback, intake=None, recursive=True):
    """Scans for existing files (including cups-pdf per-user subfolders)."""
    try:
        if recursive:
            walked = sorted(os.walk(directory))
        else:
            walked = [(directory, [], sorted(os.listdir(directory)))]
    except OSError as e:
        print(f"[Warning] Could not scan {directory}: {e}")
        return

    for root, _dirs, files in walked:
        for filename in sorted(files):
            path = os.path.join(root, filename)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext == ".pdf":
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                # Skip files that are still being written by the spooler.
                if stat.st_size == 0 or (time.time() - stat.st_mtime) < 2.0:
                    continue
                enqueue_callback(path, intake)
            elif ext in [".prn", ".ps", ".pcl"]:
                print(f"[Warning] Ignored {ext} file: {filename}. Service requires PDF files.")