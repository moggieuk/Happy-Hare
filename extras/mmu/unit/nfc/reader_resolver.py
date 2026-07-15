# Resolves the NFC reader assigned to a Happy Hare MMU gate.
#
# Physical reader construction belongs to extras/mmu/unit/nfc.  This module
# only resolves the reader that mmu_hardware.cfg assigned to an MmuUnit gate,
# keeping that repository-specific lookup out of the NFC state machine.


def resolve_gate_reader(printer, mmu, gate_number, gate_name,
                        configured_gates):
    """Return (MmuRfidReader, MmuUnit, object_name) for one global MMU gate."""
    if mmu is None:
        raise printer.config_error(
            "nfc_gate [%s]: Happy Hare 'mmu' object is not available"
            % gate_name)

    unit = mmu.mmu_machine.get_mmu_unit_by_gate(gate_number)
    if unit is None:
        raise printer.config_error(
            "nfc_gate [%s]: mmu_gate %d does not belong to an MMU unit"
            % (gate_name, gate_number))

    local_gate = gate_number - unit.first_gate
    if unit.nfc_readers:
        reader_name = unit.nfc_readers[local_gate]
    elif unit.num_gates == 1:
        # A one-gate unit's singular reader is unambiguously per-lane.
        reader_name = unit.nfc_reader
    else:
        raise printer.config_error(
            "nfc_gate [%s]: per-lane gate %d requires an entry in "
            "'nfc_readers' on MMU unit [%s]; its singular 'nfc_reader' "
            "cannot be assigned to one lane of a %d-gate unit"
            % (gate_name, gate_number, unit.name, unit.num_gates))

    if not reader_name:
        raise printer.config_error(
            "nfc_gate [%s]: MMU unit [%s] has no NFC reader configured "
            "for gate %d" % (gate_name, unit.name, gate_number))

    reader_object = printer.lookup_object(reader_name, None)
    if reader_object is None:
        raise printer.config_error(
            "nfc_gate [%s]: configured reader object [%s] was not loaded"
            % (gate_name, reader_name))

    for gate in configured_gates:
        if (getattr(gate, '_name', None) != gate_name
                and not getattr(gate, '_shared', False)
                and getattr(gate, '_reader_object', None) is reader_object):
            raise printer.config_error(
                "nfc_gate [%s]: reader [%s] is already assigned to "
                "per-lane nfc_gate [%s]"
                % (gate_name, reader_name, gate._name))

    return reader_object, unit, reader_name
