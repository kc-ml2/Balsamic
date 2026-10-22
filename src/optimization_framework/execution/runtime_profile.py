"""Measure how fast a problem's evaluator runs on this host before a long run depends on it.

The platform cannot know how to make each problem's tools faster. For any problem it can
measure how many evaluations per second the host delivers at 1, 2, 4... workers, how busy the
device is meanwhile, and how much of the time and storage is the platform's own. Each device
the problem's runs may be placed on (execution/devices.py) is measured on its own: the GPU
slots up to their number, then the CPU slots up to theirs, each level alone on the host. A profile is a
few short ordinary trials of the built-in random search. Its flags name symptoms, not causes:
they tell the researcher and the agents where to look, and a long run waits until each flag
has been fixed or waived with a reason. A note is a flag about a setting that is the
researcher's to choose, not about waste: it is shown and holds nothing.
"""
from datetime import datetime
import json
import os
import socket
import statistics
import threading
import time

from optimization_framework.contracts.base import content_hash
from optimization_framework.storage.sqlite import identifier, now

RECIPE = "runtime_profile:v1"
KIND = "runtime_efficiency"
THRESHOLDS = {"scaling_gain": 1.15, "busy_percent": 60., "overhead_share": .20, "storage_candidates": 4., "storage_bytes": 65536}
# Speed still rising at the worker ceiling says the ceiling might be raised, and the ceiling is the
# researcher's (the launcher's workers setting); it says nothing about the evaluator wasting anything.
NOTES = {"unsaturated"}
ACTIVE = {"queued", "running", "pausing", "stopping"}
JOURNALS = ("requests.jsonl", "observations.jsonl", "metrics.jsonl", "costs.jsonl")


def gate_seconds():
    """Runs at least this long wait for a runtime profile; None where the deployment sets no gate."""
    value = os.environ.get("GRATING_RUNTIME_GATE_SECONDS", "")
    return float(value) if value.strip() else None


def blocks(flag):
    """Whether a flag holds long runs until it is fixed or waived; a note does not."""
    return flag["id"] not in NOTES


def judged(summary):
    """A profile's verdict under the current rules, so that one measured before a flag became a note reads like a new one."""
    if summary["verdict"] == "inconclusive":
        return "inconclusive"
    return "failed" if any(blocks(flag) for flag in summary["flags"]) else "passed"


NAMES = {"cuda": "GPU", "cpu": "CPU"}


def rungs(ceiling):
    """1, 2, 4... concurrent workers, ending at the workspace's ceiling."""
    counts = [1]
    while counts[-1] * 2 <= ceiling:
        counts.append(counts[-1] * 2)
    return counts if counts[-1] == ceiling else [*counts, ceiling]


def probe(directory, warmup=2):
    """What one probe trial measured, without its first evaluations (process start, device set-up)."""
    from optimization_framework.execution.worker import iter_journal
    rows = [row for row in iter_journal(directory / "costs.jsonl") if row.get("status") == "measured"]
    kept = rows[warmup:]
    quantity = lambda name: [row["quantities"][name] for row in kept if row["quantities"].get(name) is not None]
    candidate = next(iter_journal(directory / "observations.jsonl"), {}).get("candidate")
    artifacts = directory / "artifacts"
    return {"times": [datetime.fromisoformat(row["created_at"]).timestamp() for row in kept],
            "evaluation_seconds": quantity("evaluation_seconds"), "worker_seconds": quantity("worker_seconds"),
            "solver_executions": quantity("solver_executions"), "evaluations": len(rows),
            "bytes": sum((directory / name).stat().st_size for name in JOURNALS if (directory / name).exists())
                     + sum(path.stat().st_size for path in artifacts.rglob("*") if path.is_file()),
            "candidate_bytes": len(json.dumps(candidate, separators=(",", ":"))) if candidate is not None else None}


