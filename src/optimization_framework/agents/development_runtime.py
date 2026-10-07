"""Docker boundary for full Pi development. No Docker socket enters a workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "grating-implementation-workspace:pi-0.87.1"


def run(args, *, timeout=45, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Workspace command failed")[-2000:])
    return result.stdout


class DockerWorkspace:
    def __init__(self, directory, image=None):
        self.directory = Path(directory)
        self.image = image or os.environ.get("GRATING_DEVELOPMENT_IMAGE", IMAGE)

    def root(self, record):
        return self.directory / record["id"]

    def name(self, record):
        return "grating-" + record["id"]

    def capability(self):
        if not shutil.which("docker"):
            return {"available": False, "reason": "Docker is not installed"}
        try:
            run(["docker", "image", "inspect", self.image], timeout=10)
            return {"available": True, "image": self.image}
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "reason": str(exc), "image": self.image}

    def inspect(self, record):
        result = subprocess.run(["docker", "inspect", self.name(record)], capture_output=True, text=True, timeout=10)
        if result.returncode:
            return None
        data = json.loads(result.stdout)[0]
        ports = data.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp") or []
        state = data["State"]
        return {"running": state["Running"], "paused": state.get("Paused", False),
                "port": int(ports[0]["HostPort"]) if ports else None, "pid": state["Pid"],
                "image_id": data["Image"], "container_id": data["Id"]}

    def prepare(self, record, brief, evidence):
        root = self.root(record)
        for name in ("work", "home", "incoming/commands", "incoming/results", "outgoing/events", "references"):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        work = root / "work"
        if not (work / "git").exists():
            run(["git", "clone", "--bare", "--no-local", "--single-branch", str(ROOT), str(work / "git")], timeout=120)
        if not (work / "repo/.git").exists():
            (work / "repo").mkdir(exist_ok=True, mode=0o700)
            branch = "implementation/" + record["id"]
            exists = subprocess.run(["git", f"--git-dir={work / 'git'}", "show-ref", "--verify", "--quiet",
                                     "refs/heads/" + branch], capture_output=True).returncode == 0
            run(["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "git",
                 "--mount", f"type=bind,src={work},dst=/work", self.image,
                 "--git-dir=/work/git", "worktree", "add",
                 *(["/work/repo", branch] if exists else ["-b", branch, "/work/repo", "HEAD"])], timeout=60)
        repo = work / "repo"
        context = repo / ".campaign"
        context.mkdir(exist_ok=True)
        # Do not replace developer-edited context when recovering an existing workspace.
        for name, content in {"assignment.md": brief, "evidence.json": json.dumps(evidence, indent=2),
                              "checkpoint.md": "# Development checkpoint\n\nRecord decisions, tests, remaining work, and blockers here.\n"}.items():
            if not (context / name).exists():
                (context / name).write_text(content)
        vscode = repo / ".vscode"
        vscode.mkdir(exist_ok=True)
        settings = {
            "terminal.integrated.profiles.linux": {
                "Pi implementation": {"path": "tmux", "args": ["attach-session", "-t", "implementation"]},
                "Development shell": {"path": "bash"}},
            "terminal.integrated.defaultProfile.linux": "Pi implementation",
            "terminal.integrated.enablePersistentSessions": True,
            "workbench.startupEditor": "none", "security.workspace.trust.enabled": False,
        }
        if not (vscode / "settings.json").exists():
            (vscode / "settings.json").write_text(json.dumps(settings, indent=2))
        pi_home = root / "home/.pi/agent"
        pi_home.mkdir(parents=True, exist_ok=True)
        if not (pi_home / "settings.json").exists():
            (pi_home / "settings.json").write_text(json.dumps({"defaultProjectTrust": "always",
                "compaction": {"enabled": True}, "telemetry": {"enabled": False}}))
        auth_file = Path(os.environ.get("GRATING_PI_AUTH_FILE", str(Path.home() / ".pi/agent/auth.json")))
        if auth_file.exists() and not (pi_home / "auth.json").exists():
            auth = json.loads(auth_file.read_text())
            if auth.get("openai-codex"):
                target = root / "incoming/auth.json"
                target.write_text(json.dumps({"openai-codex": auth["openai-codex"]}))
                target.chmod(0o600)
        # Reference code only: no host working files, credentials, or campaign databases.
        flrl = Path(os.environ.get("GRATING_FLRL_REFERENCE", str(ROOT.parent / "flrl")))
        if (flrl / ".git").exists() and not (root / "references/flrl").exists():
            target = root / "references/flrl"
            target.mkdir()
            archive = root / "references/flrl.tar"
            run(["git", "-C", str(flrl), "archive", "--output", str(archive), "HEAD"])
            import tarfile
            with tarfile.open(archive) as tar:
                tar.extractall(target, filter="data")
            archive.unlink()
            (root / "references/flrl.commit").write_text(run(["git", "-C", str(flrl), "rev-parse", "HEAD"]))
        # Optional paper PDF; set GRATING_PAPER_REFERENCE or the launcher's `paper_reference`.
        paper = Path(os.environ.get("GRATING_PAPER_REFERENCE", "")).expanduser()
        if paper.name and paper.is_file() and paper.stat().st_size < 50 * 1024 * 1024:
            target = root / "references/paper.pdf"
            if not target.exists():
                shutil.copy2(paper, target)
            extracted = root / "references/paper.txt"
            if not extracted.exists():
                from pypdf import PdfReader
                text = "\n\n".join(f"# Page {index + 1}\n{page.extract_text() or ''}"
                                   for index, page in enumerate(PdfReader(str(target)).pages))
                if len(text.encode()) <= 5 * 1024 * 1024:
                    extracted.write_text(text)

    def start(self, record):
        current = self.inspect(record)
        if current:
            if current["paused"]:
                run(["docker", "unpause", self.name(record)])
            elif not current["running"]:
                run(["docker", "start", self.name(record)])
            return self.inspect(record)
        root = self.root(record)
        args = ["docker", "run", "-d", "--init", "--name", self.name(record),
                "--label", "grating.development=" + record["id"], "--restart", "unless-stopped",
                "--cpus", "4", "--memory", "8g", "--pids-limit", "1024", "--read-only",
                "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--cap-add", "SETUID", "--cap-add", "SETGID",
                "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,nodev,size=2g",
                "--tmpfs", "/run:rw,nosuid,nodev,size=16m", "-p", "127.0.0.1::8080"]
        for source, target, readonly in [(root / "work", "/work", False), (root / "home", "/home/developer", False),
                (root / "incoming", "/bridge/in", True), (root / "outgoing", "/bridge/out", False),
                (root / "references", "/references", True),
                (ROOT / "agent-harness/extensions/implementation.ts", "/opt/workspace/extension.ts", True)]:
            args += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
        args += ["-e", f"WORKSPACE_UID={os.getuid()}", "-e", f"WORKSPACE_GID={os.getgid()}", self.image]
        run(args, timeout=60)
        return self.inspect(record)

    def pause(self, record):
        current = self.inspect(record)
        if current and current["running"] and not current["paused"]:
            run(["docker", "pause", self.name(record)])

    def stop(self, record):
        current = self.inspect(record)
        if current and current["paused"]:
            run(["docker", "unpause", self.name(record)])
        if current and current["running"]:
            run(["docker", "stop", "-t", "10", self.name(record)])

    def cpu_seconds(self, state):
        if not state or not state.get("pid"):
            return None
        try:
            relative = next(line.split(":", 2)[2] for line in Path(f"/proc/{state['pid']}/cgroup").read_text().splitlines() if line.startswith("0::"))
            stats = dict(line.split() for line in (Path("/sys/fs/cgroup") / relative.lstrip("/") / "cpu.stat").read_text().splitlines())
            return int(stats["usage_usec"]) / 1e6
        except (OSError, StopIteration, KeyError):
            return None

    def snapshot(self, record, commit, manifest_path):
        import re
        from pathlib import PurePosixPath
        from .development import SubmissionManifest
        from optimization_framework.implementations.models import Package
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("Submit the full 40-character Git commit hash")
        def read(path):
            p = PurePosixPath(path)
            if p.is_absolute() or str(p) != path or ".." in p.parts or "\\" in path:
                raise ValueError("Submission paths must stay in the repository")
            output = run(["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}", self.name(record),
                          "git", "--no-replace-objects", "-C", "/work/repo",
                          "show", commit + ":" + path], timeout=10)
            if len(output.encode()) > 1048576:
                raise ValueError("Submitted file exceeds the package transport limit")
            return output
        manifest = SubmissionManifest.model_validate_json(read(manifest_path))
        package = Package(contract=manifest.contract, entrypoint=manifest.entrypoint,
                          files=[{"path": p, "content": read(p)} for p in manifest.files])
        return package.model_dump(), manifest.model_dump()
