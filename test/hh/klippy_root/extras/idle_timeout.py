# Fake Klipper extras/idle_timeout.py. HH reads the `.idle_timeout` attribute
# (extras/mmu/mmu_controller.py:200, extras/mmu/mmu_print_state_machine.py:125,222)
# and get_status()['state'], and registers idle_timeout:{idle,printing,ready} handlers.


class IdleTimeout:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.idle_timeout = config.getfloat('timeout', 600., above=0.)
        self.state = 'Idle'
        self.last_print_start_time = 0.
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('SET_IDLE_TIMEOUT', self.cmd_SET_IDLE_TIMEOUT,
                               desc='Set the idle timeout in seconds')

    def cmd_SET_IDLE_TIMEOUT(self, gcmd):
        timeout = gcmd.get_float('TIMEOUT', self.idle_timeout, above=0.)
        self.idle_timeout = timeout
        gcmd.respond_info('idle_timeout: Timeout set to %.2f s' % (timeout,))

    def get_status(self, eventtime=None):
        return {'state': self.state, 'printing_time': 0.}

    # -- test-facing: drive the state machine HH subscribes to ---------------
    def set_state(self, state):
        assert state in ('Idle', 'Ready', 'Printing')
        self.state = state
        self.printer.send_event('idle_timeout:%s' % state.lower())


def load_config(config):
    return IdleTimeout(config)
