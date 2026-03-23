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

# Happy Hare imports
from .mmu_base_command import BaseCommand


EXCLUDED_CLASSES = {
    "BaseCommand",
}

COMMAND_REGISTRY = {}

for m in pkgutil.iter_modules(__path__):
    if m.ispkg or m.name.startswith("_"):
        continue

    mod = importlib.import_module(f".{m.name}", __name__)

    for name, cls in vars(mod).items():
        if (
            inspect.isclass(cls)
            and cls.__module__ == mod.__name__   # Defined in this module
            and issubclass(cls, BaseCommand)     # Must extend BaseCommand
            and name not in EXCLUDED_CLASSES     # Filter list
        ):
            COMMAND_REGISTRY[name] = cls


def register_command(cls):
    """
    Decorator/function to register a BaseCommand subclass. This is for
    commands that are defined outside of this module
    Usage:
        @register_command
        class MyCmd(BaseCommand): ...
    """

    if (
        not inspect.isclass(cls) or
        not issubclass(cls, BaseCommand)
    ):
        raise TypeError("register_command expects a BaseCommand subclass")

    key = cls.__name__

    if key in COMMAND_REGISTRY:
        raise KeyError(f"Command '{key}' is already registered")

    COMMAND_REGISTRY[key] = cls
    return cls
