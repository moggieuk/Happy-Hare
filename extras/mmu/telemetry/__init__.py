# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Organize Klipper-side motion telemetry and replay capture.
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
"""Klipper-side telemetry modules.

movement: source events and filament movement operations.
selector: selector stepper and servo recording hooks.
gcode: G1 extrusion wrapper and MMU command scopes.
capture: replay state, configuration deltas and bounded history.
stream: acknowledged delivery, independent of transport.
api: Klipper webhook adapter for telemetry subscribers.

Import modules directly; this package does not initialize capture or transport.
Moonraker integration lives separately under components/.
"""
