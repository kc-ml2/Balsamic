"""Where a run's evaluator computes: a pool of GPU slots beside a pool of CPU slots.

The launcher's workers setting is the most runs at once. gpu_workers of those slots run on the GPU, one run each, and the
rest on CPU, where a run takes one slot per numerical thread; a fixed-mask job takes one CPU slot. The scheduler places a
queued run when it starts and records the device on it:
- on the device the run names (TrialInput.device), or the one it started on before (a resumed or retried run);
- on the device of its race or confirmation protocol once any of its runs has started there: those compare runs on time;
- otherwise on the GPU when its evaluator can use one and a GPU slot is free, else on CPU.
An evaluator can use the GPU when its problem adapter declares ``devices = ("cpu", "cuda")``; commissioned evaluators and
runs in an isolated sandbox run on CPU. The worker reads the device from GRATING_DEVICE, and a CPU run sees no CUDA device.

A worker-second on the GPU and one on CPU buy different numbers of evaluations, so the comparison report never compares
runs of different devices on time (analysis/general.py); on evaluation counts they compare as they are.
"""
from __future__ import annotations

import os
from pathlib import Path

DEVICES = ("cpu", "cuda")
LABELS = {"cpu": "CPU", "cuda": "the GPU"}
VARIABLE = "GRATING_DEVICE"


def physical_cores():
    """Physical cores of this host: logical CPUs share a core under simultaneous multithreading."""
    cores = set()
    for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology"):
        try:
            cores.add(((path / "physical_package_id").read_text().strip(), (path / "core_id").read_text().strip()))
        except OSError:
            continue
    return len(cores) or os.cpu_count() or 1


def environment(device):
    """What a worker process of this device is given."""
    return {VARIABLE: device, **({"CUDA_VISIBLE_DEVICES": ""} if device == "cpu" else {})}


def cohort(trial):
    """The protocol whose runs are compared on time and so share one device, if the run belongs to one."""
    for key in ("race_id", "study_execution_id", "confirmation_protocol_id"):
        if trial.get(key):
            return key, trial[key]
    return None


class Pools:
    def __init__(self, workspace):
        self.workspace, self.store = workspace, workspace.store
        self.cohorts = {}   # cohort -> the device its first started run took; read from the store on first need

    def sizes(self):
        """Slots of each device now; at least one stays on CPU, where every kind of run can go."""
        total = self.workspace.max_workers
        gpu = max(0, min(self.workspace.gpu_workers, total - 1))
        return {"cuda": gpu, "cpu": total - gpu}

    def capable(self, trial):
        """The devices a run's evaluator can compute on."""
        if any(trial.get(key) for key in ("isolation_policy", "evaluator_version_id", "evaluator_manifest", "evaluator_requirement_id")):
            return ("cpu",)
        from optimization_framework.evaluation.registry import problems
        try:
            declared = problems.devices((trial.get("problem") or {}).get("definition_id", ""))
        except ValueError:
            return ("cpu",)
        return tuple(device for device in DEVICES if device in declared) or ("cpu",)

    def reachable(self, task):
        """The devices runs of a task may be placed on in this lab: what its evaluator can use, where the lab runs workers."""
        sizes = self.sizes()
        return tuple(device for device in self.capable(task) if sizes[device])

    def check(self, trial):
        """Refuse, when it is queued, a run that names a device it cannot have here."""
        device = trial.get("device")
        if device is None:
            return
        if device not in self.capable(trial):
            raise ValueError(f"This run's evaluator computes only on CPU; it cannot run on {LABELS[device]}")
        if device == "cuda" and not self.sizes()["cuda"]:
            raise ValueError("This lab runs no GPU workers (the launcher's gpu_workers setting is 0); run it on CPU or restart the lab with a GPU worker")

    def cost(self, device, trial):
        threads = trial.get("numerical_threads", 1)
        return 1 if device == "cuda" else max(1, min(threads, self.sizes()["cpu"]))

    def used(self, live, mask_jobs=0):
        """Slots taken by running work. A run started before devices were recorded counts on CPU."""
        used = {"cuda": 0, "cpu": mask_jobs}
        for trial in live:
            device = trial.get("device") or "cpu"
            used[device] += self.cost(device, trial)
        return used

    def _cohort_device(self, trial):
        key = cohort(trial)
        if key is None:
            return None
        if key not in self.cohorts:
            self.cohorts[key] = next((row["device"] for row in self.store.list("trial", trial["campaign_id"])
                                      if row.get(key[0]) == key[1] and row.get("device")), None)
        return self.cohorts[key]

    def place(self, trial, used):
        """(device, None) when a slot of the run's device is free now, else (None, why it waits)."""
        sizes = self.sizes()
        fits = lambda device: used[device] + self.cost(device, trial) <= sizes[device]
        wanted = trial.get("device") or self._cohort_device(trial)
        if wanted:
            if fits(wanted):
                return wanted, None
            if not sizes[wanted]:
                return None, f"Waits for {LABELS[wanted]}, which it is to run on, but this lab now runs no worker there."
            reason = "its race or protocol runs there" if not trial.get("device") else "it runs there"
            return None, f"Waits for a free slot on {LABELS[wanted]} ({reason}; {used[wanted]} of {sizes[wanted]} taken)."
        capable = self.capable(trial)
        for device in ("cuda", "cpu"):
            if device in capable and fits(device):
                return device, None
        shown = " and ".join(f"{used[device]} of {sizes[device]} {'GPU' if device == 'cuda' else 'CPU'} slots"
                             for device in ("cuda", "cpu") if device in capable)
        return None, f"Waits for a free worker slot ({shown} taken)."

    def started(self, trial, device, used):
        trial["device"] = device
        used[device] += self.cost(device, trial)
        if (key := cohort(trial)) is not None and not self.cohorts.get(key):
            self.cohorts[key] = device

    def guidance(self):
        """What the agents that queue and plan runs are told about the slots."""
        sizes = self.sizes()
        if not sizes["cuda"]:
            return f"All {self.workspace.max_workers} slots compute on CPU; a run takes one per numerical thread."
        return (f"Of the {self.workspace.max_workers} slots, {sizes['cuda']} compute{'s' if sizes['cuda'] == 1 else ''} on the GPU and "
                f"{sizes['cpu']} on CPU, where a run takes one per numerical thread. The scheduler places each run when it starts: on the GPU "
                "when its evaluator can use one and a GPU slot is free, else on CPU; set device (cpu or cuda) on a run to choose, and a race or "
                "confirmation protocol keeps one device. The two differ in speed, so plan a run's wall time from the runtime profile's speed "
                "on its device; comparison.report keeps the devices apart on worker_seconds and pools them on evaluation_requests. "
                "Runs placed on the GPU wait for its slot by themselves: do not chain them with dependencies, which wait for the earlier run "
                "to complete scientifically, and a run stopped at its time limit never does.")

    def view(self, live, mask_jobs=0):
        sizes, used = self.sizes(), self.used(live, mask_jobs)
        return {device: {"slots": sizes[device], "taken": used[device]} for device in ("cuda", "cpu")}
