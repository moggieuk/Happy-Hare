"""Integration tests for install.sh recovery and v3 migration paths."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
SELF_UPDATE_SH = REPO_ROOT / "installer" / "self_update.sh"
MAKEFILE = REPO_ROOT / "Makefile"


class TestInstallSh(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, path, text=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_shell(self, body, *, stdin="", env=None, argv0="install-sh-test"):
        shell_env = os.environ.copy()
        shell_env.update({
            "INSTALL_SH_SOURCE_ONLY": "y",
            "INSTALL_SH_SCRIPT_DIR": str(REPO_ROOT),
        })
        if env:
            shell_env.update({key: str(value) for key, value in env.items()})

        script = "\n".join((
            ". {}".format(shlex.quote(str(INSTALL_SH))),
            "C_OFF= C_INFO= C_NOTICE= C_WARNING= C_ERROR=",
            body,
        ))
        result = subprocess.run(
            ["/bin/sh", "-c", script, str(argv0)],
            cwd=REPO_ROOT,
            env=shell_env,
            input=stdin,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            self.fail(
                "install.sh subprocess failed with status {}\nstdout:\n{}\nstderr:\n{}"
                .format(result.returncode, result.stdout, result.stderr)
            )
        return result

    def _isolated_git_env(self):
        """Return a predictable, non-interactive environment for test git."""
        # Rebuild for every call so variables set during a test are stripped.
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("GIT_") or key in ("GNUPGHOME",
                                                 "GPG_AGENT_INFO",
                                                 "HOME",
                                                 "XDG_CONFIG_HOME"):
                del env[key]
        env.update({
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "GNUPGHOME": str(self.root / "gnupg"),
            "GIT_AUTHOR_NAME": "Installer Test",
            "GIT_AUTHOR_EMAIL": "installer@example.invalid",
            "GIT_COMMITTER_NAME": "Installer Test",
            "GIT_COMMITTER_EMAIL": "installer@example.invalid",
        })
        return env

    def _git(self, *args, cwd=None, check=True):
        result = subprocess.run(
            ["git"] + list(args), cwd=cwd,
            env=self._isolated_git_env(),
            text=True, capture_output=True)
        if check and result.returncode:
            self.fail("git {} failed (status {})\nstdout: {}\nstderr: {}".format(
                " ".join(args), result.returncode,
                result.stdout, result.stderr))
        return result

    def make_mmu_config(self, directory, *extra_names):
        self.write(directory / ".mmu_config", "CONFIG_SENTINEL=y\n")
        for name in extra_names:
            self.write(directory / name, name + "\n")

    def test_last_recovers_current_kconfig_without_moving_current(self):
        config_home = self.root / "printer_data" / "config"
        current = config_home / "mmu"
        repo_dest = self.root / "happy-hare"
        repo_dest.mkdir()
        self.make_mmu_config(
            current,
            ".mmu_config_unit0",
            ".mmu_config.old",
            "current-marker",
        )

        self.run_shell("""
            SCRIPT_DIR={repo}
            CONFIG_KLIPPER_CONFIG_HOME={config}
            TESTDIR=
            recover_last_config
            [ -z "${{F_NO_MMU_BACKUP:-}}" ]
        """.format(
            repo=shlex.quote(str(repo_dest)),
            config=shlex.quote(str(config_home)),
        ))

        self.assertTrue((current / "current-marker").exists())
        self.assertTrue((repo_dest / ".mmu_config").exists())
        self.assertTrue((repo_dest / ".mmu_config_unit0").exists())
        self.assertFalse((repo_dest / ".mmu_config.old").exists())
        self.assertEqual(list(config_home.glob("mmu.old-*")), [])

    def test_last_without_current_restores_newest_backup(self):
        config_home = self.root / "printer_data" / "config"
        older = config_home / "mmu.old-20260101-010203"
        newer = config_home / "mmu.old-20260830-120000"
        repo_dest = self.root / "happy-hare"
        repo_dest.mkdir()
        self.make_mmu_config(older, "older-marker")
        self.make_mmu_config(newer, ".mmu_config_unit1", "newer-marker")

        self.run_shell("""
            SCRIPT_DIR={repo}
            CONFIG_KLIPPER_CONFIG_HOME={config}
            TESTDIR=
            recover_last_config
        """.format(
            repo=shlex.quote(str(repo_dest)),
            config=shlex.quote(str(config_home)),
        ))

        current = config_home / "mmu"
        self.assertTrue((current / "newer-marker").exists())
        self.assertFalse((current / "older-marker").exists())
        self.assertTrue((repo_dest / ".mmu_config_unit1").exists())

    def test_prev_lists_newest_first_and_preserves_current_before_restore(self):
        config_home = self.root / "printer_data" / "config"
        current = config_home / "mmu"
        older = config_home / "mmu.old-20260101-010203"
        newer = config_home / "mmu.old-20260830-120000"
        repo_dest = self.root / "happy-hare"
        repo_dest.mkdir()
        self.make_mmu_config(current, "current-marker")
        self.make_mmu_config(older, "older-marker")
        self.make_mmu_config(
            newer,
            ".mmu_config_unit0",
            ".mmu_config.old",
            "newer-marker",
        )

        result = self.run_shell("""
            SCRIPT_DIR={repo}
            CONFIG_KLIPPER_CONFIG_HOME={config}
            TESTDIR=
            recover_previous_config
            [ "$F_NO_MMU_BACKUP" = y ]
        """.format(
            repo=shlex.quote(str(repo_dest)),
            config=shlex.quote(str(config_home)),
        ), stdin="2\n")

        self.assertIn("1) mmu (current config)", result.stdout)
        self.assertIn(
            "2) mmu.old-20260830-120000 (2026-08-30 12:00:00)",
            result.stdout,
        )
        self.assertIn(
            "3) mmu.old-20260101-010203 (2026-01-01 01:02:03)",
            result.stdout,
        )
        self.assertIn("Choose backup to restore from (1-3)?", result.stderr)
        self.assertTrue((current / "newer-marker").exists())
        self.assertFalse((current / "current-marker").exists())
        self.assertTrue((repo_dest / ".mmu_config_unit0").exists())
        self.assertFalse((repo_dest / ".mmu_config.old").exists())

        preserved = [
            path for path in config_home.glob("mmu.old-*")
            if (path / "current-marker").exists()
        ]
        self.assertEqual(len(preserved), 1)

    def test_test_mode_recovers_kconfig_into_testdir(self):
        testdir = self.root / "mmu_test"
        current = testdir / "printer_data" / "config" / "mmu"
        repo_dest = self.root / "happy-hare"
        repo_dest.mkdir()
        self.make_mmu_config(current, ".mmu_config_unit0")

        self.run_shell("""
            SCRIPT_DIR={repo}
            TESTDIR={testdir}
            CONFIG_KLIPPER_CONFIG_HOME=
            recover_last_config
        """.format(
            repo=shlex.quote(str(repo_dest)),
            testdir=shlex.quote(str(testdir)),
        ))

        self.assertTrue((testdir / ".mmu_config").exists())
        self.assertTrue((testdir / ".mmu_config_unit0").exists())
        self.assertFalse((repo_dest / ".mmu_config").exists())

    def test_recovery_options_are_noops_on_first_install(self):
        config_home = self.root / "printer_data" / "config"
        repo_dest = self.root / "happy-hare"
        repo_dest.mkdir()

        result = self.run_shell("""
            SCRIPT_DIR={repo}
            CONFIG_KLIPPER_CONFIG_HOME={config}
            TESTDIR=
            recover_last_config
            recover_previous_config
        """.format(
            repo=shlex.quote(str(repo_dest)),
            config=shlex.quote(str(config_home)),
        ))

        self.assertEqual(result.stdout.count("No MMU configuration containing"), 2)
        self.assertFalse(config_home.exists())
        self.assertEqual(list(repo_dest.iterdir()), [])

    def test_v3_choice_precedes_recovery_and_is_not_bypassed_by_yes(self):
        script = INSTALL_SH.read_text(encoding="utf-8")
        v3_choice = script.index(
            'if [ "${F_SKIP_UPDATE}" != "force" ] && v3_detected; then'
        )
        recovery = script.index(
            'if { [ "${F_RECOVER_LAST}" ] || [ "${F_RECOVER_PREVIOUS}" ]; } '
            '&& [ ! "${F_CONFIG_RECOVERED}" ]; then'
        )
        self.assertLess(v3_choice, recovery)

    def test_make_skips_only_requested_mmu_backup(self):
        mmu = self.root / "mmu"
        self.write(mmu / "marker", "current\n")
        recipe = (
            f"include {MAKEFILE}\n"
            ".PHONY: installer-backup-test\n"
            "installer-backup-test:\n"
            "\t$(Q)$(call backup,$(BACKUP_TEST_PATH),$(F_NO_MMU_BACKUP))"
        )
        test_makefile = self.write(self.root / "backup-test.mk", recipe)
        common = [
            "make",
            "--no-print-directory",
            "-f", str(test_makefile),
            "installer-backup-test",
            "Q=",
            "KCONFIG_CONFIG={}".format(self.root / "missing-kconfig"),
            "BACKUP_TEST_PATH={}".format(mmu),
        ]

        skipped = subprocess.run(
            common + ["F_NO_MMU_BACKUP=y"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("recovery already preserved it", skipped.stdout)
        self.assertEqual(list(self.root.glob("mmu.old-*")), [])

        subprocess.run(
            common,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(len(list(self.root.glob("mmu.old-*"))), 1)
        self.assertIn(
            "$(call backup,$(basename $@),$(F_NO_MMU_BACKUP))",
            MAKEFILE.read_text(encoding="utf-8"),
        )

    def test_git_is_isolated_from_developer_configuration(self):
        """Developer git configuration must not affect temporary repos."""
        config_home = self.root / "hostile-home"
        config_home.mkdir()
        self.write(config_home / ".gitconfig",
                   "[core]\n"
                   "\teditor = /nonexistent-editor\n"
                   "\thooksPath = /nonexistent-hooks\n"
                   "[commit]\n\tgpgsign = true\n"
                   "[tag]\n\tgpgsign = true\n"
                   "[include]\n\tpath = /nonexistent-include\n")
        # Restore the process environment when the test finishes.
        saved = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = str(config_home / ".gitconfig")
        if saved is None:
            self.addCleanup(os.environ.pop, "GIT_CONFIG_GLOBAL")
        else:
            self.addCleanup(os.environ.__setitem__, "GIT_CONFIG_GLOBAL", saved)

        repo = self.root / "repo"
        self._git("init", str(repo))
        for option in ("core.editor", "commit.gpgsign", "tag.gpgsign"):
            result = self._git("-C", str(repo), "config", "--get", option,
                               check=False)
            self.assertEqual(
                result.returncode, 1,
                "git read developer configuration: {}={!r} (stderr {!r})".format(
                    option, result.stdout, result.stderr))

        # A commit must succeed without inherited signing or editor settings.
        self.write(repo / "marker", "v4\n")
        self._git("-C", str(repo), "add", "marker")
        self._git("-C", str(repo), "commit", "-m", "v4")

    def make_v3_install(self):
        config_home = self.root / "printer_data" / "config"
        self.write(
            config_home / "mmu" / "base" / "mmu_parameters.cfg",
            "happy_hare_version: 3.2.1\n",
        )
        self.write(config_home / "mmu" / "v3-marker", "v3\n")
        self.write(
            config_home / "printer.cfg",
            "[include mmu/base/*.cfg]\n[include other.cfg]\n",
        )
        self.write(
            config_home / "moonraker.conf",
            "[update_manager other]\n"
            "primary_branch: other-main\n\n"
            "[update_manager happy-hare]\n"
            "primary_branch: main\n"
            "path: /tmp/happy-hare\n\n"
            "[mmu_server]\n"
            "enable_file_preprocessor: True\n\n"
            "[server]\n"
            "port: 7125\n",
        )
        return config_home

    def test_v3_detection_accepts_separator_and_whitespace_variants(self):
        config_home = self.root / "printer_data" / "config"
        parameters = config_home / "mmu" / "base" / "mmu_parameters.cfg"
        variants = (
            "happy_hare_version: 3.2.1\n",
            "happy_hare_version = 3.2.1\n",
            "  happy_hare_version  :  3.2.1\n",
            "\thappy_hare_version\t=\t3.2.1\n",
        )

        for value in variants:
            with self.subTest(value=value.strip()):
                self.write(parameters, value)
                self.run_shell("""
                    SCRIPT_DIR={repo}
                    CONFIG_KLIPPER_CONFIG_HOME={config}
                    KCONFIG_CONFIG={missing}
                    TESTDIR=
                    v3_detected
                """.format(
                    repo=shlex.quote(str(REPO_ROOT)),
                    config=shlex.quote(str(config_home)),
                    missing=shlex.quote(str(self.root / "missing-kconfig")),
                ))

    def test_v3_blue_choice_pins_v3_branch_and_reexecs(self):
        config_home = self.make_v3_install()
        fake_checkout = self.root / "checkout"
        log_dir = self.root / "log"
        log_dir.mkdir()

        self_update = self.write(
            fake_checkout / "installer" / "self_update.sh",
            "#!/bin/sh\nprintf '%s\\n' \"$BRANCH\" >\"$TEST_LOG/self-update\"\n",
        )
        self_update.chmod(0o755)
        reexec = self.write(
            self.root / "reexec.sh",
            "#!/bin/sh\n"
            "printf '%s:%s:%s\\n' \"$BRANCH\" \"$F_SKIP_UPDATE\" \"$SKIP_UPDATE\" "
            ">\"$TEST_LOG/reexec\"\n",
        )
        reexec.chmod(0o755)

        self.run_shell("""
            SCRIPT_DIR={checkout}
            CONFIG_KLIPPER_CONFIG_HOME={config}
            KCONFIG_CONFIG={missing}
            TESTDIR=
            v3_detected
            offer_v3_v4_choice
        """.format(
            checkout=shlex.quote(str(fake_checkout)),
            config=shlex.quote(str(config_home)),
            missing=shlex.quote(str(self.root / "missing-kconfig")),
        ), stdin="1\n", env={"TEST_LOG": log_dir}, argv0=reexec)

        moonraker = (config_home / "moonraker.conf").read_text(encoding="utf-8")
        self.assertIn("[update_manager other]\nprimary_branch: other-main", moonraker)
        self.assertIn("[update_manager happy-hare]\nprimary_branch: v3", moonraker)
        self.assertEqual((log_dir / "self-update").read_text().strip(), "v3")
        self.assertEqual((log_dir / "reexec").read_text().strip(), "v3:force:YES")

    def test_self_update_switches_before_checking_current_branch(self):
        remote = self.root / "remote.git"
        checkout = self.root / "checkout"

        self._git("init", "--bare", str(remote))
        self._git("init", str(checkout))
        self.write(checkout / "marker", "v3\n")
        self._git("-C", str(checkout), "add", "marker")
        self._git("-C", str(checkout), "commit", "-m", "v3")
        self._git("-C", str(checkout), "branch", "-M", "v3")
        self._git("-C", str(checkout), "tag", "v3-test")
        self._git("-C", str(checkout), "remote", "add", "origin", str(remote))
        self._git("-C", str(checkout), "push", "-u", "origin", "v3", "--tags")
        self._git("-C", str(checkout), "checkout", "-b", "codex/local-only")

        # The script runs git itself (fetch/stash/checkout/pull/describe);
        # it inherits the same isolation so none of those can prompt either.
        env = self._isolated_git_env()
        env.update({
            "BRANCH": "v3",
            "C_OFF": "",
            "C_NOTICE": "",
            "C_WARNING": "",
            "C_ERROR": "",
        })
        result = subprocess.run(
            ["/bin/sh", str(SELF_UPDATE_SH)], cwd=checkout, env=env,
            text=True, capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        current = self._git("-C", str(checkout),
                            "branch", "--show-current").stdout.strip()
        self.assertEqual(current, "v3")
        self.assertIn("Switching to 'v3' branch", result.stdout)
        self.assertNotIn("Running on 'codex/local-only' branch", result.stdout)
        self.assertNotIn("Found a new version", result.stdout)

    def test_v3_red_choice_backs_up_v3_and_cleans_for_v4(self):
        config_home = self.make_v3_install()
        kconfig = self.root / ".mmu_config"
        kconfig.write_text(
            'CONFIG_KLIPPER_CONFIG_HOME="{}"\n'
            'CONFIG_PRINTER_CONFIG_FILE="printer.cfg"\n'
            'CONFIG_MOONRAKER_CONFIG_FILE="moonraker.conf"\n'.format(config_home),
            encoding="utf-8",
        )
        make_log = self.root / "make-log"

        self.run_shell("""
            SCRIPT_DIR={repo}
            CONFIG_KLIPPER_CONFIG_HOME={config}
            KCONFIG_CONFIG={missing}
            TESTDIR=
            unset BRANCH
            F_YES=y
            v3_detected
            offer_v3_v4_choice
            [ "$F_V3_UPGRADE" = y ]
            [ -z "${{BRANCH:-}}" ]

            KCONFIG_CONFIG={kconfig}
            INSTALLER_PY={python}
            run_make() {{ printf '%s\\n' "$*" >{make_log}; }}
            time_elapsed() {{ "$@"; }}
            v3_upgrade_cleanup
        """.format(
            repo=shlex.quote(str(REPO_ROOT)),
            config=shlex.quote(str(config_home)),
            missing=shlex.quote(str(self.root / "missing-kconfig")),
            kconfig=shlex.quote(str(kconfig)),
            python=shlex.quote(sys.executable),
            make_log=shlex.quote(str(make_log)),
        ), stdin="2\n")

        self.assertFalse((config_home / "mmu").exists())
        self.assertTrue((config_home / "mmu.V3" / "v3-marker").exists())

        printer = (config_home / "printer.cfg").read_text(encoding="utf-8")
        self.assertNotIn("include mmu/", printer)
        self.assertIn("[include other.cfg]", printer)

        moonraker = (config_home / "moonraker.conf").read_text(encoding="utf-8")
        self.assertNotIn("[update_manager happy-hare]", moonraker)
        self.assertNotIn("[mmu_server]", moonraker)
        self.assertIn("[update_manager other]", moonraker)
        self.assertIn("[server]", moonraker)
        self.assertEqual(make_log.read_text().strip(), "F_NO_SERVICE=y fix_links")


if __name__ == "__main__":
    unittest.main()
