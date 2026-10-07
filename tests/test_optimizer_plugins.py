"""Optimizer plug-ins: installed packages add methods; the framework names none of them."""
import ast
from pathlib import Path
import shutil
import textwrap

import pytest

from optimization_benchmarks.problems import ContinuousProblem
from optimization_framework.execution import frozen_host, provenance
from optimization_framework.optimizers import plugins
from optimization_framework.optimizers.registry import capability_reason, create, methods


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = '''
class Step:
    def __init__(self, step):
        self.step = step


class Plugin:
    reason = None

    def methods(self):
        return [{"id": "fixed_step", "name": "Fixed step", "description": "Test plug-in.",
                 "representations": ["continuous"], "constraints": True, "parameters": {},
                 "parameter_properties": {"step": {"type": "number", "exclusiveMinimum": 0}}}]

    def unavailable_reason(self):
        return self.reason

    def validate(self, name, instance, parameters):
        if parameters.get("step", 1) > 10:
            raise ValueError("step is too large for this problem")

    def create(self, name, instance, parameters, seed):
        return Step(parameters.get("step", 1))


class Unavailable(Plugin):
    reason = "Install fixed-step 2.0 before using this method"


class Duplicate(Plugin):
    pass


class Incomplete(Plugin):
    def methods(self):
        return [{"id": "incomplete", "name": "Incomplete"}]


class NotAPlugin:
    def methods(self):
        return []
'''


@pytest.fixture
def module(tmp_path, monkeypatch):
    (tmp_path / "fixed_step_plugin.py").write_text(textwrap.dedent(PLUGIN))
    monkeypatch.syspath_prepend(str(tmp_path))
    return "fixed_step_plugin"


def instance():
    return ContinuousProblem().resolve({})


def test_framework_never_imports_an_optimizer_library_or_its_adapter():
    imported = set()
    for path in (ROOT / "src/optimization_framework").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not {name for name in imported if name.startswith(("mask_optimizers", "dqn_meent.mask_library_adapter"))}


def test_installed_plugin_contributes_complete_methods():
    assert plugins.installed.entries() == {"mask_optimizers": "dqn_meent.mask_library_adapter:MaskLibraryPlugin"}
    listed = {method["id"]: method for method in methods()}
    for name in ("motif_surgery", "nested_fourier", "phenotype_de"):
        assert listed[name]["contract"] == "optimizer_v1"
        assert listed[name]["problem_ids"] == ["meent_2d_dual_polarization_deflector"]
        assert "parameter_properties" not in listed[name]
    assert set(listed["phenotype_de"]["parameter_schema"]["properties"]) >= {"modes_x", "modes_y"}


def test_plugin_methods_follow_the_registry_they_are_listed_from(module):
    registry = plugins.OptimizerPlugins({"fixed": f"{module}:Plugin"})
    assert "fixed_step" in {method["id"] for method in methods(registry)}
    assert "fixed_step" not in {method["id"] for method in methods()}


def test_plugin_validates_and_creates_its_own_optimizer(module):
    registry = plugins.OptimizerPlugins({"fixed": f"{module}:Plugin"})
    assert capability_reason("fixed_step", instance(), registry) is None
    optimizer = create("fixed_step", instance(), {"step": 2.5}, seed=1, schedule_steps=3, optimizer_plugins=registry)
    assert optimizer.step == 2.5
    with pytest.raises(ValueError, match="too large"):
        create("fixed_step", instance(), {"step": 11}, seed=1, schedule_steps=3, optimizer_plugins=registry)
    with pytest.raises(ValueError, match="Unsupported optimizer parameters: radius"):
        create("fixed_step", instance(), {"radius": 1}, seed=1, schedule_steps=3, optimizer_plugins=registry)
    assert capability_reason("fixed_step", instance()) == "Missing implementation; commission or reuse a validated version"


def test_unavailable_plugin_reports_its_reason_instead_of_running(module):
    registry = plugins.OptimizerPlugins({"fixed": f"{module}:Unavailable"})
    assert capability_reason("fixed_step", instance(), registry) == "Install fixed-step 2.0 before using this method"
    with pytest.raises(ValueError, match="Install fixed-step 2.0"):
        create("fixed_step", instance(), {}, seed=1, schedule_steps=3, optimizer_plugins=registry)


@pytest.mark.parametrize("entries, message", [
    ({"a": "{m}:Plugin", "b": "{m}:Duplicate"}, "registered twice"),
    ({"a": "{m}:Incomplete"}, "lacks description"),
    ({"a": "{m}:NotAPlugin"}, "must provide"),
])
def test_malformed_plugins_fail_loudly(module, entries, message):
    registry = plugins.OptimizerPlugins({name: value.format(m=module) for name, value in entries.items()})
    with pytest.raises(ValueError, match=message):
        registry.methods()


def test_frozen_execution_records_plugin_source_and_library_version(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    manifest = provenance.capture(original, problem_ids=["bounded_continuous"])
    assert manifest["optimizer_entry_points"] == plugins.installed.entries()
    assert "dqn_meent/mask_library_adapter.py" in manifest["scientific_files"]
    assert manifest["runtime"]["packages"]["mask-optimizers"] == "0.1.0"
    changed = tmp_path / "changed"
    changed.mkdir()
    shutil.copytree(original / "code", changed / "code")
    adapter = changed / "code/dqn_meent/mask_library_adapter.py"
    adapter.write_text(adapter.read_text() + "\n# Changed plug-in adapter\n")
    assert provenance.capture(changed, problem_ids=["bounded_continuous"])["scientific_digest"] != manifest["scientific_digest"]


def test_frozen_host_uses_the_manifest_plugins(module, monkeypatch):
    from optimization_framework.evaluation import inference, registered_recipes, registry
    for target in (registry, "problems"), (inference, "adapters"), (registered_recipes, "recipes"), (plugins, "installed"):
        monkeypatch.setattr(*target, getattr(*target))
    frozen_host.install_entries({"entry_points": {}, "optimizer_entry_points": {"fixed": f"{module}:Plugin"}})
    assert [method["id"] for method in plugins.installed.methods()] == ["fixed_step"]
