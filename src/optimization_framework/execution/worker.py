"""Worker-owned evaluation, append-only evidence, and periodic artifact recovery.

The service remains the only scheduler. Workers consume frozen local specs and
emit records/files; they never mutate the campaign database. Pickle is used only
for the reviewed native runtime's own state, never for uploaded artifacts.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pickle
import signal
import tempfile
import threading
import time
import traceback
import uuid

from threadpoolctl import threadpool_limits

from optimization_framework.contracts.base import content_hash, canonical_json
from optimization_framework.contracts.experiments import RecoveryPolicy, ExperimentSpec
from optimization_framework.contracts.problems import Observation, ProblemInstance
from optimization_framework.evaluation.registry import problems
from optimization_framework.optimizers.registry import create
from optimization_framework.storage.artifacts import LocalArtifactStore, atomic_json
from optimization_framework.storage.sqlite import now, read_json


def append_json(path, record):
    path = Path(path)
    # A forced exit can leave an unfinished last write. Preserve those bytes as
    # recovery evidence, then append at a record boundary instead of hiding the
    # next valid observation behind a malformed line.
    if path.exists() and path.stat().st_size:
        with path.open("r+b") as previous:
            previous.seek(-1, os.SEEK_END)
            if previous.read(1) != b"\n":
                end = previous.tell()
                position = end
                tail = b""
                while position:
                    count = min(position, 65536)
                    position -= count
                    previous.seek(position)
                    tail = previous.read(count) + tail
                    separator = tail.rfind(b"\n")
                    if separator >= 0:
                        boundary = position + separator + 1
                        tail = tail[separator + 1:]
                        break
                else:
                    boundary = 0
                with path.with_name(path.name + ".torn-" + uuid.uuid4().hex).open("wb") as evidence:
                    evidence.write(tail)
                    evidence.flush()
                    os.fsync(evidence.fileno())
                previous.truncate(boundary)
    with Path(path).open("a") as stream:
        stream.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def iter_journal(path):
    if not path.exists():
        return
    with path.open() as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                # A torn final write is never reported as a completed observation.
                if line.endswith("\n"):
                    raise ValueError(f"Corrupt journal record: {path.name}")


def read_journal(path):
    return list(iter_journal(path))


def fingerprint(spec):
    keys = ("id", "study_id", "problem", "algorithm", "algorithm_config", "seed", "schedule_steps", "training",
            "source_hash", "recovery", "initial_assets", "implementation_version_id", "implementation_artifact_digest",
            "implementation_runtime_digest", "completion", "recipe", "declared_assets", "diagnostics")
    identity = {key: spec.get(key) for key in keys}
    # One thread is the historical execution policy. Explicitly recording that
    # default must not invalidate a legacy checkpoint or its process lease.
    if spec.get("numerical_threads", 1) != 1:
        identity["numerical_threads"] = spec["numerical_threads"]
    if spec.get("evaluator_version_id"):
        identity["evaluator"] = {key: spec[key] for key in ("evaluator_version_id", "evaluator_artifact_digest", "evaluator_runtime_digest")}
    if spec.get("evaluator_eligibility"):
        identity["evaluator_eligibility"] = spec["evaluator_eligibility"]
    if spec.get("execution_manifest"):
        identity["execution_manifest_digest"] = content_hash(spec["execution_manifest"])
    if spec.get("absolute_deadline"):
        identity["absolute_deadline"] = spec["absolute_deadline"]
        identity["execution_grant_id"] = spec["execution_grant_id"]
        if "stop_grace_seconds" in spec.get("experiment_spec", {}).get("schedule", {}):
            identity["stop_grace_seconds"] = spec.get("stop_grace_seconds")
    return content_hash(identity)


class ExperimentWorker:
    def __init__(self, directory, *, evaluator=None, optimizer=None, attempt_id=None):
        self.directory = Path(directory).resolve()
        self.started = time.monotonic()
        self.spec = read_json(self.directory / "spec.json")
        self.registry = problems
        from optimization_framework.evaluation.inference import InferenceRegistry
        self.inference_registry = InferenceRegistry((self.spec.get("execution_manifest") or {}).get("inference_entry_points"))
        if self.spec.get("execution_manifest"):
            from optimization_framework.execution.provenance import verify
            from optimization_framework.evaluation.registry import ProblemRegistry
            verify(self.directory, self.spec["execution_manifest"])
            self.registry = ProblemRegistry.from_entries(self.spec["execution_manifest"]["entry_points"])
        if self.spec.get("experiment_spec"):
            frozen = ExperimentSpec(**self.spec["experiment_spec"])
            if frozen.digest() != self.spec["experiment_spec_hash"]:
                raise ValueError("Frozen experiment specification hash mismatch")
            if (frozen.problem.model_dump(mode="json") != self.spec["problem"] or frozen.seed != self.spec["seed"]
                    or frozen.parameters != {k: self.spec[k] for k in ("algorithm", "algorithm_config", "training", "recipe") if k in self.spec}
                    or frozen.schedule["steps"] != self.spec["schedule_steps"]
                    or frozen.schedule.get("numerical_threads", 1) != self.spec.get("numerical_threads", 1)
                    or frozen.initial_assets != self.spec.get("initial_assets", [])
                    or frozen.contribution_asset_ids != self.spec.get("contribution_asset_ids", [])
                    or frozen.dependencies != self.spec.get("dependencies", [])
                    or frozen.diagnostics != self.spec.get("diagnostics", [])
                    or frozen.recovery != RecoveryPolicy(**self.spec.get("recovery", {}))
                    or frozen.schedule.get("absolute_deadline") != self.spec.get("absolute_deadline")
                    or frozen.schedule.get("execution_grant_id") != self.spec.get("execution_grant_id")
                    or ("stop_grace_seconds" in frozen.schedule and frozen.schedule["stop_grace_seconds"] != self.spec.get("stop_grace_seconds"))
                    or frozen.schedule.get("logical_method") != self.spec.get("logical_method")
                    or frozen.schedule.get("protocol_cell_id") != self.spec.get("protocol_cell_id")
                    or frozen.schedule.get("evaluator_eligibility") != self.spec.get("evaluator_eligibility")
                    or (self.spec.get("execution_manifest") and frozen.schedule.get("execution_manifest_digest") != content_hash(self.spec["execution_manifest"]))
                    or frozen.asset_digests != {item["id"]: item["content_hash"] for item in self.spec.get("declared_assets", [])}
                    or frozen.completion.model_dump() != {"schema_version": 1, **self.spec["completion"]}):
                raise ValueError("Operational trial disagrees with the frozen scientific procedure")
        self.spec_hash = fingerprint(self.spec)
        self.problem = ProblemInstance(**self.spec["problem"])
        from optimization_framework.evaluation.registered_recipes import RecipeRegistry
        self.recipe_registry = RecipeRegistry(self.spec["execution_manifest"].get("recipe_entry_points", {}) if self.spec.get("execution_manifest") else None)
        if self.spec.get("evaluator_version_id"):
            from optimization_framework.evaluation.generated import pinned_evaluator
            self.registry = self.registry.extended(pinned_evaluator(self.directory, self.spec, recipe_registry=self.recipe_registry))
        self.policy = RecoveryPolicy(**self.spec.get("recovery", {}))
        self.artifacts = LocalArtifactStore(self.directory / "artifacts")
        declared = self.spec.get("declared_assets", [])
        if [item["id"] for item in declared] != self.spec.get("initial_assets", []):
            raise ValueError("Declared input assets disagree with the frozen experiment")
        for asset in declared:
            if asset["content_hash"] != content_hash({key: value for key, value in asset.items() if key != "content_hash"}):
                raise ValueError("Declared asset metadata changed after allocation")
            from optimization_framework.contracts.experiments import ArtifactReference
            inputs = LocalArtifactStore(self.directory / "inputs")
            for reference in asset["artifacts"]:
                inputs.verify(ArtifactReference(**reference))
        self.attempt_id = attempt_id or "attempt_" + uuid.uuid4().hex
        if self.spec.get("recipe"):
            from optimization_framework.evaluation.recipes import PlannedEvaluations, RecipeEvaluator
            self.optimizer = PlannedEvaluations(self.spec["recipe"], self.registry)
            self.evaluator = RecipeEvaluator(self.optimizer)
        else:
            self.optimizer = None
            self.evaluator = None
            try:
                self.optimizer = optimizer or self._optimizer()
                self.evaluator = evaluator or self._evaluator()
            except BaseException:
                for component in (self.optimizer, self.evaluator):
                    if hasattr(component, "close"):
                        component.close()
                raise
        self.elapsed_before = max(self.spec.get("execution_seconds", 0),
            (read_json(self.directory / "progress.json", {}) or {}).get("elapsed_seconds", 0))
        self.max_steps, self.wall_seconds = self.spec["max_steps"], self.spec["wall_seconds"]
        self.deadline = self.spec.get("absolute_deadline")
        self.monotonic_deadline = time.monotonic() + max(0, self.deadline - time.time()) if self.deadline else None
        self.step = 0
        self.archive = []
        self.last_value = None
        self.last_command, self.control_revision = "run", -1
        self.signal_command = None
        self.last_checkpoint_step = 0
        self.checkpoint_time = time.monotonic()
        self.checkpoint_id = None
        self.observation_cursor = 0
        self.recovered_suffix = 0
        self._restore()
        measured_history = sum(item["quantities"].get("worker_seconds") or 0 for item in iter_journal(self.directory / "costs.jsonl"))
        prior_attempts = {}
        for item in iter_journal(self.directory / "attempts.jsonl"):
            measured_history += item.get("attempt_overhead_seconds", 0)
            prior_attempts.setdefault(item.get("attempt_id", item.get("id")), {}).update(item)
        self.unknown_worker_cost = any(not item.get("finished_at") for key, item in prior_attempts.items() if key != self.attempt_id)
        self.elapsed_before = max(self.elapsed_before, measured_history)
        self.cost_cursor_seconds = self.elapsed_before
        self.request_count = self.observation_count = self.successful_observations = 0
        self.solver_calls = self.cache_hits = self.interrupted_requests = 0
        self.unknown_solver_cost = False
        last_request = last_observation = None
        for request in iter_journal(self.directory / "requests.jsonl"):
            self.request_count += 1
            last_request = request
        for observation in iter_journal(self.directory / "observations.jsonl"):
            self._account_observation(observation)
            last_observation = observation
            if self.observation_count > self.observation_cursor and observation["status"] != "uncertain":
                self.recovered_suffix += 1
        if self.observation_cursor > self.observation_count:
            raise ValueError("Checkpoint refers to missing durable observations")
        if last_request and (not last_observation or last_request["id"] != last_observation["request_id"]):
                request = last_request
                record = Observation(id="observation_" + uuid.uuid4().hex, experiment_id=self.spec["id"],
                    attempt_id=request["attempt_id"], request_id=request["id"], proposal_id=request["proposal_id"],
                    candidate=request["candidate"], status="uncertain", evaluator_identity=request.get("evaluator_identity", self.problem.evaluation_identity),
                    fidelity=request.get("fidelity", self.problem.fidelity), costs={"evaluation_requests": 1, "solver_executions": None, "worker_seconds": None},
                    error="The previous attempt ended after request intent, without a durable observation")
                self._record_observation(record)
        append_json(self.directory / "attempts.jsonl", {"id": self.attempt_id, "experiment_id": self.spec["id"],
            "started_at": now(), "checkpoint_id": self.checkpoint_id, "recovered_suffix_observations": self.recovered_suffix,
            "spec_hash": self.spec_hash, "pid": os.getpid()})
        # Publish an initial checkpoint so an early forced exit also has a bounded restart.
        if self.checkpoint_id is None:
            self.save_checkpoint()

    def _evaluator(self):
        if self.problem.evaluator_version == "unresolved":
            raise ValueError("An unresolved evaluator cannot execute")
        return self.registry.evaluator(self.problem)

    def _optimizer(self):
        if self.spec["algorithm"] == "package":
            from optimization_framework.implementations.runtime import PackageOptimizer, tree_hashes, bundle_runtime_root
            from optimization_framework.implementations.models import digest
            from optimization_framework.optimizers.lifecycle import AskTellAdapter
            bundle = read_json(self.directory / "implementation" / "bundle.json")
            artifact, version = bundle["artifact"], bundle["version"]
            package_dir = self.directory / "implementation" / "package"
            if (digest(artifact) != self.spec["implementation_artifact_digest"] or version["id"] != self.spec["implementation_version_id"]
                    or tree_hashes(package_dir) != version["package_hashes"]
                    or artifact["runtime"]["digest"] != self.spec["implementation_runtime_digest"]):
                raise ValueError("Pinned implementation changed after this experiment was queued")
            def factory(descriptor, parameters, seed, assets):
                return PackageOptimizer(package_dir, bundle_runtime_root(bundle, package_dir.parent), artifact["runtime"], artifact["package"]["entrypoint"],
                    {"n_cells": self.problem.candidate_schema.dimensions, "seed": seed, "parameters": parameters,
                     "schedule_steps": self.spec["schedule_steps"], "problem": descriptor, "capabilities": self.problem.capabilities,
                     "declared_assets": self.spec.get("declared_assets", [])},
                    assets_dir=self.directory / "inputs" if self.spec.get("declared_assets") else None,
                    max_checkpoint_bytes=artifact["spec"].get("max_checkpoint_bytes", 256 * 1024**2),
                    contract=artifact["package"].get("contract", "ask_tell"),
                    supports_failure_observations=artifact["spec"].get("supports_failure_observations", False))
            if artifact["package"].get("contract") == "optimizer_v1":
                return factory(self.problem.descriptor(), self.spec.get("algorithm_config", {}), self.spec["seed"], self.spec.get("declared_assets", []))
            result = AskTellAdapter(factory)
            result.initialize(self.problem.descriptor(), self.spec.get("algorithm_config", {}), self.spec["seed"], self.spec.get("declared_assets", []))
            return result
        return create(self.spec["algorithm"], self.problem, self.spec.get("algorithm_config", {}), self.spec["seed"],
                      self.spec["schedule_steps"], self.spec.get("training"), self.spec.get("declared_assets"),
                      artifact_store=LocalArtifactStore(self.directory / "inputs"), inference_registry=self.inference_registry)

    def elapsed(self):
        return self.elapsed_before + time.monotonic() - self.started

    @property
    def observations(self):
        """An inspection view; the numerical loop keeps only cumulative counters."""
        return read_journal(self.directory / "observations.jsonl")

    def _restore(self):
        pointer = read_json(self.directory / "checkpoints" / "latest.json")
        if not pointer:
            return
        checkpoint_id = pointer["id"]
        if not checkpoint_id.startswith("checkpoint_") or len(checkpoint_id) != 75 or not all(c in "0123456789abcdef" for c in checkpoint_id[11:]):
            raise ValueError("Invalid checkpoint identifier")
        manifest = read_json(self.directory / "checkpoints" / (checkpoint_id + ".json"))
        if manifest["metadata"]["spec_hash"] != self.spec_hash:
            raise ValueError("Checkpoint is incompatible with the frozen scientific specification")
        with self.artifacts.checkpoint_stream(manifest) as stream:
            state = pickle.load(stream)
        self.optimizer.restore(state["optimizer"])
        self.evaluator.restore(state["evaluator"])
        self.step, self.archive = state["step"], state["archive"]
        self.last_value = state["last_value"]
        self.elapsed_before = max(self.elapsed_before, state["elapsed_seconds"])
        self.control_revision, self.last_command = state["control_revision"], state["last_command"]
        self.max_steps, self.wall_seconds = state["max_steps"], state["wall_seconds"]
        self.observation_cursor = manifest["metadata"]["observation_cursor"]
        self.checkpoint_id = checkpoint_id
        self.last_checkpoint_step = self.step

    def save_checkpoint(self):
        state = {"optimizer": self.optimizer.checkpoint(), "evaluator": self.evaluator.checkpoint(),
                 "step": self.step, "archive": self.archive, "last_value": self.last_value,
                 "elapsed_seconds": self.elapsed(), "control_revision": self.control_revision, "last_command": self.last_command,
                 "max_steps": self.max_steps, "wall_seconds": self.wall_seconds}
        with tempfile.SpooledTemporaryFile(max_size=self.policy.chunk_bytes) as stream:
            pickle.dump(state, stream, protocol=pickle.HIGHEST_PROTOCOL)
            stream.seek(0)
            manifest = self.artifacts.publish_checkpoint(self.directory / "checkpoints", stream,
                {"spec_hash": self.spec_hash, "attempt_id": self.attempt_id, "step": self.step,
                 "observation_cursor": self.observation_count}, chunk_bytes=self.policy.chunk_bytes,
                max_bytes=self.policy.max_checkpoint_bytes)
        self.checkpoint_id = manifest["id"]
        self.observation_cursor = self.observation_count
        self.last_checkpoint_step, self.checkpoint_time = self.step, time.monotonic()

    def control(self):
        if self.signal_command:
            self.last_command = self.signal_command
            return self.last_command
        control = read_json(self.directory / "control.json", {})
        if control.get("revision", -1) > self.control_revision:
            if control.get("command", "run") not in {"run", "pause", "stop"}:
                raise ValueError("Unknown worker control")
            self.last_command = control.get("command", "run")
            self.control_revision = control["revision"]
            self.max_steps = control.get("max_steps", self.max_steps)
            self.wall_seconds = control.get("wall_seconds", self.wall_seconds)
        return self.last_command

    def _record_observation(self, observation):
        value = observation.model_dump(mode="json")
        append_json(self.directory / "observations.jsonl", value)
        self._account_observation(value)

    def _account_observation(self, value):
        self.observation_count += 1
        self.successful_observations += int(value["status"] == "ok")
        self.interrupted_requests += int(value["status"] == "uncertain")
        self.solver_calls += int(value["costs"].get("solver_executions") or 0)
        self.cache_hits += int(value["costs"].get("cache_hits") or 0)
        self.unknown_solver_cost |= value["costs"].get("solver_executions") is None

    def one_step(self):
        proposals = self.optimizer.propose(1)
        if len(proposals) != 1:
            raise ValueError("This experiment permits one outstanding candidate")
        proposal = proposals[0]
        instance = self.optimizer.instance if self.spec.get("recipe") else self.problem
        candidate = proposal.candidate
        encoding_error = None
        try:
            canonical_json(candidate)
        except (TypeError, ValueError) as exc:
            # Preserve an inspectable representation of malformed output without
            # allowing nonfinite JSON or an invented numerical observation.
            candidate = {"invalid_representation": repr(candidate)[:4096]}
            encoding_error = f"Candidate is not finite JSON: {exc}"
        request = {"id": "request_" + uuid.uuid4().hex, "experiment_id": self.spec["id"], "attempt_id": self.attempt_id,
                   "proposal_id": proposal.id, "candidate": candidate, "created_at": now(), "spec_hash": self.spec_hash,
                   "proposal_metadata": proposal.metadata,
                   "fidelity": instance.fidelity, "evaluator_identity": instance.evaluation_identity}
        append_json(self.directory / "requests.jsonl", request)
        self.request_count += 1
        started = time.monotonic()
        status, error, evaluation = "ok", None, None
        try:
            if encoding_error:
                raise ValueError(encoding_error)
            candidate = instance.candidate_schema.canonicalize(candidate)
        except (ValueError, TypeError) as exc:
            status, error = "invalid_candidate", str(exc)
        if status == "ok":
            try:
                if hasattr(self.evaluator, "evaluate_proposal"):
                    evaluation = self.evaluator.evaluate_proposal(candidate, proposal.metadata)
                elif proposal.metadata.get("flrl_gradient") is not None:
                    raise ValueError("The selected evaluator does not support Fourier gradients")
                else:
                    evaluation = self.evaluator.evaluate(candidate)
                if self.problem.primary_objective.name not in evaluation.objectives:
                    raise ValueError("Evaluator omitted the primary objective")
            except Exception as exc:
                status, error = "evaluation_failed", f"{type(exc).__name__}: {exc}"
        costs = {"evaluation_requests": 1, "solver_executions": evaluation.solver_executions if evaluation else (0 if status == "invalid_candidate" else None),
                 "worker_seconds": time.monotonic() - started, "cache_hits": int(evaluation.cache_hit) if evaluation else 0}
        metadata = dict(evaluation.metadata) if evaluation else {}
        if status == "ok" and hasattr(self.evaluator, "drain_artifacts"):
            metadata["artifacts"] = [self.publish_payload(item, authority="evaluator") for item in self.evaluator.drain_artifacts()]
        observation = Observation(id="observation_" + uuid.uuid4().hex, experiment_id=self.spec["id"], attempt_id=self.attempt_id,
            request_id=request["id"], proposal_id=proposal.id, candidate=candidate, status=status,
            objectives=evaluation.objectives if evaluation and status == "ok" else {},
            constraints=evaluation.constraints if evaluation and status == "ok" else {}, metadata=metadata,
            fidelity=instance.fidelity, evaluator_identity=instance.evaluation_identity, costs=costs, error=error)
        self._record_observation(observation)
        if status == "ok":
            self.step += 1
            self.last_value = observation.objectives[self.problem.primary_objective.name]
            entry = {"candidate": candidate, "objective": self.last_value, "observation_id": observation.id,
                     "fidelity": instance.fidelity, "scientific_identity": instance.scientific_identity,
                     "evaluator_identity": instance.evaluation_identity, "step": self.step}
            self.archive = [e for e in self.archive if e["candidate"] != candidate] + [entry]
            self.archive.sort(key=lambda e: self.problem.primary_objective.utility(e["objective"]), reverse=True)
            self.archive = self.archive[:self.spec.get("archive_size", 10)]
        try:
            self.optimizer.observe([observation])
            self.capture_diagnostics()
        finally:
            elapsed = self.elapsed()
            append_json(self.directory / "costs.jsonl", {"request_ordinal": self.request_count - 1,
                "request_id": request["id"], "attempt_id": self.attempt_id, "created_at": request["created_at"],
                "quantities": {"evaluation_requests": 1, "solver_executions": costs["solver_executions"],
                    "evaluation_seconds": costs["worker_seconds"], "worker_seconds": max(0., elapsed - self.cost_cursor_seconds),
                    "model_calls": 0, "model_input_tokens": 0, "model_output_tokens": 0, "api_usd": 0},
                "status": "measured" if costs["solver_executions"] is not None else "uncertain"})
            self.cost_cursor_seconds = elapsed

    def progress(self, status, reason=None):
        best = self.archive[0] if self.archive else None
        completion = self.spec.get("completion", {"unit": "evaluation_requests", "count": self.spec["max_steps"]})
        diagnostics = self.optimizer.inspect()
        primary_count = diagnostics.get("decisions", 0) if completion["unit"] == "optimizer_decisions" else self.step
        result = {"schema_version": 1, "id": self.spec["id"], "attempt_id": self.attempt_id,
                "status": status, "reason": reason, "step": self.step, "objective": self.last_value,
                "objective_definition": self.problem.primary_objective.model_dump(), "best_objective": best["objective"] if best else None,
                "best_candidate": best["candidate"] if best else None, "archive": self.archive,
                "evaluations": self.request_count, "budget_requests": self.request_count, "solver_calls": self.solver_calls,
                "cache_hits": self.cache_hits, "confirmed_observations": self.successful_observations,
                "interrupted_requests": self.interrupted_requests, "unknown_solver_cost": self.unknown_solver_cost,
                "unknown_worker_cost": self.unknown_worker_cost,
                "recovered_suffix_observations": self.recovered_suffix, "elapsed_seconds": self.elapsed(),
                "scientific_complete": primary_count >= completion["count"],
                "allocation_stop": reason if reason in {"step_budget_exhausted", "wall_budget_exhausted", "study_deadline_reached"} else None,
                **({"absolute_deadline": self.deadline, "deadline_overshoot_seconds": max(0, time.time() - self.deadline)} if self.deadline else {}),
                "process_exit": None if status == "running" else int(status == "failed"),
                "checkpoint_available": self.checkpoint_id is not None, "checkpoint_id": self.checkpoint_id,
                "resume_supported": status != "failed", "schedule_steps": self.spec["schedule_steps"],
                "diagnostics": diagnostics, "updated_at": now(), "control_revision": self.control_revision}
        # A compatibility projection names the raw objective without changing its
        # direction, range, or units. New consumers use objective/best_objective.
        result[self.problem.primary_objective.name] = self.last_value
        result["best_" + self.problem.primary_objective.name] = result["best_objective"]
        result["best_design"] = result["best_candidate"]
        result["archive"] = [dict(e, design=e["candidate"], **{self.problem.primary_objective.name: e["objective"]}) for e in self.archive]
        if self.spec.get("recipe"):
            result["recipe_result"] = self.optimizer.summary()
            result["validation"] = result["recipe_result"].get("subjects", [])
        return result

    def publish_payload(self, raw, *, authority="optimizer"):
        item = dict(raw)
        data = item.pop("data")
        if not isinstance(data, bytes):
            data = canonical_json(data)
        reference = self.artifacts.put_bytes(data, media_type=item.pop("media_type", "application/octet-stream"))
        return {**item, "reference": reference.model_dump(), "authority": authority}

    def capture_diagnostics(self):
        """The first committed milestone survives recovery of the learner state.

        This runs inside the observation's measured cost interval. Ingestion
        waits for that interval to commit, so exports have an exact cost prefix.
        """
        from optimization_framework.contracts.diagnostics import DiagnosticSchedule
        for raw in self.spec.get("diagnostics", []):
            schedule = DiagnosticSchedule(**raw)
            count = self.request_count if schedule.unit == "evaluation_requests" else self.optimizer.inspect().get("decisions", 0)
            if count not in schedule.at_counts:
                continue
            identity = schedule.digest()
            destination = self.directory / "diagnostics" / identity / f"{count}.json"
            if destination.exists():
                saved = read_json(destination)
                if saved.get("spec_hash") != self.spec_hash or saved["id"] != "outputs_" + content_hash({key: value for key, value in saved.items() if key != "id"}):
                    raise ValueError("A committed diagnostic milestone changed since capture")
                continue
            manifest = self.publish_outputs(diagnostic={"schedule_digest": identity, "count": count, "unit": schedule.unit,
                "request_prefix_stop": self.request_count, "created_at": now()}, include_optimizer=schedule.export_optimizer)
            atomic_json(destination, manifest)

    def publish_outputs(self, *, diagnostic=None, include_optimizer=True):
        """Publish immutable outputs independently of resumable internal state."""
        outputs = []
        if self.archive and not self.spec.get("recipe"):
            data = canonical_json({"problem": self.problem.model_dump(mode="json"), "archive": self.archive})
            reference = self.artifacts.put_bytes(data, media_type="application/json")
            outputs.append({"kind": "solution_archive", "reference": reference.model_dump(), "authority": "worker",
                            "metadata": {"scientific_identity": self.problem.scientific_identity}})
        if include_optimizer:
            for raw in self.optimizer.export_artifacts():
                outputs.append(self.publish_payload(raw))
        manifest = {"schema_version": 1, "experiment_id": self.spec["id"], "attempt_id": self.attempt_id,
                    "spec_hash": self.spec_hash, "checkpoint_id": self.checkpoint_id, "outputs": outputs}
        if diagnostic:
            manifest["diagnostic"] = diagnostic
        manifest["id"] = "outputs_" + content_hash(manifest)
        atomic_json(self.directory / "outputs" / (manifest["id"] + ".json"), manifest)
        if not diagnostic:
            atomic_json(self.directory / "outputs.json", manifest)
        return manifest

    def run(self):
        status, reason = "running", None
        try:
            while True:
                if self.deadline and (time.time() >= self.deadline or time.monotonic() >= self.monotonic_deadline):
                    status, reason = "budget_exhausted", "study_deadline_reached"
                    break
                command = self.control()
                if command != "run":
                    status, reason = ("paused", "researcher_pause") if command == "pause" else ("stopped", "researcher_stop")
                    break
                completion = self.spec.get("completion", {})
                if (completion.get("unit") == "optimizer_decisions"
                        and self.optimizer.inspect().get("decisions", 0) >= completion["count"]):
                    status, reason = "completed", "primary_completion_reached"
                    break
                if self.spec.get("recipe") and self.optimizer.cursor >= len(self.spec["recipe"]["cases"]):
                    status, reason = "completed", "recipe_complete"
                    break
                if self.request_count >= self.max_steps:
                    status, reason = "completed", "step_budget_exhausted"
                    break
                if self.elapsed() >= self.wall_seconds:
                    status, reason = "completed", "wall_budget_exhausted"
                    break
                self.one_step()
                if self.step-self.last_checkpoint_step >= self.policy.every_observations or time.monotonic()-self.checkpoint_time >= self.policy.every_seconds:
                    self.save_checkpoint()
                progress = self.progress("running")
                append_json(self.directory / "metrics.jsonl", progress)
                atomic_json(self.directory / "progress.json", progress)
        except Exception as exc:
            status, reason = "failed", f"{type(exc).__name__}: {exc}"
            (self.directory / "error.txt").write_text(traceback.format_exc())
        if status != "failed":
            self.save_checkpoint()
            self.publish_outputs()
        result = self.progress(status, reason)
        result["attempt_overhead_seconds"] = max(0., self.elapsed() - self.cost_cursor_seconds)
        append_json(self.directory / "attempts.jsonl", {**result, "finished_at": now()})
        atomic_json(self.directory / "progress.json", result)
        atomic_json(self.directory / "result.json", result)
        return result


def run(directory, *, enforce_deadline=False, **kwargs):
    directory = Path(directory).resolve()
    with (directory / "worker.lock").open("a+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lease_path = directory / "worker-lease.json"
        worker = None
        deadline_guard = None
        handlers = {}
        lease_record = None
        stop_requested = False
        def request_stop(*_):
            nonlocal stop_requested
            stop_requested = True
            if worker is not None:
                worker.signal_command = "stop"
        try:
            spec = read_json(directory / "spec.json")
            raw = Path(f"/proc/{os.getpid()}/stat").read_text()
            identity = raw[raw.rfind(")") + 2:].split()[19]
            lease_record = {"pid": os.getpid(), "process_identity": identity, "experiment_id": spec["id"],
                "fingerprint": fingerprint(spec), "attempt": spec.get("attempt", 1), "started_at": time.time(),
                "started_monotonic": time.monotonic(), "elapsed_before": spec.get("execution_seconds", 0),
                "attempt_id": "attempt_" + uuid.uuid4().hex}
            atomic_json(lease_path, lease_record)
            append_json(directory / "attempts.jsonl", {**lease_record, "id": lease_record["attempt_id"],
                "started_at": now(), "spec_hash": lease_record["fingerprint"]})
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, request_stop)
            if enforce_deadline and spec.get("absolute_deadline"):
                from optimization_framework.execution.watchdog import arm
                deadline_guard = arm(directory, spec, lease_record)
            with threadpool_limits(limits=spec.get("numerical_threads", 1)):
                worker = ExperimentWorker(directory, attempt_id=lease_record["attempt_id"], **kwargs)
                if stop_requested:
                    worker.signal_command = "stop"
                return worker.run()
        except Exception as exc:
            (directory / "error.txt").write_text(traceback.format_exc())
            result = {**read_json(directory / "progress.json", {}), "status": "failed",
                      "reason": f"{type(exc).__name__}: {exc}", "process_exit": 1, "scientific_complete": False, "resume_supported": False}
            if lease_record:
                result["attempt_id"] = lease_record["attempt_id"]
                append_json(directory / "attempts.jsonl", {**result, "finished_at": now()})
            atomic_json(directory / "result.json", result)
            return result
        finally:
            try:
                # Cleanup can block in an executable runtime too. Keep the
                # guard and signal handlers alive until it has returned.
                if worker:
                    try:
                        if hasattr(worker.optimizer, "close"):
                            worker.optimizer.close()
                    finally:
                        if hasattr(worker.evaluator, "close"):
                            worker.evaluator.close()
            finally:
                if deadline_guard is not None and deadline_guard.poll() is None:
                    deadline_guard.terminate()
                    deadline_guard.wait(timeout=5)
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
                lease_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    return int(run(args.directory, enforce_deadline=True)["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
