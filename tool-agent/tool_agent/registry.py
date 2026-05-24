"""Tool Registry — loads adapter classes per adapters.yaml.

See TOOL-ADAPTERS.md §7 for the registry format.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from .adapters import ToolAdapter


class ToolRegistry:
    def __init__(self):
        self._adapters: dict[str, ToolAdapter] = {}

    def load(self, config_path: Path) -> None:
        data = yaml.safe_load(config_path.read_text())
        for entry in data.get("adapters", []):
            if not entry.get("enabled", True):
                continue
            cls = self._import(entry["class"])
            adapter = cls()
            self._adapters[adapter.name] = adapter

    def _import(self, dotted: str) -> Any:
        module_path, class_name = dotted.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)

    def get(self, name: str) -> ToolAdapter:
        if name not in self._adapters:
            raise KeyError(f"Unknown tool '{name}'. Loaded: {sorted(self._adapters.keys())}")
        return self._adapters[name]

    @property
    def names(self) -> list[str]:
        return sorted(self._adapters.keys())
