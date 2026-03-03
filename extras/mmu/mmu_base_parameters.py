# Happy Hare MMU Software
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Base class for all runtime changable mmu parameters
#
#   Subclasses: MmuPrinterParameters, MmuUnitParameters, MmuSelectorParameters
#   Notes:
#     - Support initial setting via config object
#     - Supports updates via parameters to MMU_TEST_CONFIG
#     - Allows for feature gating with "guards"
#     - Allows for parameter hiding from UI
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging
from dataclasses     import dataclass, field
from typing          import Any, Callable, Dict, Optional, Sequence, Union, List, Iterable

# Happy Hare imports
from ..mmu_constants import *


# ----------------------------
# Parameter specification model
# ----------------------------

_Default = Union[Any, Callable[['TunableParametersBase'], Any]]
_Guard = Optional[Callable[['TunableParametersBase'], bool]]
_OnChange = Optional[Callable[['TunableParametersBase', Any, Any], None]]


@dataclass(frozen=True)
class ParamSpec:
    """
    Single source of truth for a parameter:
      - name: instance attribute name + config key + (by default) gcmd key
      - kind: 'int' | 'float' | 'choice' | 'str' | 'list' | 'intlist'
      - default: value or callable(self)->value (for dependent defaults)
      - limits: pass-through kwargs like minval/maxval/above/below (values may be callables)
      - choices: for 'choice' (map used by config.getchoice)
      - section: required section header label for get_test_config() output
      - hidden: if True, omitted from get_test_config() (defaults False)
      - guard: callable(self)->bool (feature gating)
      - on_change: callable(self, old, new) executed after runtime change
      - fmt: printf-style format used by get_test_config()
    """
    name: str
    kind: str
    default: _Default
    section: str
    hidden: bool = False
    limits: Dict[str, Any] = field(default_factory=dict)
    choices: Optional[Dict[str, str]] = None
    guard: _Guard = None
    on_change: _OnChange = None
    fmt: Optional[str] = None

    def __post_init__(self):
        if not self.section or not isinstance(self.section, str):
            raise ValueError(f"ParamSpec.section must be a non-empty string for {self.name}")


class _SourceAdapter:
    """
    Unifies access to config (getint/getfloat/getchoice/getlist/getintlist/get)
    and gcmd (get_int/get_float/get).
    gcmd keys are expected to match spec.name (no capitalization required).
    """

    def __init__(self, src: Any, is_gcmd: bool):
        self.src = src
        self.is_gcmd = is_gcmd

    def _resolve_limits(self, owner: 'TunableParametersBase', spec: ParamSpec) -> Dict[str, Any]:
        """
        Allow limits to be specified as callables, e.g. below=lambda self: self.espooler_max_stepper_speed
        """
        if not spec.limits:
            return {}
        resolved: Dict[str, Any] = {}
        for k, v in spec.limits.items():
            resolved[k] = v(owner) if callable(v) else v
        return resolved

    def get_value(self, owner: 'TunableParametersBase', spec: ParamSpec, default: Any) -> Any:
        key = spec.name
        limits = self._resolve_limits(owner, spec)

        if spec.kind == 'int':
            fn = getattr(self.src, 'get_int' if self.is_gcmd else 'getint')
            return fn(key, default, **limits)

        if spec.kind == 'float':
            fn = getattr(self.src, 'get_float' if self.is_gcmd else 'getfloat')
            return fn(key, default, **limits)

        if spec.kind == 'choice':
            if self.is_gcmd:
                fn = getattr(self.src, 'get')
                val = fn(key, default)
                if spec.choices is not None and val not in spec.choices:
                    raise ValueError(f"Invalid {spec.name}={val}; allowed: {list(spec.choices)}")
                return val
            fn = getattr(self.src, 'getchoice')
            return fn(key, spec.choices, default)

        if spec.kind == 'str':
            fn = getattr(self.src, 'get')
            return fn(key, default)

        if spec.kind == 'list':
            if self.is_gcmd:
                fn = getattr(self.src, 'get')
                raw = fn(key, None)
                if raw is None:
                    return default

                # Allow python list/tuple passed in directly
                if isinstance(raw, (list, tuple)):
                    return list(raw)

                # Otherwise parse comma-separated list
                return [s.strip() for s in str(raw).split(',') if s.strip()]

            fn = getattr(self.src, 'getlist')
            return list(fn(key, default))

        if spec.kind == 'intlist':
            if self.is_gcmd:
                # Accept either a python list/tuple, or a comma-separated string
                fn = getattr(self.src, 'get')
                raw = fn(key, None)
                if raw is None:
                    return default

                if isinstance(raw, (list, tuple)):
                    return [int(x) for x in raw]

                # comma-separated string: "1,2,3"
                s = str(raw).strip()
                if s == '':
                    return []
                return [int(x.strip()) for x in s.split(',') if x.strip()]

            fn = getattr(self.src, 'getintlist')
            return list(fn(key, default))

        raise ValueError(f"Unknown kind={spec.kind} for {spec.name}")


