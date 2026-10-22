"""Whether an installed evaluator can start in this lab, found out before an experiment is queued instead of by its first probe.

A worker builds its evaluator in a new process of this interpreter. The check does the same in a child process: it resolves the
task's problem instance and builds its evaluator without evaluating anything, so a missing solver or package, a solver checkout
that is not the lab's or an absent device fails here, with the evaluator's own message. It is checked on every device the
task's runs may be placed on (execution/devices.py): on CPU, and on the GPU too when the lab runs GPU workers and the
evaluator can use one. The answer holds for the software it was checked against (the solver versions and build, which CUDA
devices are visible, and when the installed packages last changed): installing or updating a package checks again, without a restart. Commissioned evaluators carry their own runtime checks and are not
covered here.
"""
from __future__ import annotations

from importlib.metadata import entry_points
import json
import os
import subprocess
import sys
import sysconfig
import threading
import time

TIMEOUT = 180
_results: dict[str, dict] = {}
_running: dict[str, threading.Thread] = {}
_lock = threading.Lock()
_environment = [0.0, None]   # read at most every few seconds: a page asks for every campaign at once


def environment():
    """What building an evaluator depends on outside its own code: the interpreter, the solvers it may load, the devices it sees, the installed packages."""
    if _environment[1] is not None and time.monotonic() - _environment[0] < 5:
        return _environment[1]
    from optimization_framework.execution.source import runtime_identity
    try:
        runtime = runtime_identity()
    except ValueError as error:   # a solver installed from a directory that is no longer a checkout
        runtime = {"error": str(error)}
    sites = sorted({sysconfig.get_path(name) for name in ("purelib", "platlib")})
    value = {"executable": sys.executable, "runtime": runtime, "cuda_visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
             "packages": [[site, os.stat(site).st_mtime_ns if os.path.isdir(site) else None] for site in sites]}
    _environment[:] = [time.monotonic(), value]
    return value


def installed(task):
    """The problem instance of a task that an installed adapter evaluates; None for a commissioned evaluator or one this check cannot see."""
    if task.get("evaluator_requirement_id") or task.get("evaluator_manifest"):
        return None
    problem = task.get("problem") or {}
    names = {item.name for item in entry_points(group="optimization_framework.problems")}
    return problem if problem.get("definition_id") in names else None


def _key(problem, device):
    from optimization_framework.contracts.base import content_hash
    return content_hash([[problem.get(name) for name in ("definition_id", "definition_version", "evaluator_id", "evaluator_version")],
                         device, environment()])


def _probe(problem, device="cpu"):
    """Build the evaluator in a child as a worker on this device would, with this checkout's code and without the lab's credentials."""
    from optimization_framework.execution.devices import environment as placed
    from optimization_framework.execution.source import source_root
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GRATING_LLM_") and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}}
    env.update(PYTHONPATH=str(source_root()), PYTHONUNBUFFERED="1", MPLBACKEND="Agg", **placed(device))
    try:
        result = subprocess.run([sys.executable, "-m", __name__], input=json.dumps(problem), capture_output=True, text=True,
                                timeout=TIMEOUT, env=env, cwd=str(source_root()))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Building the evaluator took longer than {TIMEOUT} s"}
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict) and "ok" in answer:
            return answer
    tail = (result.stderr.strip().splitlines() or ["the check exited without an answer"])[-1]
    return {"ok": False, "error": tail[:600]}


def _run(key, problem, device):
    from optimization_framework.storage.sqlite import now
    started = time.monotonic()
    try:
        answer = _probe(problem, device)
    except Exception as error:   # the check itself failed; say so rather than pass the evaluator
        answer = {"ok": False, "error": f"The evaluator check could not run: {error}"}
    answer = {**answer, "checked_at": now(), "seconds": round(time.monotonic() - started, 2)}
    with _lock:
        _results[key] = answer
        _running.pop(key, None)


def check(problem, device="cpu", *, wait=True):
    """{ok, error, checked_at, seconds} for this problem's evaluator on device in the current environment, from the last check of it.
    A new environment starts a check; without wait, None is returned while it runs."""
    key = _key(problem, device)
    with _lock:
        if key in _results:
            return _results[key]
        thread = _running.get(key)
        if thread is None:
            thread = threading.Thread(target=_run, args=(key, problem, device), name="evaluator-preflight", daemon=True)
            _running[key] = thread
            thread.start()
    if not wait:
        return None
    thread.join(TIMEOUT + 30)
    return _results.get(key) or {"ok": False, "error": "The evaluator check did not finish"}


def readiness(task, *, wait=True, devices=("cpu",)):
    """EvaluatorService.readiness for an installed evaluator: runnable only once it was seen to start here on every device its runs may take."""
    problem = installed(task)
    if problem is None:
        return {"state": "ready", "runnable": True, "reason": "Installed evaluator is available."}
    answers = {device: check(problem, device, wait=wait) for device in devices}
    if any(answer is None for answer in answers.values()):
        return {"state": "checking", "runnable": True, "reason": "Checking that the installed evaluator starts in this lab."}
    checked_at = max(answer["checked_at"] for answer in answers.values())
    failed = [(device, answer) for device, answer in answers.items() if not answer["ok"]]
    if not failed:
        return {"state": "ready", "runnable": True, "reason": "The installed evaluator starts in this lab"
                + (" on " + " and ".join("the GPU" if device == "cuda" else "CPU" for device in answers) if len(answers) > 1 else "") + ".",
                "checked_at": checked_at, "devices": list(answers)}
    device, answer = failed[0]
    error = answer["error"] if len(answers) == 1 else f"on {'the GPU' if device == 'cuda' else 'CPU'}: {answer['error']}"
    where = f": {error}" if len(answers) == 1 else f" {error}"
    return {"state": "cannot_start", "runnable": False, "error": error, "checked_at": checked_at, "device": device,
            "reason": f"The evaluator of {problem['definition_id']} cannot start in this lab{where}. "
                      "No experiment or runtime probe can run on it until that is fixed; the check repeats by itself once the software changes."}


def main():
    problem = json.load(sys.stdin)
    try:
        from optimization_framework.contracts.problems import ProblemInstance
        from optimization_framework.evaluation.registry import problems
        evaluator = problems.evaluator(ProblemInstance(**problem))
        if hasattr(evaluator, "close"):
            evaluator.close()
        answer = {"ok": True, "error": None}
    except BaseException as error:   # whatever stops a worker from starting
        answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    sys.stdout.write("\n" + json.dumps(answer) + "\n")


if __name__ == "__main__":
    main()
