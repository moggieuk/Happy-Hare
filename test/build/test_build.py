import os
import os.path
import shutil
import unittest

import scripts.build
from scripts.build import Upgrades, ConfigBuilder, ConfigInput, HHConfig, Kconfig, build_mmu_hardware_cfg, build_mmu_cfg
import scripts.parser as parser


class TestBuild(unittest.TestCase):
    def __init__(self, methodName):
        super().__init__(methodName)
        self.maxDiff = None
        self.base_path = os.path.dirname(os.path.realpath(__file__))

    def assertExpected(self, path, result):
        with open(f"{self.base_path}/{path}/expected.cfg", "r") as e:
            self.assertEqual(e.read(), result)

    def cfg_input_and_builder(self, path):
        return (
            ConfigInput(HHConfig([f"{self.base_path}/{path}/in.cfg"]), Kconfig(f"{self.base_path}/{path}/.config")),
            ConfigBuilder(f"{self.base_path}/{path}/config.cfg"),
        )

    def base_test(self, path, callback=None, from_version=None, to_version=None):
        (cfg_input, builder) = self.cfg_input_and_builder(path)
        if from_version and to_version:
            upgrades = Upgrades()
            upgrades.upgrade(cfg_input, from_version, to_version)

        if callback:
            callback(builder, cfg_input)
        cfg_input.update_builder(builder)
        result = builder.write()
        self.assertExpected(path, result)

    def test_upgrade_2_71(self):
        """test upgrade from 2.70 to 2.71"""
        self.base_test("2_71/1", from_version="2.70", to_version="2.71")
        self.base_test("2_71/2", from_version="2.70", to_version="2.71")

    def test_upgrade_2_72(self):
        """test upgrade from 2.71 to 2.72"""
        self.base_test("2_72", from_version="2.71", to_version="2.72")

    def test_upgrade_2_73(self):
        """test upgrade from 2.72 to 2.73"""
        self.base_test("2_73", from_version="2.72", to_version="2.73")

    def test_upgrade_3_00(self):
        """test upgrade from 2.73 to 3.00"""
        self.base_test("3_00", from_version="2.73", to_version="3.00")

    def test_hardware(self):
        """test whether mmu_hardware.cfg is correctly built"""
        self.base_test("hardware", build_mmu_hardware_cfg)

    def test_mmu(self):
        """test whether mmu.cfg is correctly built"""
        self.base_test("mmu", build_mmu_cfg)

    def base_test_moonraker(self, path):
        shutil.copy(f"{self.base_path}/{path}/in.cfg", f"{self.base_path}/{path}/out.cfg")
        scripts.build.install_moonraker(
            "moonraker_update.txt", f"{self.base_path}/{path}/out.cfg", f"{self.base_path}/{path}/.config"
        )

        with open(f"{self.base_path}/{path}/out.cfg", "r") as f:
            result = f.read()
        os.remove(f"{self.base_path}/{path}/out.cfg")
        self.assertExpected(path, result)

    def test_moonraker(self):
        self.base_test_moonraker("moonraker/1")
        self.base_test_moonraker("moonraker/2")

    def test_parser(self):
        p = parser.Parser()
        self.assertEqual(
            p.parse_comment(parser.Tokenizer(" #comment")),
            {"type": "comment", "body": [{"type": "comment_entry", "value": " #comment"}]},
        )
        self.assertEqual(
            p.parse_section(parser.Tokenizer("[section]")),
            {"type": "section", "name": "section", "body": []},
        )
        self.assertEqual(
            p.parse_section(parser.Tokenizer("[section name]# with comment")),
            {
                "type": "section",
                "name": "section name",
                "body": [
                    {"type": "comment", "body": [{"type": "comment_entry", "value": "# with comment"}]},
                ],
            },
        )
        self.assertEqual(
            p.parse_value(parser.Tokenizer(" line1\n  line2")),
            {
                "type": "value",
                "body": [
                    {
                        "type": "value_line",
                        "body": [{"type": "whitespace", "value": " "}, {"type": "value_entry", "value": "line1\n"}],
                    },
                    {
                        "type": "value_line",
                        "body": [{"type": "value_entry", "value": "  line2"}],
                    },
                ],
            },
        )
        # self.assertEqual(
        #     p.parse_comment(parser.Tokenizer("# comment {placeholder} rest of comment")),
        #     {
        #         "type": "comment",
        #         "body": [
        #             {"type": "comment_entry", "value": "# comment "},
        #             {"type": "placeholder", "value": "placeholder"},
        #             {"type": "comment_entry", "value": " rest of comment"},
        #         ],
        #     },
        # )

    def test_parser_config_files(self):
        """test whether the parser output is the same as the input"""

        def test_file(file):
            b = parser.ConfigBuilder(file)
            with open(file, "r") as f:
                self.assertEqual(f.read(), b.parser.serialize(b.document))

        test_file("config/base/mmu.cfg")
        test_file("config/base/mmu_hardware.cfg")
        test_file("config/base/mmu_parameters.cfg")
        test_file("config/base/mmu_macro_vars.cfg")
