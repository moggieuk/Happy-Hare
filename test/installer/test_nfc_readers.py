# Per-gate NFC reader Kconfig / template installer tests.
#
# The per-gate block has two consumers of the same data and they have to agree: the
# `nfc_readers` list on [mmu_unit] (one entry per gate, blanks allowed) and the
# [mmu_nfc_reader NAME] sections themselves. mmu_unit.py rejects a name it cannot find a
# section for, so a gate the user switched off has to disappear from BOTH or neither.
#
# Single file run:
#   make test UT='test_nfc_readers.py' JOBS=1

import unittest

from test.hh import cfg, profiles, session


PER_GATE = {
    "MMU_HAS_NFC_READER": True,
    "MMU_HAS_PER_GATE_NFC_READERS": True,
}

# A gate switched off at the toggle, with the reader name the user typed earlier still
# sitting in .mmu_config. See the NFC_PER_GATE_SPARSE profile comment.
STALE = dict(PER_GATE, **{
    "PARAM_NFC_READER_GATE_2": False,
    "PARAM_NFC_READER_2": "unit0_nfc2",
})


def hardware(label, syms, base="boxturtle_test"):
    return cfg.render(profiles.get(base).derive(label, syms=syms))[
        "config/base/mmu_hardware.cfg"]


def reader_names(parser, unit="unit0"):
    return [name.strip() for name in
            parser["mmu_unit %s" % unit].get("nfc_readers", "").split(",")]


def reader_sections(parser):
    return sorted(s[len("mmu_nfc_reader "):] for s in parser.sections()
                  if s.startswith("mmu_nfc_reader "))


class TestPerGateToggle(unittest.TestCase):
    """A deselected gate must lose its reader, however the name got there."""

    def test_switched_off_gate_renders_a_blank(self):
        # PARAM_NFC_READER_2 has a non-empty default, so the name outlives the toggle
        parser = cfg.assemble(cfg.render(
            profiles.get("boxturtle_test").derive("nfc_stale_list", syms=STALE)))
        self.assertEqual(reader_names(parser),
                         ["unit0_nfc0", "unit0_nfc1", "", "unit0_nfc3"])

    def test_switched_off_gate_gets_no_reader_section(self):
        # The other half: a section here would be an orphan claiming this gate's pins,
        # and a name in the list with no section is a config error at load
        parser = cfg.assemble(cfg.render(
            profiles.get("boxturtle_test").derive("nfc_stale_section", syms=STALE)))
        self.assertEqual(reader_sections(parser),
                         ["unit0_nfc0", "unit0_nfc1", "unit0_nfc3"])

    def test_list_keeps_one_entry_per_gate(self):
        # Blanks are placeholders, not omissions - mmu_unit.py indexes this list by
        # local gate and rejects any other length
        self.assertEqual(len(reader_names(cfg.assemble(cfg.render(
            profiles.get("boxturtle_test").derive("nfc_stale_len", syms=STALE))))), 4)

    def test_every_gate_switched_off_renders_an_all_blank_list(self):
        syms = dict(PER_GATE, **{"PARAM_NFC_READER_GATE_%d" % i: False
                                 for i in range(4)})
        parser = cfg.assemble(cfg.render(
            profiles.get("boxturtle_test").derive("nfc_all_off", syms=syms)))
        self.assertEqual(reader_names(parser), ["", "", "", ""])
        self.assertEqual(reader_sections(parser), [])

    def test_gates_default_to_having_a_reader(self):
        parser = cfg.assemble(cfg.render(
            profiles.get("boxturtle_test").derive("nfc_all_on", syms=PER_GATE)))
        self.assertEqual(reader_names(parser),
                         ["unit0_nfc0", "unit0_nfc1", "unit0_nfc2", "unit0_nfc3"])


class TestCustomBoardSetup(unittest.TestCase):
    """
    A board that supplies its own readers hides the whole "NFC reader h/w config" menu
    (installer/Kconfig.nfc_reader:92), which takes PARAM_NFC_READER_GATE_$(i) down with
    it. Reading the toggle without allowing for that would blank the board's list.
    """

    def test_vivid_still_renders_its_shared_reader_pairs(self):
        parser = cfg.assemble(cfg.render(profiles.get("ercf_vvd")))
        self.assertEqual(reader_names(parser, "unit1"),
                         ["unit1_nfc01", "unit1_nfc01", "unit1_nfc23", "unit1_nfc23"])


class TestSparseProfileLoads(unittest.TestCase):
    """The rendered sparse config has to survive klippy, not just look right."""

    def test_the_unit_accepts_a_blank_gate(self):
        hh = session("nfc_per_gate_sparse")
        try:
            hh.boot()
            self.assertEqual(hh.errors, [])
            unit = hh.mmu.mmu_unit()
            self.assertEqual(list(unit.nfc_readers),
                             ["unit0_nfc0", "unit0_nfc1", "", "unit0_nfc3"])
            # The gate that was switched off must end up with no reader OBJECT either
            self.assertIsNone(unit.nfc_manager.gate_readers[2])
        finally:
            hh.close()

    def test_rendered_tree_is_sane(self):
        cfg.assert_sane(cfg.render(profiles.get("nfc_per_gate_sparse")))


if __name__ == "__main__":
    unittest.main()
