# Misc hardware text lives in Kconfig preprocessor variables. Those share one global
# namespace and every board file is parsed on every run, so a board that expanded a
# variable it never defined (or defined below the use) would silently get another
# board's hardware. Each board/type file must name its variables mh_<type>_<file>
# (<file> being the yyy of Kconfig.yyy) and define each one above its first use.

import glob, os, re, unittest

ROOT = os.path.join(os.path.dirname(__file__), '..', '..', 'installer')
FILES = sorted(glob.glob(os.path.join(ROOT, 'boards', '**', 'Kconfig.*'), recursive=True)
               + glob.glob(os.path.join(ROOT, 'mmu_types', '**', 'Kconfig.*'), recursive=True))

DEFINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\+=|=)')
USE = re.compile(r'\$\((mh_[A-Za-z0-9_]+|misc_hardware_[A-Za-z0-9_]+)\)')


def problems(text, namespace):
    """Return a list of naming and define-before-use problems in one Kconfig file."""
    found, defined = [], set()
    for lineno, line in enumerate(text.splitlines(), 1):
        match = DEFINE.match(line)
        if match and match.group(1).startswith(('mh_', 'misc_hardware_')):
            name = match.group(1)
            if not (name.startswith('mh_') and name.endswith('_' + namespace)
                    and len(name) > len('mh__' + namespace)):
                found.append('%d: %s should be named mh_<type>_%s' % (lineno, name, namespace))
            defined.add(name)
            continue
        for name in USE.findall(line):
            if name == 'misc_hardware_hint':
                continue
            if name not in defined:
                found.append('%d: $(%s) used without being defined above it' % (lineno, name))
    return found


class TestMiscHardwareVariables(unittest.TestCase):

    def test_board_and_type_files(self):
        self.assertTrue(FILES)
        for path in FILES:
            namespace = os.path.basename(path).split('.', 1)[1]
            with open(path) as f:
                text = f.read()
            with self.subTest(file=os.path.relpath(path, ROOT)):
                self.assertEqual(problems(text, namespace), [])

    def test_checker_catches_each_mistake(self):
        good = 'mh_heater_kms := \\\n[heater_generic x]\\n\\\n\n  default "$(ml,$(mh_heater_kms))"\n'
        self.assertEqual(problems(good, 'kms'), [])
        self.assertEqual(len(problems(good, 'vvd')), 1)  # wrong namespace
        self.assertEqual(len(problems(good.replace('mh_heater_kms', 'misc_hardware_heater'), 'kms')), 1)
        use_first = '  default "$(ml,$(mh_heater_kms))"\nmh_heater_kms := \\\n'
        self.assertEqual(len(problems(use_first, 'kms')), 1)
        self.assertEqual(problems('  comment "$(misc_hardware_hint)"\n', 'kms'), [])


if __name__ == '__main__':
    unittest.main()
