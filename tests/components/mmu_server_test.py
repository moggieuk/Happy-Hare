import os
import shutil
import unittest
from unittest.mock import MagicMock

from components.mmu_server import MmuServer

class TestMmuServerFileProcessor(unittest.TestCase):
    TOOLCHANGE_FILEPATH = 'tests/support/toolchange.gcode'
    NO_TOOLCHANGE_FILEPATH = 'tests/support/no_toolchange.gcode'

    def setUp(self):
        self.subject = MmuServer(MagicMock())
        shutil.copyfile('tests/support/toolchange.orig.gcode', self.TOOLCHANGE_FILEPATH)
        shutil.copyfile('tests/support/no_toolchange.orig.gcode', self.NO_TOOLCHANGE_FILEPATH)

    def tearDown(self):
        os.remove(self.TOOLCHANGE_FILEPATH)
        os.remove(self.NO_TOOLCHANGE_FILEPATH)
        
    def test_filelist_callback_when_enabled(self):
        self.subject.enable_file_preprocessor = True
        self.subject._write_mmu_metadata = MagicMock()

        self.subject._filelist_changed({'action': 'create_file', 'item': {'path': 'test.gcode'}})

        self.subject._write_mmu_metadata.assert_called_once()

    def test_filelist_callback_when_disabled(self):
        self.subject.enable_file_preprocessor = False
        self.subject._write_mmu_metadata = MagicMock()

        self.subject._filelist_changed({'action': 'create_file', 'item': {'path': 'test.gcode'}})

        self.subject._write_mmu_metadata.assert_not_called()

    def test_filelist_callback_when_wrong_event(self):
        self.subject.enable_file_preprocessor = False
        self.subject._write_mmu_metadata = MagicMock()

        self.subject._filelist_changed({'action': 'move_file', 'item': {'path': 'test.gcode'}})

        self.subject._write_mmu_metadata.assert_not_called()

    def test_filelist_callback_when_wrong_file_type(self):
        self.subject.enable_file_preprocessor = False
        self.subject._write_mmu_metadata = MagicMock()

        self.subject._filelist_changed({'action': 'create_file', 'item': {'path': 'test.txt'}})

        self.subject._write_mmu_metadata.assert_not_called()

    def test_write_mmu_metadata_when_writing_to_files(self):
        self.subject._write_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        with open(self.TOOLCHANGE_FILEPATH, 'r') as f:
            file_contents = f.read()
            self.assertIn('PRINT_START MMU_TOOLS_USED=0,1,2,5,11\n', file_contents)
            self.assertNotIn('[mmu_inject_tools_used]', file_contents)

    def test_write_mmu_metadata_when_no_toolchanges(self):
        self.subject._write_mmu_metadata(self.NO_TOOLCHANGE_FILEPATH)

        with open(self.NO_TOOLCHANGE_FILEPATH, 'r') as f:
            file_contents = f.read()
            self.assertIn('PRINT_START MMU_TOOLS_USED=\n', file_contents)
            self.assertNotIn('[mmu_inject_tools_used]', file_contents)

