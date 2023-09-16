import os
import shutil
import unittest
from unittest.mock import MagicMock

from components.mmu_server import MmuServer

class TestMmuServerFileProcessor(unittest.TestCase):
    TOOLCHANGE_FILEPATH = 'test/support/toolchange.gcode'
    NO_TOOLCHANGE_FILEPATH = 'test/support/no_toolchange.gcode'

    def setUp(self):
        self.subject = MmuServer(MagicMock())
        shutil.copyfile('test/support/toolchange.orig.gcode', self.TOOLCHANGE_FILEPATH)
        shutil.copyfile('test/support/no_toolchange.orig.gcode', self.NO_TOOLCHANGE_FILEPATH)

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
            self.assertIn('PRINT_START MMU_TOOLS_USED=0,1,3,4,5,12\n', file_contents)

    def test_write_mmu_metadata_when_no_toolchanges(self):
        self.subject._write_mmu_metadata(self.NO_TOOLCHANGE_FILEPATH)

        with open(self.NO_TOOLCHANGE_FILEPATH, 'r') as f:
            file_contents = f.read()
            self.assertIn('PRINT_START MMU_TOOLS_USED=\n', file_contents)

    def test_write_mmu_metadata_does_not_replace_comments(self):
        self.subject._write_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        with open(self.TOOLCHANGE_FILEPATH, 'r') as f:
            file_contents = f.read()
            self.assertIn('; start_gcode: PRINT_START MMU_TOOLS_USED=!mmu_inject_referenced_tools!', file_contents)

    def test_inject_tool_usage_called_if_placeholder(self):
        self.subject._inject_tool_usage = MagicMock()

        self.subject._write_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        self.subject._inject_tool_usage.assert_called()

    def test_inject_tool_usage_not_called_if_no_placeholder(self):
        # Call it once to remove the placeholder
        self.subject._write_mmu_metadata(self.TOOLCHANGE_FILEPATH)
        self.subject._inject_tool_usage = MagicMock()

        self.subject._write_mmu_metadata(self.TOOLCHANGE_FILEPATH)

        self.subject._inject_tool_usage.assert_not_called()
