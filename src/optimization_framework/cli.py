"""Local and connected CLI adapters to the researcher command boundary.

Local mode holds the workspace scheduler lease. Connected mode never opens the
server's database. Both retain the exact command before attempting admission.
"""
import argparse
import fcntl
from contextlib import AbstractContextManager
import json
from pathlib import Path
import shutil
import time
from urllib.parse import quote
import uuid

import httpx

from optimization_framework.contracts.base import canonical_json
from optimization_framework.contracts.commands import Command
from optimization_framework.storage.artifacts import atomic_json
from optimization_framework.storage.bundles import Reader
from optimization_framework.storage.sqlite import read_json


class Session(AbstractContextManager):
    def __init__(self, *, directory=None, url=None, journal=None):
        if bool(directory) == bool(url):
            raise ValueError("Select one local workspace directory or workspace URL")
        self.directory = Path(directory).resolve() if directory else None
        self.url = url.rstrip("/") if url else None
        if self.url and httpx.URL(self.url).scheme not in {"http", "https"}:
            raise ValueError("A workspace URL must use HTTP or HTTPS")
        self.journal = Path(journal or self.directory / "cli-commands").resolve() if journal or self.directory else None
        if self.journal is None:
            raise ValueError("Connected commands need a local command journal directory")
        self.workspace = self.http = None

    def __enter__(self):
        if self.directory:
            from optimization_framework.execution.service import Workspace
            self.workspace = Workspace(self.directory)
            try:
                self.workspace.start()
            except RuntimeError as exc:
                self.workspace = None
                raise RuntimeError("Another service owns this workspace; use --workspace-url to connect to it") from exc
        else:
            self.http = httpx.Client(base_url=self.url, timeout=60)
        try:
            self.workspace_id = self.state().get("workspace_id")
            if not self.workspace_id:
                raise ValueError("The service does not expose a stable workspace identity")
            self.journal.mkdir(parents=True, exist_ok=True)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.workspace:
            self.workspace.close()
        if self.http:
            self.http.close()

    def request(self, method, path, **kwargs):
        response = self.http.request(method, path, headers={"X-Workspace-Id": self.workspace_id} if hasattr(self, "workspace_id") else {}, **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json().get("detail", response.text)
            except ValueError:
                message = response.text
            if response.status_code == 404:
                raise KeyError(message)
            raise ValueError(f"Workspace rejected the request ({response.status_code}): {message}")
        return response

    def state(self, campaign_id=None):
        if self.workspace:
            store = self.workspace.store
            campaigns = store.list("campaign")
            campaign = store.get(campaign_id, "campaign") if campaign_id else (campaigns[-1] if campaigns else None)
            return {"workspace_id": store.identity(), "campaigns": campaigns, "campaign": campaign,
                **{name: store.list(kind, campaign["id"]) if campaign else [] for kind, name in
                   (("task", "tasks"), ("trial", "trials"), ("study", "studies"))}}
        campaigns = self.request("GET", "/api/v1/campaigns").json()["campaigns"]
        current = campaign_id or (campaigns[-1]["id"] if campaigns else None)
        workspace_id = self.request("GET", "/api/v1/workspace").json()["workspace_id"]
        if not current:
            return {"workspace_id": workspace_id, "campaigns": campaigns, "campaign": None, "tasks": [], "trials": [], "studies": []}
        summary = self.request("GET", f"/api/v1/campaigns/{quote(current, safe='')}/summary").json()
        return {"workspace_id": workspace_id, "campaigns": campaigns, "campaign": summary["campaign"], "tasks": summary["tasks"],
                "trials": summary["trials"], "studies": summary["studies"]}

    def trial(self, campaign_id, identity):
        """One run's whole record; state() lists runs without their designs."""
        if self.workspace:
            trial = self.workspace.store.get(identity, "trial")
        else:
            trial = self.request("GET", f"/api/v1/experiments/{quote(identity, safe='')}").json()["trial"]
        if trial["campaign_id"] != campaign_id:
            raise KeyError(f"{identity} is not an experiment of {campaign_id}")
        return trial

    def submit(self, request):
        command = request if isinstance(request, Command) else Command.model_validate(request)
        with (self.journal / (command.id + ".lock")).open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._submit(command)

    def _submit(self, command):
        raw = command.model_dump(mode="json")
        path = self.journal / (command.id + ".json")
        saved = read_json(path)
        if saved:
            if saved["workspace_id"] != self.workspace_id or canonical_json(saved["request"]) != canonical_json(raw):
                raise ValueError("This command journal belongs to a different workspace or request")
        else:
            atomic_json(path, {"workspace_id": self.workspace_id, "request": raw})
        # Always reconcile before retrying. Neither a later charter nor an
        # uncertain response authorizes another command with a new identity.
        if self.workspace:
            accepted = self.workspace.commands.execute(command, actor="researcher")
        else:
            try:
                accepted = self.request("GET", "/api/v1/commands/" + quote(command.id, safe="")).json()
            except KeyError:
                accepted = self.request("POST", "/api/v1/commands", json=raw).json()
            if accepted.get("actor") != "researcher" or canonical_json(accepted.get("request")) != canonical_json(raw):
                raise ValueError("The accepted receipt differs from the saved researcher request")
        atomic_json(path, {"workspace_id": self.workspace_id, "request": raw, "receipt": accepted})
        return accepted

    def command(self, operation, campaign_id, payload, *, identity=None):
        identity = identity or "cli_" + uuid.uuid4().hex
        saved = read_json(self.journal / (identity + ".json"))
        if saved:
            request = saved["request"]
            if request["operation"] != operation or request["campaign_id"] != campaign_id or canonical_json(request["payload"]) != canonical_json(payload):
                raise ValueError("The saved CLI operation differs from this request")
            return self.submit(request)["outcome"]
        version = 0 if operation == "campaign.create" else self.state(campaign_id)["campaign"]["version"]
        return self.submit(Command(id=identity, operation=operation, campaign_id=campaign_id,
            expected_revision=version, payload=payload))["outcome"]

    def wait_trial(self, campaign_id, identity):
        capture_deadline = None
        while True:
            trial = self.trial(campaign_id, identity)
            if trial["status"] not in {"queued", "running", "pausing", "stopping"}:
                if trial.get("asset_capture_attempt", -1) >= trial.get("attempt", 0):
                    return trial
                if capture_deadline is None:
                    capture_deadline = time.monotonic() + 30
                if self.workspace:
                    self.workspace.capture_evidence(trial)
                if time.monotonic() > capture_deadline:
                    raise RuntimeError(f"Experiment {identity} finished but evidence capture needs attention; its recorded work is retained")
            time.sleep(.2)

    def wait_command(self, identity):
        while True:
            delivery = (self.workspace.commands.delivery(identity) if self.workspace else
                self.request("GET", "/api/v1/commands/" + quote(identity, safe="") + "/delivery").json())
            if not delivery["pending"] or delivery["requires_attention"] and not delivery["active"]:
                return delivery
            time.sleep(.2)

    def assets(self, campaign_id):
        if self.workspace:
            return self.workspace.assets.visible(campaign_id)
        return self.request("GET", "/api/v1/assets", params={"campaign_id": campaign_id}).json()

    def asset(self, identity):
        if self.workspace:
            return self.workspace.store.get(identity, "asset")
        return self.request("GET", "/api/v1/assets/" + quote(identity, safe="")).json()["asset"]

    def operation(self, identity):
        while True:
            operation = (self.workspace.store.get(identity, "bundle_operation") if self.workspace else
                self.request("GET", "/api/v1/bundles/operations/" + identity).json())
            if operation["status"] in {"completed", "failed"}:
                if operation["status"] == "failed":
                    raise ValueError(operation.get("error", "Evidence transfer failed"))
                return operation
            parent = operation.get("command_id", identity.removeprefix("bundle_op_"))
            delivery = (self.workspace.commands.delivery(parent) if self.workspace else
                self.request("GET", "/api/v1/commands/" + quote(parent, safe="") + "/delivery").json())
            if delivery["requires_attention"]:
                raise RuntimeError(f"Evidence operation {identity} needs attention; its accepted request and delivery are retained")
            time.sleep(.2)

    def import_bundle(self, path, campaign_id, *, identity):
        with Path(path).open("rb") as stream:
            uploaded = (self.workspace.bundles.upload(stream) if self.workspace else
                self.request("POST", "/api/v1/bundle-uploads", content=stream).json())
        inspected = self.command("bundle.inspect", campaign_id, {"upload_id": uploaded["upload_id"]}, identity=identity + "_inspect")
        inspection = self.operation(inspected["operation_id"])
        published = self.command("bundle.publish", campaign_id, {"inspection_id": inspection["inspection_id"]}, identity=identity + "_publish")
        return self.operation(published["operation_id"])

    def export_trial(self, trial, output):
        """Legacy files are projections from a verified portable evidence bundle."""
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        assets = [row["id"] for row in self.assets(trial["campaign_id"]) if row.get("producer_id") == trial["id"]]
        if not assets:
            raise ValueError("No committed experiment artifacts are available; inspect the recorded job failure before exporting")
        receipt = self.command("bundle.export", trial["campaign_id"], {"asset_ids": sorted(assets)},
            identity=f"cli_export_{trial['id']}_{trial['attempt']}")
        identity = receipt["operation_id"]
        operation = self.operation(identity)
        archive_path = output / "evidence.zip"
        if self.workspace:
            from optimization_framework.contracts.experiments import ArtifactReference
            with self.workspace.assets.artifacts.open(ArtifactReference(**operation["archive"])) as source, archive_path.open("wb") as target:
                shutil.copyfileobj(source, target)
        else:
            with self.http.stream("GET", "/api/v1/bundles/operations/" + identity + "/download",
                    headers={"X-Workspace-Id": self.workspace_id}) as response:
                response.raise_for_status()
                with archive_path.open("wb") as target:
                    for chunk in response.iter_bytes():
                        target.write(chunk)
        with Reader(archive_path) as reader:
            records = reader.verify()
            roots = {row.reference.key for row in records if row.reference.kind == "trial" and row.reference.id == trial["id"]}
            captures = [capture for capture in reader.manifest.captures if capture.record_key in roots and capture.purpose == "experiment"]
            if len(captures) != 1:
                raise ValueError("The export does not contain one exact capture of the selected experiment")
            blobs = {blob.sha256: blob for blob in reader.manifest.blobs}
            for name, checksum in captures[0].files.items():
                target = output / name
                if not target.resolve().is_relative_to(output):
                    raise ValueError("An export destination escapes the selected directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                with reader.open_blob(blobs[checksum]) as source, target.open("wb") as stream:
                    shutil.copyfileobj(source, stream)
        atomic_json(output / "workspace-trial.json", {"workspace_id": self.workspace_id,
            "campaign_id": trial["campaign_id"], "trial_id": trial["id"], "record": trial,
            "connection": {"directory": str(self.directory) if self.directory else None, "url": self.url}})


def add_connection_options(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--workspace", type=Path, help="Own this durable local workspace under its scheduler lease")
    group.add_argument("--workspace-url", help="Submit commands to an already running workspace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_options(parser)
    parser.add_argument("--journal", type=Path, help="Local directory retaining submitted envelopes and receipts")
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("command", help="Submit any typed researcher command, including study and implementation work")
    command.add_argument("request", type=Path, help="JSON Command envelope with a stable id and explicit preconditions")
    command.add_argument("--no-wait", action="store_true", help="Return after admission (connected mode only)")
    state = sub.add_parser("state")
    state.add_argument("--campaign")
    args = parser.parse_args()
    if args.action == "command" and args.no_wait and not args.workspace_url:
        parser.error("--no-wait requires a running service selected with --workspace-url")
    with Session(directory=args.workspace, url=args.workspace_url, journal=args.journal) as session:
        result = session.submit(json.loads(args.request.read_text())) if args.action == "command" else session.state(args.campaign)
        if args.action == "command" and not args.no_wait:
            result = {"receipt": result, "delivery": session.wait_command(result["id"])}
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
