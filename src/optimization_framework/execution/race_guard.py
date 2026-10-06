"""Independent elapsed-time guard for an explicitly authorized adaptive race.

This process cannot admit work or change scientific allocations. It signals only
processes owned by this race whose Linux start identity still matches SQLite.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from optimization_framework.storage.artifacts import atomic_json


def process_identity(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, ValueError, IndexError, TypeError):
        return None


def owned_processes(database, race_id):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as connection:
        return connection.execute("""SELECT id,json_extract(data,'$.pid'),json_extract(data,'$.process_identity')
            FROM records WHERE kind='trial' AND json_extract(data,'$.race_id')=?
            AND json_extract(data,'$.status') IN ('running','pausing','stopping')""", (race_id,)).fetchall()


def signal_owned(rows, sig):
    signalled = []
    for trial_id, pid, owner in rows:
        if not pid or not owner or process_identity(pid) != owner:
            continue
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
            signalled.append(trial_id)
        except ProcessLookupError:
            pass
    return signalled


def enforce(database, race_id, deadline, directory, grace=5):
    cutoff = time.monotonic() + max(0, deadline - time.time())
    while time.monotonic() < cutoff - grace and time.time() < deadline - grace:
        time.sleep(min(1, max(.01, cutoff - grace - time.monotonic())))
    rows = owned_processes(database, race_id)
    receipt = {"race_id": race_id, "deadline_at": deadline, "requested_at": time.time(),
        "cooperative_stop": signal_owned(rows, signal.SIGTERM)}
    while time.monotonic() < cutoff and time.time() < deadline:
        time.sleep(min(.1, max(.01, cutoff - time.monotonic())))
    # Re-read so a race-owned launch during the grace window is also covered.
    receipt.update(enforced_at=time.time(), hard_stop=signal_owned(owned_processes(database, race_id), signal.SIGKILL))
    atomic_json(Path(directory) / "deadline-receipt.json", receipt)


def arm(workspace, race_id, deadline):
    destination = workspace.directory / "races" / race_id
    destination.mkdir(parents=True, exist_ok=True)
    lease_path = destination / "deadline-guard.json"
    if lease_path.exists():
        lease = json.loads(lease_path.read_text())
        if (lease.get("deadline_at") == deadline and lease.get("process_identity")
                and process_identity(lease.get("pid")) == lease["process_identity"]):
            return lease
    with (destination / "deadline-guard.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", __name__, "--database", str(workspace.store.path),
            "--race-id", race_id, "--deadline", str(deadline), "--directory", str(destination)],
            cwd=workspace.directory, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    lease = {"race_id": race_id, "deadline_at": deadline, "pid": process.pid,
        "process_identity": process_identity(process.pid), "armed_at": time.time()}
    atomic_json(lease_path, lease)
    return lease


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--race-id", required=True)
    parser.add_argument("--deadline", required=True, type=float)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    enforce(args.database, args.race_id, args.deadline, args.directory)