def rung(workers, probes, samples=()):
    """One concurrency level, measured over the time all of its workers were evaluating."""
    result = {"workers": workers}
    if len(probes) != workers or any(len(item["times"]) < 3 for item in probes):
        return {**result, "status": "inconclusive", "reason": "A probe finished too few evaluations; give the profile longer probes"}
    start, end = max(item["times"][0] for item in probes), min(item["times"][-1] for item in probes)
    counted = sum(start < moment <= end for item in probes for moment in item["times"])
    if end <= start or counted < 2 * workers:
        return {**result, "status": "inconclusive", "reason": "The probes of this level barely overlapped; give the profile longer probes"}
    pooled = lambda name: [value for item in probes for value in item[name]]
    evaluation, worker = pooled("evaluation_seconds"), pooled("worker_seconds")
    during = [sample for sample in samples if start <= sample["at"] <= end]
    mean = lambda name: statistics.fmean(values) if (values := [s[name] for s in during if s.get(name) is not None]) else None
    solves, evaluations = pooled("solver_executions"), sum(item["evaluations"] for item in probes)
    return {**result, "status": "measured", "evaluations_per_second": counted / (end - start),
            "evaluation_seconds": statistics.median(evaluation) if evaluation else None,
            "worker_seconds": statistics.median(worker) if worker else None,
            "overhead_share": max(0., 1 - sum(evaluation) / sum(worker)) if evaluation and sum(worker) > 0 else None,
            "solver_executions_per_evaluation": statistics.fmean(solves) if solves else None,
            "bytes_per_evaluation": sum(item["bytes"] for item in probes) / evaluations if evaluations else None,
            "cpu_percent": mean("cpu_percent"), "cpu_core_percent": core_percent(mean("cpu_percent")),
            "gpu_percent": mean("gpu_percent"), "device_samples": len(during)}


def core_percent(value):
    """Host CPU load as a share of the physical cores: one busy thread per core is a fully used CPU, whatever its sibling does."""
    if value is None:
        return None
    from optimization_framework.execution.devices import physical_cores
    return min(100., value * (os.cpu_count() or 1) / physical_cores())


