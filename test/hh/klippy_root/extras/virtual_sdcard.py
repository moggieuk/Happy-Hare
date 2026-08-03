# Fake Klipper extras/virtual_sdcard.py. [virtual_sdcard] ships in the rendered
# mmu_macro_vars.cfg. In real Klipper this is what brings print_stats into
# existence, so we reproduce that chain rather than registering print_stats by hand.


class VirtualSD:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sdcard_dirname = config.get('path', '/tmp')
        self.print_stats = self.printer.load_object(config, 'print_stats')
        self.must_pause_work = False
        self.file_path_str = None

    def get_status(self, eventtime=None):
        return {'file_path': self.file_path_str, 'progress': 0.,
                'is_active': False, 'file_position': 0, 'file_size': 0}

    def is_active(self):
        return False


def load_config(config):
    return VirtualSD(config)
