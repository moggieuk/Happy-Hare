# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
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
from dataclasses    import dataclass, field
from typing         import Any, Callable, Dict, Optional, Sequence, Union, List, Iterable, Tuple

# Happy Hare imports
from .mmu_constants import *


# ----------------------------
# Parameter specification model
# ----------------------------

_Default  = Union[Any, Callable[['TunableParametersBase'], Any]]
_Guard    = Optional[Callable[['TunableParametersBase'], bool]]
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
      - section: required section header label for listing output
      - hidden: if True, omitted from listing output (defaults False)
      - guard: callable(self)->bool (feature gating for runtime change / listing if desired)
      - on_change: callable(self, old, new) executed after runtime change
      - fmt: printf-style format used by listing output
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

    def _key(self, spec: ParamSpec) -> str:
        return spec.name.upper() if self.is_gcmd else spec.name

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
        key = self._key(spec)
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
      - define self._SPECS (Sequence[ParamSpec])
      - call super().__init__(config) early in __init__
      - optionally override _post_load_fixups()

    This base class provides:
      - load from config via specs
      - apply overrides from gcmd via specs (strict validation + guarded-out detection)
      - param listing for UI / diagnostics
    """

    _SPECS: Sequence[ParamSpec] = ()

    def __init__(self, config):
        self._config = config
        self._not_in_configfile = set()

        # Build name->spec index once (fast lookups, easy validation)
        self._spec_by_name: Dict[str, ParamSpec] = {}
        for spec in self._SPECS:
            if spec.name in self._spec_by_name:
                raise ValueError(f"Duplicate ParamSpec name: {spec.name}")
            self._spec_by_name[spec.name] = spec

        self._load_from_config()
        self._post_load_fixups()


    # ----- Hooks -----

    def _post_load_fixups(self) -> None:
        """
        Subclass hook for derived defaults / clamping / conversions.
        """
        return


    # ----- Defaults / guards -----

    def _resolve_default(self, spec: ParamSpec) -> Any:
        return spec.default(self) if callable(spec.default) else spec.default

    def _is_available(self, spec):
        """
        Availability for runtime change. (You can also use this for listing if desired.)
        """
        return (spec.guard(self) if spec.guard is not None else True)

    def _is_in_configfile(self, spec):
        return spec.name not in self._not_in_configfile


    # ----- Load -----

    def _load_from_config(self):
        adapter = _SourceAdapter(self._config, is_gcmd=False)

        # All keys explicitly present in this config section
        present = set()
        if hasattr(self._config, "get_prefix_options"):
            present = {str(o).strip().lower() for o in self._config.get_prefix_options('')}
        else:
            present = set()

        self._not_in_configfile.clear()

        for spec in self._SPECS:
            if spec.name.lower() not in present:
                self._not_in_configfile.add(spec.name)

            default = self._resolve_default(spec)
            val = adapter.get_value(self, spec, default)
            setattr(self, spec.name, val)


    # ----------------------------
    # Public: apply runtime overrides
    # ----------------------------

    def get_known_param_names(self) -> Iterable[str]:
        return self._spec_by_name.keys()

    def apply_gcmd(self, gcmd: Any, *, strict: bool = True) -> Tuple[List[str], List[str], List[str]]:
        """
        Apply parameters present in the gcmd to this parameter set.

        Returns (applied, guarded_out, unknown) as 3 lists of parameter names.

        strict=True:
          - raises ValueError if any unknown parameters are present
          - raises ValueError if any guarded-out parameters are attempted to be set

        Notes:
          - Only applies keys explicitly present on the command line
          - Keys are expected to match spec.name (lower-case)
        """
        raw = dict(getattr(gcmd, "get_command_parameters", lambda: {})())
        supplied = {str(k).strip().lower() for k in raw.keys()}
        supplied.discard('')
        supplied.discard('quiet')
        supplied.discard('unit')
        supplied.discard('all')

        unknown: List[str] = []
        guarded_out: List[str] = []
        applied: List[str] = []

        adapter = _SourceAdapter(gcmd, is_gcmd=True)

        for name in sorted(supplied):
            spec = self._spec_by_name.get(name)
            if spec is None:
                unknown.append(name)
                continue

            if not self._is_available(spec):
                guarded_out.append(name)
                continue

            current = getattr(self, spec.name)
            new_val = adapter.get_value(self, spec, current)
            if new_val != current:
                # Call on_change handler before the actual parameter is updated
                if spec.on_change is not None:
                    spec.on_change(self, current, new_val)
                setattr(self, spec.name, new_val)
                applied.append(name)

        if strict and unknown:
            raise ValueError("Unknown parameter(s): %s" % ", ".join(unknown))
        if strict and guarded_out:
            raise ValueError("Parameter(s) not available for runtime change: %s" % ", ".join(guarded_out))

        return applied, guarded_out, unknown


    # ----------------------------
    # Public: listing / formatting
    # ----------------------------

    def iter_params(self, *, include_hidden=False, include_guarded_out=False, include_not_in_configfile=False):
        """
        Iterate (spec, value) in spec order, with optional filtering.
        """
        for spec in self._SPECS:
            if not include_hidden and spec.hidden:
                continue
            if not include_guarded_out and not self._is_available(spec):
                continue
            if not include_not_in_configfile and not self._is_in_configfile(spec):
                continue
            yield spec, getattr(self, spec.name)


    def format_params(self, *, include_hidden: bool = False,
                      include_guarded_out: bool = False,
                      include_not_in_configfile: bool = False,
                      show_sections: bool = True) -> str:
        """
        Pretty-print current parameters, optionally filtered.
        Sections are sorted alphabetically.
        Params within each section retain original _SPECS order.
        """
        lines: List[str] = []
        last_section: Optional[str] = None

        params = list(self.iter_params(
            include_hidden=include_hidden,
            include_guarded_out=include_guarded_out,
            include_not_in_configfile=include_not_in_configfile,
        ))

        # Stable sort: section order changes, intra-section spec order is preserved
        params.sort(key=lambda item: item[0].section)

        for spec, v in params:
            if show_sections and spec.section != last_section:
                if lines:
                    lines.append("")
                lines.append(f"{spec.section}:")
                last_section = spec.section

            fmt = spec.fmt or "%s"
            try:
                rendered = fmt % v
            except Exception:
                rendered = str(v)
            lines.append(f"{spec.name} = {rendered}")

        return "\n" + "\n".join(lines) if lines else ""


    # ----------------------------
    # Public: single param set (for non-gcmd callers)
    # ----------------------------

    def set_param(self, name: str, value: Any, *, strict: bool = True) -> bool:
        """
        Set a single parameter with full validation/conversion.

        Returns True if value changed, False if unchanged.

        strict=True:
          - raises AttributeError if unknown
          - raises ValueError if guarded-out
        """
        spec = self._spec_by_name.get(name)
        if spec is None:
            if strict:
                raise AttributeError(f"Unknown parameter: {name}")
            return False
        if not self._is_available(spec):
            if strict:
                raise ValueError(f"Parameter not available for runtime change: {name}")
            return False

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
            return True
        return False