# ----------------------------
# Base class with shared logic
# ----------------------------

class TunableParametersBase:
    """
    Base class for spec-driven tunable parameter containers.

    Subclasses must:
      - define self._ALL_SPECS (Sequence[ParamSpec])
      - call super().__init__(config) early in __init__
      - optionally override _post_load_fixups()

    This base class provides:
      - load from config via specs
      - apply overrides from gcmd via specs
      - get_test_config() using section + hidden flags
      - generic get_param/set_param (validated)
      - check_test_config helper
    """

    _ALL_SPECS: Sequence[ParamSpec] = ()

    def __init__(self, config):
        self._config = config
        self._load_from(self._ALL_SPECS, config, is_gcmd=False)
        self._post_load_fixups()

    # ----- Hooks -----

    def _post_load_fixups(self) -> None:
        """Subclass hook for derived defaults / clamping / conversions."""
        return

    # ----- Spec iteration / defaults -----

    # All possible parameters
    def _iter_all_specs(self, specs: Sequence[ParamSpec]) -> Iterable[ParamSpec]:
        for spec in specs:
            yield spec

    # Parameters not removed by guard
    def _iter_specs(self, specs: Sequence[ParamSpec]) -> Iterable[ParamSpec]:
        for spec in specs:
            if spec.guard is not None and not spec.guard(self):
                continue
            yield spec

    def _resolve_default(self, spec: ParamSpec) -> Any:
        return spec.default(self) if callable(spec.default) else spec.default

    # ----- Core load/apply -----

    def _load_from(self, specs: Sequence[ParamSpec], src: Any, *, is_gcmd: bool) -> None:
        adapter = _SourceAdapter(src, is_gcmd=is_gcmd)
        for spec in self._iter_all_specs(specs):  # Ignore guard for initial creation (defensive)
            default = self._resolve_default(spec) if not is_gcmd else getattr(self, spec.name, self._resolve_default(spec))
            val = adapter.get_value(self, spec, default)
            setattr(self, spec.name, val)

    def _apply_from_gcmd(self, specs: Sequence[ParamSpec], gcmd: Any) -> None:
        adapter = _SourceAdapter(gcmd, is_gcmd=True)
        for spec in self._iter_specs(specs):
            current = getattr(self, spec.name)
            new_val = adapter.get_value(self, spec, current)
            if new_val != current:
                setattr(self, spec.name, new_val)
                if spec.on_change is not None:
                    spec.on_change(self, current, new_val)

    # ----- Public API -----

    def set_test_config(self, gcmd):
        """
        Apply runtime overrides. gcmd keys are expected to match spec.name.
        """
        self._apply_from_gcmd(self._ALL_SPECS, gcmd)

    def get_test_config(self) -> str:
        """
        Pretty-print current parameters that are not hidden, grouped by section.
        """
        lines: List[str] = []
        last_section: Optional[str] = None

        for spec in self._iter_specs(self._ALL_SPECS):
            if spec.hidden:
                continue

            if spec.section != last_section:
                if lines:
                    lines.append("")
                lines.append(f"{spec.section}:")
                last_section = spec.section

            v = getattr(self, spec.name)
            fmt = spec.fmt or "%s"
            try:
                rendered = fmt % v
            except TypeError:
                rendered = str(v)
            lines.append(f"{spec.name} = {rendered}")

        return "\n" + "\n".join(lines) if lines else ""

    def get_param(self, name: str) -> Any:
        """
        Optional param getter. Direct instance variable access is also fine
        """
        if not hasattr(self, name):
            raise AttributeError(f"Unknown parameter: {name}")
        return getattr(self, name)

    def set_param(self, name: str, value: Any) -> None:
        """
        Set parameters inforcing bounds and other checks
        """
        spec = self._find_spec(name)
        if spec is None:
            raise AttributeError(f"Unknown parameter: {name}")
        if spec.guard is not None and not spec.guard(self):
            raise ValueError(f"Parameter not available in this configuration: {name}")

        class _Fake:
            def __init__(self, v): self._v = v
            def get(self, _k, _d=None): return self._v
            def get_int(self, _k, _d=None, **_kw): return int(self._v)
            def get_float(self, _k, _d=None, **_kw): return float(self._v)

        old = getattr(self, name)
        adapter = _SourceAdapter(_Fake(value), is_gcmd=True)
        new = adapter.get_value(self, spec, old)

        if new != old:
            setattr(self, name, new)
            if spec.on_change is not None:
                spec.on_change(self, old, new)

    def check_test_config(self, param: str) -> bool:
        """
        Check if parameter exists.
        """
        if not hasattr(self, param):
            return False
        return getattr(self, param) is None

    # ----- Helpers -----

    def _find_spec(self, name: str) -> Optional[ParamSpec]:
        for spec in self._ALL_SPECS:
            if spec.name == name:
                return spec
        return None
