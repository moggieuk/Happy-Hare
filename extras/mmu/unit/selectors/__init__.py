# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Package definition for the selectors directory
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#

from __future__ import annotations

import inspect, pkgutil, importlib, logging

from .mmu_base_selectors import BaseSelector

EXCLUDED_CLASSES = {
    "BaseSelector",
    "PhysicalSelector"
}

SELECTOR_REGISTRY = {}

for m in pkgutil.iter_modules(__path__):
    if m.ispkg or m.name.startswith("_"):
        continue

    mod = importlib.import_module(f".{m.name}", __name__)

    for name, obj in vars(mod).items():
        if (
            inspect.isclass(obj)
            and obj.__module__ == mod.__name__      # Defined in this module
            and issubclass(obj, BaseSelector)       # Must extend BaseSelector
            and name not in EXCLUDED_CLASSES        # Filter list
        ):
            SELECTOR_REGISTRY[name] = obj

logging.info("PAUL: ***** SELECTOR_REGISTRY=%s" % SELECTOR_REGISTRY)
