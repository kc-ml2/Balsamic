"""How hard the machine may be used, what each kind of run uses, and whether the next run fits now.

The researcher chooses one level for the whole server. Code turns it into limits, measures what every
run used, and starts a queued run only when what it is expected to use fits beside what is running.
Usage is measured, not declared, and nothing here knows any problem: a run's kind is its problem as
configured and its method with their settings, and a kind's expected use is the most any of its runs
used, with a margin. A run of a kind never measured starts while no other such run is still starting;
once it has made a few evaluations, or after a minute, its own live use counts as its expectation.

The launcher's workers setting stays the researcher's hard maximum on runs at once, split into GPU and CPU
slots (execution/devices.py); within a free slot, memory decides, and only a run on the GPU is held for GPU
memory. Each campaign's resource planner (a page agent) may plan fewer runs and give a kind never
measured an estimate. When several campaigns have runs waiting, each gets an even share of the runs at
once. Processes outside the platform are seen only in what the host reports free, which is what the
level's headroom is for.
"""
import math
import os
import re
import subprocess
import time

from pydantic import Field

from optimization_framework.contracts.base import Contract, content_hash
from optimization_framework.storage.sqlite import now

GIB = 1024 ** 3
LEVELS = {
    "cautious": {"label": "Cautious", "target": .60, "headroom_bytes": 8 * GIB, "implementation_jobs": 1, "unmeasured_alone": True,
                 "estimate_margin": 1.5, "retry_out_of_memory": False,
                 "summary": "Uses at most 60% of memory and keeps 8 GiB free; a run of a kind never measured starts alone, and a run that "
                            "runs out of memory is not run again."},
    "balanced": {"label": "Balanced", "target": .80, "headroom_bytes": 4 * GIB, "implementation_jobs": 2, "unmeasured_alone": False,
                 "estimate_margin": 1.5, "retry_out_of_memory": True,
                 "summary": "Uses at most 80% of memory and keeps 4 GiB free; two implementation jobs at once; a run that runs out of "
                            "memory is queued again once."},
    "aggressive": {"label": "Aggressive", "target": .92, "headroom_bytes": 2 * GIB, "implementation_jobs": 4, "unmeasured_alone": False,
                   "estimate_margin": 1., "retry_out_of_memory": True,
                   "summary": "Uses at most 92% of memory and keeps 2 GiB free; four implementation jobs at once; a run that runs out of "
                              "memory is queued again once."},
}
DEFAULT = "balanced"
POLICY = "resource_policy"
USAGE = "job_usage"
MARGIN = 1.25            # a kind is expected to use this much more than the most it was measured using
SETTLING_SECONDS = 60.   # a run of a kind never measured counts by its live use after this long,
SETTLING_EVALUATIONS = 3  # or after this many evaluations, whichever comes first
OUT_OF_MEMORY = re.compile(r"out of memory|OutOfMemory|MemoryError", re.I)
LONG_WAIT_SECONDS = 600.  # a queued run waiting this long is brought to the resource planner
PLAN, ESTIMATE = "concurrency_plan", "usage_estimate"
# What wakes a campaign's resource planner (agents/controller.py routes these events to it).
SIGNALS = ("resources.unmeasured_kind", "resources.out_of_memory", "resources.level_changed", "resources.long_wait")


class ResourcePlanInput(Contract):
    """The resource planner's plan for its campaign, inside the researcher's level and worker maximum."""
    workers: int | None = Field(default=None, ge=1, le=16,
        description="Runs of this campaign at once; at most the launcher's workers setting. Omit for no limit of the plan's own.")
    kinds: dict[str, int] = Field(default_factory=dict, max_length=200,
        description="Most runs of a kind at once, by kind id as resource_inspect lists it (admission.kinds and admission.running)")
    rationale: str = Field(min_length=1, max_length=4000)


class UsageEstimateInput(Contract):
    """What a kind never measured is expected to use, until a run of it is measured."""
    kind: str = Field(pattern=r"^kind_[0-9a-f]{24}$")
    memory_bytes: float = Field(gt=0, le=2 ** 44, description="Host memory of one run's whole process tree, at its peak")
    device_bytes: float | None = Field(default=None, gt=0, le=2 ** 42, description="GPU memory of one run at its peak, if it uses a GPU")
    rationale: str = Field(min_length=1, max_length=4000, description="How the numbers were reached, e.g. from nvidia-smi and the process list")


