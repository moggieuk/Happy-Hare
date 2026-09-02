# Happy Hare - regression test for install.sh's KCONFIG_CONFIG path anchoring.
#
# install.sh runs from the caller's cwd, but every make invocation it issues
# goes through run_make (`make -C $SCRIPT_DIR`), so kconfiglib - which uses
# KCONFIG_CONFIG as-is against its own process cwd - reads and writes the file
# that lives next to the checkout, not next to the caller. Running e.g.
# ../Happy-Hare/install.sh -i from the parent directory used to abort with
# "Config '.mmu_config' has not been saved, exiting" even though menuconfig had
# saved, because install.sh's [ -e ]/source checks looked in the caller's
# directory instead of the checkout's.
#
# This test sources the real install.sh (its INSTALL_SH_SOURCE_ONLY hook is
# designed for exactly this) with the cwd outside the repo, and asserts the
# resolver anchors relative paths to the checkout.
#
# Requires no dependencies beyond /bin/sh - run with the repo venv:
#   ./venv/bin/python -m unittest test.test_mmu_install_sh
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestKconfigConfigAnchoring(unittest.TestCase):
    def _resolve(self, env_kconfig, cwd):
        env = {
            'PATH': os.environ['PATH'],
            'INSTALL_SH_SCRIPT_DIR': REPO_ROOT,
            'INSTALL_SH_SOURCE_ONLY': 'y',
        }
        if env_kconfig is not None:
            env['KCONFIG_CONFIG'] = env_kconfig
        # Note the printf format deliberately ends at the closing quote: a shell
        # "%s\n" is fine, but the Python string would carry a literal newline
        # that terminates the format early and swallows the value.
        script = (
            '. "%s/install.sh"; '
            'resolve_kconfig_config; '
            'printf "%%s\\n" "$KCONFIG_CONFIG"' % REPO_ROOT
        )
        result = subprocess.run(
            ['sh', '-c', script], env=env, cwd=cwd,
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         'sh -c failed: %s' % result.stderr)
        return result.stdout.strip()

    def test_unset_defaults_to_checkout_dot_mmu_config(self):
        with tempfile.TemporaryDirectory() as outside:
            self.assertEqual(
                self._resolve(None, outside),
                REPO_ROOT + '/.mmu_config')

    def test_relative_env_var_anchors_to_checkout_not_cwd(self):
        with tempfile.TemporaryDirectory() as outside:
            self.assertEqual(
                self._resolve('custom.cfg', outside),
                REPO_ROOT + '/custom.cfg')

    def test_absolute_path_is_left_alone(self):
        with tempfile.TemporaryDirectory() as outside:
            absolute = outside + '/somewhere/mmu.cfg'
            self.assertEqual(
                self._resolve(absolute, outside),
                absolute)
