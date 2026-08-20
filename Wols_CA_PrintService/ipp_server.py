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

        if len(body) < 8:
            self.send_error(400, "Bad Request: Malformed IPP Header")
            return

        version = body[0:2]
        operation = struct.unpack('>H', body[2:4])[0]
        request_id = body[4:8]

        offset = 8
        while offset < len(body):
            tag = body[offset]
            offset += 1

            if tag == 0x03: # End of attributes group tag
                break
            if tag < 0x10:
                continue

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
            self.send_ipp_response(request_id, version, IPP_STATUS_SUCCESSFUL_OK)

    def determine_drop_directory(self):
        path = self.path.lower()
        c = config.get_config()
        queues = c.get("intake", {}).get("queues", [])

        for q in queues:
            queue_id = q.get("id", "").lower()
            cups_name = q.get("cups_queue", "").lower()
            if queue_id in path or cups_name in path:
                return q.get("directory") or os.path.join(config.DROP_DIR, queue_id)

        return config.DROP_DIR

    def handle_print_job(self, request_id, version, payload):
        if not payload:
            self.send_ipp_response(request_id, version, 0x0400)
            return

        drop_dir = self.determine_drop_directory()
        os.makedirs(drop_dir, exist_ok=True)

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
            self.send_ipp_response(request_id, version, 0x0500)

    # --- Start IPP Binary Encoders ---
    def encode_ipp_attr(self, tag, name, value_bytes):
        """Constructs a binary IPP attribute sequence."""
        name_b = name.encode('utf-8')
        return struct.pack('>B H', tag, len(name_b)) + name_b + struct.pack('>H', len(value_bytes)) + value_bytes

    def encode_ipp_string(self, tag, name, value):
        """Helper to encode string values into IPP binary format."""
        return self.encode_ipp_attr(tag, name, value.encode('utf-8'))
    # --- End IPP Binary Encoders ---

    def handle_get_attributes(self, request_id, version):
        """Responds with an exhaustive IPP dictionary to force Windows into selecting the native PDF IPP driver."""
        response = version
        response += struct.pack('>H', IPP_STATUS_SUCCESSFUL_OK)
        response += request_id

        # 1. Operation Attributes Group (0x01)
        response += bytes([0x01])
        response += self.encode_ipp_string(0x47, 'attributes-charset', 'utf-8')
        response += self.encode_ipp_string(0x48, 'attributes-natural-language', 'en-us')

        # 2. Printer Attributes Group (0x04)
        response += bytes([0x04])

        # IPP Versions Supported
        response += self.encode_ipp_string(0x44, 'ipp-versions-supported', '1.1')
        response += self.encode_ipp_string(0x44, '', '2.0') # Empty name = 1setOf (array continuation)

        # Document Format Supported (Forces Microsoft to use PDF)
        response += self.encode_ipp_string(0x49, 'document-format-supported', 'application/pdf')
        response += self.encode_ipp_string(0x49, '', 'application/octet-stream')

        # Printer URI Supported
        host = self.headers.get('Host', 'localhost')
        uri = f"http://{host}{self.path}"
        response += self.encode_ipp_string(0x45, 'printer-uri-supported', uri)

        # Printer Name
        printer_name = self.path.strip('/').split('/')[-1]
        response += self.encode_ipp_string(0x42, 'printer-name', printer_name)

        # Printer State (3 = Idle)
        response += self.encode_ipp_attr(0x23, 'printer-state', struct.pack('>I', 3))

        # Accepting Jobs (1 = True)
        response += self.encode_ipp_attr(0x22, 'printer-is-accepting-jobs', bytes([0x01]))

        # Operations Supported (Print-Job=2, Validate-Job=4, Get-Printer-Attributes=11)
        response += self.encode_ipp_attr(0x23, 'operations-supported', struct.pack('>I', 2))
        response += self.encode_ipp_attr(0x23, '', struct.pack('>I', 4))
        response += self.encode_ipp_attr(0x23, '', struct.pack('>I', 11))

        # 3. End of Attributes (0x03)
        response += bytes([0x03])

        self.send_response(200)
        self.send_header('Content-Type', 'application/ipp')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def send_ipp_response(self, request_id, version, status_code):
        response = version
        response += struct.pack('>H', status_code)
        response += request_id
        response += bytes([0x01, 0x03])

        self.send_response(200)
        self.send_header('Content-Type', 'application/ipp')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

class ThreadedIPPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def start_server(shutdown_event):
    server_address = ('0.0.0.0', IPP_PORT)
    try:
        httpd = ThreadedIPPServer(server_address, IPPRequestHandler)
        print(f"[System] Wols CA Native IPP Server listening on port {IPP_PORT}")

        while not shutdown_event.is_set():
            httpd.handle_request()

    except OSError as e:
        print(f"[Error] IPP Server failed to bind to port {IPP_PORT}: {e}")