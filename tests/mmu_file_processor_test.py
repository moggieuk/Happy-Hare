import unittest
import os
import shutil

from extras.mmu_file_processor import MmuFileProcessor

class TestMmuFileProcessor(unittest.TestCase):
    TOOLCHANGE_FILEPATH = 'tests/support/toolchange.gcode'
    NO_TOOLCHANGE_FILEPATH = 'tests/support/no_toolchange.gcode'

    def setUp(self):
        shutil.copyfile('tests/support/toolchange.orig.gcode', self.TOOLCHANGE_FILEPATH)
        shutil.copyfile('tests/support/no_toolchange.orig.gcode', self.NO_TOOLCHANGE_FILEPATH)

    def tearDown(self):
        os.remove(self.TOOLCHANGE_FILEPATH)
        os.remove(self.NO_TOOLCHANGE_FILEPATH)

    def test_write_mmu_metadata_when_writing_to_files(self):
        MmuFileProcessor().write_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        with open(self.TOOLCHANGE_FILEPATH, 'r') as f:
            file_contents = f.read()
            self.assertIn('[mmu_file_stats]', file_contents)
            self.assertIn('MMU_TOOLS_USED=0,1,2,5', file_contents)
            self.assertIn('MMU_TOOL_USE_FREQUENCY=[[0,2],[1,3],[2,1],[5,1]]', file_contents)

    def test_write_mmu_metadata_when_no_toolchanges(self):
        MmuFileProcessor().write_mmu_metadata(self.NO_TOOLCHANGE_FILEPATH)

        with open(self.NO_TOOLCHANGE_FILEPATH, 'r') as f:
            file_contents = f.read()
            self.assertIn('[mmu_file_stats]', file_contents)
            self.assertIn('MMU_TOOLS_USED=', file_contents)
            self.assertIn('MMU_TOOL_USE_FREQUENCY=[]', file_contents)

    def test_read_mmu_metadata_when_metadata_is_present(self):
        MmuFileProcessor().write_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        metadata = MmuFileProcessor().read_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        self.assertEqual(metadata['tools_used'], [0, 1, 2, 5])
        self.assertEqual(metadata['tool_use_frequency'], {'0': 2, '1': 3, '2': 1, '5': 1})
    
    def test_read_mmu_metadata_when_no_toolchanges(self):
        MmuFileProcessor().write_mmu_metadata(self.NO_TOOLCHANGE_FILEPATH)

        metadata = MmuFileProcessor().read_mmu_metadata(self.NO_TOOLCHANGE_FILEPATH)

        self.assertEqual(metadata['tools_used'], [])
        self.assertEqual(metadata['tool_use_frequency'], {})

    def test_read_mmu_metadata_when_metadata_is_not_present(self):
        metadata = MmuFileProcessor().read_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        self.assertEqual(metadata['tools_used'], [0, 1, 2, 5])
        self.assertEqual(metadata['tool_use_frequency'], {'0': 2, '1': 3, '2': 1, '5': 1})

    def test_read_mmu_metadata_when_metadata_is_not_present_and_should_not_be_written(self):
        metadata = MmuFileProcessor().read_mmu_metadata(self.TOOLCHANGE_FILEPATH, write_if_missing=False)

        self.assertEqual(metadata, {})
