"""What an agent may read outside the campaign database, and where it may run its own analysis.

Two roots are readable. "repository" is this server's checkout, limited to the files git tracks:
credentials, run data and agent runtime state are never tracked, so they are never listed, read or
searched. "experiment" is the directory of one experiment of the agent's own campaign.

Whatever an agent reads is sent to its model provider. Nothing outside these two roots is reachable.
"""
from fnmatch import fnmatch
from itertools import islice
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from optimization_framework.storage.sqlite import now

REPOSITORY = Path(__file__).resolve().parents[3]
LINE_CHARACTERS = 2000
SEARCHED_FILE_BYTES = 2 * 1024 * 1024
SEARCH_SECONDS = 4.
OUTPUT_BYTES = 12000
ANALYSIS_SECONDS_PER_CAMPAIGN = 7200.
CLOSED = {"checkpoints", "runtime-locks"}   # binary saved states and locks of an experiment directory
_lock = threading.Lock()
_tracked = (0., frozenset())


def tracked():
    """Paths of the files git tracks in this checkout, re-read at most twice a minute."""
    global _tracked
    with _lock:
        if time.monotonic() - _tracked[0] > 30 or not _tracked[1]:
            try:
                listed = subprocess.run(["git", "-C", str(REPOSITORY), "ls-files", "-z"], capture_output=True, timeout=20, check=True).stdout
            except (OSError, subprocess.SubprocessError):
                raise ValueError("This server has no repository checkout that can be read") from None
            _tracked = (time.monotonic(), frozenset(listed.decode().split("\0")) - {""})
        return _tracked[1]


def relative(value):
    path = Path(value or "")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Give a path inside the chosen root, without .. or a leading /")
    return path


class Files:
    def __init__(self, tools, agent):
        self.tools, self.agent = tools, agent

    def root(self, args):
        """(directory, the paths readable in it or None for all, a label for messages)."""
        if args["root"] == "repository":
            return REPOSITORY, tracked(), "the repository"
        if not args.get("trial_id"):
            raise ValueError('The "experiment" root needs a trial_id')
        trial = self.tools.record(self.agent, args["trial_id"])
        if self.tools.store.get_entry(trial["id"])["kind"] != "trial":
            raise ValueError("That record is not an experiment")
        return self.tools.workspace.job_dir(trial["id"]), None, "experiment " + trial["id"]

    def file(self, args):
        directory, listed, label = self.root(args)
        path = relative(args["path"])
        filename = directory / path
        if listed is not None and str(path) not in listed:
            raise ValueError(f"{path} is not a committed file of the repository")
        if listed is None and (CLOSED & set(path.parts) or not filename.resolve().is_relative_to(directory.resolve())):
            raise ValueError(f"{path} is not readable in {label}")
        if filename.is_symlink() or not filename.is_file():
            raise ValueError(f"{path} is not a file in {label}")
        return filename, path

    def list(self, args):
        directory, listed, label = self.root(args)
        path = relative(args.get("path"))
        entries = {}
        if listed is not None:
            prefix = path.parts
            for name in listed:
                parts = Path(name).parts
                if parts[:len(prefix)] == prefix and len(parts) > len(prefix):
                    child = parts[len(prefix)]
                    entries[child] = {"name": child, "type": "directory"} if len(parts) > len(prefix) + 1 else {
                        "name": child, "type": "file", "bytes": (directory / name).stat().st_size if (directory / name).is_file() else None}
        else:
            target = directory / path
            if CLOSED & set(path.parts) or not target.resolve().is_relative_to(directory.resolve()) or not target.is_dir():
                raise ValueError(f"{path} is not a directory in {label}")
            for entry in os.scandir(target):
                if entry.name not in CLOSED and not entry.is_symlink():
                    entries[entry.name] = {"name": entry.name, "type": "directory"} if entry.is_dir() else {"name": entry.name, "type": "file", "bytes": entry.stat().st_size}
        if not entries and path.parts:
            raise ValueError(f"{path} is not a directory in {label}")
        ordered = sorted(entries.values(), key=lambda entry: (entry["type"] == "file", entry["name"]))
        return {"root": args["root"], "path": str(path), "entries": ordered[:500], "more": max(0, len(ordered) - 500)}

    def read(self, args):
        filename, path = self.file(args)
        first, count, allowance = args.get("first_line", 1), args.get("lines", 200), args.get("max_bytes", 24 * 1024)
        content, cut, used, last, more = [], 0, 0, first - 1, False
        try:
            with filename.open(encoding="utf-8") as stream:
                for line in islice(stream, first - 1, first - 1 + count):
                    text = line.rstrip("\n")
                    cut += len(text) > LINE_CHARACTERS
                    text = text[:LINE_CHARACTERS]
                    if content and used + len(text.encode()) + 1 > allowance:
                        more = True
                        break
                    content.append(text)
                    used += len(text.encode()) + 1
                    last += 1
                else:
                    more = next(stream, None) is not None
        except UnicodeDecodeError:
            raise ValueError(f"{path} is not a text file") from None
        return {"root": args["root"], "path": str(path), "bytes": filename.stat().st_size, "first_line": first, "last_line": last,
                "next_first_line": last + 1 if more else None, "lines_cut_to_2000_characters": cut, "content": "\n".join(content)}

    def search(self, args):
        directory, listed, label = self.root(args)
        try:
            pattern = re.compile(args["query"])
        except re.error as error:
            raise ValueError(f"The query is not a regular expression: {error}") from None
        under, glob, limit = relative(args.get("path")).parts, args.get("glob"), args.get("limit", 40)
        if listed is not None:
            names = sorted(listed)
        else:
            names = sorted(str(Path(folder).relative_to(directory) / name) for folder, folders, files in os.walk(directory)
                           for name in files if not CLOSED & set(Path(folder).relative_to(directory).parts))
        matches, searched, skipped, started = [], 0, 0, time.monotonic()
        for name in names:
            if Path(name).parts[:len(under)] != under or glob and not (fnmatch(name, glob) or fnmatch(Path(name).name, glob)):
                continue
            filename = directory / name
            if filename.is_symlink() or not filename.is_file():
                continue
            if filename.stat().st_size > SEARCHED_FILE_BYTES:
                skipped += 1
                continue
            if len(matches) >= limit or time.monotonic() - started > SEARCH_SECONDS:
                return {"matches": matches, "files_searched": searched, "complete": False,
                        "note": "Stopped early: narrow the search with path or glob, or raise limit."}
            searched += 1
            try:
                for number, line in enumerate(filename.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line):
                        matches.append({"path": name, "line": number, "text": line.strip()[:300]})
                        if len(matches) >= limit:
                            break
            except UnicodeDecodeError:
                continue
        return {"matches": matches, "files_searched": searched, "complete": True,
                **({"files_over_2_MB_not_searched": skipped} if skipped else {})}