def tree_rss(pid, proc_root="/proc"):
    """Resident memory of a process and all its descendants, in bytes; None when the process is gone."""
    total, stack, seen = 0, [pid], set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            with open(f"{proc_root}/{current}/status") as stream:
                total += next((int(line.split()[1]) * 1024 for line in stream if line.startswith("VmRSS:")), 0)
            for task in os.listdir(f"{proc_root}/{current}/task"):
                with open(f"{proc_root}/{current}/task/{task}/children") as stream:
                    stack.extend(int(value) for value in stream.read().split())
        except (OSError, ValueError):
            if current == pid:
                return None
    return total


def kind(trial, task):
    """What a run's use depends on: the problem as configured, and the method with its settings."""
    return "kind_" + content_hash([task.get("problem") or task.get("physics"), trial.get("algorithm") or "",
        trial.get("implementation_version_id"), trial.get("algorithm_config") or {}, trial.get("training") or {},
        trial.get("numerical_threads", 1)])[:24]


def gib(value):
    return f"{value / GIB:.1f} GiB"


def gpu_processes():
    """Processes holding GPU memory, as nvidia-smi lists them. Under CUDA MPS the runs show as one MPS server."""
    try:
        output = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=3, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) == 3 and fields[0].isdigit():
            rows.append({"pid": int(fields[0]), "name": fields[1], "memory_bytes": int(fields[2]) * 2 ** 20 if fields[2].isdigit() else None})
    return rows


