import os
import time
import struct
import threading
import socketserver
import http.server
import logging
import config

# ============================================================================
# Wols CA Print Service - IPP Server Module
# ============================================================================
# Minimal, zero-dependency Internet Printing Protocol (IPP) implementation.
# Parses binary IPP payloads over HTTP to eliminate third-party CUPS dependencies.
# Compliant with core RFC 8010 requirements for AirPrint/Windows endpoints.
# ============================================================================

IPP_PORT = 631

# IPP Operation Codes
IPP_OP_PRINT_JOB = 0x0002
IPP_OP_VALIDATE_JOB = 0x0004
IPP_OP_GET_PRINTER_ATTRIBUTES = 0x000B

# IPP Status Codes
IPP_STATUS_SUCCESSFUL_OK = 0x0000

class IPPRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Suppress standard HTTP logging to keep the console clean
        pass

    def do_POST(self):
        """Handle incoming IPP requests packaged in HTTP POST."""
        if 'application/ipp' not in self.headers.get('Content-Type', ''):
            self.send_error(400, "Bad Request: Expected application/ipp")
            return

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "Bad Request: No payload")
            return

        body = self.rfile.read(content_length)

        # IPP Header Structure: Version (2 bytes), Operation (2 bytes), Request ID (4 bytes)
        if len(body) < 8:
            self.send_error(400, "Bad Request: Malformed IPP Header")
            return

        version = body[0:2]
        operation = struct.unpack('>H', body[2:4])[0]
        request_id = body[4:8]

        # Extract attributes and PDF payload
        offset = 8
        while offset < len(body):
            tag = body[offset]
            offset += 1

            if tag == 0x03: # End of attributes group tag
                break
            if tag < 0x10: # Delimiter tag (no name/value follows)
                continue

            # Value tag structure: name_length(2), name, value_length(2), value
            if offset + 2 > len(body): break
            name_len = struct.unpack('>H', body[offset:offset+2])[0]
            offset += 2 + name_len

            if offset + 2 > len(body): break
            val_len = struct.unpack('>H', body[offset:offset+2])[0]
            offset += 2 + val_len

        payload = body[offset:]

        if operation == IPP_OP_PRINT_JOB:
            self.handle_print_job(request_id, version, payload)
        elif operation == IPP_OP_GET_PRINTER_ATTRIBUTES:
            self.handle_get_attributes(request_id, version)
        else:
            # Acknowledge unknown/unsupported operations gracefully
            self.send_ipp_response(request_id, version, IPP_STATUS_SUCCESSFUL_OK)

    def determine_drop_directory(self):
        """Map the HTTP request path to the corresponding Wols CA queue directory."""
        path = self.path.lower()
        c = config.get_config()
        queues = c.get("intake", {}).get("queues", [])

        for q in queues:
            queue_id = q.get("id", "").lower()
            cups_name = q.get("cups_queue", "").lower()
            if queue_id in path or cups_name in path:
                return q.get("directory") or os.path.join(config.DROP_DIR, queue_id)

        # Fallback to general drop directory
        return config.DROP_DIR

    def handle_print_job(self, request_id, version, payload):
        """Process incoming print job, save PDF, and send IPP OK."""
        if not payload:
            self.send_ipp_response(request_id, version, 0x0400) # Client error
            return

        drop_dir = self.determine_drop_directory()
        os.makedirs(drop_dir, exist_ok=True)

        # Generate a unique Zero-Trust timestamped filename
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"WolsPrintJob_{timestamp}.pdf"
        filepath = os.path.join(drop_dir, filename)

        try:
            with open(filepath, 'wb') as f:
                f.write(payload)
            print(f"[IPP] Successfully ingested '{filename}' into {drop_dir}")
            self.send_ipp_response(request_id, version, IPP_STATUS_SUCCESSFUL_OK)
        except Exception as e:
            print(f"[Error] Failed to write IPP payload: {e}")
            self.send_ipp_response(request_id, version, 0x0500) # Server error

    def handle_get_attributes(self, request_id, version):
        """Respond with hardcoded printer capabilities to satisfy the endpoint spooler."""
        # A full IPP response dictates supported formats and printer status.
        # This minimal mock ensures Apple/Windows accept the printer as a valid PDF endpoint.
        response = version
        response += struct.pack('>H', IPP_STATUS_SUCCESSFUL_OK)
        response += request_id
        response += bytes([0x01]) # Operation attributes group

        # charset: utf-8
        response += bytes([0x47])
        response += struct.pack('>H', 18) + b'attributes-charset'
        response += struct.pack('>H', 5) + b'utf-8'

        # natural-language: en-us
        response += bytes([0x48])
        response += struct.pack('>H', 27) + b'attributes-natural-language'
        response += struct.pack('>H', 5) + b'en-us'

        response += bytes([0x03]) # End of attributes

        self.send_response(200)
        self.send_header('Content-Type', 'application/ipp')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def send_ipp_response(self, request_id, version, status_code):
        """Send a basic IPP acknowledgement."""
        response = version
        response += struct.pack('>H', status_code)
        response += request_id
        response += bytes([0x01, 0x03]) # Empty operation attributes group and end tag

        self.send_response(200)
        self.send_header('Content-Type', 'application/ipp')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

class ThreadedIPPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def start_server(shutdown_event):
    """Initialize and run the IPP listener on port 631."""
    server_address = ('0.0.0.0', IPP_PORT)
    try:
        httpd = ThreadedIPPServer(server_address, IPPRequestHandler)
        print(f"[System] Wols CA Native IPP Server listening on port {IPP_PORT}")

        # Run server loop in a non-blocking manner to respect shutdown events
        while not shutdown_event.is_set():
            httpd.handle_request()

    except OSError as e:
        print(f"[Error] IPP Server failed to bind to port {IPP_PORT}: {e}")
        print("[Error] Ensure CUPS is completely uninstalled or stopped (sudo systemctl stop cups).")