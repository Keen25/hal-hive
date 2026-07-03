#!/usr/bin/env python3
"""HAL hive smart-HTTP git server.

Serves ~/hive/lab.git (and any bare repo under GIT_PROJECT_ROOT) over Git's
smart-HTTP protocol by invoking the stock ``git-http-backend`` CGI. This exists
because git:// push deadlocks the Git-for-Windows client (sideband/pipe-buffer
half-duplex deadlock in receive-pack); smart-HTTP is fully request/response
framed and does not hit that bug. Clone/fetch/push all go through here.

No auth (host-only LABNet). Streams request/response bodies so large packs are
not buffered in memory. Pure stdlib (Python 3.13+ removed the ``cgi`` module,
so we build the CGI environment by hand).
"""
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GIT_HTTP_BACKEND = os.environ.get(
    "GIT_HTTP_BACKEND", "/usr/libexec/git-core/git-http-backend"
)
PROJECT_ROOT = os.environ.get("GIT_PROJECT_ROOT", os.path.expanduser("~/hive"))
BIND = os.environ.get("GIT_HTTP_BIND", "0.0.0.0")
PORT = int(os.environ.get("GIT_HTTP_PORT", "8418"))
# Chunk size for streaming bodies between socket and the CGI process.
CHUNK = 65536


class GitHTTPHandler(BaseHTTPRequestHandler):
    server_version = "hal-git-http/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._run_cgi("GET")

    def do_POST(self):
        self._run_cgi("POST")

    def _build_env(self, method):
        path = self.path
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
        env = {
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_PROJECT_ROOT": PROJECT_ROOT,
            # Allow git-http-backend to serve a bare repo without tripping the
            # safe.bareRepository=explicit guard inherited from global config.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "all",
            "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "SERVER_SOFTWARE": self.server_version,
            "REQUEST_METHOD": method,
            "QUERY_STRING": query,
            "PATH_INFO": path,
            "PATH_TRANSLATED": os.path.join(PROJECT_ROOT, path.lstrip("/")),
            "REMOTE_ADDR": self.client_address[0],
            "PATH": os.environ.get("PATH", ""),
        }
        ct = self.headers.get("Content-Type")
        if ct:
            env["CONTENT_TYPE"] = ct
        cl = self.headers.get("Content-Length")
        if cl:
            env["CONTENT_LENGTH"] = cl
        # git smart-http passes the service via these headers on some paths.
        gp = self.headers.get("Git-Protocol")
        if gp:
            env["HTTP_GIT_PROTOCOL"] = gp
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        return env

    def _run_cgi(self, method):
        env = self._build_env(method)
        try:
            proc = subprocess.Popen(
                [GIT_HTTP_BACKEND],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            self.send_error(500, "git-http-backend spawn failed: %s" % exc)
            return

        # Stream the request body to the CGI stdin.
        try:
            remaining = int(env.get("CONTENT_LENGTH", "0") or "0")
        except ValueError:
            remaining = 0
        try:
            while remaining > 0:
                buf = self.rfile.read(min(CHUNK, remaining))
                if not buf:
                    break
                proc.stdin.write(buf)
                remaining -= len(buf)
            if proc.stdin:
                proc.stdin.close()
        except BrokenPipeError:
            pass

        # Parse the CGI header block from stdout, then stream the rest.
        header_data = b""
        while b"\r\n\r\n" not in header_data and b"\n\n" not in header_data:
            b = proc.stdout.read(1)
            if not b:
                break
            header_data += b
        sep = b"\r\n\r\n" if b"\r\n\r\n" in header_data else b"\n\n"
        head, _, rest = header_data.partition(sep)

        status = "200 OK"
        headers = []
        for line in head.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                name, value = line.split(b":", 1)
            except ValueError:
                continue
            name = name.decode("latin-1").strip()
            value = value.decode("latin-1").strip()
            if name.lower() == "status":
                status = value
                continue
            headers.append((name, value))

        try:
            code = int(status.split()[0])
        except (ValueError, IndexError):
            code = 200
        self.send_response_only(code, status.split(" ", 1)[-1] if " " in status else "")
        for name, value in headers:
            self.send_header(name, value)
        # We stream an unknown-length body, so use chunked transfer if the CGI
        # did not set Content-Length.
        has_len = any(n.lower() == "content-length" for n, _ in headers)
        use_chunked = not has_len
        if use_chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_body(data):
            if not data:
                return
            if use_chunked:
                self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            else:
                self.wfile.write(data)

        try:
            if rest:
                write_body(rest)
            while True:
                data = proc.stdout.read(CHUNK)
                if not data:
                    break
                write_body(data)
            if use_chunked:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass
        finally:
            err = proc.stderr.read()
            proc.wait()
            if proc.returncode not in (0, None) and err:
                sys.stderr.write("git-http-backend stderr: %s\n" % err.decode("latin-1", "replace"))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))


def main():
    httpd = ThreadingHTTPServer((BIND, PORT), GitHTTPHandler)
    sys.stderr.write(
        "hal-git-http serving %s on http://%s:%d (backend=%s)\n"
        % (PROJECT_ROOT, BIND, PORT, GIT_HTTP_BACKEND)
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
