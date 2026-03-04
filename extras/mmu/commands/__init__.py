# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Package definition for the commands directory
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#

import inspect, pkgutil, importlib, logging

from .mmu_base_command import BaseCommand

EXCLUDED_CLASSES = {
    "BaseCommand",
}

COMMAND_REGISTRY = {}

for m in pkgutil.iter_modules(__path__):
    if m.ispkg or m.name.startswith("_"):
        continue

    mod = importlib.import_module(f".{m.name}", __name__)

    for name, obj in vars(mod).items():
        if (
            inspect.isclass(obj)
            and obj.__module__ == mod.__name__   # Defined in this module
            and issubclass(obj, BaseCommand)     # Must extend BaseCommand
            and name not in EXCLUDED_CLASSES     # Filter list
        ):
            COMMAND_REGISTRY[name] = obj
