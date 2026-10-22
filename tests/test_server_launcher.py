"""Real service lifecycle, ownership protection and isolated Serve configuration."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/manage_servers.py"
spec = importlib.util.spec_from_file_location("manage_servers", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def free_ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    directory = tmp_path / "data with spaces"
    frontend = tmp_path / "frontend with spaces"
    frontend.mkdir()
    (frontend / "assets").mkdir()
    (frontend / "index.html").write_text("<html>Launcher test</html>")
    library, workspace, tailnet = free_ports()
    config = dict(directory=str(directory), frontend_directory=str(frontend), implementation_port=library,
                  workspace_port=workspace, tailnet_port=tailnet, workers=1,
                  provider="codex", model="gpt-6-sol", llm_enabled=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    fake = tmp_path / "bin"
    fake.mkdir()
    tailnet_state = tmp_path / "tailscale.json"
    tailnet_state.write_text(json.dumps({"TCP": {"8443": {"HTTPS": True}}, "Web": {
        "test.tail.example:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}}))
    tool = fake / "tailscale"
    tool.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = Path(os.environ["FAKE_TAILSCALE_STATE"])
state = json.loads(path.read_text())
if args == ["status", "--json"]:
    print(json.dumps({"BackendState": "Running", "Self": {"DNSName": "test.tail.example."}}))
elif args == ["serve", "status", "--json"]:
    print(json.dumps(state))
elif args[:2] == ["serve", "--bg"] and args[2].startswith("--https="):
    if os.environ.get("FAKE_TAILSCALE_DENIED"):
        print("Access denied: serve config denied", file=sys.stderr)
        sys.exit(1)
    port = args[2].split("=")[1]
    key = "test.tail.example:" + port
    if args[-1] == "off":
        state["TCP"].pop(port, None)
        state["Web"].pop(key, None)
    else:
        state["TCP"][port] = {"HTTPS": True}
        state["Web"][key] = {"Handlers": {"/": {"Proxy": args[3]}}}
    path.write_text(json.dumps(state))
else:
    print("Unexpected command: " + repr(args), file=sys.stderr)
    sys.exit(2)
''')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_TAILSCALE_STATE", str(tailnet_state))
    # An inherited force-disable/token must not override the chosen config or library.
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    monkeypatch.setenv("GRATING_IMPLEMENTATIONS_TOKEN", "wrong-library-token")
    runtime = directory / ".server-control"

    def run(command, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), command, "--config", str(path), *args],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=55)
        if ok:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    yield config, path, runtime, tailnet_state, run
    monkeypatch.delenv("FAKE_TAILSCALE_DENIED", raising=False)
    run("stop", ok=False)
    state = launcher.read_json(runtime / "state.json", {"processes": {}})
    for record in state["processes"].values():
        if launcher.alive(record):
            launcher.stop_process(record)


def request(config, path, data=None):
    req = urllib.request.Request(f"http://127.0.0.1:{config['workspace_port']}{path}",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.load(response)


def campaign_input(name):
    return {"name": name, "autonomy": "manual", "llm_budget_usd": 0,
            "tasks": [{"name": "Quadratic", "problem_id": "bounded_continuous", "configuration": {}}]}


def create_campaign(config, name):
    """Campaigns are created through the command bus; the receipt carries the campaign."""
    identity = "launcher_" + name.lower().replace(" ", "_")
    receipt = request(config, "/api/v1/commands", {"id": "ui_" + identity, "campaign_id": identity, "operation": "campaign.create",
                                                   "expected_revision": 0, "payload": campaign_input(name)})
    return receipt["outcome"]["campaign"]


def campaign_ids(config):
    return {row["id"] for row in request(config, "/api/v1/campaigns")["campaigns"]}


def test_explicit_codex_timeout_survives_launcher_environment(setup, monkeypatch):
    config, path, _, _, _ = setup
    monkeypatch.setenv("GRATING_CODEX_TIMEOUT_SECONDS", "120")
    config["codex_timeout_seconds"] = 600
    path.write_text(json.dumps(config))
    actual = launcher.configuration(path)
    assert launcher.service_environment(actual)["GRATING_CODEX_TIMEOUT_SECONDS"] == "600"
    for invalid in (0, 601, True, "600", float("inf")):
        path.write_text(json.dumps({**config, "codex_timeout_seconds": invalid}))
        with pytest.raises(launcher.LauncherError, match="codex_timeout_seconds"):
            launcher.configuration(path)


def test_paper_reference_is_optional_and_reaches_services(setup, monkeypatch):
    config, path, _, _, _ = setup
    monkeypatch.delenv("GRATING_PAPER_REFERENCE", raising=False)
    assert "GRATING_PAPER_REFERENCE" not in launcher.service_environment(launcher.configuration(path))
    path.write_text(json.dumps({**config, "paper_reference": "~/papers/main text.pdf"}))
    actual = launcher.configuration(path)
    assert launcher.service_environment(actual)["GRATING_PAPER_REFERENCE"] == str(Path.home() / "papers/main text.pdf")
    for invalid in ("", " ", 1, True):
        path.write_text(json.dumps({**config, "paper_reference": invalid}))
        with pytest.raises(launcher.LauncherError, match="paper_reference"):
            launcher.configuration(path)


def test_runtime_gate_is_on_by_default_and_the_configuration_can_change_or_remove_it(setup, monkeypatch):
    config, path, _, _, _ = setup
    monkeypatch.setenv("GRATING_RUNTIME_GATE_SECONDS", "5")   # a variable left in the calling shell does not decide
    environment = launcher.service_environment(launcher.configuration(path))
    assert environment["GRATING_RUNTIME_GATE_SECONDS"] == "600"
    assert json.loads(environment["GRATING_RESOURCE_SETTINGS"]) == {"workers": config["workers"], "gpu_workers": 0, "cuda_mps": None}
    monkeypatch.setenv("GRATING_DEVICE", "cuda")   # the scheduler sets each worker's device, not the calling shell
    path.write_text(json.dumps({**config, "runtime_gate_seconds": 1800, "workers": 3, "gpu_workers": 1}))
    environment = launcher.service_environment(launcher.configuration(path))
    assert environment["GRATING_RUNTIME_GATE_SECONDS"] == "1800" and json.loads(environment["GRATING_RESOURCE_SETTINGS"])["workers"] == 3
    assert "GRATING_DEVICE" not in environment and json.loads(environment["GRATING_RESOURCE_SETTINGS"])["gpu_workers"] == 1
    for invalid in (-1, 1.5, True):
        path.write_text(json.dumps({**config, "gpu_workers": invalid}))
        with pytest.raises(launcher.LauncherError, match="gpu_workers"):
            launcher.configuration(path)
    # The settings that placed the 2D solver for the whole lab are gone: each run is placed.
    for retired in ({"meent_2d_source": "runs/solvers/meent-private"}, {"meent_device": "cuda"}):
        path.write_text(json.dumps({**config, **retired}))
        with pytest.raises(launcher.LauncherError, match="gpu_workers"):
            launcher.configuration(path)
    path.write_text(json.dumps({**config, "runtime_gate_seconds": None}))
    assert "GRATING_RUNTIME_GATE_SECONDS" not in launcher.service_environment(launcher.configuration(path))
    for invalid in (0, -1, "600", True):
        path.write_text(json.dumps({**config, "runtime_gate_seconds": invalid}))
        with pytest.raises(launcher.LauncherError, match="runtime_gate_seconds"):
            launcher.configuration(path)


def test_cuda_mps_is_optional_started_once_and_kept_while_a_worker_uses_it(setup, monkeypatch, tmp_path):
    config, path, _, _, _ = setup
    monkeypatch.delenv("CUDA_MPS_PIPE_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert "CUDA_MPS_PIPE_DIRECTORY" not in launcher.service_environment(launcher.configuration(path))
    path.write_text(json.dumps({**config, "cuda_mps": "yes"}))
    with pytest.raises(launcher.LauncherError, match="cuda_mps"):
        launcher.configuration(path)
    path.write_text(json.dumps({**config, "cuda_mps": True}))
    actual = launcher.configuration(path)
    mps = launcher.Mps(actual)
    assert launcher.service_environment(actual)["CUDA_MPS_PIPE_DIRECTORY"] == mps.environment["CUDA_MPS_PIPE_DIRECTORY"]
    state = tmp_path / "mps.json"
    tool = tmp_path / "bin/nvidia-cuda-mps-control"
    tool.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_MPS_STATE"])
state = json.loads(path.read_text()) if path.exists() else None
if sys.argv[1:] == ["-d"]:
    assert all(Path(os.environ[name]).is_dir() for name in ("CUDA_MPS_PIPE_DIRECTORY", "CUDA_MPS_LOG_DIRECTORY"))
    path.write_text(json.dumps({"starts": int(os.environ.get("FAKE_MPS_STARTS", 0)) + 1, "clients": []}))
    sys.exit(0)
if state is None:
    print("Cannot find MPS control daemon process", file=sys.stderr)
    sys.exit(1)
command = sys.stdin.read().split()
if command == ["get_server_list"]:
    print(7 if state["clients"] else "")
elif command == ["get_client_list", "7"]:
    print(*state["clients"], sep="\\n")
elif command == ["quit"]:
    path.unlink()
else:
    sys.exit(2)
''')
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_MPS_STATE", str(state))
    assert not mps.running()
    mps.start()
    monkeypatch.setenv("FAKE_MPS_STARTS", "1")
    mps.start()
    assert mps.running() and json.loads(state.read_text())["starts"] == 1
    state.write_text(json.dumps({"starts": 1, "clients": [4242, 4243]}))
    assert mps.clients() == ["4242", "4243"]
    mps.stop()
    assert mps.running()        # a worker that outlived the servers keeps its GPU
    state.write_text(json.dumps({"starts": 1, "clients": []}))
    mps.stop()
    assert not mps.running()


def test_no_model_provider_is_the_launcher_default(setup, monkeypatch):
    config, path, _, _, _ = setup
    monkeypatch.setenv("GRATING_LLM_MODEL", "stale-shell-model")
    bare = {key: value for key, value in config.items() if key not in {"provider", "model"}}
    path.write_text(json.dumps(bare))
    environment = launcher.service_environment(launcher.configuration(path))
    assert environment["GRATING_LLM_PROVIDER"] == "none" and "GRATING_LLM_MODEL" not in environment
    path.write_text(json.dumps({**bare, "llm_enabled": True}))
    with pytest.raises(launcher.LauncherError, match="needs a provider"):
        launcher.configuration(path)
    # A Pi harness brings its agents' own models: its campaigns need no research-role provider.
    pi_port = next(port for port in free_ports() if port not in config.values())
    path.write_text(json.dumps({**bare, "llm_enabled": True, "pi_port": pi_port}))
    environment = launcher.service_environment(launcher.configuration(path))
    assert (environment["GRATING_LLM_PROVIDER"], environment["GRATING_LLM_ENABLED"]) == ("none", "true")
    assert "GRATING_LLM_MODEL" not in environment
    path.write_text(json.dumps({**bare, "llm_enabled": True}))
    assert launcher.configuration(path, no_llm=True)["llm_enabled"] is False
    path.write_text(json.dumps({**bare, "pi_port": pi_port, "pi_provider": "openai-codex"}))
    assert launcher.service_environment(launcher.configuration(path))["GRATING_PI_PROVIDER"] == "openai-codex"
    path.write_text(json.dumps({**bare, "pi_provider": ""}))
    with pytest.raises(launcher.LauncherError, match="pi_provider"):
        launcher.configuration(path)


def test_real_services_restart_keep_campaign_and_only_remove_their_route(setup):
    config, path, runtime, ts_path, run = setup
    original_routes = json.loads(ts_path.read_text())
    run("start")
    initial = json.loads((runtime / "state.json").read_text())
    assert all(launcher.alive(record) for record in initial["processes"].values())
    assert "healthy" in run("status").stdout
    run("start")
    assert json.loads((runtime / "state.json").read_text())["processes"] == initial["processes"]
    state = request(config, "/api/v1/workspace")
    assert state["provider"]["configured"] is False
    campaign = create_campaign(config, "Launcher persistence")
    campaign_id = campaign["id"]
    config["workers"] = 2
    path.write_text(json.dumps(config))
    assert "Configuration changed" in run("start", ok=False).stderr
    # A restart keeps its unchanged route, so it works where changing Serve needs a password.
    os.environ["FAKE_TAILSCALE_DENIED"] = "1"
    try:
        run("restart")
    finally:
        del os.environ["FAKE_TAILSCALE_DENIED"]
    assert json.loads(ts_path.read_text())["TCP"][str(config["tailnet_port"])] == {"HTTPS": True}
    assert campaign_id in campaign_ids(config)
    assert request(config, "/api/v1/workspace")["max_workers"] == 2
    assert not any(launcher.alive(record) for record in initial["processes"].values())
    run("stop")
    assert json.loads(ts_path.read_text()) == original_routes
    assert json.loads((runtime / "state.json").read_text())["processes"] == {}
    run("stop")


def test_permission_failure_can_be_retried_without_duplicate_servers(setup, monkeypatch):
    config, _, runtime, ts_path, run = setup
    monkeypatch.setenv("FAKE_TAILSCALE_DENIED", "1")
    failure = run("start", ok=False)
    assert failure.returncode == 1 and "sudo tailscale serve" in failure.stdout
    before = json.loads((runtime / "state.json").read_text())["processes"]
    assert len(before) == 2 and all(launcher.alive(record) for record in before.values())
    monkeypatch.delenv("FAKE_TAILSCALE_DENIED")
    run("start")
    assert json.loads((runtime / "state.json").read_text())["processes"] == before
    monkeypatch.setenv("FAKE_TAILSCALE_DENIED", "1")
    assert run("stop", ok=False).returncode == 1
    state = json.loads((runtime / "state.json").read_text())
    assert state["processes"] == {} and state["tailnet"]
    assert not any(launcher.alive(record) for record in before.values())
    monkeypatch.delenv("FAKE_TAILSCALE_DENIED")
    run("stop")
    assert str(config["tailnet_port"]) not in json.loads(ts_path.read_text())["TCP"]


@pytest.mark.parametrize("collision", ["other_target", "extra_path", "funnel", "foreground"])
def test_tailnet_collisions_are_preserved_before_any_service_start(setup, collision):
    config, _, runtime, ts_path, run = setup
    state = json.loads(ts_path.read_text())
    port = str(config["tailnet_port"])
    key = "test.tail.example:" + port
    state["TCP"][port] = {"HTTPS": True}
    state["Web"][key] = {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{config['workspace_port']}"}}}
    if collision == "other_target":
        state["Web"][key]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
    elif collision == "extra_path":
        state["Web"][key]["Handlers"]["/other"] = {"Text": "keep me"}
    elif collision == "funnel":
        state["AllowFunnel"] = {key: True}
    else:
        state = {"Foreground": {"session": state}}
    ts_path.write_text(json.dumps(state))
    assert run("start", ok=False).returncode == 1
    assert json.loads(ts_path.read_text()) == state
    assert not (runtime / "state.json").exists()


def test_foreign_listener_is_not_stopped(setup):
    config, _, runtime, _, run = setup
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", config["workspace_port"]))
        listener.listen()
        failure = run("start", "--replace-fixture", ok=False)
        assert failure.returncode == 1 and "already in use" in failure.stderr
        assert listener.getsockname()[1] == config["workspace_port"]
        assert not (runtime / "state.json").exists()


def test_stale_pid_record_never_signals_another_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        record = launcher.process_identity(process.pid)
        stale = {**record, "start_ticks": "stale"}
        launcher.stop_process(stale)
        assert process.poll() is None
        launcher.stop_process(record)
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)


def test_failed_second_service_rolls_back_started_library(setup, monkeypatch):
    config, _, runtime, _, _ = setup
    servers = launcher.Servers(config)
    original = servers.launch
    records = []

    def fail_workspace(role):
        if role == "workspace":
            raise launcher.LauncherError("test workspace failure")
        original(role)
        records.append(servers.state["processes"][role])

    monkeypatch.setattr(servers, "launch", fail_workspace)
    with servers.locked(), pytest.raises(launcher.LauncherError, match="test workspace failure"):
        servers.start()
    assert records and not any(launcher.alive(record) for record in records)
    assert json.loads((runtime / "state.json").read_text())["processes"] == {}


def test_handover_replaces_only_matching_fixture_and_preserves_data(setup):
    config, _, runtime, _, run = setup
    processes = []
    try:
        for role in launcher.ROLES:
            processes.append(subprocess.Popen([sys.executable, str(ROOT / "scripts/serve_commissioning_fixture.py"),
                role, "--directory", config["directory"], "--port", str(launcher.port_for(config, role)),
                "--library-url", f"http://127.0.0.1:{config['implementation_port']}",
                "--frontend-directory", config["frontend_directory"]],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            deadline = time.monotonic() + 35
            while not launcher.healthy(config, role) and time.monotonic() < deadline:
                assert processes[-1].poll() is None
                time.sleep(.1)
            assert launcher.healthy(config, role)
        campaign = create_campaign(config, "Fixture campaign")
        assert "old review fixture" in run("status").stdout
        assert run("start", ok=False).returncode == 1
        run("start", "--replace-fixture")
        for process in processes:
            process.wait(timeout=5)
        assert campaign["id"] in campaign_ids(config)
        managed = json.loads((runtime / "state.json").read_text())["processes"]
        for record in managed.values():
            args = Path(f"/proc/{record['pid']}/cmdline").read_bytes()
            assert b"manage_servers.py" in args and b"serve_commissioning_fixture" not in args
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def test_model_opt_in_reaches_both_services_and_override_disables_it(setup, monkeypatch):
    config, path, runtime, _, run = setup
    binary = path.parent / "fake-codex"
    binary.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ['login', 'status']\nprint('Logged in using ChatGPT')\n")
    binary.chmod(0o755)
    monkeypatch.setenv("GRATING_CODEX_BINARY", str(binary))
    config["llm_enabled"] = True
    path.write_text(json.dumps(config))
    run("start")
    provider = request(config, "/api/v1/workspace")["provider"]
    assert provider["configured"]
    for record in json.loads((runtime / "state.json").read_text())["processes"].values():
        selected = [value for value in Path(f"/proc/{record['pid']}/environ").read_bytes().split(b"\0")
                    if value.startswith((b"GRATING_LLM_ENABLED=", b"GRATING_LLM_DISABLED="))]
        assert b"GRATING_LLM_ENABLED=true" in selected and b"GRATING_LLM_DISABLED=false" in selected
    run("restart", "--no-llm")
    assert request(config, "/api/v1/workspace")["provider"]["configured"] is False


def test_authentication_failure_is_actionable_and_starts_nothing(setup, monkeypatch):
    config, path, runtime, _, run = setup
    binary = path.parent / "fake-codex"
    binary.write_text(f"#!{sys.executable}\nprint('Not logged in; sign in with ChatGPT')\n")
    binary.chmod(0o755)
    monkeypatch.setenv("GRATING_CODEX_BINARY", str(binary))
    config["llm_enabled"] = True
    path.write_text(json.dumps(config))
    result = run("start", ok=False)
    assert result.returncode == 1 and "codex login --device-auth" in result.stderr
    assert not (runtime / "state.json").exists()
