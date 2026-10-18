"""Client — shared client-server HTTP plumbing for the Parley clients.

Both the CLI (`parley.py`) and the desktop app (`parley_desktop.py`) talk to the
same `/v1/*` server. This base holds the HTTP helper, the server-up probe,
base-URL resolution from a config, the local-daemon spawn ("if the local server
isn't open, open it"), and the high-level calls — so each client only adds its
own UX (the CLI: one instruction; the desktop: an accept/deny popup loop).
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from parley.client_server.config import PARLEY_HOME, load_server

# src/parley/client_server/client.py → the import root (src/) and the repo root,
# used to spawn the daemon as `python -m parley` regardless of an editable install.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO_ROOT = os.path.dirname(_SRC_DIR)


class Client:
    """A client of one Parley server. Give it a `base` URL directly, or a
    `config_path` and call `ensure_server()` to resolve the base (and spawn a
    local daemon if it isn't up yet)."""

    def __init__(self, base=None, config_path=None):
        self.base = base.rstrip("/") if base else None
        self.config_path = config_path

    # -- HTTP -----------------------------------------------------------------

    @staticmethod
    def _http(url, method="GET", body=None, timeout=5):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    @classmethod
    def _server_up(cls, base, timeout=2):
        try:
            cls._http(base + "/v1/status", timeout=timeout)
            return True
        except (urllib.error.URLError, OSError):
            return False

    @staticmethod
    def _is_loopback(host):
        return host in ("127.0.0.1", "localhost", "::1")

    # -- Lifecycle: resolve the base, spawn a local daemon if needed ----------

    @staticmethod
    def _parley_launcher():
        """This environment's installed `parley` launcher, if any. Spawning the
        daemon through it makes Task Manager show `parley.exe` (with the cfg on
        the Command line column) instead of an anonymous `python.exe`. Resolved
        via sysconfig — the CURRENT interpreter's Scripts dirs only — so a stray
        `parley` from another install on PATH can never hijack the spawn."""
        import sysconfig
        exe = "parley.exe" if os.name == "nt" else "parley"
        schemes = [None, "nt_user" if os.name == "nt" else "posix_user"]
        for scheme in schemes:
            try:
                scripts = (sysconfig.get_path("scripts", scheme) if scheme
                           else sysconfig.get_path("scripts"))
            except KeyError:
                continue
            cand = os.path.join(scripts, exe)
            if scripts and os.path.exists(cand):
                return cand
        return None

    def _spawn_daemon(self, host, port):
        """Start a local Parley server detached, logging to ~/.parley/logs/, and wait
        for it to come up."""
        log_dir = os.path.join(PARLEY_HOME, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"server-{port}.log")
        print(f"Starting local Parley server on {host}:{port} (log: {log_path}) …")
        log = open(log_path, "a", encoding="utf-8")
        # Prefer the installed launcher (identifiable in Task Manager); fall back to
        # `python -m parley` from the REPO ROOT with PYTHONPATH=src so the spawn also
        # works with no install at all. (Config/state paths are absolute or resolve
        # against ~/.parley, so they don't depend on the cwd.)
        launcher = self._parley_launcher()
        if launcher:
            cmd = [launcher, "--serve", "--config-file", self.config_path]
        else:
            cmd = [sys.executable, "-m", "parley", "--serve", "--config-file", self.config_path]
        env = dict(os.environ)
        env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
        # The child inherits the log file DESCRIPTOR, not the encoding we opened it
        # with, so on Windows it would pick cp1252 for stdout and raise on every
        # emoji the journal echoes. Say utf-8 explicitly.
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs = {}
        if sys.platform == "win32":
            # parley.exe is a CONSOLE executable: spawned from a console-less
            # parent (the tray app, an autostart shortcut) Windows would pop a
            # black console window for it. All output goes to the log anyway.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            cmd,
            cwd=_REPO_ROOT, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            **kwargs,
        )
        base = f"http://{host}:{port}"
        for _ in range(60):  # up to ~30s; first run waits on OAuth in the browser
            if self._server_up(base):
                return True
            time.sleep(0.5)
        print(f"  server didn't come up in time — check {log_path} "
              "(first run needs you to finish the OAuth sign-in in your browser).")
        return False

    def ensure_server(self):
        """Resolve the base URL from `config_path`; if a *local* server isn't up,
        spawn it. Sets and returns `self.base`."""
        host, port = load_server(self.config_path)
        base = f"http://{host}:{port}"
        if self._server_up(base):
            self.base = base
            return base
        if not self._is_loopback(host):
            raise SystemExit(f"Remote Parley server {base} is unreachable (won't auto-start a remote).")
        if not self._spawn_daemon(host, port):
            raise SystemExit("Could not start the local server.")
        self.base = base
        return base

    # -- High-level calls (need `self.base`) ----------------------------------

    def status(self):
        return self._http(self.base + "/v1/status")

    def pending(self, timeout=3):
        return self._http(self.base + "/v1/pending", timeout=timeout)

    # These run the brain's LLM tool-loop synchronously on the server, so they can take
    # tens of seconds — wait well past the 5s default (topic 1).
    BRAIN_TIMEOUT = 180

    def instruct(self, text):
        return self._http(self.base + "/v1/instruct", method="POST", body={"text": text},
                          timeout=self.BRAIN_TIMEOUT)

    def pending(self):
        """The questions this agent is waiting on a human to answer."""
        return self._http(self.base + "/v1/pending", timeout=10)

    def policy(self, rule=None):
        """No rule: the policy the brain actually runs on. With one: add it as
        standing guidance (see ParleyServer.policy)."""
        if rule:
            return self._http(self.base + "/v1/policy", method="POST",
                              body={"rule": rule}, timeout=30)
        return self._http(self.base + "/v1/policy", timeout=10)

    def consult(self, payload):
        """Run one READ-ONLY verb for context while helping the human decide. The
        daemon enforces which verbs those are; see ParleyServer.consult."""
        return self._http(self.base + "/v1/consult", method="POST", body=payload,
                          timeout=120)

    def negotiate(self, payload):
        """Path A: hand the daemon a negotiation the caller already researched.
        See ParleyServer.negotiate - Claude discovers, Parley commits."""
        return self._http(self.base + "/v1/negotiate", method="POST", body=payload,
                          timeout=self.BRAIN_TIMEOUT)

    def decide(self, negotiation_id, choice):
        return self._http(self.base + "/v1/decide", method="POST",
                          body={"negotiation_id": negotiation_id, "choice": choice},
                          timeout=self.BRAIN_TIMEOUT)

    def focus(self, payload):
        """Dynamic Focus Blocks: {action: create|list|cancel, ...}."""
        return self._http(self.base + "/v1/focus", method="POST", body=payload, timeout=120)

    def dismiss(self, negotiation_id, reason=None):
        """Close a question without answering it — the brain is not woken."""
        return self._http(self.base + "/v1/dismiss", method="POST",
                          body={"negotiation_id": negotiation_id, "reason": reason or ""},
                          timeout=10)

    def answer(self, negotiation_id, choice):
        return self._http(self.base + "/v1/answer", method="POST",
                          body={"negotiation_id": negotiation_id, "choice": choice},
                          timeout=self.BRAIN_TIMEOUT)

    def stop(self):
        return self._http(self.base + "/v1/shutdown", method="POST", body={})
