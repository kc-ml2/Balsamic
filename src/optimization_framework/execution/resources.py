"""One accounting projection for standalone jobs and shared study grants.

Immutable reservations explain admission. Trial/diagnostic state is the current
projection of spending and unspent reservations; adding the containing grant to
its child reservations would count the same authorization twice.
"""
import time

from optimization_framework.contracts.resources import ExecutionGrant
from optimization_framework.storage.sqlite import now


ACTIVE = {"queued", "running", "pausing", "stopping"}


def spent(trial):
    if trial.get("isolation_policy"):
        return trial.get("execution_seconds", 0)  # host observation, not a namespace clock or recovery allowance
    return max(trial.get("execution_seconds", 0), (trial.get("progress") or {}).get("elapsed_seconds", 0),
               (trial.get("result") or {}).get("elapsed_seconds", 0))


def budget_spent(trial):
    return max(spent(trial), trial.get("execution_seconds_upper_bound", 0))


class ResourceLedger:
    def __init__(self, store):
        self.store = store

    def members(self, campaign_id, *, exclude=None):
        totals = {}
        for diagnostic in self.store.list("fixed_mask_job", campaign_id):
            actual = diagnostic.get("execution_seconds", 0)
            row = totals.setdefault(None, {"actual": 0.0, "committed": 0.0})
            row["actual"] += actual
            row["committed"] += max(actual, diagnostic["wall_seconds"]) if diagnostic["status"] in {"queued", "starting", "running"} else actual
        for trial in self.store.list_trial_costs(campaign_id, exclude=exclude):
            grant = trial.get("execution_grant_id")
            actual = spent(trial)
            row = totals.setdefault(grant, {"actual": 0.0, "committed": 0.0})
            row["actual"] += actual
            row["committed"] += max(budget_spent(trial), trial["wall_seconds"]) if trial["status"] in ACTIVE else budget_spent(trial)
        for diagnostic in self.store.list("diagnostic_grant", campaign_id):
            if diagnostic["status"] != "reserved" or diagnostic["parent_trial_id"] == exclude:
                continue
            grant = diagnostic.get("execution_grant_id")
            row = totals.setdefault(grant, {"actual": 0.0, "committed": 0.0})
            row["committed"] += diagnostic["reserved_seconds"]
        for check in self.store.list("execution_check_grant", campaign_id):
            if check["status"] == "reserved" and check["trial_id"] != exclude:
                row = totals.setdefault(check["execution_grant_id"], {"actual": 0.0, "committed": 0.0})
                row["committed"] += check["reserved_seconds"]
        return totals

    def assessment(self, campaign_id, *, exclude=None):
        members = self.members(campaign_id, exclude=exclude)
        result = {"actual_seconds": sum(row["actual"] for row in members.values()), "allocated_seconds": 0.0, "grants": []}
        releases = {row["grant_id"] for row in self.store.list("execution_grant_release", campaign_id)}
        for grant in self.store.list("execution_grant", campaign_id):
            row = members.pop(grant["id"], {"actual": 0.0, "committed": 0.0})
            committed = row["committed"] if grant["id"] in releases else max(grant["worker_seconds"], row["committed"])
            result["allocated_seconds"] += committed
            result["grants"].append({"grant_id": grant["id"], "actual_seconds": row["actual"],
                "allocated_seconds": committed, "member_committed_seconds": row["committed"],
                "available_seconds": max(0, grant["worker_seconds"] - row["committed"]),
                "released": grant["id"] in releases, "deadline_at": grant["deadline_at"]})
        if set(members) - {None}:
            raise ValueError("A job references an unavailable execution grant")
        result["allocated_seconds"] += members.get(None, {}).get("committed", 0)
        return result

    def create(self, grant):
        grant = grant if isinstance(grant, ExecutionGrant) else ExecutionGrant(**grant)
        raw = grant.model_dump(mode="json")
        with self.store.transaction():
            try:
                previous = self.store.get(grant.id, "execution_grant")
            except KeyError:
                previous = None
            if previous:
                if {k: v for k, v in previous.items() if k != "content_hash"} != raw:
                    raise ValueError("An execution grant cannot change after activation")
                return previous
            campaign = self.store.get(grant.campaign_id, "campaign")
            remaining = campaign["compute_budget_seconds"] - self.assessment(grant.campaign_id)["allocated_seconds"]
            # Study grants include their declared validation allocation. Their
            # sub-reservations determine its use; no second reserve is added.
            if grant.worker_seconds > remaining + 1e-6:
                raise ValueError("Execution grant exceeds the campaign's uncommitted compute")
            return self.store.put_immutable("execution_grant", raw, "execution.grant_created")

    def check(self, grant_id, campaign_id, seconds, *, exclude=None, deadline_at=None):
        grant = self.store.get(grant_id, "execution_grant")
        if grant["campaign_id"] != campaign_id:
            raise ValueError("Execution grant belongs to another campaign")
        if any(row["grant_id"] == grant_id for row in self.store.list("execution_grant_release", campaign_id)):
            raise ValueError("The execution grant has been released")
        deadline = min(grant["deadline_at"], deadline_at if deadline_at is not None else grant["deadline_at"])
        if time.time() < grant["starts_at"] or time.time() >= deadline:
            raise ValueError("The fixed execution admission window is closed")
        committed = self.members(campaign_id, exclude=exclude).get(grant_id, {}).get("committed", 0)
        if seconds + committed > grant["worker_seconds"] + 1e-6:
            raise ValueError("Requested allocation exceeds the execution grant's remaining worker seconds")
        return grant

    def reserve_trial(self, trial):
        identity = "reservation_" + trial["id"]
        return self.store.put_immutable("resource_reservation", {"id": identity, "campaign_id": trial["campaign_id"],
            "owner_id": trial["id"], "owner_kind": "trial", "worker_seconds": trial["wall_seconds"],
            "grant_id": trial.get("execution_grant_id"), "parent_reservation_id": trial.get("diagnostic_grant_id"),
            "created_at": trial["created_at"]}, "resource.reserved")

    def release(self, grant_id, *, rationale):
        with self.store.transaction():
            grant = self.store.get(grant_id, "execution_grant")
            identity = "release_" + grant_id
            try:
                return self.store.get(identity, "execution_grant_release")
            except KeyError:
                pass
            active = [row for row in self.store.list_trials_in_status(ACTIVE, grant["campaign_id"])
                      if row.get("execution_grant_id") == grant_id]
            pending = [row for row in self.store.list("diagnostic_grant", grant["campaign_id"])
                       if row.get("execution_grant_id") == grant_id and row["status"] == "reserved"]
            pending += [row for row in self.store.list("execution_check_grant", grant["campaign_id"])
                        if row["execution_grant_id"] == grant_id and row["status"] == "reserved"]
            if active or pending:
                raise ValueError("An execution grant cannot be released while it owns active reservations")
            return self.store.put_immutable("execution_grant_release", {"id": identity, "grant_id": grant_id,
                "campaign_id": grant["campaign_id"], "rationale": rationale, "created_at": now()}, "execution.grant_released")
