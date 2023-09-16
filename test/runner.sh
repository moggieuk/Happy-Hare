#!/bin/bash
# Happy Hare MMU Software
# Test runner
#
# Copyright (C) 2023  Kieran Eglin <@kierantheman (discord)>, <kieran.eglin@gmail.com>
#
# NOTE: in order for tests to get picked up automatically, you must do the following:
#   1. Create a file in the test directory with the name test_*.py
#   2. Create a class in that file that inherits from unittest.TestCase
#   3. Ensure that each test directory has a blank file named `__init__.py`

python3 -m unittest
