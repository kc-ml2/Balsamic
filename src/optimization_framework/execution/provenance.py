"""Portable source identities and verified execution of captured framework code.

Paths are resolver state. Manifests contain relative files, installed entry-point
declarations and runtime requirements, and are derived from the captured bytes.
Only reviewed, installed framework/adapter source enters this archive. Generated
executables continue to use the independent implementation service's runtime.
"""
from __future__ import annotations

import ast
from functools import lru_cache
import hashlib
from importlib import metadata
from importlib.util import resolve_name
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

from optimization_framework.contracts.base import content_hash
from optimization_framework.execution.source import snapshot, source_root
from optimization_framework.storage.sqlite import atomic_json


ROOTS = {
    "worker": ["optimization_framework.execution.worker"],
    "recipe": ["optimization_framework.evaluation.recipes"],
    "rule": ["optimization_framework.analysis.rules", "optimization_framework.analysis.standard_rules"],
}
OPERATIONS = {"worker": ["recipe.compile", "trial.prepare", "inference.compile", "problem.describe"], "recipe": ["recipe.compile"],
              "rule": ["rule.freeze", "rule.evaluate", "rule.check_design"]}


def _files(directory):
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("A captured source archive cannot contain symbolic links")
        if path.is_file():
            data = path.read_bytes()
            result[path.relative_to(directory).as_posix()] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return result


def _modules(files):
    return {(name[:-12] if name.endswith("/__init__.py") else name[:-3]).replace("/", "."): name
            for name in files if name.endswith(".py")}


@lru_cache(maxsize=1024)
def _imports(module, filename, text):
    package = module if filename.endswith("/__init__.py") else module.rpartition(".")[0]
    names = set()
    for node in ast.walk(ast.parse(text, filename=filename)):
        if isinstance(node, ast.Import):
            names.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
            names.add(base)
            names.update(base + "." + item.name for item in node.names if item.name != "*")
    return names


def _closure(code, files, roots):
    modules, pending, seen, external = _modules(files), set(roots), set(), set()
    local_packages = {module.split(".")[0] for module in modules}
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        if module not in modules:
            if module.split(".")[0] not in local_packages:
                external.add(module.split(".")[0])
            continue
        seen.add(module)
        filename = modules[module]
        pending.update(_imports(module, filename, (code / filename).read_text()))
        parts = module.split(".")
        pending.update(".".join(parts[:index]) for index in range(1, len(parts)))
    # Adapter resources are conservatively included even if loaded dynamically.
    selected = {modules[module]: files[modules[module]] for module in sorted(seen)}
    for filename, identity in files.items():
        if not filename.endswith(".py") and filename.split("/")[0] in {root.split(".")[0] for root in roots}:
            selected[filename] = identity
    return selected, external


@lru_cache(maxsize=1)
def _distribution_names():
    return metadata.packages_distributions()


def _package_versions(imports):
    pending = set()
    mapping = _distribution_names()
    for name in imports - sys.stdlib_module_names - {"__future__", ""}:
        pending.update(mapping.get(name, [name]))
    result = {}
    while pending:
        name = re.sub(r"[-_.]+", "-", pending.pop()).lower()
        if name in result:
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            result[name] = None
            continue
        result[name] = distribution.version
        for requirement in distribution.requires or []:
            # Optional extras are not an implicit runtime requirement. Include
            # other platform requirements conservatively, including missing ones.
            if "extra" in requirement.partition(";")[2]:
                continue
            match = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if match:
                pending.add(match[0])
    return dict(sorted(result.items()))


def runtime_manifest(imports, locks):
    return {"schema_version": 1, "python": platform.python_version(), "implementation": platform.python_implementation(),
            "abi": sysconfig.get_config_var("SOABI"), "system": platform.system(), "machine": platform.machine(),
            "libc": list(platform.libc_ver()), "packages": _package_versions(set(imports)), "locks": locks}


def _entries(problem_ids):
    installed = {item.name: item.value for item in metadata.entry_points(group="optimization_framework.problems")}
    if set(problem_ids) - set(installed):
        raise ValueError("Captured execution requires an installed, versioned problem entry point")
    return {name: installed[name] for name in sorted(set(problem_ids))}


def capture(directory, *, problem_ids, purpose="worker", recipe_ids=()):
    """Pin actual bytes before producing any scientific or runtime identity."""
    directory = Path(directory)
    if purpose not in ROOTS:
        raise ValueError("Unknown frozen execution purpose")
    snapshot(directory)
    code = directory / "code"
    files = _files(code)
    entries = _entries(problem_ids)
    inference_entries, optimizer_entries = {}, {}
    if purpose == "worker":
        from optimization_framework.evaluation.inference import adapters
        from optimization_framework.optimizers import plugins
        inference_entries = adapters.entries()
        optimizer_entries = plugins.installed.entries()
    from optimization_framework.evaluation.registered_recipes import recipes
    available_recipes = recipes.entries()
    recipe_entries = {identity: available_recipes[identity] for identity in sorted(set(recipe_ids)) if identity in available_recipes}
    adapter_roots = [value.split(":")[0] for value in entries.values()]
    roots = ROOTS[purpose] + adapter_roots + [value.split(":")[0] for value in
        [*inference_entries.values(), *recipe_entries.values(), *optimizer_entries.values()]]
    if purpose == "worker":
        roots.append("optimization_framework.execution.preparation")
    relevant, imports = _closure(code, files, roots)
    locks = {}
    lock_directory = directory / "runtime-locks"
    lock_directory.mkdir(exist_ok=True)
    for name in ("pyproject.toml", "uv.lock"):
        original = source_root().parent / name
        if original.is_file():
            destination = lock_directory / name
            shutil.copyfile(original, destination)
            locks[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "purpose": purpose, "operations": OPERATIONS[purpose], "files": files, "entry_points": entries,
                "inference_entry_points": inference_entries,
                "recipe_entry_points": recipe_entries,
                **({"optimizer_entry_points": optimizer_entries} if optimizer_entries else {}),
                "roots": roots, "scientific_files": relevant, "imports": sorted(imports),
                "scientific_digest": content_hash({"files": relevant, "entry_points": entries,
                    "inference_entry_points": inference_entries, "recipe_entry_points": recipe_entries,
                    **({"optimizer_entry_points": optimizer_entries} if optimizer_entries else {})}),
                "runtime": runtime_manifest(imports, locks)}
    atomic_json(directory / "execution-manifest.json", manifest)
    return manifest


