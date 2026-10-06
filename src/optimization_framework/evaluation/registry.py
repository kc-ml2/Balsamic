"""Lazy installed entry points. Model output can select IDs, never import paths."""
from __future__ import annotations

from importlib.metadata import entry_points
from importlib import import_module
from typing import Callable

from optimization_framework.contracts.problems import ProblemAdapter, ProblemInstance


class ProblemRegistry:
    def __init__(self):
        self._loaders: dict[str, Callable] = {}
        self._adapters: dict[str, ProblemAdapter] = {}

    def discover(self):
        for item in entry_points(group="optimization_framework.problems"):
            if item.name not in self._loaders:
                self.register(item.name, item.load)
        return self

    def register(self, name: str, loader: Callable):
        """Application installation hook; intentionally absent from the HTTP API."""
        if name in self._loaders:
            raise ValueError(f"Duplicate problem registration: {name}")
        self._loaders[name] = loader

    @classmethod
    def from_entries(cls, entries):
        """Installed declarations from a verified source manifest, never an API input."""
        registry = cls()
        def load(value):
            module, attribute = value.split(":", 1)
            factory = import_module(module)
            for part in attribute.split("."):
                factory = getattr(factory, part)
            return factory
        for name, value in entries.items():
            registry.register(name, lambda value=value: load(value))
        return registry

    def ids(self) -> list[str]:
        return sorted(self._loaders)

    def extended(self, adapter):
        """Bind this exact adapter in a caller's scope, including an existing name.

        Resolution still checks the complete definition/evaluator identity. The
        installed registry and other callers retain their original registration.
        """
        registry = ProblemRegistry()
        registry._loaders = self._loaders.copy()
        registry._adapters = self._adapters.copy()
        name = adapter.describe().id
        registry._loaders[name] = lambda: lambda: adapter
        registry._adapters[name] = adapter
        return registry

    def get(self, name: str) -> ProblemAdapter:
        if name not in self._loaders:
            raise ValueError(f"Problem adapter {name!r} is not installed; commission and validate an adapter first")
        if name not in self._adapters:
            factory = self._loaders[name]()
            adapter = factory()
            if adapter.describe().id != name:
                raise ValueError("Problem entry point and manifest identifiers disagree")
            self._adapters[name] = adapter
        return self._adapters[name]

    def examples(self) -> list[dict]:
        """Starting setups that installed adapters publish through an optional ``examples()`` hook."""
        result = []
        for name in self.ids():
            hook = getattr(self.get(name), "examples", None)
            if hook is not None:
                result.extend(hook())
        return result

    def resolve(self, name: str, configuration: dict, fidelity: dict | None = None) -> ProblemInstance:
        return self.get(name).resolve(configuration, fidelity)

    def evaluator(self, instance: ProblemInstance):
        adapter = self.get(instance.definition_id)
        resolved = adapter.resolve(instance.configuration, instance.fidelity)
        if resolved != instance:
            raise ValueError("Pinned problem/evaluator version is no longer available with the same identity")
        return adapter.evaluator(instance)


problems = ProblemRegistry().discover()
