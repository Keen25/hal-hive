#!/usr/bin/env python3
"""HAL swarm smart-HTTP git server.

Serves ~/hive/lab.git (and any bare repo under GIT_PROJECT_ROOT) over Git's
smart-HTTP protocol by invoking the stock ``git-http-backend`` CGI. This exists
because git:// push deadlocks the Git-for-Windows client (sideband/pipe-buffer
half-duplex deadlock in receive-pack); smart-HTTP is fully request/response
framed and does not hit that bug. Clone/fetch/push all go through here.

Token auth (shared with the queue control plane): every request must present
``Authorization: Bearer <token>`` (or HTTP Basic where the password equals the
token). Workers inject the header with ``git -c http.extraheader=...``. The token
is resolved from ``HIVE_GIT_TOKEN`` → ``HIVE_TOKEN`` → ``~/.hive/worker.token`` —
the same secret the REST server uses — so the LAN is no longer an implicit trust
boundary. (``GIT_HTTP_EXPORT_ALL`` stays on so git-http-backend serves the bare
repo; the HTTP auth layer above is the real gate now.) If NO token can be
resolved the server refuses to start rather than silently running open.

Streams request/response bodies so large packs are not buffered in memory. Pure
stdlib (Python 3.13+ removed the ``cgi`` module, so we build the CGI environment
by hand).
"""
import base64
import hmac
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _default_backend() -> str:
    """Locate git-http-backend on any OS (Linux/macOS/Windows).

    The CGI lives in git's exec-path, which differs per platform/install
    (Fedora `/usr/libexec/git-core`, Homebrew `/opt/homebrew/.../git-core`,
    Git-for-Windows `…\\mingw64\\libexec\\git-core\\git-http-backend.exe`). Ask
    git itself via `git --exec-path` rather than hardcoding, then fall back to a
    PATH lookup, then the historical Linux default.
    """
    exe = "git-http-backend.exe" if os.name == "nt" else "git-http-backend"
    try:
        ep = subprocess.run(["git", "--exec-path"], capture_output=True,
                            text=True, timeout=5)
        if ep.returncode == 0 and ep.stdout.strip():
            cand = os.path.join(ep.stdout.strip(), exe)
            if os.path.isfile(cand):
                return cand
    except (OSError, subprocess.SubprocessError):
        pass
    found = shutil.which(exe)
    if found:
        return found
    return "/usr/libexec/git-core/" + exe


GIT_HTTP_BACKEND = os.environ.get("GIT_HTTP_BACKEND") or _default_backend()
PROJECT_ROOT = os.environ.get("GIT_PROJECT_ROOT", os.path.expanduser("~/hive"))
BIND = os.environ.get("GIT_HTTP_BIND")  # None -> dual-bind localhost + lab NIC (S2)
PORT = int(os.environ.get("GIT_HTTP_PORT", "8418"))
# S6 (#9): GIT_HTTP_EXPORT_ALL + GIT_PROJECT_ROOT=~/hive would let git-http-backend
# serve ANY bare repo under ~/hive. The hive only needs lab.git, so route only
# requests whose first path segment is an allowed repo (comma-sep override).
ALLOWED_REPOS = {r.strip() for r in
                 os.environ.get("GIT_HTTP_REPOS", "lab.git").split(",") if r.strip()}
# Chunk size for streaming bodies between socket and the CGI process.
CHUNK = 65536

TOKEN_FILE = os.path.expanduser("~/.hive/worker.token")


def _resolve_token() -> str:
    """Shared swarm token — same precedence as queue_server so both agree."""
    for key in ("HIVE_GIT_TOKEN", "HIVE_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    return ""


AUTH_TOKEN = _resolve_token()


class GitHTTPHandler(BaseHTTPRequestHandler):
    server_version = "hal-git-http/1.0"
    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        """True iff the request carries the shared token (Bearer or Basic)."""
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer "):
            return hmac.compare_digest(hdr[7:].strip(), AUTH_TOKEN)
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:].strip()).decode("utf-8", "replace")
            except Exception:
                return False
            user, _, pw = raw.partition(":")
            # Accept the token as either the username or the password so both
            # `https://<token>@host/` and `https://x:<token>@host/` URL forms work.
            return (hmac.compare_digest(pw, AUTH_TOKEN)
                    or hmac.compare_digest(user, AUTH_TOKEN))
        return False

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        body = b"401 unauthorized: swarm git transport requires the shared token\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="hal-hive-git"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass
        return False

    def _repo_allowed(self) -> bool:
        """S6 (#9): only serve requests under an allowed repo. Reject path
        traversal and any repo not in ALLOWED_REPOS with 404 (don't confirm what
        else exists under PROJECT_ROOT)."""
        path = self.path.split("?", 1)[0]
        segs = [s for s in path.split("/") if s]
        if not segs or ".." in segs or segs[0] not in ALLOWED_REPOS:
            body = b"404 not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
            return False
        return True

    def do_GET(self):
        if not self._require_auth():
            return
        if not self._repo_allowed():
            return
        self._run_cgi("GET")

    def do_POST(self):
        if not self._require_auth():
            return
        if not self._repo_allowed():
            return
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
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            # A chunked request has no up-front length. Do NOT set CONTENT_LENGTH:
            # git-http-backend then reads its stdin to EOF. We de-chunk the body
            # ourselves (self.rfile is the raw socket and is not auto-dechunked)
            # before writing to the backend's stdin.
            pass
        elif cl:
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

        # Stream the request body to the CGI stdin on a background thread.
        # This MUST be concurrent with reading stdout: git-http-backend
        # (receive-pack) starts writing its response while still consuming the
        # request pack, so its stdout pipe fills at ~64 KB. If we drained stdin
        # fully before reading stdout, the backend would block on its stdout
        # write while we block on our stdin write -> a pipe deadlock that aborts
        # the connection (seen as "curl 55 Send failure: Broken pipe" on any
        # push larger than one pipe buffer). A dedicated pump thread keeps both
        # directions flowing.
        try:
            content_length = int(env.get("CONTENT_LENGTH", "0") or "0")
        except ValueError:
            content_length = 0
        is_chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()

        def pump_fixed(remaining):
            while remaining > 0:
                buf = self.rfile.read(min(CHUNK, remaining))
                if not buf:
                    break
                proc.stdin.write(buf)
                remaining -= len(buf)

        def pump_chunked():
            # Decode HTTP/1.1 chunked transfer-encoding from the raw socket:
            # <hex-size>[;ext]\r\n <size bytes> \r\n ... 0\r\n <trailers> \r\n
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size_field = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_field, 16)
                except ValueError:
                    break
                if size == 0:
                    # Consume trailer lines up to the terminating blank line.
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                remaining = size
                while remaining > 0:
                    buf = self.rfile.read(min(CHUNK, remaining))
                    if not buf:
                        break
                    proc.stdin.write(buf)
                    remaining -= len(buf)
                self.rfile.read(2)  # trailing CRLF after each chunk

        def pump_stdin():
            try:
                if is_chunked:
                    pump_chunked()
                else:
                    pump_fixed(content_length)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        stdin_thread = threading.Thread(target=pump_stdin, daemon=True)
        stdin_thread.start()

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
            stdin_thread.join(timeout=5)
            err = proc.stderr.read()
            proc.wait()
            if proc.returncode not in (0, None) and err:
                sys.stderr.write("git-http-backend stderr: %s\n" % err.decode("latin-1", "replace"))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))


