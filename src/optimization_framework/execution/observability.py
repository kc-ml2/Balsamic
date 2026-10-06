"""Read-only, bounded host and campaign resource observations.

Polling never asks the scheduler to reconcile, allocates a grant, or opens an
optimizer checkpoint. Physical readings, accounting reservations, and planning
estimates deliberately remain separate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

from optimization_framework.execution.resources import ACTIVE


GIB = 1024 ** 3
ENDED = {"completed", "stopped", "budget_exhausted", "failed"}
MANIFEST_FIELDS = {"campaign_id", "race_id", "status", "numerical_status", "hold_category",
                   "waiting_reason", "memory_predictions", "worker_memory_samples", "fidelities", "updated_at"}


def _number(value, default=0.):
    return float(value) if isinstance(value, (int, float)) else default


def _read(path):
    try:
        return Path(path).read_text()
    except (OSError, UnicodeError):
        return None


def _skip_value(raw, position):
    """Skip JSON without constructing an unselected geometry/archive object."""
    depth, quoted, escaped = 0, False, False
    while position < len(raw):
        character = raw[position]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
        elif character in "]}":
            if depth == 0:
                break
            depth -= 1
        elif character == "," and depth == 0:
            break
        position += 1
    return position


def selected_manifest(path):
    """Read a capped manifest and decode only resource metadata."""
    try:
        if path.stat().st_size > 512 * 1024:
            return None
        raw = path.read_text()
        decoder, result, position = json.JSONDecoder(), {}, 0
        position = len(raw) - len(raw.lstrip())
        if raw[position] != "{":
            return None
        position += 1
        while position < len(raw):
            while raw[position].isspace() or raw[position] == ",":
                position += 1
            if raw[position] == "}":
                return result
            key, position = decoder.raw_decode(raw, position)
            while raw[position].isspace():
                position += 1
            if raw[position] != ":":
                return None
            position += 1
            while raw[position].isspace():
                position += 1
            if key in MANIFEST_FIELDS:
                result[key], position = decoder.raw_decode(raw, position)
            else:
                position = _skip_value(raw, position)
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return None


class ResourceObservability:
    def __init__(self, workspace, *, proc_root="/proc", sys_root="/sys", clock=time.time,
                 monotonic=time.monotonic, cache_seconds=1.):
        self.workspace, self.store = workspace, workspace.store
        self.proc_root, self.sys_root = Path(proc_root), Path(sys_root)
        self.clock, self.monotonic, self.cache_seconds = clock, monotonic, cache_seconds
        self.lock = threading.Lock()
        self._host_cache, self._host_at, self._cpu_sample = None, -float("inf"), None
        self._process_samples, self._manifests, self._manifest_paths = {}, {}, []
        self._process_details = {}
        self._cost_cache = {}
        self._manifest_scan_at = -float("inf")
        self._tick_rate = os.sysconf("SC_CLK_TCK")

    def _rows(self, kind, fields, campaign_id=None, *, active=False, race_id=None, limit=None):
        # SQLite extracts the scalars before Python sees numerical design blobs.
        columns = ["id", "campaign_id"] + [f"json_extract(data,'$.{path}') AS \"{name}\"" for name, path in fields.items()]
        if active and kind == "trial":
            query, values = f"SELECT {','.join(columns)} FROM records INDEXED BY records_trial_status WHERE kind='trial'", []
        else:
            query, values = f"SELECT {','.join(columns)} FROM records WHERE kind=?", [kind]
        if campaign_id is not None:
            query += " AND campaign_id=?"
            values.append(campaign_id)
        if active:
            query += " AND json_extract(data,'$.status') IN ('queued','starting','running','pausing','stopping')"
        if race_id is not None:
            query += " AND json_extract(data,'$.race_id')=?"
            values.append(race_id)
        query += " ORDER BY rowid DESC" if limit else " ORDER BY rowid"
        if limit:
            query += " LIMIT ?"
            values.append(limit)
        with self.store.connection() as database:
            return [dict(row) for row in database.execute(query, values).fetchall()]

    def _cgroup(self):
        relative = None
        for line in (_read(self.proc_root / "self/cgroup") or "").splitlines():
            if line.startswith("0::"):
                relative = line.split("::", 1)[1]
        if relative is None or ".." in Path(relative).parts:
            return {}
        base = self.sys_root / "fs/cgroup"
        directory = base / relative.lstrip("/")
        if not directory.exists():
            directory = base  # A cgroup namespace may already mount its own root.
        result = {}
        while directory == base or base in directory.parents:
            maximum, current = _read(directory / "memory.max"), _read(directory / "memory.current")
            if maximum and maximum.strip().isdigit():
                limit = int(maximum)
                if limit < result.get("memory_limit_bytes", float("inf")):
                    result["memory_limit_bytes"] = limit
                if current and current.strip().isdigit():
                    available = max(0, limit - int(current))
                    result["memory_available_bytes"] = min(result.get("memory_available_bytes", available), available)
            quota = (_read(directory / "cpu.max") or "").split()
            if len(quota) == 2 and quota[0].isdigit() and quota[1].isdigit() and int(quota[1]):
                cores = int(quota[0]) / int(quota[1])
                result["cpu_capacity_cores"] = min(result.get("cpu_capacity_cores", cores), cores)
            if directory == base:
                break
            directory = directory.parent
        return result

    def _process(self, pid, sampled):
        cached = self._process_details.get(int(pid))
        if cached and cached[0] == sampled:
            return cached[1]
        raw = _read(self.proc_root / str(pid) / "stat")
        status = _read(self.proc_root / str(pid) / "status")
        if not raw or not status:
            return None
        try:
            fields = raw[raw.rfind(")") + 2:].split()
            identity, ticks = fields[19], int(fields[11]) + int(fields[12])
            values = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in status.splitlines() if ":" in line}
            previous = self._process_samples.get((int(pid), identity))
            cpu = None
            if previous and sampled > previous[0]:
                cpu = max(0., 100 * (ticks - previous[1]) / self._tick_rate / (sampled - previous[0]))
            self._process_samples[(int(pid), identity)] = (sampled, ticks)
            def size(name):
                return int(values[name].split()[0]) * 1024 if name in values else None
            result = {"pid": int(pid), "process_identity": identity, "name": values.get("Name", "unknown")[:80],
                    "state": fields[0], "rss_bytes": size("VmRSS"), "peak_rss_bytes": size("VmHWM"),
                    "threads": int(values.get("Threads", "0")) or None, "cpu_percent": cpu}
            self._process_details[int(pid)] = (sampled, result)
            return result
        except (ValueError, IndexError):
            return None

    def _gpu(self):
        devices = []
        try:
            cards = list((self.sys_root / "class/drm").iterdir())[:32]
        except OSError:
            return {"status": "unavailable", "devices": []}
        for card in cards:
            if not card.name.startswith("card") or not card.name[4:].isdigit():
                continue
            directory = card / "device"
            vendor = (_read(directory / "vendor") or "").strip()
            if not vendor:
                continue
            def value(name):
                content = (_read(directory / name) or "").strip()
                return int(content) if content.isdigit() else None
            devices.append({"id": card.name, "vendor": {"0x1002": "AMD", "0x10de": "NVIDIA", "0x8086": "Intel"}.get(vendor, vendor),
                            "utilization_percent": value("gpu_busy_percent"), "memory_total_bytes": value("mem_info_vram_total"),
                            "memory_used_bytes": value("mem_info_vram_used"), "memory_kind": "driver-reported VRAM; shared host allocations may be additional"})
        return {"status": "available" if devices else "none_detected", "devices": devices}

    def _host(self, sampled):
        if self._host_cache is not None and sampled - self._host_at < self.cache_seconds:
            return self._host_cache
        memory = {}
        for line in (_read(self.proc_root / "meminfo") or "").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                memory[fields[0].rstrip(":")] = int(fields[1]) * 1024
        total, available = memory.get("MemTotal"), memory.get("MemAvailable")
        cgroup = self._cgroup()
        effective = min(available, cgroup["memory_available_bytes"]) if available is not None and "memory_available_bytes" in cgroup else available
        logical = os.cpu_count()
        try:
            affinity = len(os.sched_getaffinity(0))
        except (OSError, AttributeError):
            affinity = logical
        capacity = min(affinity, cgroup.get("cpu_capacity_cores", affinity)) if affinity else None
        utilization = None
        raw = _read(self.proc_root / "stat") or ""
        try:
            counters = [int(value) for value in raw.splitlines()[0].split()[1:9]]
            current = (sum(counters), counters[3] + counters[4])
            if self._cpu_sample and current[0] > self._cpu_sample[0]:
                utilization = max(0., min(100., 100 * (1 - (current[1] - self._cpu_sample[1]) / (current[0] - self._cpu_sample[0]))))
            self._cpu_sample = current
        except (ValueError, IndexError):
            pass
        try:
            load = [float(value) for value in (_read(self.proc_root / "loadavg") or "").split()[:3]] or None
        except ValueError:
            load = None
        processes = []
        try:
            entries = list(self.proc_root.iterdir())[:4096]
        except OSError:
            entries = []
        for path in entries:
            if not path.name.isdigit():
                continue
            process = self._process(int(path.name), sampled)
            if process and process["rss_bytes"]:
                command = _read(self.proc_root / path.name / "cmdline") or ""
                role = "workspace" if process["pid"] == os.getpid() else "service" if any(name in (process["name"] + command).lower() for name in ("llama", "uvicorn", "pi-service", "tensorboard", "optimization_framework", "grating-lab")) else "other"
                processes.append({key: process[key] for key in ("pid", "name", "rss_bytes", "threads")} | {"role": role})
        processes.sort(key=lambda process: process["rss_bytes"], reverse=True)
        result = {"cpu": {"logical_count": logical, "capacity_cores": capacity, "utilization_percent": utilization,
                          "load_average": load, "sample_basis": "Host /proc/stat counter delta; first observation is unavailable"},
                  "memory": {"total_bytes": total, "available_bytes": available, "used_bytes": total - available if total is not None and available is not None else None,
                             "capacity_bytes": min(total, cgroup["memory_limit_bytes"]) if total is not None and "memory_limit_bytes" in cgroup else total,
                             "effective_available_bytes": effective, "cgroup_limit_bytes": cgroup.get("memory_limit_bytes"),
                             "cgroup_available_bytes": cgroup.get("memory_available_bytes"), "swap_total_bytes": memory.get("SwapTotal"),
                             "swap_free_bytes": memory.get("SwapFree"), "used_basis": "MemTotal minus MemAvailable (includes unreclaimable host and shared GPU allocations)"},
                  "gpu": self._gpu(), "processes": processes[:8]}
        self._host_cache, self._host_at = result, sampled
        return result

    def _budget(self, campaign_id, costs):
        campaigns = self._rows("campaign", {"limit": "compute_budget_seconds"})
        if campaign_id is not None:
            campaigns = [row for row in campaigns if row["id"] == campaign_id]
            if not campaigns:
                raise KeyError(campaign_id)
        members = {}
        for trial in costs:
            actual = _number(trial["spent_seconds"])
            committed = max(actual, _number(trial["upper_bound_seconds"]))
            if trial["status"] in ACTIVE or trial["status"] == "starting":
                committed = max(committed, _number(trial["wall_seconds"]))
            key = trial.get("execution_grant_id")
            row = members.setdefault(key, {"actual": 0., "committed": 0.})
            row["actual"] += actual
            row["committed"] += committed
        for kind, seconds in (("diagnostic_grant", "reserved_seconds"), ("execution_check_grant", "reserved_seconds")):
            for grant in self._rows(kind, {"status": "status", "execution_grant_id": "execution_grant_id", "reserved_seconds": seconds}, campaign_id):
                if grant["status"] == "reserved":
                    members.setdefault(grant["execution_grant_id"], {"actual": 0., "committed": 0.})["committed"] += _number(grant["reserved_seconds"])
        actual = sum(row["actual"] for row in members.values())
        releases = {row["grant_id"] for row in self._rows("execution_grant_release", {"grant_id": "grant_id"}, campaign_id)}
        grants, allocated = [], 0.
        for grant in self._rows("execution_grant", {"worker_seconds": "worker_seconds", "deadline_at": "deadline_at"}, campaign_id):
            row = members.pop(grant["id"], {"actual": 0., "committed": 0.})
            committed = row["committed"] if grant["id"] in releases else max(_number(grant["worker_seconds"]), row["committed"])
            allocated += committed
            grants.append({"grant_id": grant["id"], "actual_seconds": row["actual"], "allocated_seconds": committed,
                           "member_committed_seconds": row["committed"], "available_seconds": max(0., _number(grant["worker_seconds"]) - row["committed"]),
                           "released": grant["id"] in releases, "deadline_at": grant["deadline_at"]})
        allocated += sum(row["committed"] for row in members.values())
        limit = sum(_number(row["limit"]) for row in campaigns)
        return {"actual_seconds": actual, "allocated_seconds": allocated, "limit_seconds": limit,
                "remaining_seconds": max(0., limit - allocated), "grants": grants,
                "basis": "Summed worker wall time; grant reservations include their child jobs once. Forecasts are not reservations."}

    def _costs(self, campaign_id):
        # Prefix ranges ('/' follows '.') can use the events_campaign_kind index.
        query = "SELECT COALESCE(MAX(id),0) FROM events WHERE ((kind >= 'trial.' AND kind < 'trial/') OR (kind >= 'fixed_mask.' AND kind < 'fixed_mask/'))"
        values = []
        if campaign_id is not None:
            query += " AND campaign_id=?"
            values.append(campaign_id)
        count_query = "SELECT COUNT(*) FROM records WHERE kind IN ('trial','fixed_mask_job')"
        if campaign_id is not None:
            count_query += " AND campaign_id=?"
        with self.store.connection() as database:
            cursor = database.execute(query, values).fetchone()[0]
            count = database.execute(count_query, values).fetchone()[0]
        sampled = self.monotonic()
        cached = self._cost_cache.get(campaign_id)
        # Normal supervisor progress emits an event; a bounded expiry also
        # catches maintenance/import writes that intentionally omit events.
        if cached and cached[1:3] == (cursor, count) and sampled - cached[0] < 30:
            return cached[3]
        fields = {"status": "status", "execution_grant_id": "execution_grant_id", "wall_seconds": "wall_seconds",
                  "execution_seconds": "execution_seconds", "upper_bound_seconds": "execution_seconds_upper_bound",
                  "progress_seconds": "progress.elapsed_seconds", "result_seconds": "result.elapsed_seconds", "isolated": "isolation_policy"}
        trials = self._rows("trial", fields, campaign_id)
        for row in trials:
            row["kind"] = "trial"
            row["spent_seconds"] = _number(row["execution_seconds"]) if row["isolated"] else max(_number(row["execution_seconds"]), _number(row["progress_seconds"]), _number(row["result_seconds"]))
            row.pop("isolated", None)
        for row in self._rows("fixed_mask_job", {"status": "status", "wall_seconds": "wall_seconds", "execution_seconds": "execution_seconds"}, campaign_id):
            row.update(kind="fixed_mask_job", spent_seconds=_number(row["execution_seconds"]), upper_bound_seconds=0, execution_grant_id=None)
            trials.append(row)
        self._cost_cache[campaign_id] = (sampled, cursor, count, trials)
        return trials

    def _workers(self, campaign_id, costs, sampled, wall_clock):
        fields = {"status": "status", "pid": "pid", "process_identity": "process_identity", "attempt": "attempt",
                  "attempt_started_at": "attempt_started_at", "started_epoch": "started_epoch", "prior_execution_seconds": "prior_execution_seconds",
                  "algorithm": "algorithm", "seed": "seed"}
        rows = self._rows("trial", fields, campaign_id, active=True) + self._rows("fixed_mask_job", fields, campaign_id, active=True)
        index = {row["id"]: row for row in costs}
        jobs, warnings = [], []
        for row in rows[:256]:
            cost = index[row["id"]]
            measured, state = None, "not_started" if row["status"] == "queued" else "unavailable"
            if row["pid"]:
                observed = self._process(row["pid"], sampled)
                if observed is None:
                    state = "exited"
                elif not row["process_identity"] or observed["process_identity"] != row["process_identity"]:
                    state = "identity_mismatch"
                else:
                    if cost["kind"] == "fixed_mask_job":
                        roots = [self.workspace.directory / "fixed-masks" / row["id"]]
                    else:
                        roots = [self.workspace.directory / "trials" / row["id"], self.workspace.directory / "execution-hosts" / row["id"] / str(row["attempt"])]
                    try:
                        cwd = (self.proc_root / str(row["pid"]) / "cwd").resolve(strict=True)
                        owned = any(cwd == root or root in cwd.parents for root in roots)
                    except OSError:
                        owned = False
                    command = (_read(self.proc_root / str(row["pid"]) / "cmdline") or "").split("\0")
                    owned = owned or any(str(root) in command for root in roots)
                    if not owned:
                        state = "unverified_directory"
                    elif observed["state"] in {"Z", "X"}:
                        state = "exited"
                    else:
                        measured, state = observed, "alive"
            if row["status"] in {"running", "pausing", "stopping"} and state != "alive":
                warnings.append(f"{row['id']} is recorded as {row['status']}, but its worker is {state.replace('_', ' ')}.")
            elapsed = max(0., wall_clock - _number(row["attempt_started_at"] or row["started_epoch"], wall_clock)) if measured else None
            spent = max(cost["spent_seconds"], _number(row["prior_execution_seconds"]) + elapsed) if elapsed is not None and cost["kind"] == "trial" else max(cost["spent_seconds"], elapsed or 0.)
            jobs.append({"id": row["id"], "campaign_id": row["campaign_id"], "kind": cost["kind"], "status": row["status"], "pid": row["pid"],
                         "algorithm": row["algorithm"], "seed": row["seed"],
                         "process_state": state, "rss_bytes": measured["rss_bytes"] if measured else None,
                         "peak_rss_bytes": measured["peak_rss_bytes"] if measured else None, "cpu_percent": measured["cpu_percent"] if measured else None,
                         "threads": measured["threads"] if measured else None, "wall_seconds": cost["wall_seconds"],
                         "spent_seconds": spent, "remaining_seconds": max(0., _number(cost["wall_seconds"]) - spent),
                         "measurement_scope": "Verified owner process only; descendant memory is not included"})
        return {"configured_limit": self.workspace.max_workers, "running_count": sum(job["process_state"] == "alive" for job in jobs),
                "queued_count": sum(job["status"] == "queued" for job in jobs), "jobs": jobs}, warnings

    def _manifest(self, race, sampled):
        if sampled - self._manifest_scan_at > 30:
            paths = []
            for ancestor in [self.workspace.directory, *list(self.workspace.directory.parents)[:4]]:
                root = ancestor / "runs/pilots"
                if not root.is_dir():
                    continue
                try:
                    directories = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)[:32]
                except OSError:
                    continue
                paths.extend(path / "preflight/preflight.json" for path in directories)
                break
            self._manifest_paths, self._manifest_scan_at = paths, sampled
        candidates = [self.workspace.directory / "races" / race["id"] / "preflight.json",
                      self.workspace.directory / "races" / race["id"] / "preflight/preflight.json", *self._manifest_paths]
        for path in candidates:
            try:
                timestamp = path.stat().st_mtime_ns
            except OSError:
                continue
            cached = self._manifests.get(path)
            if cached is None or cached[0] != timestamp:
                cached = (timestamp, selected_manifest(path))
                self._manifests[path] = cached
            manifest = cached[1]
            if manifest and manifest.get("race_id") == race["id"] and manifest.get("campaign_id") == race["campaign_id"]:
                return manifest
        return {}

    def _memory_plan(self, manifest, available, configuration=None):
        samples = [row for row in manifest.get("worker_memory_samples", []) if row.get("phase") == "validation" and row.get("completed_evaluations", 0) > 0 and row.get("peak_rss_bytes", 0) > 0][-4096:]
        predictions = manifest.get("memory_predictions", [])
        historical = predictions[-1] if predictions else {}
        measured = max(samples, key=lambda row: ((2 * row["fidelity"]["rcwa_order_x"] + 1) * (2 * row["fidelity"]["rcwa_order_y"] + 1), row["peak_rss_bytes"]), default={})
        forecast = None
        headroom = _number(historical.get("memory_headroom_bytes"), 4 * GIB)
        if historical:
            predicted, historic_available = historical.get("predicted_peak_bytes"), historical.get("available_memory_bytes")
            forecast = {"fidelity": historical.get("fidelity"), "predicted_bytes": predicted, "headroom_bytes": headroom,
                        "available_bytes": available, "historical_available_bytes": historic_available,
                        "basis": historical.get("basis"), "growth_model": historical.get("growth_model"),
                        "observed_at": manifest.get("updated_at"), "measured_peak_bytes": measured.get("peak_rss_bytes"),
                        "measured_fidelity": measured.get("fidelity"), "measured_at": measured.get("sampled_at"),
                        "safety_factor": 2 if measured else None,
                        "fit_scope": "Available memory only; active-worker future peak growth and other admission rules are not included",
                        "historical_fits": predicted + headroom <= historic_available if predicted is not None and historic_available is not None else None,
                        "fits_now": predicted + headroom <= available if predicted is not None and available is not None else None,
                        "historical_reason": manifest.get("waiting_reason")}
        checks = []
        from optimization_framework.execution.rcwa_memory import estimate_rcwa_memory
        fidelities = manifest.get("fidelities", []) + [{"rcwa_order_x": x, "rcwa_order_y": x // 2} for x in (22, 26, 30, 34)]
        for fidelity in fidelities[:16]:
            if any(row["fidelity"] == fidelity for row in checks):
                continue
            configuration = configuration or {}
            estimate = estimate_rcwa_memory(fidelity, samples, available_memory_bytes=available, memory_headroom_bytes=int(headroom),
                                            grid_x=configuration.get("grid_x", 256), grid_y=configuration.get("grid_y", 128))
            measurements = [row for row in samples if row["fidelity"] == fidelity]
            estimate.update(predicted_bytes=estimate["predicted_peak_bytes"], headroom_bytes=headroom,
                            measured_peak_bytes=max((row["peak_rss_bytes"] for row in measurements), default=None),
                            fits_now=estimate["predicted_peak_bytes"] + headroom <= available if available is not None else None)
            checks.append(estimate)
        return forecast, checks

    def _plans(self, campaign_id, costs, sampled, wall_clock, available):
        index, plans = {row["id"]: row for row in costs}, []
        for race in self.store.list("adaptive_race", campaign_id)[-32:]:
            protocol = self.store.get(race["protocol_id"], "race_protocol")["definition"]
            manifest = self._manifest(race, sampled)
            tasks = self._rows("task", {"definition_id": "problem.definition_id", "grid_x": "problem.configuration.grid_x", "grid_y": "problem.configuration.grid_y",
                                        "fidelity": "problem.fidelity"}, race["campaign_id"])
            task = next((row for row in tasks if row["id"] == race.get("task_id")), {})
            is_grating = task.get("definition_id") == "meent_2d_dual_polarization_deflector"
            configuration = {key: task[key] for key in ("grid_x", "grid_y") if task.get(key) is not None}
            if is_grating and not manifest.get("fidelities"):
                fidelity = json.loads(task["fidelity"]) if isinstance(task.get("fidelity"), str) else task.get("fidelity")
                planned_fidelities = protocol.get("numerical_parameters", {}).get("fidelities", [])
                manifest = {**manifest, "fidelities": planned_fidelities or ([fidelity] if fidelity else [])}
            forecast, checks = self._memory_plan(manifest, available, configuration) if is_grating or manifest.get("fidelities") else (None, [])
            ended = race["status"] in ENDED or wall_clock >= race["deadline_at"]
            cells = race.get("cells", [])
            pending = [cell for cell in cells if cell["status"] == "pending"]
            configurations = {row["id"]: row for row in protocol.get("configurations", [])}
            upcoming = [{"configuration_id": cell.get("configuration_id"),
                         "algorithm": configurations.get(cell.get("configuration_id"), {}).get("algorithm"),
                         "seed": cell.get("seed"), "phase": cell["phase"], "target_seconds": cell.get("rung_seconds"),
                         "remaining_seconds": max(0., _number(cell.get("rung_seconds")) - _number(index.get(cell.get("trial_id"), {}).get("spent_seconds"))),
                         "status": "ended_before_allocation" if ended else "conditional", "reservation": False} for cell in pending[:32]]
            phases = []
            for name in ("numerical_checks", "resource_calibration", "development", "confirmation", "final_validation"):
                phase_name = "preflight" if name == "numerical_checks" else "calibration" if name == "resource_calibration" else name
                phase_cells = [cell for cell in cells if cell["phase"] == phase_name]
                if name == "development":
                    planned_jobs = len([cell for cell in phase_cells if cell["status"] == "pending"]) if pending else len(phase_cells)
                    planned_seconds = sum(max(0., _number(cell.get("rung_seconds")) - _number(index.get(cell.get("trial_id"), {}).get("spent_seconds"))) for cell in phase_cells)
                    basis = "Declared first-rung cells minus existing checkpoint spending; later rungs are adaptive"
                elif name == "confirmation":
                    planned_jobs = len(protocol.get("confirmation_seeds", [])) * 3
                    planned_seconds = planned_jobs * max(protocol.get("rungs_seconds", [0]))
                    basis = "Conditional roster: two selected finalists plus baseline on fresh seeds"
                elif name == "numerical_checks":
                    planned_jobs = len(protocol.get("preflight_subject_trial_ids", []))
                    planned_seconds = planned_jobs * _number(protocol.get("validation_wall_seconds"))
                    basis = "Per-fixture validation allowance; additional fidelity checks are conditional"
                elif name == "final_validation":
                    planned_jobs, planned_seconds = None, None
                    basis = "Final fixed-horizon masks and fidelity depend on confirmation; not yet allocated"
                else:
                    planned_jobs, planned_seconds = None, None
                    basis = "Concurrency and threads require measured calibration; not yet allocated"
                current_stage = race.get("stage")
                if current_stage == "adaptive_followup":
                    current_stage = "development"
                stage_index = ["numerical_checks", "resource_calibration", "development", "confirmation", "final_validation"].index(current_stage) if current_stage in {"numerical_checks", "resource_calibration", "development", "confirmation", "final_validation"} else -1
                own_index = ["numerical_checks", "resource_calibration", "development", "confirmation", "final_validation"].index(name)
                state = "passed" if own_index < stage_index else "ended_before_start" if ended and own_index > stage_index else "ended" if ended else "blocked" if manifest.get("waiting_reason") and own_index == stage_index else "current" if own_index == stage_index else "conditional"
                phases.append({"name": name, "state": state, "jobs": planned_jobs, "worker_seconds": planned_seconds,
                               "threads": race.get("profile", {}).get("threads"), "concurrency": 1 if name == "numerical_checks" else race.get("profile", {}).get("max_workers"),
                               "estimate_basis": basis, "reservation": False})
            spent = sum(max(0., _number(index.get(cell.get("trial_id"), {}).get("spent_seconds")) - _number(cell.get("initial_execution_seconds"))) for cell in cells if cell.get("trial_id"))
            decisions = list(reversed(self._rows("race_decision", {"action": "action", "rationale": "rationale", "created_at": "created_at"},
                                                race["campaign_id"], race_id=race["id"], limit=10)))
            plans.append({"race_id": race["id"], "campaign_id": race["campaign_id"], "status": race["status"], "stage": race["stage"], "ended": ended,
                          "deadline_at": race["deadline_at"], "elapsed_seconds": min(_number(protocol.get("total_seconds")), max(0., wall_clock - race["started_at"])),
                          "total_seconds": protocol.get("total_seconds"), "worker_seconds_spent": spent, "worker_seconds_cap": protocol.get("worker_seconds"),
                          "pending_jobs": len(pending), "upcoming_jobs": upcoming, "blocked_reason": manifest.get("waiting_reason") or race.get("last_error"),
                          "reason": race.get("reason"), "memory_forecast": forecast, "memory_checks": checks, "phases": phases,
                          "decisions": [{key: row.get(key) for key in ("id", "action", "rationale", "created_at")} for row in decisions]})
        return plans

    def snapshot(self, campaign_id=None):
        with self.lock:
            sampled, wall_clock = self.monotonic(), self.clock()
            host = self._host(sampled)
            costs = self._costs(campaign_id)
            budget = self._budget(campaign_id, costs)
            cached_at = self._cost_cache[campaign_id][0]
            age = max(0., sampled - cached_at)
            budget.update(sampled_at=datetime.fromtimestamp(wall_clock - age, timezone.utc).isoformat(),
                          staleness_seconds=age, max_cache_age_seconds=30)
            workers, warnings = self._workers(campaign_id, costs, sampled, wall_clock)
            plans = self._plans(campaign_id, costs, sampled, wall_clock, host["memory"]["effective_available_bytes"])
            own = self._process(os.getpid(), sampled)
            service = {key: own[key] for key in ("pid", "rss_bytes", "peak_rss_bytes", "threads", "cpu_percent")} if own else None
            self._process_samples = {key: value for key, value in self._process_samples.items() if sampled - value[0] < 120}
            self._process_details = {key: value for key, value in self._process_details.items() if sampled - value[0] < 120}
            if host["memory"]["effective_available_bytes"] is None:
                warnings.append("Host memory availability could not be read; memory forecasts cannot establish admission.")
            return {"schema_version": 1, "sampled_at": datetime.fromtimestamp(wall_clock, timezone.utc).isoformat(),
                    "campaign_id": campaign_id, "host": host, "service": service, "workers": workers, "budget": budget,
                    "plans": plans, "warnings": warnings}


_observer_lock = threading.Lock()


def get_observer(workspace):
    """Share a cheap sampler between HTTP polling and agent tool reads."""
    with _observer_lock:
        observer = getattr(workspace, "resource_observer", None)
        if observer is None:
            observer = ResourceObservability(workspace)
            workspace.resource_observer = observer
        return observer
