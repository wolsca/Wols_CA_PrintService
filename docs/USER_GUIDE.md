# User Guide: Wols CA Print Service

This guide explains how to use the Wols CA Booklet Printing service from your devices. The service allows you to print PDFs as A5 booklets on A4 paper. It handles the imposition (reordering pages) and prompts you to flip the paper halfway through.

## Pick the printer, pick the way it prints

The server offers **three printers** on the network. You do not have to change any
setting: the printer you select in the normal print dialog decides how your
document comes out.

| Printer you select | What you get |
| :--- | :--- |
| **WolsCA_Booklet** | An A5 booklet on A4 paper: pages are reordered, the front side is printed, you flip the stack, the back side is printed. Fold in the middle and you have a booklet. |
| **WolsCA_DoubleSided** | Normal pages, printed on both sides of the sheet. No booklet reordering. On a printer without a duplex unit you are asked to flip the stack once. |
| **WolsCA_SingleSided** | Normal pages, one page per sheet. Never any flipping. |

The printer always wins: a document sent to **WolsCA_SingleSided** is printed
single sided, even if the web app shows another mode. The mode chosen in the web
app only applies to files you copy into the shared drop folder yourself.

---

## How it works (booklet example)

1.  **Submit a job**: Send a PDF to the `WolsCA_Booklet` printer on your network.
2.  **Front side prints**: The service reorders the pages and prints the first side.
3.  **Flip the paper**: The service pauses. The web app, Home Assistant, and your phone (if configured) will show **Waiting for you to flip the paper**.
4.  **Confirm**: Re-insert the printed sheets into the printer tray and press **Continue** in the web app, Home Assistant, or on the phone notification.
    - *Default instruction*: "Take the whole stack out of the output tray, do NOT rotate it, and put it back in the paper tray printed side down, top edge first."
5.  **Back side prints**: The service prints the remaining pages.

*Note: If you use a duplex-capable printer, the flip step is skipped entirely.*

---

## The Mobile Web App

The service includes a mobile-friendly web app to track status and manage jobs.

*   **Address**: `http://<server-name>.local:8080/` (or visit `/qr` on the server for a QR code). If your administrator added a name to the local DNS server, use that instead, for example `http://print.home.lan:8080/`.
*   **Available printers**: A card lists the three printers you can pick in the print dialog and what each one does.
*   **Status & Progress**: Shows if the printer is Ready, Preparing, or Printing (with a real-time progress bar if supported).
*   **Flip Help**: When the front side is done, the app shows an illustration and specific instructions on how to flip the paper for your printer.
*   **Control Buttons**:
    - **Continue**: Press after re-inserting paper.
    - **Reprint Front**: Prints the front side again if the first attempt failed (e.g. paper jam).
    - **Cancel**: Aborts the current job and clears the queue.
*   **Job Options**: You can pick the **Printer**, **Print Mode** (Booklet / DoubleSided / SingleSided / Standard / Bypass), and number of **Copies** (1-10) for your next job. These choices last for 15 minutes. The print mode here is only used for files copied into the shared drop folder - a job sent to one of the three network printers always uses that printer's mode.
*   **Job History**: A list of recently completed jobs is shown at the bottom.

### Add to Home Screen (PWA)
For quick access, you can install the web app as an app:
*   **iPhone/iPad**: Open the URL in Safari → Tap the **Share** button → **Add to Home Screen**.
*   **Android**: Open the URL in Chrome → Tap the **three dots** menu → **Install app**.
*   **Windows**: Open the URL in Edge or Chrome → Click the **Install** icon in the address bar.

---

## Push Notifications (ntfy)

You can receive a notification on your phone when the printer is waiting for you.

Messages are sent when the front side is printed and the paper must be flipped (with a click link to the web app, taken from `web.public_url`), when a job fails and when a document is finished.

### Setup on iPhone and Android
1.  Install the free **ntfy** app from the App Store or Google Play Store.
2.  Open the app and tap **Subscribe to topic**.
3.  Enter the topic name provided by your administrator. If they left it empty, the service generated a unique topic like `wolsca_print_service_a1b2c3d4` at first use; subscribe to exactly that topic (found in the configuration or the web app).
4.  Ensure notifications are allowed.
5.  When the phone buzzes, you can tap the **Continue printing** button directly in the notification after you've flipped the paper.

*Note: For the notification button to work, the `web.public_url` must be set to an address your phone can reach (e.g. `http://print.local:8080`).*

---

## Printing from your Devices

