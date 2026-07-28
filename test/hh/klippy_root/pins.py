# Fake Klipper `klippy/pins.py` for the Happy Hare test harness.
#
# The point of this layer is the BINDING REGISTRY: every setup_pin() records which
# type each pin description was bound as (digital_out / pwm / adc / endstop /
# counter / stepper), because that is how a test proves e.g. that
# assist_motor_pin_0 really became a pwm and not a digital_out - HH branches on
# config.getboolean("pwm", True) at extras/mmu/unit/mmu_espooler.py:84-101.
#
# Pin types HH requests: 'endstop' (extras/mmu_stepper.py:226,311), 'pwm'
# (extras/mmu_servo.py:49, espooler), 'adc' (hall sensors at
# extras/mmu/unit/mmu_toolhead_wrapper.py:215,228; proportional buffer at
# extras/mmu/unit/mmu_buffer.py:214; led_effect analog trigger), 'digital_out'
# (espooler, pn7160 ven_pin at extras/mmu/unit/nfc/pn7160_driver.py:201).
#
# Multi-use pins are REAL, not an edge case: extras/mmu/mmu_unit.py:503-505 shares a
# switch-sensor endstop pin, and extras/mmu/unit/mmu_toolhead_wrapper.py:214,227
# reuses the hall ADC pins alongside a standard hall_filament_width_sensor. So
# by_desc maps to a LIST.
#
# get_pin_resolver(chip).aliases is required, not optional: both _is_empty_pin
# helpers (extras/mmu/mmu_sensor_utils.py:294-302 and
# extras/mmu/unit/mmu_espooler.py:163-167) use
# `aliases.get(pin, '_real_') == ''` as the "deliberately unconfigured" signal, so
# injecting an empty alias is how a test exercises the sensor-omitted branch.
#
# Chip registration is STRICT - an unknown chip raises rather than auto-vivifying,
# so a pin typo in a shipped template cannot hide.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import re

import mcu as mcu_mod


class error(Exception):
    pass


class PinBinding:
    __slots__ = ('desc', 'chip', 'pin', 'type', 'invert', 'pullup', 'obj')

    def __init__(self, desc, chip, pin, type_, invert, pullup, obj):
        self.desc = desc
        self.chip = chip
        self.pin = pin
        self.type = type_
        self.invert = invert
        self.pullup = pullup
        self.obj = obj

    def __repr__(self):
        return 'PinBinding(%r, type=%r)' % (self.desc, self.type)


class PinResolver:
    def __init__(self, chip_name):
        self.chip_name = chip_name
        self.aliases = {}
        self.active_pins = {}

    def alias_pin(self, alias, pin):
        self.aliases[alias] = pin

    def update_command(self, cmd):
        return cmd


class _Chip:
    """
    A pin chip. Real Klipper has one per MCU; TMC drivers also register a
    `tmc<chip>_<stepper>` chip exposing 'virtual_endstop', which HH's rendered
    config uses (extra_endstops: mmu_gear_touch=tmc2209_unit0_gear:virtual_endstop).
    """

    def __init__(self, name, ppins, mcu=None):
        self.name = name
        self.ppins = ppins
        self.mcu = mcu

    def setup_pin(self, pin_type, pin_params):
        return self.ppins._make_pin(self, pin_type, pin_params)


class _VirtualEndstopChip:
    """Chip whose only pin is 'virtual_endstop' (TMC stallguard / touch homing)."""

    def __init__(self, name, ppins, mcu=None):
        self.name = name
        self.ppins = ppins
        self.mcu = mcu

    def setup_pin(self, pin_type, pin_params):
        if pin_params['pin'] != 'virtual_endstop':
            raise error("Chip '%s' only supports 'virtual_endstop', got %r"
                        % (self.name, pin_params['pin']))
        if pin_type != 'endstop':
            raise error("Chip '%s' virtual_endstop must be an endstop, got %r"
                        % (self.name, pin_type))
        return self.ppins._make_pin(self, pin_type, pin_params)


_PIN_RE = re.compile(r'^(?P<invert>!?)(?P<pullup>\^?~?)'
                     r'(?:(?P<chip>[A-Za-z_][A-Za-z0-9_]*):)?(?P<pin>.+)$')


