"""Pin executable source independently of the running workspace process."""
from importlib.metadata import entry_points, version, PackageNotFoundError
from importlib.util import find_spec
from pathlib import Path
import hashlib
import shutil
import sys


def package_names():
    return sorted({"optimization_framework", *(p.value.split(":")[0].split(".")[0]
        for group in ("optimization_framework.problems", "optimization_framework.inference", "optimization_framework.recipes",
                      "optimization_framework.optimizers") for p in entry_points(group=group))})


def source_root():
    return Path(__file__).resolve().parents[2]


def snapshot(directory):
    code = Path(directory) / "code"
    if not code.exists():
        for name in package_names():
            package = source_root() / name
            if not package.is_dir():
                spec = find_spec(name)
                locations = list(spec.submodule_search_locations or []) if spec else []
                if len(locations) != 1:
                    raise ValueError(f"Installed source package {name!r} has no unambiguous capture location")
                package = Path(locations[0])
            shutil.copytree(package, code / name, ignore=shutil.ignore_patterns("__pycache__"))
    digest = hashlib.sha256()
    for path in sorted(code.rglob("*.py")):
        digest.update(str(path.relative_to(code)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def copy_snapshot(source, destination, expected_digest):
    """A confirmation repeats captured code even after the workspace changes."""
    source, destination = Path(source), Path(destination)
    if not (source / "code").is_dir() or snapshot(source) != expected_digest:
        raise ValueError("The frozen prototype source is unavailable or changed")
    shutil.copytree(source / "code", destination / "code", ignore=shutil.ignore_patterns("__pycache__"))
    if snapshot(destination) != expected_digest:
        raise ValueError("The prototype source changed while capturing confirmation code")
    return expected_digest


def scientific_hash(root=None):
    root = Path(root) if root else source_root()
    if root.name in package_names():
        root = root.parent
    h = hashlib.sha256()
    for name in package_names():
        for path in sorted((root / name).rglob("*.py")):
            relative = path.relative_to(root)
            if name == "optimization_framework" and relative.parts[1] not in {"contracts", "evaluation", "execution", "optimizers", "implementations"}:
                continue
            if "workspace" in relative.parts and path.name not in {"worker.py", "optimizers.py", "custom_optimizer.py"}:
                continue
            if path.name == "api.py":
                continue
            h.update(str(relative).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def runtime_identity():
    result = {"python": ".".join(map(str, sys.version_info[:3]))}
    for name in ("meent", "numpy", "scipy", "torch"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result
