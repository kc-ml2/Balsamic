"""Optimizer plug-ins: methods contributed by installed packages.

The framework never imports a project or optimizer library by name. A package
declares an entry point in the ``optimization_framework.optimizers`` group whose
value names a zero-argument factory (normally a class) for an ``OptimizerPlugin``.
See docs/optimizer-plugins.md for the design and a worked example.

Entries come from installed package metadata, or from a verified execution
manifest inside frozen hosts and workers. Model output selects method IDs,
never import paths.
"""
from __future__ import annotations

from importlib import import_module, metadata
from typing import Any, Protocol, runtime_checkable


GROUP = "optimization_framework.optimizers"
REQUIRED = ("id", "name", "description", "representations", "constraints", "parameters", "parameter_properties")


@runtime_checkable
class OptimizerPlugin(Protocol):
    def methods(self) -> list[dict]:
        """Method descriptors: REQUIRED keys, optional problem_ids and completion_units."""

    def unavailable_reason(self) -> str | None:
        """Why these methods cannot run on this host (for example a missing library), else None."""

    def validate(self, name: str, instance, parameters: dict) -> None:
        """Reject parameters beyond the declared JSON schema by raising ValueError."""

    def create(self, name: str, instance, parameters: dict, seed: int) -> Any:
        """Return an optimizer with propose/observe/checkpoint/restore/inspect/export_artifacts/close."""


class OptimizerPlugins:
    def __init__(self, entries=None):
        self._entries = None if entries is None else dict(entries)
        self._owners = None

    def entries(self):
        """Plug-in name -> "module:factory", as recorded in execution manifests."""
        if self._entries is not None:
            return dict(self._entries)
        entries = {}
        for entry in metadata.entry_points(group=GROUP):
            if entry.name in entries:
                raise ValueError(f"Duplicate optimizer plug-in {entry.name!r}")
            entries[entry.name] = entry.value
        return dict(sorted(entries.items()))

    def _load(self):
        if self._owners is None:
            owners = {}
            for name, value in self.entries().items():
                module, factory = value.split(":", 1)
                plugin = getattr(import_module(module), factory)()
                if not isinstance(plugin, OptimizerPlugin):
                    raise ValueError(f"Optimizer plug-in {name!r} must provide methods, unavailable_reason, validate and create")
                for method in plugin.methods():
                    if missing := [key for key in REQUIRED if key not in method]:
                        raise ValueError(f"Optimizer plug-in {name!r} method lacks {', '.join(missing)}")
                    if method["id"] in owners:
                        raise ValueError(f"Optimizer method {method['id']!r} is registered twice")
                    owners[method["id"]] = (plugin, dict(method))
            self._owners = owners
        return self._owners

    def methods(self):
        return [dict(method) for _, method in self._load().values()]

    def get(self, method_id):
        """(plugin, descriptor) for a plug-in method, or None for any other ID."""
        return self._load().get(method_id)


# Installed plug-ins; frozen hosts replace this with their manifest's entries.
installed = OptimizerPlugins()