def _resolve_bind_addrs(override, default_lab_ip="192.168.100.1"):
    """127.0.0.1 (HAL-local) + the lab-net NIC, NOT the corp interface, unless
    GIT_HTTP_BIND overrides (comma-separated; '0.0.0.0' to bind all). Lab IP from
    HIVE_LAB_IP. Absent addresses are skipped at bind time. (S2 #5)"""
    if override:
        return [h.strip() for h in override.split(",") if h.strip()]
    lab = os.environ.get("HIVE_LAB_IP", default_lab_ip).strip()
    addrs = ["127.0.0.1"]
    if lab and lab not in addrs:
        addrs.append(lab)
    return addrs


def _tls_context():
    """ssl.SSLContext when HIVE_TLS_CERT and HIVE_TLS_KEY are both set, else None
    (plain HTTP). Encrypts git packs + the token header (issue #8 / S5). Mint the
    cert/key with gen-lab-ca.sh; the same pair is shared with queue_server."""
    cert = os.environ.get("HIVE_TLS_CERT")
    key = os.environ.get("HIVE_TLS_KEY")
    if not (cert and key):
        return None
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def _enable_partial_clone():
    """Partial clone (`git clone --filter=blob:none`) needs uploadpack.allowFilter
    set on the SERVER repo (Sc4 #13). Set it idempotently on each served bare repo
    at startup so workers can blobless-clone lab.git; full-clone cost otherwise
    scales with repo size × task count. Survives fresh seeds/redeploys.

    Writes the bare repo's config file directly (`--file <repo>/config`) rather
    than `git -C <repo> config`: a global `safe.bareRepository=explicit` makes the
    latter fail with "not in a git directory" on a bare repo."""
    for repo in ALLOWED_REPOS:
        path = os.path.join(PROJECT_ROOT, repo)
        cfg = os.path.join(path, "config")
        if not os.path.isfile(cfg):
            continue
        try:
            subprocess.run(
                ["git", "config", "--file", cfg, "uploadpack.allowFilter", "true"],
                check=False, capture_output=True,
            )
        except Exception as exc:  # noqa: BLE001 — never let this block serving
            sys.stderr.write("warn: allowFilter on %s failed (%s)\n" % (path, exc))


def main():
    if not AUTH_TOKEN:
        sys.stderr.write(
            "FATAL: no hive token resolvable (HIVE_GIT_TOKEN / HIVE_TOKEN / "
            "~/.hive/worker.token). Refusing to serve lab.git without auth.\n"
        )
        sys.exit(1)
    _enable_partial_clone()
    addrs = _resolve_bind_addrs(BIND)
    ctx = _tls_context()
    scheme = "https" if ctx else "http"
    if not ctx:
        sys.stderr.write(
            "TLS OFF — set HIVE_TLS_CERT/HIVE_TLS_KEY to encrypt (issue #8)\n"
        )
    servers = []
    for addr in addrs:
        try:
            httpd = ThreadingHTTPServer((addr, PORT), GitHTTPHandler)
        except OSError as exc:
            sys.stderr.write("skip bind %s:%d (%s)\n" % (addr, PORT, exc))
            continue
        if ctx:
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        servers.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        sys.stderr.write(
            "hal-git-http serving %s on %s://%s:%d (backend=%s, token-auth ON)\n"
            % (PROJECT_ROOT, scheme, addr, PORT, GIT_HTTP_BACKEND)
        )
    if not servers:
        sys.stderr.write("FATAL: could not bind any of %r:%d\n" % (addrs, PORT))
        sys.exit(1)
    threading.Event().wait()


if __name__ == "__main__":
    main()