### iPhone and iPad
1.  Ensure you are on the same Wi-Fi as the print server.
2.  Open the document you want to print.
3.  Tap the **Share** button → **Print**.
4.  Tap **Printer** and select the printer you want: **WolsCA_Booklet**, **WolsCA_DoubleSided** or **WolsCA_SingleSided**.
5.  Tap **Print**.

### Android
1.  Ensure the **Default Print Service** or **Mopria Print Service** is enabled in your phone settings.
2.  Open the document → **Print**.
3.  Select **WolsCA_Booklet**, **WolsCA_DoubleSided** or **WolsCA_SingleSided** from the list of available printers.
4.  Tap the printer icon to start.

### Windows 10 & 11
1.  Go to **Settings** → **Bluetooth & devices** → **Printers & scanners**.
2.  Click **Add device**. If the printer is not found, click **The printer that I want isn't listed**.
3.  Select **Select a shared printer by name**.
4.  Enter the IPP URL of the printer you want:
    - `http://<server-ip>:631/printers/WolsCA_Booklet`
    - `http://<server-ip>:631/printers/WolsCA_DoubleSided`
    - `http://<server-ip>:631/printers/WolsCA_SingleSided`
5.  When prompted for a driver, choose **Generic** → **MS Publisher Imagesetter** or **Generic PostScript**.
6.  Repeat for the other queues if you want all three available in the print dialog.

### macOS
1.  Go to **System Settings** → **Printers & Scanners**.
2.  Click **Add Printer, Scanner, or Fax...**.
3.  Select **WolsCA_Booklet**, **WolsCA_DoubleSided** or **WolsCA_SingleSided** from the list (they appear via Bonjour/mDNS).
4.  Click **Add**. Add the other queues the same way if you want all three.

### Linux / Raspberry Pi
The printers are announced via `cups-browsed`. If they do not appear automatically, you can add them manually using their IPP URIs:

```
ipp://<server-ip>:631/printers/WolsCA_Booklet
ipp://<server-ip>:631/printers/WolsCA_DoubleSided
ipp://<server-ip>:631/printers/WolsCA_SingleSided
```

---

## Alternative: Drop Folder
If you cannot add the printer to your device, you can copy a PDF file directly into the network-shared drop folder (if configured by your administrator via SMB or NFS). The service will detect the new file and start the job immediately.

The drop folder has one sub-folder per mode, so you can pick the mode by choosing where you copy the file:

| Sub-folder | Mode |
| :--- | :--- |
| `booklet` | A5 booklet |
| `duplex` | DoubleSided |
| `simplex` | SingleSided |

A file copied into the top level of the drop folder uses the mode selected in the web app, or the administrator default.

---

## Troubleshooting

| Symptom | Solution |
| :--- | :--- |
| **Web app not reachable** | Ensure you are on the same network. Try using the server's IP address instead of `.local` name. |
| **Printer not discovered** | Ensure the server and device are on the same subnet. Check if mDNS/Avahi is running on the server. |
| **No notification arrives** | Check if the `ntfy` app is subscribed to the correct topic. Notifications are ENABLED by default; ensure `notify.enabled` has not been set to false. |
| **Progress bar stays at 0%** | Real-time progress requires a CUPS-dispatched printer. Raw port 9100 transfers only show the active sheet. |
| **Job stuck "Waiting for flip"** | Open the web app and press the **CONTINUE** button. Ensure you have re-inserted the paper correctly. |
| **Job cancelled itself** | Jobs are automatically cancelled after 30 minutes of waiting to prevent blocking the queue. |
| **Second document does nothing** | The service prints jobs one by one. Check the web app to see if your job is in the waiting list. |
| **Nothing comes out and you do not know why** | Open the **Job log** card in the web app: it shows every step of your job (which printer, which mode, what the printer answered) and where it stopped. *Copy job log* copies the whole thing so it can be sent to the administrator. |
| **Only one printer shows up** | The other two queues were not created. The administrator has to run the installer again (CUPS and all queues are included by default) or restart the service, which creates missing queues itself. |
| **Printed single sided although I chose Booklet in the web app** | You printed to the `WolsCA_SingleSided` printer. The printer you select in the print dialog always wins; use `WolsCA_Booklet` instead. |
| **Blank pages at the end** | A booklet requires a multiple of 4 pages. If your document has 5 pages, 3 blank A5 sides will be added to complete the last sheet. |
| **Files not appearing in drop folder** | Check network share permissions. Ensure the service has write access to the spool directories. |

---

*See the [README](../README.md) for installation and technical configuration.*
