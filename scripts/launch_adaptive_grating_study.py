"""Freeze the approved protocol and launch its durable preflight operators.

Re-running reconciles saved command receipts and verified operator PIDs. The
workspace controller owns development, confirmation, reporting, and cutoff.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import subprocess
import sys

from run_adaptive_grating_preflight import DurableCommands, ReadOnlyRecords, read_json
from optimization_framework.execution.race_guard import process_identity
from optimization_framework.storage.artifacts import atomic_json


def launch_operator(directory, name, arguments):
    receipt_path = directory / (name + "-process.json")
    previous = read_json(receipt_path, {})
    if previous.get("process_identity") and process_identity(previous.get("pid")) == previous["process_identity"]:
        return previous
    with (directory / (name + ".log")).open("ab") as log:
        child = subprocess.Popen([sys.executable, *arguments], stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    receipt = {"pid": child.pid, "process_identity": process_identity(child.pid), "arguments": arguments}
    atomic_json(receipt_path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("runs/pilots/adaptive-20261001"))
    parser.add_argument("--pilot", type=Path, default=Path("runs/pilots/dual-pol-long-20261001"))
    parser.add_argument("--database", type=Path, default=Path("runs/workspace/workspace/workspace.sqlite3"))
    parser.add_argument("--workspace-url", default="http://127.0.0.1:8765")
    parser.add_argument("--create-only", action="store_true")
    args = parser.parse_args()
    directory, pilot, database = args.directory.resolve(), args.pilot.resolve(), args.database.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = read_json(pilot / "manifest.json")
    fixtures = read_json(directory / "preflight" / "preflight.json")
    if not fixtures or any(not row.get("asset_id") for row in fixtures["fixtures"]):
        raise ValueError("Run the numerical preflight operator with --prepare-only to freeze nine actual-mask assets first")
    records = ReadOnlyRecords(database)
    campaign_id = manifest["campaign_id"]
    commands = DurableCommands(args.workspace_url, campaign_id, directory / "launch-command-receipts", records,
        namespace=str(directory))
    try:
        campaign = records.get(campaign_id, "campaign")
        authorization_path = directory / "authorization.json"
        authorization = read_json(authorization_path)
        if authorization is None:
            authorization = {"compute_budget_seconds": campaign["compute_budget_seconds"] + 230400}
            atomic_json(authorization_path, authorization)
        budget = commands.execute("authorize_protocol_envelope", "campaign.update", {
            "compute_budget_seconds": authorization["compute_budget_seconds"],
            "delegated_trial_seconds": 3600,
            "rationale": "Execute the researcher-approved adaptive protocol: sixteen elapsed hours, at most four workers, "
                "and at most 230400 new worker seconds. H12 stays held and its implementation allocation is unchanged."})
        grouped = defaultdict(list)
        for row in manifest["created"]:
            grouped[row["algorithm"]].append(row)
        configurations = [{"id": algorithm, "algorithm": algorithm, "algorithm_config": rows[0]["algorithm_config"],
            "hypothesis_id": rows[0]["hypothesis_id"], "pilot_trial_ids": [row["trial_id"] for row in rows]}
            for algorithm, rows in sorted(grouped.items())]
        created = commands.execute("freeze_adaptive_protocol", "study.race.create", {
            "task_id": manifest["task_id"], "configurations": configurations,
            "baseline_configuration_id": "flrl_lsf_random",
            "preflight_subject_trial_ids": [row["source_trial_id"] for row in fixtures["fixtures"]],
            "preflight_source_asset_ids": [row["asset_id"] for row in fixtures["fixtures"]],
            "numerical_parameters": {"fidelities": [{"rcwa_order_x": x, "rcwa_order_y": y} for x, y in ((10,5),(12,6),(14,7))]},
            "rationale": "Run docs/adaptive-algorithm-testing.md: test numerical stability first, measure safe concurrency, "
                "protect undertrained methods, adapt development effort, freeze two finalists, confirm on ten fresh seeds, "
                "and validate fixed-horizon masks. Four-hour batches with a sixteen-hour elapsed cutoff; H12 remains held."})
        launch = {"race_id": created["race_id"], "study_id": created["study_id"], "budget": budget,
            "deadline_at": created["race"]["deadline_at"], "plan": str(Path("docs/adaptive-algorithm-testing.md").resolve())}
        current_race = records.get(created["race_id"], "adaptive_race")
        if not args.create_only and current_race["status"] in {"preflight", "paused"} and current_race["preflight"]["status"] != "passed":
            common = ["--database", str(database), "--workspace-url", args.workspace_url, "--campaign-id", campaign_id,
                "--race-id", created["race_id"]]
            launch["preflight_operator"] = launch_operator(directory, "preflight-operator", [
                "scripts/run_adaptive_grating_preflight.py", *common, "--task-id", manifest["task_id"],
                "--directory", str(directory / "preflight"), "--pilot-results", str(pilot / "results.json"),
                "--resource-profile", str(directory / "resources" / "profile.json")])
            launch["calibration_operator"] = launch_operator(directory, "calibration-operator", [
                "scripts/run_adaptive_resource_calibration.py", "--database", str(database),
                "--url", args.workspace_url, "--campaign-id", campaign_id, "--race-id", created["race_id"],
                "--output", str(directory / "resources"),
                "--preflight-manifest", str(directory / "preflight" / "preflight.json")])
        atomic_json(directory / "launch.json", launch)
        print(json.dumps({key: value for key, value in launch.items() if key != "budget"}, indent=2))
    finally:
        commands.close()


if __name__ == "__main__":
    main()
