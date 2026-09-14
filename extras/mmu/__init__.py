# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Package definition for the mmu directory
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# This file must exist. Without it `mmu` is a namespace package, and Klipper
# forks that rewrite imports into a `klippy` package (e.g. Kalico's
# klippy/compat.py KlippyPathFinder) cannot import one - their hook calls
# spec.loader.exec_module() and a namespace package spec has no loader, so
# plugin loading dies at startup with
# "'NoneType' object has no attribute 'exec_module'".
