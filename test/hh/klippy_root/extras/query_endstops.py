# Fake Klipper extras/query_endstops.py. HH: extras/mmu_stepper.py:83,233,325
# (load_object then register_endstop for each rail/extra endstop).


class QueryEndstops:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.endstops = []          # [(mcu_endstop, name)] assertion surface
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('QUERY_ENDSTOPS', self.cmd_QUERY_ENDSTOPS,
                               desc='Report on the status of each endstop')

    def register_endstop(self, mcu_endstop, name):
        self.endstops.append((mcu_endstop, name))

    def names(self):
        return [n for _es, n in self.endstops]

    def cmd_QUERY_ENDSTOPS(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead', None)
        pt = toolhead.get_last_move_time() if toolhead else 0.
        msg = ' '.join('%s:%s' % (name, 'TRIGGERED' if es.query_endstop(pt) else 'open')
                       for es, name in self.endstops)
        gcmd.respond_info(msg or 'no endstops registered')

    def get_status(self, eventtime=None):
        return {'last_query': {}}


def load_config(config):
    return QueryEndstops(config)
