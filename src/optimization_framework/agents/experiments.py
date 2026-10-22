"""What the experiment operator reads and computes: how the campaign's runs are going, and which settings to try next."""
import math

import numpy as np

from optimization_framework.campaigns.experiment_rules import check_trial_ids, classify, current as current_rules

AT_WORK = {"queued", "running", "pausing", "stopping", "paused"}


def run_name(trial):
    return ((trial.get("question") or "").strip().splitlines() or [""])[0][:160] or f"{trial.get('algorithm')} · seed {trial.get('seed')}"


def _trend(workspace, trial, elapsed):
    """The best score at half the elapsed time and now: whether the run is still improving."""
    try:
        rows = workspace.metrics(trial["id"], fields=["elapsed_seconds", "best_objective"])
    except (KeyError, ValueError, OSError):
        return None
    rows = [row for row in rows if isinstance(row.get("elapsed_seconds"), (int, float)) and isinstance(row.get("best_objective"), (int, float))]
    if len(rows) < 2 or not elapsed:
        return None
    half = [row["best_objective"] for row in rows if row["elapsed_seconds"] <= elapsed / 2]
    return {"best_at_half_time": half[-1] if half else rows[0]["best_objective"], "best_now": rows[-1]["best_objective"]}


def progress(workspace, campaign_id, finished=8):
    """The runs at work, in queue order, and the latest finished ones, with how far each has come."""
    rules = current_rules(workspace.store, campaign_id)
    trials = workspace.store.list_compact_trials(campaign_id)
    kinds = classify(trials, rules["short_run_seconds"], check_trial_ids(workspace.store, campaign_id))
    def row(trial, trend=False):
        measured = {**(trial.get("result") or {}), **(trial.get("progress") or {})}
        elapsed, cap = measured.get("elapsed_seconds"), trial.get("wall_seconds")
        evaluations, allowed = measured.get("evaluations") or 0, trial.get("max_steps")
        left = []
        if isinstance(cap, (int, float)) and isinstance(elapsed, (int, float)):
            left.append(max(0.0, cap - elapsed))
        if allowed and evaluations and elapsed:
            left.append(max(0.0, (allowed - evaluations) * elapsed / evaluations))
        item = {"trial_id": trial["id"], "name": run_name(trial), "kind": kinds[trial["id"]], "status": trial["status"],
                "method": trial.get("algorithm"), "seed": trial.get("seed"), "device": trial.get("device"), "priority": trial.get("priority", 0),
                "elapsed_seconds": elapsed, "allotted_seconds": cap, "evaluations": evaluations, "allotted_evaluations": allowed,
                "best": measured.get("best_objective"), "seconds_left_at_most": round(min(left)) if left else None,
                "reason": trial.get("reason") if trial["status"] in {"failed", "interrupted", "stopped"} else None}
        if trend and trial["status"] == "running":
            item["trend"] = _trend(workspace, trial, elapsed)
        return item
    at_work = sorted((trial for trial in trials if trial["status"] in AT_WORK), key=lambda trial: (trial["status"] != "running", -trial.get("priority", 0), trial["created_at"]))
    done = sorted((trial for trial in trials if trial["status"] not in AT_WORK and not trial.get("hidden")),
                  key=lambda trial: trial.get("finished_at") or trial.get("updated_at") or "")[-finished:]
    return {"worker_ceiling": workspace.max_workers, "device_slots": workspace.devices.sizes(), "campaign_limit": rules["max_concurrent"],
            "running": sum(trial["status"] == "running" for trial in trials),
            "short_run_seconds": rules["short_run_seconds"], "at_work": [row(trial, trend=True) for trial in at_work[:40]],
            "recently_finished": [row(trial) for trial in reversed(done)],
            "hidden": sum(bool(trial.get("hidden")) for trial in trials),
            "note": "best is the best score so far; trend compares it with the best at half the elapsed time. The queue runs in priority order."}


# ---- Which settings to try next -------------------------------------------------------------------------------------

def _dimensions(space):
    """The unit-cube coordinates of each parameter: one for a number, one per choice for a category."""
    dims = []
    for name, spec in space.items():
        kind = spec.get("type", "float")
        if kind == "choice":
            if not spec.get("choices"):
                raise ValueError(f"{name}: a choice needs its choices")
            dims += [(name, kind, choice) for choice in spec["choices"]]
        else:
            low, high = spec.get("low"), spec.get("high")
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or not low < high:
                raise ValueError(f"{name}: give low < high")
            if kind == "log" and low <= 0:
                raise ValueError(f"{name}: a log-scaled range must be positive")
            if kind not in {"float", "int", "log"}:
                raise ValueError(f"{name}: type is float, int, log or choice")
            dims.append((name, kind, None))
    return dims


def _encode(space, config):
    point = []
    for name, spec in space.items():
        kind, value = spec.get("type", "float"), config.get(name)
        if kind == "choice":
            point += [1.0 if value == choice else 0.0 for choice in spec["choices"]]
            continue
        if not isinstance(value, (int, float)):
            raise ValueError(f"An observation has no number for {name}")
        low, high = float(spec["low"]), float(spec["high"])
        unit = (math.log(value) - math.log(low)) / (math.log(high) - math.log(low)) if kind == "log" else (value - low) / (high - low)
        point.append(min(1.0, max(0.0, unit)))
    return np.array(point)