def _verify(directory, manifest, *, runtime=True):
    directory = Path(directory)
    saved = json.loads((directory / "execution-manifest.json").read_text())
    if saved != manifest or _files(directory / "code") != manifest["files"]:
        raise ValueError("The frozen execution source archive is unavailable or changed")
    scientific = {"files": manifest["scientific_files"], "entry_points": manifest["entry_points"]}
    if "inference_entry_points" in manifest:
        scientific["inference_entry_points"] = manifest["inference_entry_points"]
    if "recipe_entry_points" in manifest:
        scientific["recipe_entry_points"] = manifest["recipe_entry_points"]
    if "optimizer_entry_points" in manifest:
        scientific["optimizer_entry_points"] = manifest["optimizer_entry_points"]
    if manifest["scientific_digest"] != content_hash(scientific):
        raise ValueError("The frozen scientific source manifest is inconsistent")
    if any(manifest["files"].get(name) != identity for name, identity in manifest["scientific_files"].items()):
        raise ValueError("The scientific manifest differs from the captured source")
    locks = manifest["runtime"]["locks"]
    for name, expected in locks.items():
        if name not in {"pyproject.toml", "uv.lock"} or hashlib.sha256((directory / "runtime-locks" / name).read_bytes()).hexdigest() != expected:
            raise ValueError("The captured dependency lock changed")
    if runtime and runtime_manifest(manifest["imports"], locks) != manifest["runtime"]:
        raise ValueError("A compatible frozen execution runtime is unavailable; current dependencies cannot replace it")
    return manifest


def verify(directory, manifest, *, runtime=True):
    try:
        return _verify(directory, manifest, runtime=runtime)
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("The frozen execution source archive or dependency manifest is unavailable") from exc


def copy_manifest(source, destination, manifest):
    """The source tree itself is copied by the ordinary worker snapshot path."""
    verify(source, manifest)
    destination = Path(destination)
    shutil.copytree(Path(source) / "runtime-locks", destination / "runtime-locks")
    atomic_json(destination / "execution-manifest.json", manifest)
    return verify(destination, manifest)


def archive(store, *, problem_ids, purpose, recipe_ids=()):
    base = store.directory / "sources"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=base) as temporary:
        manifest = capture(temporary, problem_ids=problem_ids, purpose=purpose, recipe_ids=recipe_ids)
        identity = "source_" + content_hash(manifest)
        destination = base / identity
        if destination.exists():
            verify(destination, manifest)
        else:
            try:
                os.rename(temporary, destination)
            except FileExistsError:
                verify(destination, manifest)
    record = {"schema_version": 1, "id": identity, "manifest": manifest}
    return store.put_immutable("execution_source", record, "execution.source_captured")


def resolve(store, identity):
    if not re.fullmatch(r"source_[a-f0-9]{64}", identity):
        raise ValueError("Invalid captured source identifier")
    record = store.get(identity, "execution_source")
    manifest = record["manifest"]
    if identity != "source_" + content_hash(manifest):
        raise ValueError("The captured source record no longer matches its identity")
    directory = store.directory / "sources" / identity
    verify(directory, manifest)
    return directory, manifest


def invoke(directory, manifest, operation, payload, *, timeout=30, isolated=False):
    """Run a fixed operation against verified archived code, outside the service."""
    if operation not in manifest.get("operations", []):
        raise ValueError("Unsupported frozen compiler operation")
    directory = Path(directory).resolve()
    verify(directory, manifest)
    data = json.dumps({"operation": operation, "payload": payload}, allow_nan=False).encode()
    if len(data) > 32 * 1024**2:
        raise ValueError("Frozen analysis input exceeds the declared 32 MiB limit")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GRATING_LLM_")
           and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}}
    env.update(PYTHONPATH=str(directory / "code"), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    with tempfile.TemporaryDirectory(prefix="optimization-frozen-") as temporary:
        request, output = Path(temporary) / "request.json", Path(temporary) / "output.json"
        request.write_bytes(data)
        try:
            arguments = ["optimization_framework.execution.frozen_host", "--directory", str(directory),
                         "--request", str(request), "--output", str(output)]
            if isolated:
                from optimization_framework.execution.isolation import run
                output.touch()
                process = run(directory, manifest, temporary, arguments, timeout=timeout, compiler_io=(request, output))
            else:
                process = subprocess.run([sys.executable, "-B", "-m", *arguments],
                    cwd=temporary, env=env, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("The frozen rule or recipe compiler exceeded its time allowance") from exc
        if process.returncode or not output.exists():
            raise ValueError("The frozen rule or recipe compiler failed to produce a result")
        if output.stat().st_size > 32 * 1024**2:
            raise ValueError("Frozen analysis output exceeds the declared 32 MiB limit")
        result = json.loads(output.read_text())
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]


def invoke_archive(store, identity, operation, payload):
    directory, manifest = resolve(store, identity)
    return invoke(directory, manifest, operation, payload)
