#!/usr/bin/env python3
"""
Recording stand-in for the posted-tariff rate service (integration point NET-1).

This is TEST INFRASTRUCTURE, not test logic. It exists only because the service
under test makes one outbound HTTP call and the real dependency is a third party.
No test assertion lives here; assertions live in cases/*.json and the recorded
request log this server writes.

Contract reproduced from src/ingest.php:8-22 (do not "improve" it):
  request : GET {LOOKUP_URL}/r/{latitude}/{longitude}
  response: JSON; the caller reads  ->now->v

Every request is appended verbatim to REQUEST_LOG so the harness can assert the
exact payload the service under test emitted, including path segments.

Behavior is selected per test case with environment variables:
  TARIFF_MODE      ok        -> 200, {"now":{"v":TARIFF_VALUE}}
                   empty     -> 200 with a zero-length body
                   malformed -> 200 with a body that is not JSON
                   no_field  -> 200, valid JSON lacking the now.v path
                   http500   -> 500 with an error body
                   reset     -> TCP connection closed without a response
  TARIFF_VALUE     numeric value placed at now.v (default 9)
  TARIFF_DELAY_MS  server-side delay before responding (default 0)
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUEST_LOG = "/tmp/requests.log"
CONTROL_PREFIX = "/__"


def _mode():
    return os.environ.get("TARIFF_MODE", "ok")


def _value():
    raw = os.environ.get("TARIFF_VALUE", "9")
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def _delay_seconds():
    try:
        return int(os.environ.get("TARIFF_DELAY_MS", "0")) / 1000.0
    except ValueError:
        return 0.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Silence per-request stderr noise; the request log is the record of truth.
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith(CONTROL_PREFIX):
            return self._control()

        # Record the payload BEFORE responding, so a client that gives up or a
        # server mode that drops the connection still leaves evidence.
        self._record()

        delay = _delay_seconds()
        if delay > 0:
            time.sleep(delay)

        mode = _mode()
        if mode == "reset":
            # Close without writing a response: curl_exec() returns false.
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if mode == "http500":
            return self._send(500, b'{"error":"rate service unavailable"}')
        if mode == "empty":
            return self._send(200, b"")
        if mode == "malformed":
            return self._send(200, b"<html>not json</html>")
        if mode == "no_field":
            return self._send(200, json.dumps({"later": {"v": _value()}}).encode())
        return self._send(200, json.dumps({"now": {"v": _value()}}).encode())

    # Any other verb is still recorded, so an unexpected method shows up in a diff.
    def do_POST(self):
        self._record()
        self._send(405, b'{"error":"method not allowed"}')

    def _record(self):
        line = "{} {}".format(self.command, self.path)
        with open(REQUEST_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def _control(self):
        if self.path == CONTROL_PREFIX + "reset":
            open(REQUEST_LOG, "w", encoding="utf-8").close()
            return self._send(200, b'{"reset":true}')
        if self.path == CONTROL_PREFIX + "requests":
            try:
                with open(REQUEST_LOG, "r", encoding="utf-8") as fh:
                    body = fh.read().encode()
            except FileNotFoundError:
                body = b""
            return self._send(200, body, content_type="text/plain")
        if self.path == CONTROL_PREFIX + "health":
            return self._send(200, b'{"ok":true}')
        return self._send(404, b'{"error":"unknown control endpoint"}')

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def main():
    open(REQUEST_LOG, "w", encoding="utf-8").close()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("tariff mock listening on {}".format(port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
