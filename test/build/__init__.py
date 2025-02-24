# import os
# import os.path
# import shutil
# import unittest
#
# from scripts.build import build
# # from test.build import TestBuild
#
#
# def find_test_cases(path):
#     required_test_files = [".config", "in.cfg", "expected.cfg"]
#     optional_test_files = ["config.cfg", "mmu.cfg", "hardware.cfg", "parameters.cfg"]
#     test_cases = []
#     for d in os.listdir(path):
#         if os.path.isdir(f"{path}/{d}"):
#             if all(os.path.exists(f"{path}/{d}/{file}") for file in required_test_files) and any(
#                 os.path.exists(f"{path}/{d}/{file}") for file in optional_test_files
#             ):
#                 cfg = next(file for file in optional_test_files if os.path.exists(f"{path}/{d}/{file}"))
#                 test_cases.append(TestBuild(f"{path}/{d}", cfg))
#             else:
#                 test_cases.extend(find_test_cases(f"{path}/{d}"))
#     return test_cases
#
#
# def load_tests(loader, tests, pattern):
#     test_cases = find_test_cases("test/build")
#     suite = unittest.TestSuite()
#     suite.addTests(test_cases)
#     return suite
