# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Goal: Per-gate NFC/RFID reader exposed as a "software" homing endstop that
#       "triggers" when a tag is detected. Lets a gear/filament homing move stop
#       as soon as the spool's RFID tag reaches the reader.
#
# PROTOTYPE - host-polled trigger during a drip-homing move (see MmuNfcManager
# poll orchestration). Confirmed viable on RC522 (SPI); PN532/PN7160 (I2C) need
# a bench check of per-poll blocking time against the drip budget (~50ms safe).
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging

from ...mmu_sensor_utils import MmuVirtualEndstopSensor


class MmuNfcEndstop(MmuVirtualEndstopSensor):
    """
    A per-gate NFC reader presented as a Klipper endstop. It reuses the host
    software-endstop machinery of MmuVirtualEndstopSensor (home_start arms a
    reactor completion; trigger_handler completes it). The trigger is driven by
    the manager's homing poll loop calling trigger_handler(print_time, True) when
    the reader reports a tag - so 'home_start' starts that poll and 'home_wait'
    stops it.
    """

    def __init__(self, config, gate, reader, poll_controller, register=False):
        # name becomes "<SENSOR_NFC_PREFIX>_<gate>" via the MmuSensor base
        from ...mmu_constants import SENSOR_NFC_PREFIX
        super().__init__(config, SENSOR_NFC_PREFIX, gate, register=register)
        self.gate = gate
        self.reader = reader
        self._poll_controller = poll_controller


    def _endstop_trigger_time(self, eventtime):
        # The manager's poll drives trigger_handler from a host reactor timer, so
        # convert to MCU print_time for the homing trigger position calc.
        # (estimated_print_time is inherited from MmuVirtualEndstopSensor.)
        return self.estimated_print_time(eventtime)


    # Endstop homing interface -----------------------------------------------------

    def home_start(self, print_time, sample_time, sample_count, rest_time, triggered):
        # Reset detection state so a previous read can't pre-trigger this home,
        # then start the manager's tight poll of this gate's reader.
        self.runout_helper.note_filament_present(print_time, False)
        self._poll_controller.start_homing_poll(self)
        return super().home_start(print_time, sample_time, sample_count, rest_time, triggered)


    def home_wait(self, home_end_time):
        self._poll_controller.stop_homing_poll()
        return super().home_wait(home_end_time)
