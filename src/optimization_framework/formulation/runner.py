"""Runs a notebook's code cells in the sandbox and reads back what they produced.

The run's directory is the one place the cells can write, so nothing in it is trusted: the result
is rebuilt field by field, and a picture is kept only if it is a regular file that starts as a PNG.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading

from optimization_framework.agents.files import run_sandboxed, sandbox_command

from . import kernel

RUN_SECONDS = 90.   # an agent's tool call is given up on after 120 s
RESULT_BYTES = 4 * 1024 * 1024
TEXT = {"text/markdown", "text/latex", "text/plain"}
PICTURE = re.compile(r"[a-z][a-z0-9_]{0,60}\.png")
PNG = b"\x89PNG\r\n\x1a\n"
STATUSES = {"ok", "error", "skipped"}


_fonts_lock, _fonts_built = threading.Lock(), set()


def fonts(directory):
    """A matplotlib font list built by this trusted process, for runs to copy instead of rebuilding it each time
    (about half a second of a run that draws). Built once per directory and server process; None if it cannot be."""
    directory = Path(directory)
    with _fonts_lock:
        if directory not in _fonts_built:
            directory.mkdir(parents=True, exist_ok=True)
            built = subprocess.run([sys.executable, "-c", "import matplotlib.font_manager"], capture_output=True, timeout=120,
                env={"PATH": "/usr/bin:/bin", "HOME": str(directory), "MPLCONFIGDIR": str(directory), "MPLBACKEND": "Agg"})
            if built.returncode != 0 or not any(directory.glob("fontlist-*.json")):
                return None
            _fonts_built.add(directory)
    return directory


def regular_file(path, limit):
    """Whether `path` is a plain file of at most `limit` bytes. A link is never followed."""
    try:
        found = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(found.st_mode) and found.st_size <= limit


def picture(directory, name):
    """The path of a picture a run left behind, or None when it is not a PNG file of that run."""
    path = Path(directory) / name
    if not PICTURE.fullmatch(name) or not regular_file(path, kernel.FIGURE_BYTES):
        return None
    with open(path, "rb") as stream:
        return path if stream.read(len(PNG)) == PNG else None


def _cell(directory, found):
    """One cell's record, keeping only the fields and sizes the notebook shows."""
    found = found if isinstance(found, dict) else {}
    record = {"status": found.get("status") if found.get("status") in STATUSES else "error",
              "stdout": str(found.get("stdout", ""))[-kernel.OUTPUT_CHARACTERS:], "outputs": []}
    for key in ("error", "after"):
        if isinstance(found.get(key), str):
            record[key] = found[key][-kernel.OUTPUT_CHARACTERS:]
    if isinstance(found.get("elapsed_seconds"), (int, float)):
        record["elapsed_seconds"] = float(found["elapsed_seconds"])
    for output in (found.get("outputs") if isinstance(found.get("outputs"), list) else [])[:kernel.OUTPUTS_PER_CELL]:
        if not isinstance(output, dict):
            continue
        if output.get("mime") in TEXT and isinstance(output.get("text"), str):
            record["outputs"].append({"mime": output["mime"], "text": output["text"][:kernel.OUTPUT_CHARACTERS]})
        elif output.get("mime") == "image/png" and isinstance(output.get("file"), str) and picture(directory, output["file"]):
            record["outputs"].append({"mime": "image/png", "file": output["file"]})
    return record


def execute(directory, cells, *, scratch=None, seconds=RUN_SECONDS, parameters=None, font_list=None):
    """Run `cells` (id, name, source), then `scratch` if given, in `directory`, with `parameters` (name: value) as P.
    `font_list` is a directory from fonts(), shown to the run read-only.

    Returns {"status", "cells": {id: record}, "scratch"?, "elapsed_seconds", "stderr"?}. A cell the
    time limit stopped has the status "timeout"; the cells after it, "not_run".
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.json").write_text(json.dumps({"cells": cells, "scratch": scratch, "parameters": parameters}))
    shown = ((font_list, kernel.FONT_LIST),) if font_list else ()
    ended, output, elapsed = run_sandboxed([*sandbox_command(directory, read_only=shown), sys.executable, "-u", kernel.__file__], seconds)
    found = {}
    if regular_file(directory / "result.json", RESULT_BYTES):
        try:
            found = json.loads((directory / "result.json").read_text())
        except (OSError, ValueError):
            found = {}
    found = found if isinstance(found, dict) else {}
    reported = found.get("cells") if isinstance(found.get("cells"), dict) else {}
    stopped = "timeout" if ended.get("status") == "timeout" else None if found.get("finished") is True else "failed"
    records = {}
    for cell in cells:
        if cell["id"] in reported:
            records[cell["id"]] = _cell(directory, reported[cell["id"]])
        elif stopped and found.get("running") == cell["id"]:
            records[cell["id"]] = {"status": stopped, "stdout": "", "outputs": [],
                "error": f"Stopped at the {seconds:g} s limit of a run." if stopped == "timeout" else "The run ended while this cell was running."}
        else:
            records[cell["id"]] = {"status": "not_run", "stdout": "", "outputs": []}
    statuses = {record["status"] for record in records.values()}
    result = {"status": stopped or ("error" if "error" in statuses else "completed"), "cells": records, "elapsed_seconds": round(elapsed, 2)}
    if scratch is not None:
        result["scratch"] = (_cell(directory, found["scratch"]) if "scratch" in found else
            {"status": stopped or "not_run", "stdout": "", "outputs": [], **({"error": f"Stopped at the {seconds:g} s limit."} if stopped == "timeout" else {})})
        if result["scratch"]["status"] == "error" and result["status"] == "completed":
            result["status"] = "error"
    if stopped == "failed":
        result["stderr"] = output.get("stderr", "")[-4000:]
    return result