def _decode(space, point):
    config, index = {}, 0
    for name, spec in space.items():
        kind = spec.get("type", "float")
        if kind == "choice":
            width = len(spec["choices"])
            config[name] = spec["choices"][int(np.argmax(point[index:index + width]))]
            index += width
            continue
        low, high, unit = float(spec["low"]), float(spec["high"]), float(point[index])
        value = math.exp(math.log(low) + unit * (math.log(high) - math.log(low))) if kind == "log" else low + unit * (high - low)
        config[name] = int(round(value)) if kind == "int" else float(f"{value:.6g}")
        index += 1
    return config


def _snap(space, points):
    """Points as they would be run: a category takes one choice, an integer a whole number."""
    return np.array([_encode(space, _decode(space, point)) for point in points])


def _gaussian_process(x, y):
    """A Gaussian process on the observations, its length scale and noise chosen by marginal likelihood."""
    from scipy.linalg import cho_factor, cho_solve
    best = None
    distances = ((x[:, None, :] - x[None, :, :]) ** 2).sum(-1)
    for scale in (0.1, 0.2, 0.35, 0.6, 1.0):
        for noise in (1e-4, 1e-2, 1e-1):
            kernel = np.exp(-distances / (2 * scale ** 2)) + noise * np.eye(len(x))
            try:
                factor = cho_factor(kernel, lower=True)
            except np.linalg.LinAlgError:
                continue
            alpha = cho_solve(factor, y)
            likelihood = -0.5 * y @ alpha - np.log(np.diag(factor[0])).sum()
            if best is None or likelihood > best[0]:
                best = (likelihood, scale, factor, alpha)
    _, scale, factor, alpha = best
    def predict(points):
        cross = np.exp(-((points[:, None, :] - x[None, :, :]) ** 2).sum(-1) / (2 * scale ** 2))
        mean = cross @ alpha
        variance = np.clip(1.0 - (cross * cho_solve(factor, cross.T).T).sum(-1), 1e-12, None)
        return mean, np.sqrt(variance)
    return predict, scale


def suggest(space, observations, *, count=4, maximize=True, seed=0):
    """The next settings to run. With few observations, a space-filling spread (scrambled Sobol); after that, the points of
    highest expected improvement under a Gaussian process fitted to the scores, chosen one at a time so a batch spreads out."""
    from scipy.stats import norm, qmc
    if not space or len(space) > 12:
        raise ValueError("Describe 1 to 12 parameters")
    dims = _dimensions(space)
    width = len(dims)
    rows = [(row["config"], float(row["score"])) for row in observations
            if isinstance(row.get("config"), dict) and isinstance(row.get("score"), (int, float)) and math.isfinite(row["score"])]
    x = np.array([_encode(space, config) for config, _ in rows]).reshape(len(rows), width)
    seen = {tuple(np.round(point, 6)) for point in x}
    sobol = qmc.Sobol(width, scramble=True, seed=seed)
    initial = max(4, min(10, width + 2))
    if len(rows) < initial:
        candidates = _snap(space, sobol.random(64))
        picked = [point for point in candidates if tuple(np.round(point, 6)) not in seen]
        unique = list({tuple(np.round(point, 6)): point for point in picked}.values())
        return {"method": "space-filling", "suggestions": [_decode(space, point) for point in unique[:count]],
                "note": f"{len(rows)} of the {initial} scored settings needed before scores guide the choice; these spread over the space."}
    sign = 1.0 if maximize else -1.0
    raw = sign * np.array([score for _, score in rows])
    spread = raw.std() or 1.0
    y = (raw - raw.mean()) / spread
    rng = np.random.default_rng(seed)
    best_points = x[np.argsort(-y)[:3]]
    local = np.clip(np.repeat(best_points, 200, axis=0) + rng.normal(0, 0.08, (600, width)), 0, 1)
    candidates = _snap(space, np.vstack([sobol.random(2048), local]))
    chosen, xs, ys = [], x.copy(), y.copy()
    for _ in range(count):
        predict, scale = _gaussian_process(xs, ys)
        mean, deviation = predict(candidates)
        gain = mean - ys.max()
        z = gain / deviation
        improvement = gain * norm.cdf(z) + deviation * norm.pdf(z)
        for point in chosen:
            improvement[np.all(np.isclose(candidates, point), axis=1)] = -np.inf
        for point in seen:
            improvement[np.all(np.isclose(candidates, np.array(point)), axis=1)] = -np.inf
        pick = candidates[int(np.argmax(improvement))]
        chosen.append(pick)
        # Believe the model's mean at the pick, so the next pick goes elsewhere.
        xs = np.vstack([xs, pick]); ys = np.append(ys, predict(pick[None, :])[0][0])
    predict, scale = _gaussian_process(x, y)
    mean, deviation = predict(np.array(chosen))
    return {"method": "model", "length_scale": scale,
            "suggestions": [{"config": _decode(space, point), "predicted": float(raw.mean() + spread * m) * sign,
                             "uncertainty": float(spread * d)} for point, m, d in zip(chosen, mean, deviation)],
            "note": "Expected improvement under a Gaussian process fitted to the scores; predicted and uncertainty are in score units."}