def derive(levels, ceiling, candidate_bytes=None, thresholds=None, device=None):
    """The recommendation and the flags one device's measured levels support; ceiling is its number of slots."""
    limits = {**THRESHOLDS, **(thresholds or {})}
    measured = [level for level in levels if level.get("status") == "measured"]
    if not measured or measured[0]["workers"] != 1 or len(measured) != len(levels):
        reasons = [level["reason"] for level in levels if level.get("reason")]
        return {"verdict": "inconclusive", "flags": [], "reason": reasons[0] if reasons else "No level was measured"}
    rate = lambda level: level["evaluations_per_second"]
    if device == "cuda":
        busy = lambda level: level.get("gpu_percent")
    elif device == "cpu":
        busy = lambda level: level.get("cpu_core_percent", level.get("cpu_percent"))
    else:   # measured before runs were placed on a device
        busy = lambda level: max((value for value in (level.get("cpu_percent"), level.get("gpu_percent")) if value is not None), default=None)
    idle = lambda level: busy(level) is None or busy(level) < limits["busy_percent"]
    describe = lambda level: "of unknown load" if busy(level) is None else f"{busy(level):.0f}% busy"
    one, best = measured[0], max(measured, key=rate)
    flags = []
    def flag(identity, message, **evidence):
        flags.append({"id": identity, "message": message, "evidence": evidence})
    two = next((level for level in measured if level["workers"] == 2), None)
    if two and rate(two) < limits["scaling_gain"] * rate(one) and idle(two):
        flag("no_scaling", f"Two workers deliver {rate(two):.2f} evaluations/s against {rate(one):.2f} for one, while the device is "
             f"{describe(two)}: concurrent workers are not sharing it.", one=rate(one), two=rate(two), busy_percent=busy(two))
    last = measured[-1]
    if len(measured) > 1 and last["workers"] == ceiling and rate(last) >= limits["scaling_gain"] * rate(measured[-2]):
        flag("unsaturated", f"Throughput was still growing at the ceiling of {ceiling} workers ({rate(measured[-2]):.2f} to "
             f"{rate(last):.2f} evaluations/s): a higher ceiling may give more.", previous=rate(measured[-2]), last=rate(last))
    elif not flags and busy(best) is not None and idle(best):
        flag("idle_device", f"Throughput stops growing at {best['workers']} workers while the device is only {describe(best)}.",
             workers=best["workers"], busy_percent=busy(best))
    if one.get("overhead_share") is not None and one["overhead_share"] > limits["overhead_share"]:
        flag("platform_overhead", f"{100 * one['overhead_share']:.0f}% of each step is spent outside the evaluator "
             "(proposing, journals, checkpoints).", share=one["overhead_share"])
    allowance = max(limits["storage_candidates"] * (candidate_bytes or 0), limits["storage_bytes"])
    if one.get("bytes_per_evaluation") is not None and one["bytes_per_evaluation"] > allowance:
        flag("storage_heavy", f"Each evaluation stores {one['bytes_per_evaluation'] / 1024:.0f} kB, more than "
             f"{limits['storage_candidates']:g} candidates' worth.", bytes_per_evaluation=one["bytes_per_evaluation"], candidate_bytes=candidate_bytes)
    solves = one.get("solver_executions_per_evaluation")
    if solves is not None and solves > 1:
        flag("repeated_solves", f"Each evaluation runs the solver {solves:g} times; check that every execution is needed.", solves=solves)
    recommended = min(level["workers"] for level in measured if rate(level) >= .9 * rate(best))
    chosen = next(level for level in measured if level["workers"] == recommended)
    return {"verdict": "failed" if any(blocks(item) for item in flags) else "passed", "flags": flags, "recommended_workers": recommended,
            "evaluations_per_hour": 3600 * rate(chosen), "candidate_bytes": candidate_bytes}


def combine(parts, candidate_bytes=None):
    """One profile's summary from each device's: the GPU and CPU slots run side by side, so their speeds add."""
    if list(parts) == [None]:
        return parts[None]
    verdicts = {part["verdict"] for part in parts.values()}
    named = len(parts) > 1
    summary = {"verdict": "inconclusive" if "inconclusive" in verdicts else "failed" if "failed" in verdicts else "passed",
               "flags": [{**flag, "device": device, "message": f"{NAMES[device]}: {flag['message']}" if named else flag["message"]}
                         for device, part in parts.items() for flag in part["flags"]],
               "devices": parts}
    if summary["verdict"] == "inconclusive":
        device, part = next((device, part) for device, part in parts.items() if part["verdict"] == "inconclusive")
        summary["reason"] = f"{NAMES[device]}: {part['reason']}" if named else part["reason"]
        return summary
    return {**summary, "recommended_workers": sum(part["recommended_workers"] for part in parts.values()),
            "evaluations_per_hour": sum(part["evaluations_per_hour"] for part in parts.values()), "candidate_bytes": candidate_bytes}


class DeviceSampler:
    """Host CPU and GPU load between successive readings."""

    def __init__(self, gpu, proc_root="/proc"):
        self.gpu, self.proc_root, self.previous = gpu, proc_root, None

    def read(self):
        cpu = None
        try:
            with open(os.path.join(self.proc_root, "stat")) as stream:
                counters = [int(value) for value in stream.readline().split()[1:9]]
            current = (sum(counters), counters[3] + counters[4])
            if self.previous and current[0] > self.previous[0]:
                cpu = 100 * (1 - (current[1] - self.previous[1]) / (current[0] - self.previous[0]))
            self.previous = current
        except (OSError, ValueError, IndexError):
            pass
        loads = [device["utilization_percent"] for device in self.gpu()["devices"] if device.get("utilization_percent") is not None]
        return {"at": time.time(), "cpu_percent": cpu, "gpu_percent": max(loads) if loads else None}