class Admission:
    def __init__(self, workspace, *, proc_root="/proc", clock=time.monotonic, host=None):
        self.workspace, self.store = workspace, workspace.store
        self.proc_root, self.clock = proc_root, clock
        self.host_reader = host            # tests pass a fake host; otherwise the resource observer's readings
        self.live = {}                     # trial id -> what is being followed while it runs
        self.waiting = {}                  # queued trial id -> why it has not started
        self.sampled_at = -float("inf")
        self.host_at, self.host_cache = -float("inf"), None
        self.usage_cache = (None, {})
        self.pushed = (None, -float("inf"))   # implementation concurrency last sent, and when
        self.caches = {}                      # record kind -> (write count, value)
        self.signalled = set()                # signals already raised in this process
        self.wait_since = {}                  # queued trial id -> when it first waited

    # The level -------------------------------------------------------------------------------------------

    def _cached(self, kind, load):
        """A value read from records only this process writes, kept until one of them is written."""
        revision = self.store.revision(kind)
        if kind not in self.caches or self.caches[kind][0] != revision:
            self.caches[kind] = (revision, load())
        return self.caches[kind][1]

    def level(self):
        def load():
            try:
                return self.store.get(POLICY, POLICY)
            except KeyError:
                return {"level": DEFAULT, "set_by": None, "updated_at": None}
        record = self._cached(POLICY, load)
        return {**record, **LEVELS[record["level"]]}

    def set_level(self, level, *, authority, reason=""):
        if level not in LEVELS:
            raise ValueError("Choose cautious, balanced or aggressive")
        previous = self.level()["level"]
        record = {"id": POLICY, "level": level, "previous": previous, "set_by": authority, "reason": reason, "updated_at": now()}
        self.store.put(POLICY, record, "resources.level_changed")
        self.pushed = (None, -float("inf"))
        self.push()
        if level != previous:
            # The level is the server's; each campaign with a resource planner hears of it.
            for campaign_id in sorted({row["campaign_id"] for row in self.store.list("agent_session") if row.get("role") == "resource_planner"}):
                self.store.event(campaign_id, "resources.level_changed", {"record_id": POLICY, "level": level, "previous": previous,
                    "reason": reason})
        return self.view()

    # The resource planner's plan and estimates -----------------------------------------------------------

    def plan_of(self, campaign_id):
        return self._cached(PLAN, lambda: {row["campaign_id"]: row for row in self.store.list(PLAN)}).get(campaign_id) or {}

    def _estimates(self):
        return self._cached(ESTIMATE, lambda: {row["kind"]: row for row in self.store.list(ESTIMATE)})

    def plan(self, campaign_id, values, *, authority):
        """Record a campaign's plan; it holds runs back, it never starts one."""
        limit = getattr(self.workspace, "max_workers", None)
        if values.workers and limit and values.workers > limit:
            raise ValueError(f"The launcher's workers setting allows at most {limit} runs at once; that maximum is the researcher's")
        if any(count < 1 for count in values.kinds.values()):
            raise ValueError("A kind's limit is at least one run at once; to stop a kind, ask whoever started its runs")
        previous = self.plan_of(campaign_id)
        record = {"id": PLAN + "_" + campaign_id, "campaign_id": campaign_id, "workers": values.workers, "kinds": values.kinds,
                  "rationale": values.rationale, "set_by": authority, "revision": previous.get("revision", 0) + 1, "updated_at": now()}
        self.store.put(PLAN, record, "resources.plan_changed")
        return record

    def estimate(self, campaign_id, values, *, authority):
        """Record what a kind is expected to use before any run of it is measured; a measurement replaces it."""
        record = {"id": ESTIMATE + "_" + values.kind, "campaign_id": campaign_id, "kind": values.kind, "memory_bytes": values.memory_bytes,
                  "device_bytes": values.device_bytes, "rationale": values.rationale, "set_by": authority, "created_at": now()}
        self.store.put(ESTIMATE, record, "resources.estimate_recorded")
        return {**record, "measured_runs": len(self._usage().get(values.kind, [])),
                "note": "A measured run of this kind replaces the estimate."}

    def _signal(self, campaign_id, kind, key, data):
        """Raise an event for the campaign's resource planner once."""
        identity = (campaign_id, kind, key)
        if identity in self.signalled:
            return
        self.signalled.add(identity)
        self.store.event(campaign_id, kind, data)

    def push(self):
        """Tell the implementation service how many jobs the level allows; retried while it is away."""
        jobs = self.level()["implementation_jobs"]
        if self.pushed[0] == jobs and self.clock() - self.pushed[1] < 300:
            return
        bridge = getattr(self.workspace, "implementations", None)
        client = getattr(bridge, "client", None)
        # Only a service this deployment names: an unconfigured client would reach whatever listens on its default port.
        if client is None or not hasattr(client, "set_concurrency") or not (
                getattr(bridge, "client_given", False) or os.environ.get("GRATING_IMPLEMENTATIONS_URL")):
            return
        try:
            client.set_concurrency(jobs, f"Resource level {self.level()['level']}")
            self.pushed = (jobs, self.clock())
        except Exception:
            self.pushed = (None, self.clock() - 240)   # try again in about a minute

    # Measuring -------------------------------------------------------------------------------------------

    def observe(self, trials, live_statuses):
        """Called on every scheduler pass: follow live runs' memory, and record what each used once it ends."""
        if self.clock() - self.sampled_at < 1:
            return
        self.sampled_at = self.clock()
        self.push()
        current = {trial["id"]: trial for trial in trials if trial["status"] in live_statuses}
        for identity, trial in current.items():
            entry = self.live.get(identity) or self.started(trial)
            entry["evaluations"] = (trial.get("progress") or {}).get("evaluations") or 0
            rss = tree_rss(trial["pid"], self.proc_root) if trial.get("pid") else None
            if rss is not None:
                entry["current"], entry["peak"] = rss, max(entry["peak"], rss)
        for identity in [identity for identity in self.live if identity not in current]:
            self._record(identity, self.live.pop(identity))
        queued = {trial["id"] for trial in trials if trial["status"] == "queued"}
        self.waiting = {identity: reason for identity, reason in self.waiting.items() if identity in queued}
        self.wait_since = {identity: since for identity, since in self.wait_since.items() if identity in queued}

    def started(self, trial):
        """Follow a run from the moment it starts, so that runs admitted in the same pass count against each other."""
        kind_id = self._kind(trial)
        entry = self.live[trial["id"]] = {"kind": kind_id, "campaign_id": trial["campaign_id"], "device": trial.get("device"),
            "started": self.clock(), "label": self._label(trial),
            "started_at": now(), "measured": self.expected(kind_id)["memory_bytes"] is not None, "peak": 0, "current": 0, "evaluations": 0}
        self.waiting.pop(trial["id"], None)
        return entry

    def _kind(self, trial):
        try:
            return kind(trial, self.store.get(trial["task_id"], "task"))
        except KeyError:
            return kind(trial, {})

    def _record(self, identity, entry):
        try:
            trial = self.store.get(identity, "trial")
        except KeyError:
            return
        from optimization_framework.storage.sqlite import read_json
        result = read_json(self.workspace.job_dir(identity) / "result.json", {}) if hasattr(self.workspace, "job_dir") else {}
        reason = str(trial.get("reason") or result.get("reason") or "")
        usage_id = "usage_" + identity
        try:
            earlier = self.store.get(usage_id, USAGE)
        except KeyError:
            earlier = {}
        peaks = [value for value in (earlier.get("memory_peak_bytes"), entry["peak"] or None) if value]
        devices = [value for value in (earlier.get("device_peak_bytes"), result.get("device_peak_bytes")) if value]
        self.store.put(USAGE, {"id": usage_id, "campaign_id": entry["campaign_id"], "subject": "trial", "subject_id": identity,
            "kind": entry["kind"], "label": self._label(trial), "device": trial.get("device"), "memory_peak_bytes": max(peaks) if peaks else None,
            "device_peak_bytes": max(devices) if devices else None, "status": trial.get("status"),
            "out_of_memory": bool(OUT_OF_MEMORY.search(reason)) or earlier.get("out_of_memory", False),
            "started_at": earlier.get("started_at", entry["started_at"]), "ended_at": now(),
            "basis": "Resident memory of the run's process tree, sampled every second; GPU memory as the run's array library counted it"},
            "resources.usage_recorded")
        if OUT_OF_MEMORY.search(reason) and trial.get("status") == "failed":
            retry = self._retry(trial) if self.level()["retry_out_of_memory"] else None
            self._signal(entry["campaign_id"], "resources.out_of_memory", identity, {"record_id": usage_id, "trial_id": identity,
                "kind": entry["kind"], "label": self._label(trial), "reason": reason[:500], "memory_peak_bytes": max(peaks) if peaks else None,
                "device_peak_bytes": max(devices) if devices else None, **(retry or {"retry": None})})

    def _retry(self, trial):
        """Queue an ordinary run that ran out of memory once more, as it was asked for; admission now expects more of its kind."""
        if (trial.get("oom_retry_of") or trial.get("race_id") or trial.get("confirmation_protocol_id") or trial.get("execution_grant_id")
                or trial.get("validation_of") or trial.get("recipe") or trial.get("confirmatory") or not hasattr(self.workspace, "create_trial")):
            return {"retry": None, "retry_note": "Not queued again: it was already a retry, or it belongs to a protocol, race or check."}
        from optimization_framework.contracts.requests import TrialInput
        values = {key: trial[key] for key in TrialInput.model_fields if key in trial and trial[key] is not None}
        values["question"] = (str(trial.get("question") or "") + f"\nQueued again after {trial['id']} ran out of memory.")[:10000]
        try:
            created = self.workspace.create_trial(TrialInput(**values))
        except Exception as error:
            return {"retry": None, "retry_note": f"Could not be queued again: {error}"[:500]}
        created["oom_retry_of"] = trial["id"]
        self.store.put("trial", created, "trial.requeued_after_out_of_memory")
        return {"retry": created["id"], "retry_note": "Queued again once; admission now expects its kind to use at least what it reached."}

    def _label(self, trial):
        try:
            task = self.store.get(trial["task_id"], "task")["name"]
        except KeyError:
            task = trial.get("task_id", "")
        method = trial.get("algorithm") or trial.get("implementation_version_id") or "package"
        return f"{task} · {method}"

    def _usage(self):
        revision = self.store.revision(USAGE)
        if self.usage_cache[0] != revision:   # only this process records usage, so its write count is enough
            kinds = {}
            for row in self.store.list(USAGE):
                kinds.setdefault(row["kind"], []).append(row)
            self.usage_cache = (revision, kinds)
        return self.usage_cache[1]

    def expected(self, kind_id):
        """What a run of this kind is expected to use at most: the most any measured run of it used, with a margin."""
        rows = self._usage().get(kind_id, [])
        memory = max((row["memory_peak_bytes"] for row in rows if row.get("memory_peak_bytes")), default=None)
        device = max((row["device_peak_bytes"] for row in rows if row.get("device_peak_bytes")), default=None)
        expected = {"memory_bytes": memory * MARGIN if memory else None, "device_bytes": device * MARGIN if device else None,
                    "basis": "measured" if memory else None, "runs": len(rows), "out_of_memory": sum(bool(row.get("out_of_memory")) for row in rows),
                    "label": rows[-1]["label"] if rows else None}
        estimate = self._estimates().get(kind_id)
        if estimate:
            margin = self.level()["estimate_margin"]
            # The planner's estimate stands where nothing was measured: for memory until a run is, for the GPU until a run reports it.
            if memory is None:
                expected.update(memory_bytes=estimate["memory_bytes"] * margin, basis="estimated")
            if device is None and estimate.get("device_bytes"):
                expected["device_bytes"] = estimate["device_bytes"] * margin
            expected["estimate"] = {key: estimate.get(key) for key in ("memory_bytes", "device_bytes", "rationale", "set_by", "created_at")}
        return expected

    # Admitting -------------------------------------------------------------------------------------------

    def _host(self):
        if self.clock() - self.host_at < 2 and self.host_cache is not None:
            return self.host_cache
        if self.host_reader is not None:
            reading = self.host_reader()
        else:
            from optimization_framework.execution.observability import get_observer
            reading = get_observer(self.workspace).host()
        memory = reading.get("memory", {})
        devices = [device for device in reading.get("gpu", {}).get("devices", []) if device.get("memory_total_bytes")]
        device = max(devices, key=lambda item: item["memory_total_bytes"], default=None)
        self.host_cache = {"memory_capacity": memory.get("capacity_bytes") or memory.get("total_bytes"),
            "memory_available": memory.get("effective_available_bytes") or memory.get("available_bytes"),
            "device_total": (device or {}).get("memory_total_bytes"), "device_used": (device or {}).get("memory_used_bytes")}
        self.host_at = self.clock()
        return self.host_cache

    def _running(self, entry):
        """What a running run is expected to use: its kind's expectation, or its own live use once it has settled.
        A run on CPU holds no GPU memory, whatever runs of its kind on the GPU used."""
        expected = self.expected(entry["kind"])
        own = entry["peak"] * MARGIN if entry["peak"] else 0
        return max(expected["memory_bytes"] or 0, own), expected["device_bytes"] if entry.get("device") != "cpu" else None

    def fits(self, trial, live, waiting_campaigns=(), *, device=None, held=None):
        """None when a queued run may start now on device; otherwise why it waits, in words (held: no slot of its device is free)."""
        kind_id = self._kind(trial)
        reason = held or self._fits(trial, kind_id, waiting_campaigns, device)
        if reason:
            self.waiting[trial["id"]] = reason
            since = self.wait_since.setdefault(trial["id"], self.clock())
            if self.clock() - since >= LONG_WAIT_SECONDS:
                self._signal(trial["campaign_id"], "resources.long_wait", trial["id"], {"record_id": trial["id"], "trial_id": trial["id"],
                    "kind": kind_id, "label": self._label(trial), "waited_seconds": round(self.clock() - since), "reason": reason})
        else:
            self.waiting.pop(trial["id"], None)
            self.wait_since.pop(trial["id"], None)
        return reason

    def _fits(self, trial, kind_id, waiting_campaigns=(), device=None):
        level = self.level()
        running = list(self.live.values())
        mine = self.expected(kind_id)
        if device == "cpu":
            mine["device_bytes"] = None
        if mine["memory_bytes"] is None:
            self._signal(trial["campaign_id"], "resources.unmeasured_kind", kind_id, {"record_id": trial["id"], "trial_id": trial["id"],
                "kind": kind_id, "label": self._label(trial)})
        campaign = [entry for entry in running if entry["campaign_id"] == trial["campaign_id"]]
        plan = self.plan_of(trial["campaign_id"])
        if plan.get("workers") and len(campaign) >= plan["workers"]:
            return f"Waits: the resource planner runs at most {plan['workers']} of this campaign's runs at once ({plan['rationale'][:200]})."
        cap = (plan.get("kinds") or {}).get(kind_id)
        if cap and sum(entry["kind"] == kind_id for entry in running) >= cap:
            return f"Waits: the resource planner runs at most {cap} of this kind at once ({plan['rationale'][:200]})."
        others = set(waiting_campaigns) - {trial["campaign_id"]}
        limit = getattr(self.workspace, "max_workers", None)
        if others and limit:
            share = max(1, math.ceil(limit / len(others | {trial["campaign_id"]})))
            if len(campaign) >= share:
                return (f"Waits: {len(others)} other campaign{'s' if len(others) > 1 else ''} also {'have' if len(others) > 1 else 'has'} runs "
                        f"waiting, and each campaign gets {share} of the {limit} runs at once.")
        never_measured = mine["basis"] != "measured"
        if never_measured and level["unmeasured_alone"] and running:
            return "At the cautious level a run of a kind never measured before starts only when nothing else runs."
        settling = [entry for entry in running if not entry["measured"] and entry["evaluations"] < SETTLING_EVALUATIONS
                    and self.clock() - entry["started"] < SETTLING_SECONDS]
        if settling and (mine["memory_bytes"] is None or level["unmeasured_alone"]):
            return ("Waits while a run of a kind never measured before shows how much memory it uses "
                    "(its first evaluations, at most a minute).")
        host = self._host()
        need = mine["memory_bytes"] or 0
        if host["memory_capacity"] and host["memory_available"] is not None:
            expected = [self._running(entry) for entry in running]
            planned = sum(memory for memory, _ in expected)
            allowed = level["target"] * host["memory_capacity"]
            if planned + need > allowed:
                return (f"Waits for memory: running work is expected to use {gib(planned)} and this run {gib(need)}, more than the "
                        f"{gib(allowed)} the {level['label'].lower()} level allows ({level['target']:.0%} of {gib(host['memory_capacity'])}).")
            growth = sum(max(0, memory - entry["current"]) for (memory, _), entry in zip(expected, running))
            spare = host["memory_available"] - growth - need
            if spare < level["headroom_bytes"]:
                return (f"Waits for memory: {gib(host['memory_available'])} is free, running work may still grow by {gib(growth)}, "
                        f"and this run needs {gib(need)}, leaving less than the {gib(level['headroom_bytes'])} kept free.")
        if mine["device_bytes"] and host["device_total"]:
            planned = sum(device or 0 for _, device in (self._running(entry) for entry in running))
            allowed = level["target"] * host["device_total"]
            if planned + mine["device_bytes"] > allowed:
                return (f"Waits for GPU memory: running work is expected to use {gib(planned)} and this run {gib(mine['device_bytes'])}, "
                        f"more than the {gib(allowed)} the level allows.")
            if host["device_used"] is not None:
                spare = host["device_total"] - host["device_used"] - mine["device_bytes"]
                headroom = min(level["headroom_bytes"], .1 * host["device_total"])
                if spare < headroom:
                    return (f"Waits for GPU memory: {gib(host['device_total'] - host['device_used'])} is free on the GPU and this run "
                            f"needs {gib(mine['device_bytes'])}, leaving less than the {gib(headroom)} kept free.")
        return None

    # Showing ---------------------------------------------------------------------------------------------

    def view(self, campaign_id=None, *, detail=False):
        """The level, what each kind is expected to use, and why queued runs wait.

        With detail (what the resource planner reads), also each running run's own memory and the GPU's processes.
        """
        level = self.level()
        kinds = []
        known = set(self._usage()) | set(self._estimates()) | {entry["kind"] for entry in self.live.values()}
        for kind_id in known:
            rows = self._usage().get(kind_id, [])
            estimate = self._estimates().get(kind_id)
            if campaign_id and not (any(row.get("campaign_id") == campaign_id for row in rows) or (estimate or {}).get("campaign_id") == campaign_id
                                    or any(entry["kind"] == kind_id and entry["campaign_id"] == campaign_id for entry in self.live.values())):
                continue
            expected = self.expected(kind_id)
            if expected["label"] is None:
                expected["label"] = next((entry.get("label") for entry in self.live.values() if entry["kind"] == kind_id and entry.get("label")), None)
            kinds.append({"kind": kind_id, **expected})
        view = {"level": level["level"], "set_by": level.get("set_by"), "updated_at": level.get("updated_at"),
                "levels": [{"id": key, **{name: value for name, value in item.items()}} for key, item in LEVELS.items()],
                "max_workers": getattr(self.workspace, "max_workers", None),
                "pools": self.workspace.devices.view(self.store.list_trials_in_status({"running", "pausing", "stopping"}))
                         if hasattr(self.workspace, "devices") else None,
                "plan": self.plan_of(campaign_id) if campaign_id else None,
                "waiting": [{"trial_id": identity, "reason": reason} for identity, reason in self.waiting.items()],
                "kinds": sorted(kinds, key=lambda item: -(item["memory_bytes"] or 0))}
        if detail:
            view["running"] = [{"trial_id": identity, "campaign_id": entry["campaign_id"], "kind": entry["kind"], "label": entry.get("label"),
                "device": entry.get("device"),
                "memory_now_bytes": entry["current"], "memory_peak_bytes": entry["peak"], "evaluations": entry["evaluations"],
                "running_seconds": round(self.clock() - entry["started"])} for identity, entry in self.live.items()]
            view["gpu_processes"] = gpu_processes()
            view["gpu_note"] = ("Under CUDA MPS this platform's runs share one MPS server process; other processes on the card are listed "
                                "separately. A GPU total minus other processes, divided among the runs alive, estimates what each run holds.")
        return view
