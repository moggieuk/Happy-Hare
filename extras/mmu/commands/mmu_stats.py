# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Implements MMU_STATS command
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# Happy Hare imports
from ..mmu_constants   import *
from ..mmu_utils       import MmuError
from .mmu_base_command import *


class MmuStatsCommand(BaseCommand):

    CMD = "MMU_STATS"

    HELP_BRIEF = "Dump and optionally reset the MMU statistics"
    HELP_PARAMS = (
        "%s: %s\n" % (CMD, HELP_BRIEF)
        + "RESET      = [0|1]\n"
        + "TOTAL      = [0|1]\n"
        + "DETAIL     = [0|1]\n"
        + "QUIET      = [0|1]\n"
        + "SHOWCOUNTS = [0|1]\n"
        + "COUNTER    = _name_\n"
        + "DELETE     = [0|1] (with COUNTER)\n"
        + "LIMIT      = #(int) (with COUNTER)\n"
        + "INCR       = #(int) (with COUNTER)\n"
        + "WARNING    = _text_ (with COUNTER)\n"
        + "PAUSE      = [0|1] (with COUNTER)\n"
    )
    HELP_SUPPLEMENT = (
        ""  # add examples here if desired
    )

    def __init__(self, mmu):
        super().__init__(mmu)
        self.register(
            name=self.CMD,
            handler=self._run,
            help_brief=self.HELP_BRIEF,
            help_params=self.HELP_PARAMS,
            help_supplement=self.HELP_SUPPLEMENT,
            category=CATEGORY_GENERAL
        )

    def _run(self, gcmd):
        # Note: BaseCommand wrapper already logs commandline + handles HELP=1.
        mmu = self.mmu

        if mmu.check_if_disabled(): return
        counter = gcmd.get('COUNTER', None)
        reset = bool(gcmd.get_int('RESET', 0, minval=0, maxval=1))
        total = bool(gcmd.get_int('TOTAL', 0, minval=0, maxval=1))
        detail = bool(gcmd.get_int('DETAIL', 0, minval=0, maxval=1))
        quiet = bool(gcmd.get_int('QUIET', 0, minval=0, maxval=1))
        showcounts = bool(gcmd.get_int('SHOWCOUNTS', 0, minval=0, maxval=1))

        if counter:
            counter = counter.strip()
            delete = bool(gcmd.get_int('DELETE', 0, minval=0, maxval=1))
            limit = gcmd.get_int('LIMIT', 0, minval=-1)
            incr = gcmd.get_int('INCR', 0, minval=1)
            quiet = True
            if delete:
                _ = mmu.counters.pop(counter, None)
            elif reset:
                if counter in mmu.counters:
                    mmu.counters[counter]['count'] = 0
            elif not limit == 0:
                if counter not in mmu.counters:
                    mmu.counters[counter] = {'count': 0}
                warning = gcmd.get('WARNING', mmu.counters[counter].get('warning', ""))
                pause = bool(gcmd.get_int('PAUSE', mmu.counters[counter].get('pause', 0), minval=0, maxval=1))
                mmu.counters[counter].update({'limit': limit, 'warning': warning, 'pause': pause})
            elif incr:
                if counter in mmu.counters:
                    metric = mmu.counters[counter]
                    metric['count'] += incr
                    if metric['limit'] >= 0 and metric['count'] > metric['limit']:
                        warn = "Warning: %s" % metric.get('warning', "")
                        msg = "Count %s (%d) above limit %d" % (counter, metric['count'], metric['limit'])
                        msg += "\nUse 'MMU_STATS COUNTER=%s RESET=1' to reset" % counter
                        if metric.get('pause', False):
                            mmu.handle_mmu_error("%s\n%s" % (warn, msg))
                        else:
                            mmu.log_error(warn)
                            mmu.log_always(msg)
                else:
                    mmu.counters[counter] = {'count': 0, 'limit': -1, 'warning': ""}
            mmu._persist_counters()
        elif reset:
            mmu._reset_statistics()
            mmu._persist_swap_statistics()
            mmu._persist_gate_statistics()
            if not quiet:
                mmu._dump_statistics(force_log=True, total=True)
            return

        if not quiet:
            mmu._dump_statistics(force_log=True, total=total or detail, job=True, gate=True, detail=detail, showcounts=showcounts)