class RuntimeProfiles:
    def __init__(self, workspace):
        self.workspace, self.store = workspace, workspace.store
        self.samples, self.sampling, self.sample_lock = {}, None, threading.Lock()   # device readings of unfinished profiles
        self.running = None   # ids of unfinished profiles; read from the store on first use
        self.source = {}   # what an experiment would be frozen from -> (when it was looked at, its scientific digest)

    def subject(self, task, record=True):
        """What a profile is a measurement of: this evaluator's current source on this host and its settings."""
        from optimization_framework.execution.observability import get_observer
        from optimization_framework.execution.provenance import current_scientific_digest
        task = self.workspace.evaluators.task_view(task)
        version_id = (self.workspace.evaluators.binding(task) or {}).get("version_id")
        # The digest an experiment of this task would record now, so that "the source changed" means here
        # what it means for an experiment and for an equivalence record. A commissioned evaluator is its version.
        frozen = ((), tuple((task.get("evaluator_manifest") or {}).get("recipe_ids", []))) if version_id else (
            ((task.get("problem") or {}).get("definition_id", "meent_grating"),), ())
        if time.monotonic() - self.source.get(frozen, (-float("inf"),))[0] > 5:   # looking at the source on every poll would be its own overhead
            self.source[frozen] = (time.monotonic(), current_scientific_digest(*frozen))
        body = {"campaign_id": task["campaign_id"], "task_id": task["id"],
                "problem": content_hash(task.get("problem") or task.get("physics")), "source": self.source[frozen][1],
                "evaluator_version_id": version_id,
                "host": {"name": socket.gethostname(), "cpus": os.cpu_count(),
                         "gpus": sorted(device["vendor"] for device in get_observer(self.workspace)._gpu()["devices"])},
                "resources": {"max_workers": self.workspace.max_workers, "gpu_workers": self.workspace.gpu_workers,
                              "settings": os.environ.get("GRATING_RESOURCE_SETTINGS", "")}}
        # One record per task that follows its current state: a requirement, profile or waiver bound to an
        # earlier state then reads as stale, and returning to that state makes it current again.
        value = {"id": "runtime_subject_" + content_hash([task["campaign_id"], task["id"]])[:32], **body}
        if record:
            try:
                changed = self.store.get(value["id"], "runtime_subject") != value
            except KeyError:
                changed = True
            if changed:
                self.store.put("runtime_subject", value, "runtime.subject_recorded")
        return {**value, "digest": content_hash(value)}   # the digest validation binds a requirement to

    def latest(self, subject):
        profiles = [row for row in self.store.list("runtime_profile", subject["campaign_id"])
                    if row["subject_id"] == subject["id"] and row.get("subject_digest") == subject["digest"]]
        return profiles[-1] if profiles else None

    @staticmethod
    def _requirement_id(subject, study_id):
        return "runtime_requirement_" + content_hash([subject["id"], subject["digest"], study_id])[:32]

    def start(self, campaign_id, request):
        """Queue the first level's probe; later levels follow as each finishes."""
        task = self.store.get(request.task_id, "task")
        if task["campaign_id"] != campaign_id:
            raise ValueError("Select a task belonging to this campaign")
        subject = self.subject(task)
        current = self.latest(subject)
        if current and current["status"] == "running":
            return current   # asked again while it measures
        busy = [trial["id"] for trial in self.store.list_trials_in_status(ACTIVE)]
        if busy:
            raise ValueError(f"A runtime profile needs an idle workspace; {len(busy)} experiment(s) are queued or running")
        allowance = self.allowance(campaign_id)
        if allowance["spent_seconds"] >= allowance["cap_seconds"]:
            raise ValueError(f"The campaign's runtime optimization allowance ({allowance['cap_seconds']:.0f} s of profiling) is used up; "
                             "ask the researcher for a fix or a waiver")
        study_id = self.store.get(campaign_id, "campaign").get("active_study_id")
        if study_id:
            self.require(subject, study_id)   # so that the finished profile answers it and a waiver has something to name
        sizes = self.workspace.devices.sizes()
        pools = {device: sizes[device] for device in ("cuda", "cpu") if device in self.workspace.devices.reachable(task)}
        profile = {"id": identifier("runtime_profile"), "campaign_id": campaign_id, "task_id": task["id"],
                   "subject_id": subject["id"], "subject_digest": subject["digest"], "status": "running", "ceiling": self.workspace.max_workers,
                   "pools": pools, "plan": [[device, workers] for device, slots in pools.items() for workers in rungs(slots)],
                   "parameters": request.model_dump(mode="json", exclude={"task_id"}), "levels": [], "created_at": now()}
        self._launch(profile)
        self.store.put("runtime_profile", profile, "runtime.profile_started")
        self._running().add(profile["id"])
        return profile

    def allowance(self, campaign_id):
        """How much measuring the campaign may spend on getting faster: by default a twentieth of its compute."""
        from optimization_framework.execution.resources import spent
        campaign = self.store.get(campaign_id, "campaign")
        probes = {trial_id for profile in self.store.list("runtime_profile", campaign_id) for level in profile["levels"] for trial_id in level["trial_ids"]}
        return {"cap_seconds": campaign.get("runtime_optimization_seconds", .05 * campaign["compute_budget_seconds"]),
                "spent_seconds": sum(spent(row) for row in self.store.list_trial_costs(campaign_id) if row["id"] in probes)}

    def _running(self):
        if self.running is None:
            self.running = {row["id"] for row in self.store.list("runtime_profile") if row["status"] == "running"}
        return self.running

    @staticmethod
    def _plan(profile):
        """(device, workers) of each level; a profile from before devices were placed measured the whole ceiling."""
        return profile.get("plan") or [[None, workers] for workers in rungs(profile["ceiling"])]

    def _launch(self, profile):
        from optimization_framework.contracts.requests import TrialInput
        device, workers = self._plan(profile)[len(profile["levels"])]
        parameters = profile["parameters"]
        where = f" on {NAMES[device]}" if device else ""
        trials = [self.workspace.create_trial(TrialInput(campaign_id=profile["campaign_id"], task_id=profile["task_id"],
            algorithm="random", seed=1000 * len(profile["levels"]) + index, max_steps=parameters["max_evaluations"],
            wall_seconds=parameters["probe_seconds"], priority=100, device=device,
            question=f"Runtime profile probe: random search with {workers} concurrent worker(s){where}, to measure evaluations per second.",
            ), runtime_gate=False)["id"] for index in range(workers)]
        profile["levels"].append({"device": device, "workers": workers, "trial_ids": trials})

    def holds(self, trial):
        """Whether a queued experiment waits because a profile is measuring: only its probes may run meanwhile."""
        if not self._running():
            return False
        probes = {trial_id for identity in self.running for level in self.store.get(identity, "runtime_profile")["levels"]
                  for trial_id in level["trial_ids"]}
        return trial["id"] not in probes

    def advance(self):
        """Called by the scheduler: keep the device readings coming while probes run, and move finished levels on."""
        if not self._running():
            return
        if self.sampling is None or not self.sampling.is_alive():
            self.sampling = threading.Thread(target=self._sample, name="runtime-profile-sampler", daemon=True)
            self.sampling.start()
        for identity in list(self.running):
            profile = self.store.get(identity, "runtime_profile")
            level = profile["levels"][-1]
            trials = [self.store.get(trial_id, "trial") for trial_id in level["trial_ids"]]
            if any(trial["status"] in ACTIVE for trial in trials):
                continue
            try:
                failed = [trial for trial in trials if trial["status"] == "failed"]
                if failed:
                    raise ValueError(f"Probe {failed[0]['id']} failed: {failed[0].get('reason')}")
                level.update(rung(level["workers"], [probe(self.workspace.job_dir(trial["id"]), profile["parameters"]["warmup_evaluations"])
                                                     for trial in trials], self._readings(identity)))
                if len(profile["levels"]) < len(self._plan(profile)):
                    self._launch(profile)
                    self.store.put("runtime_profile", profile, "runtime.profile_advanced")
                    continue
                self._finish(profile)
            except Exception as error:
                profile.update(status="failed", reason=str(error), finished_at=now())
                self.store.put("runtime_profile", profile, "runtime.profile_failed")
            with self.sample_lock:
                self.running.discard(identity)
                self.samples.pop(identity, None)

    def _readings(self, identity):
        with self.sample_lock:
            return list(self.samples.get(identity, ()))

    def _sample(self):
        """Read the device every second while a profile measures.

        This runs apart from the scheduler: a command that keeps the scheduler waiting must not
        thin the readings the verdict rests on.
        """
        from optimization_framework.execution.observability import get_observer
        sampler = DeviceSampler(get_observer(self.workspace)._gpu)
        while self.running and not self.workspace.shutdown_event.is_set():
            reading = sampler.read()
            with self.sample_lock:
                for identity in list(self.running):
                    self.samples.setdefault(identity, []).append(reading)
            self.workspace.shutdown_event.wait(1)

    def _finish(self, profile):
        first = self.workspace.job_dir(profile["levels"][0]["trial_ids"][0])
        study_id = self.store.get(profile["campaign_id"], "campaign").get("active_study_id")
        thresholds = self.store.get(study_id, "study").get("validation_policy", {}).get("runtime_thresholds") if study_id else None
        candidate_bytes = probe(first)["candidate_bytes"]
        devices = list(dict.fromkeys(level.get("device") for level in profile["levels"]))
        summary = combine({device: derive([level for level in profile["levels"] if level.get("device") == device],
                                          (profile.get("pools") or {}).get(device, profile["ceiling"]), candidate_bytes, thresholds, device)
                           for device in devices}, candidate_bytes)
        profile.update(status="completed", summary=summary, finished_at=now())
        self.store.put("runtime_profile", profile, "runtime.profile_completed")
        for requirement in self.store.list("validation_requirement", profile["campaign_id"]):
            if requirement["kind"] == KIND and (requirement["subject_id"], requirement["subject_digest"]) == (profile["subject_id"], profile["subject_digest"]):
                self._record(requirement, profile)

    def _record(self, requirement, profile):
        from optimization_framework.contracts.validation import ValidationResult
        summary = profile["summary"]
        verdict = judged(summary)
        # A profile judged again under rules that changed since it was measured adds a result; otherwise it has one.
        identity = "runtime_result_" + content_hash([requirement["id"], profile["id"], *([verdict] if verdict != summary["verdict"] else [])])[:32]
        try:
            self.store.get(identity, "validation_result")
        except KeyError:
            self.workspace.validations.record_result(ValidationResult(id=identity, campaign_id=requirement["campaign_id"],
                requirement_id=requirement["id"], subject_digest=requirement["subject_digest"], recipe_id=RECIPE,
                verdict=verdict, evidence_ids=[profile["id"]],
                measurements={"flags": [flag["id"] for flag in summary["flags"]], "recommended_workers": summary.get("recommended_workers"),
                              "evaluations_per_hour": summary.get("evaluations_per_hour")},
                rationale="; ".join(flag["message"] for flag in summary["flags"]) or summary.get("reason") or "No runtime flag was raised.",
                producer="trusted_service", authority="runtime_profile", created_at=now()))

    def require(self, subject, study_id):
        from optimization_framework.contracts.validation import ValidationRequirement
        identity = self._requirement_id(subject, study_id)
        try:
            requirement = self.store.get(identity, "validation_requirement")
        except KeyError:
            requirement = self.workspace.validations.require(ValidationRequirement(id=identity, campaign_id=subject["campaign_id"],
                study_id=study_id, subject_id=subject["id"], subject_digest=subject["digest"], kind=KIND, recipe_id=RECIPE,
                scope={"task_id": subject["task_id"]},
                evidence_requirements=["A runtime profile of this evaluator's current source on this host, with every flag that holds runs fixed or waived"],
                authority="runtime_profile", created_at=now()))
        profile = self.latest(subject)
        if profile and profile["status"] == "completed":
            self._record(requirement, profile)
        return requirement

    def view(self, task, study_id=None, record=True):
        """The current subject's profile and what, if anything, still keeps long runs from starting.

        A dashboard reads with record=False, which never writes the subject or its requirement.
        """
        subject = self.subject(task, record)
        profile = self.latest(subject)
        raised = ((profile or {}).get("summary") or {}).get("flags", [])
        flags = [flag["id"] for flag in raised]
        waived, requirement = set(), None
        if study_id and record:
            requirement = self.require(subject, study_id)
        elif study_id:
            try:
                requirement = self.store.get(self._requirement_id(subject, study_id), "validation_requirement")
            except KeyError:
                pass
        if requirement:
            assessment = self.workspace.validations.assess(requirement["id"])
            for waiver in assessment["waivers"]:
                if waiver["id"] in assessment["active_waiver_ids"]:
                    # A waiver covers the flags of the profiles it cites, or everything when it cites the subject itself.
                    if subject["id"] in waiver["evidence_ids"]:
                        waived |= {"*"}
                    for evidence in waiver["evidence_ids"]:
                        cited = self.store.get(evidence)
                        if (cited.get("subject_id"), cited.get("subject_digest")) == (subject["id"], subject["digest"]):
                            waived |= {flag["id"] for flag in (cited.get("summary") or {}).get("flags", [])}
        if "*" in waived:
            blocker = None
        elif profile is None:
            blocker = "The evaluator has no runtime profile for its current source on this host. Run runtime.profile for this task first."
        elif profile["status"] == "running":
            blocker = "The runtime profile is still being measured."
        elif profile["status"] == "failed":
            blocker = f"The runtime profile failed: {profile.get('reason')}. Run runtime.profile again."
        elif profile["summary"]["verdict"] == "inconclusive":
            blocker = f"The runtime profile is inconclusive: {profile['summary'].get('reason')}"
        else:
            open_flags = [flag for flag in profile["summary"]["flags"] if flag["id"] not in waived and blocks(flag)]
            campaign = self.store.get(subject["campaign_id"], "campaign")
            remaining = max(0., campaign["compute_budget_seconds"] - self.workspace.allocated_seconds(campaign["id"]))
            blocker = ("The runtime profile raises: " + " ".join(f"[{flag['id']}] {flag['message']}" for flag in open_flags)
                       + f" Each 10% of speed is worth {remaining / 36000:.1f} h of the campaign's unallocated {remaining / 3600:.1f} h."
                       + " Fix and profile again, or waive the requirement with a reason.") if open_flags else None
        return {"subject_id": subject["id"], "profile": profile, "flags": flags, "waived_flags": sorted(waived),
                "note_flags": [flag["id"] for flag in raised if not blocks(flag)],
                "requirement_id": requirement["id"] if requirement else None, "gate_seconds": gate_seconds(), "blocker": blocker,
                "optimization_allowance": self.allowance(subject["campaign_id"])}

    def check(self, task, study_id, wall_seconds):
        """Refuse a long run whose evaluator has not been measured here, or whose profile has an open flag."""
        limit = gate_seconds()
        if limit is None or wall_seconds < limit:
            return
        blocker = self.view(task, study_id)["blocker"]
        if blocker:
            raise ValueError(f"Runs of {limit:g} s or longer wait for the runtime check. {blocker}")
