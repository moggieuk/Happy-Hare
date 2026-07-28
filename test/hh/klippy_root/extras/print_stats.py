# Fake Klipper extras/print_stats.py. OPTIONAL to HH - it does
# lookup_object("print_stats", None) and falls back to idle_timeout state
# (extras/mmu/mmu_controller.py:191-192) - so a "no virtual_sdcard" profile is
# also a valid thing to test.


class PrintStats:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.state = 'standby'
        self.filename = ''
        self.print_duration = 0.
        self.total_duration = 0.
        self.filament_used = 0.
        self.message = ''
        self.info = {'total_layer': None, 'current_layer': None}

    def set_state(self, state):
        assert state in ('standby', 'printing', 'paused', 'complete',
                         'cancelled', 'error')
        self.state = state

    def get_status(self, eventtime=None):
        return {'filename': self.filename, 'total_duration': self.total_duration,
                'print_duration': self.print_duration,
                'filament_used': self.filament_used, 'state': self.state,
                'message': self.message, 'info': self.info}


def load_config(config):
    return PrintStats(config)