class PrinterPins:
    error = error

    def __init__(self, printer):
        self.printer = printer
        self.chips = {}
        self.pin_resolvers = {}
        self.active_pins = {}
        self.allow_multi_use = set()
        # -- assertion surfaces -------------------------------------------
        self.bindings = []          # every PinBinding, in creation order
        self.by_desc = {}           # desc -> [PinBinding, ...]
        # Test knobs for the ADC compat matrix (see mcu.MCU_adc)
        self.adc_api = 'new'
        self.adc_payload = 'samples'

    # -- chips --------------------------------------------------------------
    def register_chip(self, chip_name, chip):
        if chip_name in self.chips:
            raise error("Duplicate chip name '%s'" % (chip_name,))
        self.chips[chip_name] = chip
        self.pin_resolvers[chip_name] = PinResolver(chip_name)

    def register_mcu_chip(self, chip_name, mcu=None):
        self.register_chip(chip_name, _Chip(chip_name, self, mcu))

    def register_virtual_endstop_chip(self, chip_name, mcu=None):
        self.register_chip(chip_name, _VirtualEndstopChip(chip_name, self, mcu))

    def get_pin_resolver(self, chip_name):
        if chip_name not in self.pin_resolvers:
            raise error("Unknown chip name '%s'" % (chip_name,))
        return self.pin_resolvers[chip_name]

    # -- parsing ------------------------------------------------------------
    def parse_pin(self, pin_desc, can_invert=False, can_pullup=False):
        desc = pin_desc.strip()
        m = _PIN_RE.match(desc)
        if not m:
            raise error("Invalid pin description '%s'" % (pin_desc,))
        invert = bool(m.group('invert'))
        pullup_str = m.group('pullup') or ''
        chip_name = m.group('chip') or 'mcu'
        pin = m.group('pin').strip()
        if invert and not can_invert:
            raise error("Can not invert pin '%s' in this context" % (pin_desc,))
        if pullup_str and not can_pullup:
            raise error("Can not pullup/pulldown pin '%s' in this context"
                        % (pin_desc,))
        pullup = 0
        if '^' in pullup_str:
            pullup = 1
        if '~' in pullup_str:
            pullup = -1
        if chip_name not in self.chips:
            raise error("Unknown pin chip name '%s' (pin '%s'). Known chips: %s"
                        % (chip_name, pin_desc, ', '.join(sorted(self.chips))))
        return {'chip': self.chips[chip_name], 'chip_name': chip_name,
                'pin': pin, 'invert': int(invert), 'pullup': pullup,
                'share_name': None}

    def lookup_pin(self, pin_desc, can_invert=False, can_pullup=False,
                   share_type=None):
        return self.parse_pin(pin_desc, can_invert, can_pullup)

    def setup_pin(self, pin_type, pin_desc):
        can_invert = pin_type in ('stepper', 'endstop', 'digital_out', 'pwm')
        can_pullup = pin_type in ('endstop', 'adc')
        pin_params = self.parse_pin(pin_desc, can_invert, can_pullup)
        pin_params['desc'] = pin_desc.strip()
        pin_params['type'] = pin_type
        return pin_params['chip'].setup_pin(pin_type, pin_params)

    def allow_multi_use_pin(self, share_name):
        self.allow_multi_use.add(share_name)

    # -- object construction ------------------------------------------------
    def _make_pin(self, chip, pin_type, pin_params):
        reactor = self.printer.get_reactor()
        mcu = chip.mcu
        if pin_type == 'digital_out':
            obj = mcu_mod.MCU_digital_out(mcu, pin_params)
        elif pin_type == 'pwm':
            obj = mcu_mod.MCU_pwm(mcu, pin_params)
        elif pin_type == 'adc':
            obj = mcu_mod.MCU_adc(mcu, pin_params, api=self.adc_api,
                                  payload=self.adc_payload)
        elif pin_type == 'endstop':
            obj = mcu_mod.MCU_endstop(mcu, pin_params, reactor=reactor,
                                      printer=self.printer)
        elif pin_type in ('counter', 'stepper'):
            obj = mcu_mod._PinBase(mcu, pin_params)
        else:
            raise error("pin type %r is not supported by the harness (pin %r). "
                        "Add it to pins.PrinterPins._make_pin if HH now needs it."
                        % (pin_type, pin_params.get('desc')))
        binding = PinBinding(pin_params.get('desc'), pin_params['chip_name'],
                            pin_params['pin'], pin_type, bool(pin_params['invert']),
                            pin_params['pullup'], obj)
        self.bindings.append(binding)
        self.by_desc.setdefault(binding.desc, []).append(binding)
        return obj

    # -- test-facing lookups ------------------------------------------------
    def of_type(self, pin_type):
        return [b for b in self.bindings if b.type == pin_type]

    def find(self, desc):
        return list(self.by_desc.get(desc.strip(), ()))

    def assert_bound(self, desc, pin_type):
        found = [b.type for b in self.find(desc)]
        if pin_type not in found:
            raise AssertionError(
                "pin %r was not bound as %r (bound as: %s)"
                % (desc, pin_type, ', '.join(found) or 'nothing'))
        return True

    def types_by_pin(self):
        """{desc: [type, ...]} - the 'what does each pin act as' summary."""
        return {d: [b.type for b in bs] for d, bs in self.by_desc.items()}


def add_printer_objects(config):
    printer = config.get_printer()
    printer.add_object('pins', PrinterPins(printer))
