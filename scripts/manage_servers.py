"""Start and stop this checkout's two local services and private Tailscale Serve route.

Linux only. Invoke through scripts/labctl; service processes run as the invoking
user. The launcher never disconnects Tailscale or resets other Serve routes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
from urllib.parse import urlparse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
BOOT = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
ROLES = ("library", "workspace")
HEALTH = {"pi": "/health", "library": "/health", "workspace": "/api/health"}
SERVICES = {"pi": "grating-pi", "library": "grating-implementations", "workspace": "grating-lab"}


def roles(config):
    return ("pi", *ROLES) if config.get("pi_port") else ROLES


class LauncherError(Exception):
    pass


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def configuration(path, no_llm=False):
    config = read_json(path)
    if not isinstance(config, dict):
        raise LauncherError(f"Configuration file not found: {path}")
    for key in ("directory", "frontend_directory"):
        value = Path(config[key]).expanduser()
        config[key] = str((ROOT / value).resolve())
    # Optional Pi dev profile (an agent directory, e.g. agent-harness/profiles/dev) and
    # the provider-key file read only by the Pi harness process.
    if config.get("pi_profile"):
        config["pi_profile"] = str((ROOT / Path(config["pi_profile"]).expanduser()).resolve())
        if not (Path(config["pi_profile"]) / "settings.json").is_file():
            raise LauncherError(f"Pi profile has no settings.json: {config['pi_profile']}")
        config["secrets_file"] = str(Path(config.get("secrets_file") or "~/.config/balsamic/secrets.env").expanduser())
    # Optional paper PDF copied into development workspaces as /references/paper.pdf.
    if config.get("paper_reference") is not None:
        if not isinstance(config["paper_reference"], str) or not config["paper_reference"].strip():
            raise LauncherError("paper_reference must be a nonempty path or null.")
        config["paper_reference"] = str((ROOT / Path(config["paper_reference"]).expanduser()).resolve())
    ports = [config["workspace_port"], config["implementation_port"]]
    if config.get("pi_port") is not None:
        ports.append(config["pi_port"])
    if config.get("tailnet_port") is not None:
        ports.append(config["tailnet_port"])
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != len(ports):
        raise LauncherError("Choose distinct ports between 1024 and 65535; tailnet_port may be null.")
    if type(config.get("llm_enabled")) is not bool or type(config.get("workers")) is not int or config["workers"] < 1:
        raise LauncherError("llm_enabled must be a boolean and workers a positive integer.")
    if config.get("provider") not in {"codex", "openai_api", "compatible"} or not config.get("model"):
        raise LauncherError("Choose a supported provider and a nonempty model.")
    if "codex_timeout_seconds" in config and (type(config["codex_timeout_seconds"]) not in {int, float}
            or not 5 <= config["codex_timeout_seconds"] <= 600):
        raise LauncherError("codex_timeout_seconds must be between 5 and 600 seconds.")
    if type(config.get("development_enabled", False)) is not bool:
        raise LauncherError("development_enabled must be a boolean.")
    if config.get("development_public_url"):
        parsed = urlparse(str(config["development_public_url"]))
        # Tailscale's authenticated HTTP listener is encrypted on the TailNet
        # even when the browser-facing URL itself is not HTTPS.
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and
                (parsed.hostname or "").endswith(".ts.net")) or not parsed.netloc or parsed.path not in {"", "/"}):
            raise LauncherError("development_public_url must be HTTPS or a TailNet .ts.net HTTP URL.")
    if no_llm:
        config["llm_enabled"] = False
    return config


def port_for(config, role):
    return config[{"library": "implementation_port", "workspace": "workspace_port", "pi": "pi_port"}[role]]


def process_identity(pid):
    """Birth identity, including reboot protection; zombies count as stopped."""
    try:
        proc = Path(f"/proc/{pid}")
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z" or proc.stat().st_uid != os.getuid():
            return None
        return {"pid": pid, "start_ticks": fields[19], "boot_id": BOOT}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record):
    return bool(record and process_identity(record["pid"]) == record)


def process_handle(pid):
    # The uv-managed Python build can omit os.pidfd_open despite kernel/libc
    # support. Use libc's typed wrapper in that case, never a numeric-PID signal.
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        call = libc.pidfd_open
    except AttributeError:
        raise LauncherError("This launcher needs Linux pidfd support (Python or glibc 2.36+).") from None
    call.argtypes, call.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    result = call(pid, 0)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def signal_handle(fd):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.pidfd_send_signal
    call.argtypes, call.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int
    if call(fd, signal.SIGTERM, None, 0) < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def stop_process(record, timeout=30):
    if not alive(record):
        return
    # Bind the signal to the process before rechecking identity, so PID reuse
    # between the check and signal can never terminate an unrelated process.
    try:
        fd = process_handle(record["pid"])
    except ProcessLookupError:
        return
    try:
        if not alive(record):
            return
        signal_handle(fd)
    except ProcessLookupError:
        return
    finally:
        os.close(fd)
    deadline = time.monotonic() + timeout
    while alive(record) and time.monotonic() < deadline:
        time.sleep(.1)
    if alive(record):
        raise LauncherError(f"PID {record['pid']} is still shutting down. Kept its record; retry stop. No forced kill was sent.")


def port_free(port):
    with socket.socket() as sock:
        # Match Uvicorn: completed HTTP connections may leave TIME_WAIT sockets
        # after a clean stop, but those must not prevent an immediate restart.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def healthy(config, role):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port_for(config, role)}{HEALTH[role]}", timeout=1) as response:
            value = json.load(response)
        return value.get("status") == "ok" and value.get("service") == SERVICES[role]
    except (OSError, ValueError, urllib.error.URLError):
        return False


def service_environment(config):
    env = dict(os.environ)
    env.update(GRATING_LLM_PROVIDER=config["provider"], GRATING_LLM_MODEL=config["model"],
               GRATING_LLM_ENABLED=str(config["llm_enabled"]).lower(),
               GRATING_LLM_DISABLED=str(not config["llm_enabled"]).lower(),
               GRATING_IMPLEMENTATIONS_URL=f"http://127.0.0.1:{config['implementation_port']}",
               GRATING_IMPLEMENTATIONS_TOKEN_FILE=str(Path(config["directory"]) / "library/service.token"),
               GRATING_DEVELOPMENT_ENABLED=str(config.get("development_enabled", False)).lower(),
               GRATING_DEVELOPMENT_PUBLIC_URL=config.get("development_public_url", ""),
               PYTHONUNBUFFERED="1")
    if "codex_timeout_seconds" in config:
        env["GRATING_CODEX_TIMEOUT_SECONDS"] = str(config["codex_timeout_seconds"])
    if config.get("paper_reference"):
        env["GRATING_PAPER_REFERENCE"] = config["paper_reference"]
    # The token file must identify this library, even if another service's token
    # happens to be present in the calling shell.
    env.pop("GRATING_IMPLEMENTATIONS_TOKEN", None)
    if config.get("pi_port"):
        env.update(GRATING_PI_PORT=str(config["pi_port"]), GRATING_PI_URL=f"http://127.0.0.1:{config['pi_port']}",
            GRATING_PI_WORKSPACE_URL=f"http://127.0.0.1:{config['workspace_port']}",
            GRATING_PI_DIRECTORY=str(Path(config["directory"]) / "pi"),
            GRATING_PI_TOKEN_FILE=str(Path(config["directory"]) / "pi/service.token"))
        if config.get("pi_profile"):
            # Every service learns dev mode and its profile defaults; keys go only to the harness.
            env["GRATING_PI_PROFILE"] = config["pi_profile"]
    return env


def check_model(config):
    if config.get("pi_port") or not config["llm_enabled"] or config["provider"] != "codex":
        return
    binary = shutil.which(os.environ.get("GRATING_CODEX_BINARY", "codex"))
    if not binary:
        raise LauncherError("Codex is not installed/on PATH. Set GRATING_CODEX_BINARY or use --no-llm.")
    # Same credential environment as the application adapter; never print login
    # output, which might contain account or API credential details.
    from optimization_framework.research.codex_provider import _environment
    result = subprocess.run([binary, "login", "status"], env=_environment(),
                            capture_output=True, text=True, timeout=15)
    if result.returncode or not any(line.strip() == "Logged in using ChatGPT"
                                   for line in (result.stdout + "\n" + result.stderr).splitlines()):
        raise LauncherError("Sign in as this OS user with `codex login --device-auth`, then retry. ChatGPT login is required; --no-llm runs without model calls.")


class Tailnet:
    def __init__(self, port, target):
        self.port, self.target = port, target

    def query(self, *args):
        if not shutil.which("tailscale"):
            raise LauncherError("Install/connect Tailscale, or set tailnet_port to null for local access.")
        result = subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise LauncherError(f"Tailscale query failed: {result.stderr.strip()}")
        return json.loads(result.stdout or "{}")

    def route(self):
        config = self.query("serve", "status", "--json") or {}
        sections = [config, *(config.get("Foreground") or {}).values()]
        if any(enabled and key.endswith(f":{self.port}") for section in sections
               for key, enabled in (section.get("AllowFunnel") or {}).items()):
            raise LauncherError(f"Tailnet port {self.port} has Funnel enabled. Leaving it unchanged.")
        if any(str(self.port) in (section.get("TCP") or {}) for section in sections[1:]):
            raise LauncherError(f"Tailnet port {self.port} belongs to a foreground Serve process. Leaving it unchanged.")
        routes = [(key, value) for section in sections for key, value in (section.get("Web") or {}).items()
                  if key.endswith(f":{self.port}")]
        listeners = [(section.get("TCP") or {}).get(str(self.port)) for section in sections
                     if str(self.port) in (section.get("TCP") or {})]
        expected = {"Handlers": {"/": {"Proxy": self.target}}}
        if not routes and not listeners:
            return None
        if len(routes) != 1 or routes[0][1] != expected or listeners != [{"HTTPS": True}]:
            raise LauncherError(f"Tailnet port {self.port} has a different/shared/public route. Leaving it unchanged.")
        return routes[0][0]

    def mutate(self, *, off=False):
        command = ["tailscale", "serve", "--bg", f"--https={self.port}", self.target]
        if off:
            command.append("off")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode and ("access denied" in (result.stdout + result.stderr).lower()
                                  or "permission denied" in result.stderr.lower()):
            elevated = ["sudo", *command]
            print("Tailscale needs permission for this app's route: " + shlex.join(elevated), flush=True)
            if not sys.stdin.isatty():
                raise LauncherError("Run the command above in a terminal, then retry labctl. Local service state is retained.")
            if subprocess.run(elevated).returncode:
                raise LauncherError("Could not change the Tailnet route. Local service state is retained.")
        elif result.returncode:
            raise LauncherError(f"Could not change the Tailnet route: {result.stderr.strip() or result.stdout.strip()}")

    def enable(self):
        status = self.query("status", "--json")
        if status.get("BackendState") != "Running":
            raise LauncherError("Tailscale is disconnected. Connect it on the host, then retry start.")
        host = status.get("Self", {}).get("DNSName", "").rstrip(".")
        if not host:
            raise LauncherError("Tailscale did not report a DNS name for this machine.")
        if self.route() is None:
            self.mutate()
        route = self.route()
        if route is None:
            raise LauncherError("Tailscale did not retain the requested route; retry start.")
        return f"https://{route}"

    def disable(self):
        if self.route() is not None:
            self.mutate(off=True)
        if self.route() is not None:
            raise LauncherError("The Tailnet route is still present; retry stop.")


def fixture_processes(config):
    """Only the exact old fixture launcher, checkout, data directory and ports."""
    found = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
            cwd = (proc / "cwd").resolve()
            if len(args) < 4 or (cwd / args[1]).resolve() != ROOT / "scripts/serve_commissioning_fixture.py":
                continue
            role = args[2]
            if role not in roles(config):
                continue
            directory = (cwd / args[args.index("--directory") + 1]).resolve()
            port = int(args[args.index("--port") + 1])
            if directory == Path(config["directory"]) and port == port_for(config, role):
                identity = process_identity(int(proc.name))
                if identity:
                    if role in found:
                        raise LauncherError(f"Multiple matching {role} fixtures; stop them manually.")
                    found[role] = identity
        except (OSError, ValueError, IndexError):
            continue
    return found


class Servers:
    def __init__(self, config):
        self.config = config
        self.runtime = Path(config["directory"]) / ".server-control"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.state_file = self.runtime / "state.json"
        self.state = read_json(self.state_file, {"processes": {}})

    def save(self):
        save_json(self.state_file, self.state)

    @contextmanager
    def locked(self):
        with (self.runtime / "control.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LauncherError("Another labctl operation is in progress; retry when it finishes.") from None
            self.state = read_json(self.state_file, {"processes": {}})
            yield

    def launch(self, role):
        log = self.runtime / f"{role}.log"
        with log.open("ab") as stream:
            process = subprocess.Popen([sys.executable, str(SCRIPT), "_serve", role,
                                        "--config", str(self.runtime / "active-config.json")],
                                       cwd=ROOT, env=service_environment(self.config),
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        record = process_identity(process.pid)
        if not record:
            raise LauncherError(f"{role} exited during startup; inspect {log}")
        self.state["processes"][role] = record
        self.save()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LauncherError(f"{role} exited during startup; inspect {log}")
            if healthy(self.config, role):
                print(f"{role}: ready on 127.0.0.1:{port_for(self.config, role)} (PID {process.pid})", flush=True)
                return
            time.sleep(.2)
        raise LauncherError(f"{role} did not become healthy; inspect {log}")

    def start(self, replace_fixture=False):
        os.close(process_handle(os.getpid()))
        living = {role: record for role, record in self.state["processes"].items() if alive(record)}
        if (living or self.state.get("tailnet")) and self.state.get("config") != self.config:
            raise LauncherError("Configuration changed. Use restart to apply it.")
        frontend = Path(self.config["frontend_directory"])
        if not (frontend / "index.html").is_file() or not (frontend / "assets").is_dir():
            raise LauncherError("Frontend index.html/assets are missing. Build the frontend and set frontend_directory in the config.")
        check_model(self.config)
        if self.config.get("pi_port"):
            if not shutil.which("node") or not (ROOT / "agent-harness/dist/server.js").is_file():
                raise LauncherError("Install Node.js and run npm ci && npm run build in agent-harness first.")
            import secrets
            directory = Path(self.config["directory"]) / "pi"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            token = directory / "service.token"
            if not token.exists():
                with token.open("x") as stream:
                    stream.write(secrets.token_hex(32) + "\n")
                token.chmod(0o600)
        tailnet = None
        if self.config.get("tailnet_port"):
            tailnet = Tailnet(self.config["tailnet_port"], f"http://127.0.0.1:{self.config['workspace_port']}")
            # Check collisions before stopping a fixture or starting processes.
            tailnet.route()
        fixtures = fixture_processes(self.config) if replace_fixture else {}
        for role in roles(self.config):
            if role not in living and role not in fixtures and not port_free(port_for(self.config, role)):
                raise LauncherError(f"Port {port_for(self.config, role)} is already in use. For the old review fixture use start --replace-fixture; other servers must be stopped by their owner.")
        for role in reversed(roles(self.config)):
            if role in fixtures:
                print(f"Stopping the old {role} fixture (PID {fixtures[role]['pid']})", flush=True)
                stop_process(fixtures[role])
        self.state["config"] = self.config
        self.state["processes"] = living
        save_json(self.runtime / "active-config.json", self.config)
        self.save()
        started = []
        try:
            for role in roles(self.config):
                if role not in living:
                    started.append(role)
                    self.launch(role)
                elif not healthy(self.config, role):
                    raise LauncherError(f"Managed {role} is running but unhealthy. Inspect logs or use restart.")
        except (Exception, KeyboardInterrupt):
            for role in reversed(started):
                record = self.state["processes"].get(role)
                if record:
                    stop_process(record)
                    self.state["processes"].pop(role, None)
                    self.save()
            raise
        print(f"Local UI: http://127.0.0.1:{self.config['workspace_port']}", flush=True)
        print(f"Models: {self.config['provider']} / {self.config['model']} / "
              + ("enabled" if self.config["llm_enabled"] else "disabled"), flush=True)
        if tailnet:
            # Persist intent before changing Serve so a lost reply is recoverable.
            self.state["tailnet"] = {"port": tailnet.port, "target": tailnet.target}
            self.save()
            url = tailnet.enable()
            self.state["tailnet"]["url"] = url
            self.save()
            print(f"Tailnet UI: {url}", flush=True)
        print(f"Logs: {self.runtime}", flush=True)

    def stop(self):
        errors = []
        if route := self.state.get("tailnet"):
            try:
                Tailnet(route["port"], route["target"]).disable()
                self.state.pop("tailnet")
                self.save()
                print("This app's Tailnet route is off.", flush=True)
            except (LauncherError, OSError, subprocess.TimeoutExpired) as error:
                errors.append(str(error))
        for role in reversed(roles(self.state.get("config") or self.config)):
            record = self.state["processes"].get(role)
            if not record:
                continue
            try:
                stop_process(record)
                self.state["processes"].pop(role)
                self.save()
                print(f"{role}: stopped", flush=True)
            except LauncherError as error:
                errors.append(str(error))
                # Keep the library available until the workspace has shut down.
                break
        if errors:
            raise LauncherError("\n".join(errors))
        print("Managed servers stopped. Campaigns, library artifacts and logs are retained.", flush=True)

    def status(self):
        fixtures = fixture_processes(self.config)
        for role in roles(self.config):
            record = self.state["processes"].get(role)
            if alive(record):
                label = "healthy" if healthy(self.state["config"], role) else "unhealthy"
                print(f"{role}: {label}, PID {record['pid']}")
            elif role in fixtures:
                print(f"{role}: old review fixture, PID {fixtures[role]['pid']} (use start --replace-fixture)")
            else:
                print(f"{role}: not managed/running" + ("; configured port is occupied" if not port_free(port_for(self.config, role)) else ""))
        route = self.state.get("tailnet")
        if route:
            name = Tailnet(route["port"], route["target"]).route()
            print(f"Tailnet: https://{name}" if name else "Tailnet: route is off")
        else:
            print("Tailnet: no route managed by labctl yet")
        print(f"Data: {self.config['directory']}\nLogs: {self.runtime}")


def read_secrets(path):
    """KEY=VALUE lines (optionally quoted); comments and blank lines are ignored."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def serve(role, config):
    if role == "pi":
        env = service_environment(config)
        if config.get("pi_profile"):
            if Path(config["secrets_file"]).is_file():
                env.update(read_secrets(config["secrets_file"]))
        os.execvpe("node", ["node", str(ROOT / "agent-harness/dist/server.js")], env)
    import uvicorn
    directory = Path(config["directory"])
    if role == "library":
        from optimization_framework.implementations.api import create_app
        app = create_app(directory / "library")
    else:
        from optimization_framework.api.app import create_app
        app = create_app(directory / "workspace", max_workers=config["workers"],
                         frontend_directory=config["frontend_directory"])
    uvicorn.run(app, host="127.0.0.1", port=port_for(config, role), timeout_graceful_shutdown=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "restart", "status", "logs", "_serve"))
    parser.add_argument("role", nargs="?", choices=("pi", *ROLES), help="Optional service name for logs")
    parser.add_argument("--config", type=Path, default=ROOT / "deploy/review-servers.json")
    parser.add_argument("--no-llm", action="store_true", help="Start with all model calls disabled")
    parser.add_argument("--replace-fixture", action="store_true", help="Replace only matching old review fixture processes")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run labctl as your normal user, not with sudo. Only Tailscale route changes may request sudo.")
    os.umask(0o077)
    try:
        config = configuration(args.config, args.no_llm)
        if args.command == "_serve":
            if args.role is None:
                parser.error("_serve needs a role")
            serve(args.role, config)
            return 0
        servers = Servers(config)
        if args.command == "logs":
            paths = [servers.runtime / f"{role}.log" for role in ([args.role] if args.role else roles(config))]
            paths = [path for path in paths if path.exists()]
            if not paths:
                raise LauncherError("No logs yet; start the services first.")
            os.execvp("tail", ["tail", "-n", "60", "-F", *map(str, paths)])
        with servers.locked():
            if args.command in {"stop", "restart"}:
                servers.stop()
            if args.command in {"start", "restart"}:
                servers.start(args.replace_fixture)
            if args.command == "status":
                servers.status()
        return 0
    except (LauncherError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"labctl: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Run labctl status; rerun start/stop to finish the operation.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