def interpreter():
    """The directories this Python runs from: its environment, its installation, and every link between them."""
    roots, hop = [sys.base_prefix, sys.prefix], sys.executable
    while os.path.islink(hop):
        hop = os.path.join(os.path.dirname(hop), os.readlink(hop))   # an absolute target replaces the directory
        roots.append(str(Path(hop).parent.parent))
    return roots


def sandbox_command(work, *, read_only=()):
    """The start of a sandboxed command line: it sees the interpreter, this checkout's source and each
    (host path, path inside) of `read_only`, writes only `work` (mounted at /work), and has no network."""
    if not shutil.which("bwrap"):
        raise ValueError("Sandboxed analysis requires bubblewrap on this server")
    command = ["bwrap", "--die-with-parent", "--unshare-all", "--new-session", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for root in dict.fromkeys(["/usr", "/lib", "/lib64", "/bin", *interpreter(), str(REPOSITORY / "src")]):
        if Path(root).exists():
            command += ["--ro-bind", root, root]
    for source, inside in read_only:
        command += ["--ro-bind", str(source), inside]
    return command + ["--bind", str(work), "/work", "--chdir", "/work"]


def run_sandboxed(command, seconds):
    """Run a sandboxed command to its end or for `seconds`. Returns (how it ended, the end of its output, seconds taken)."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
                 "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg"})
        try:
            child.wait(timeout=seconds)
            status = {"exit_code": child.returncode}
        except subprocess.TimeoutExpired:
            import signal
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            status = {"status": "timeout", "seconds": seconds}
        output = {}
        for key, stream in (("stdout", stdout), ("stderr", stderr)):
            size = stream.seek(0, 2)
            stream.seek(max(0, size - OUTPUT_BYTES))
            output[key] = stream.read(OUTPUT_BYTES).decode(errors="replace")
            if size > OUTPUT_BYTES:
                output[key + "_bytes_before_this_end"] = size - OUTPUT_BYTES
    return status, output, time.monotonic() - started


def analysis_run(tools, agent, args):
    """Run the agent's own script over the experiments it names, in a sandbox that can read them and write only its work directory."""
    work = tools.workspace.directory / "agents" / agent["id"] / "work"
    work.mkdir(parents=True, exist_ok=True)
    script = relative(args["path"])
    if not (work / script).is_file() or (work / script).is_symlink() or not (work / script).resolve().is_relative_to(work.resolve()):
        raise ValueError("Write the script into your work directory with workspace_write first")
    trials = [tools.record(agent, identity) for identity in dict.fromkeys(args.get("trial_ids", []))]
    if any(tools.store.get_entry(trial["id"])["kind"] != "trial" for trial in trials):
        raise ValueError("trial_ids names this campaign's experiments")
    used = sum(session.get("analysis_seconds", 0) for session in tools.store.list("agent_session", agent["campaign_id"]))
    seconds = min(args.get("seconds", 60), ANALYSIS_SECONDS_PER_CAMPAIGN - used)
    if seconds <= 0:
        raise ValueError(f"This campaign's agents have used their {ANALYSIS_SECONDS_PER_CAMPAIGN:g} s of analysis time")
    command = sandbox_command(work, read_only=[(tools.workspace.job_dir(trial["id"]), "/data/" + trial["id"]) for trial in trials])
    wrapper = ("import resource,runpy,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(64<<20,64<<20)); "
               "resource.setrlimit(resource.RLIMIT_NOFILE,(256,256)); runpy.run_path(sys.argv[1],run_name='__main__')")
    status, output, elapsed = run_sandboxed([*command, sys.executable, "-c", wrapper, str(script)], seconds)
    with tools.workspace.lock:
        session = tools.store.get(agent["id"], "agent_session")
        session["analysis_seconds"] = session.get("analysis_seconds", 0) + elapsed
        tools.store.put("agent_session", session)
    return {**status, **output, "elapsed_seconds": round(elapsed, 2), "mounted": ["/data/" + trial["id"] for trial in trials],
            "analysis_seconds_left": round(ANALYSIS_SECONDS_PER_CAMPAIGN - used - elapsed), "finished_at": now()}
