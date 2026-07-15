"""Small Klipper/NFC test doubles shared by the NFC regression suite."""

from unittest.mock import MagicMock


class ConfigError(Exception):
    pass


class FakeConfig:
    def __init__(self, values=None, name='nfc_gate test'):
        self.values = dict(values or {})
        self.name = name

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getint(self, key, default=None, **_limits):
        return int(self.get(key, default))

    def getfloat(self, key, default=None, **_limits):
        return float(self.get(key, default))

    def get_name(self):
        return self.name

    def error(self, message):
        return ConfigError(message)


class FakePrinter:
    config_error = ConfigError

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class FakeReactor:
    def __init__(self):
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def run_callbacks(self):
        for callback in self.callbacks:
            callback(0.0)


def fake_gate(**values):
    gate = MagicMock()
    gate._debug = values.pop('_debug', 0)
    for name, value in values.items():
        setattr(gate, name, value)
    return gate
