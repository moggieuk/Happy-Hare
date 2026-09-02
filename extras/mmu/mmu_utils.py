# Happy Hare MMU Software
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# Utility classes for Happy Hare MMU
#
# MmuError
# Goal: Wrapper exception for all MMU errors.
#
# SaveVariableManager
# Goal: Centralization of all save_variable manipulation for per-unit namespacing and efficiency.
#
# DebugStepperMovement
# Goal: Internal testing class for debugging synced movement.
#
# PurgeVolCalculator
# Goal: Purge volume calculator based on color change
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, math, contextlib, re, copy

# Happy Hare imports
from .mmu_constants import *



# -----------------------------------------------------------------------------------------------------------
# DEDICATED MMU EXCEPTION
# -----------------------------------------------------------------------------------------------------------

class MmuError(Exception):
    """
    Wrapper exception for all MMU errors.
    """
    pass



# -----------------------------------------------------------------------------------------------------------
# WRAPPER FOR EFFICIENT USE OF SAVE VARIABLES
# -----------------------------------------------------------------------------------------------------------

_DELETED = object() # Journal sentinel: "this key must not exist on disk"


class SaveVariableManager:
    """
    Centralization of all save_variable manipulation for per-unit namespacing and efficiency

    SAVE_VARIABLE snapshots klipper's variable dict up front, then pauses and finally
    replaces it with a re-read of the file, so anything set mid-command is silently lost.
    Every set()/delete() is therefore journalled in _pending and only dropped once a
    post-write re-read proves it reached disk. Writes are coalesced onto a reactor timer.
    """

    MAX_FLUSH_ATTEMPTS = 3
    FLUSH_RETRY_DELAY  = 0.05 # Re-check interval while a command holds the gcode mutex

    def __init__(self, config, mmu_machine):
        """
        Initialize manager, validate save_variables config, and register ready handler.

        Expects mmu_vars.cfg to be included and to contain the mmu revision variable.
        """
        self.config = config
        self.mmu_machine = mmu_machine
        self.printer = config.get_printer()
        self.gcode = self.mmu_machine.printer.lookup_object('gcode')
        self.save_variables = self.mmu_machine.printer.load_object(config, 'save_variables')

        self._can_write_variables = False # Whether it is ok to write to "save_variables"

        self._pending = {}           # Unpersisted deltas: key -> value, or _DELETED
        self._mutations = 0          # Bumped by every set()/delete(); detects write races
        self._flush_timer = None     # Non-None while a coalesced flush is outstanding
        self._writing = False        # Guards against overlapping physical writes

        # Sanity check to see that mmu_vars.cfg is included.  This will verify path
        # because default deliberately has 'mmu_revision' entry
        if self.save_variables:
            revision_var = self.save_variables.allVariables.get(VARS_MMU_REVISION, None)
            if revision_var is None:
                self.save_variables.allVariables[VARS_MMU_REVISION] = 0
        else:
            revision_var = None
        if not self.save_variables or revision_var is None:
            raise config.error(
                "Calibration settings file (mmu_vars.cfg) not found. "
                "Check [save_variables] section in mmu_macro_vars.cfg\n"
                "Also ensure you only have a single [save_variables] section defined "
                "in your printer config and it contains the line: mmu__revision = 0. "
                "If not, add this line and restart"
            )

        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)


    def handle_ready(self):
        """
        Schedule the startup flush; it must not run on the "klippy:ready" dispatch.

        Klipper forbids pausing inside its ready handler loop, and SAVE_VARIABLE pauses,
        so writing from here raises "Internal error - reactor pause disabled".
        """
        self.printer.get_reactor().register_callback(self._handle_ready_flush)


    def _handle_ready_flush(self, eventtime):
        """
        Enable writes once we are clear of ready dispatch and flush pending updates.

        Writes _write_now() directly because we are already on the reactor. Everything
        the ready handlers staged is in the journal and lands in this one flush.
        """
        if self.printer.is_shutdown():
            return                       # A later ready handler failed; gcode is dead
        self._can_write_variables = True # This prevents early writes until klipper is ready
        self._write_now()                # Flush anything that was pending


    def namespace(self, variable, namespace):
        """
        Return a variable name namespaced to an MMU unit (if provided).
        """
        if namespace is not None:
            return variable.replace("mmu_", "mmu_%s_" % namespace)
        return variable


    @staticmethod
    def _snapshot(value):
        """
        Copy containers, pass scalars through.

        Callers pass live objects they keep mutating (gate maps, stats), so the journal
        must own a private copy or it cannot tell a dropped write from a later edit.
        No `set`: klipper cannot round-trip one, repr() gives "set()" which won't parse.
        """
        if isinstance(value, (list, dict, tuple)):
            return copy.deepcopy(value)
        return value


    def get(self, variable, default, namespace=None):
        """
        Get a variable value from save_variables with optional namespacing.

        Unpersisted deltas win over klipper's dict, which a reload can rewrite at any
        time. Returns a copy so a caller cannot end up owning the journal's object.
        """
        key = self.namespace(variable, namespace)
        if key in self._pending:
            value = self._pending[key]
            return default if value is _DELETED else self._snapshot(value)
        return self.save_variables.allVariables.get(key, default)


    def set(self, variable, value, namespace=None, write=False):
        """
        Set a variable value with optional namespacing and optional batch flush.

        write=True means "this batch is complete, schedule a flush", not "write now" -
        several in the same reactor pass collapse into a single file write.
        """
        key = self.namespace(variable, namespace)
        value = self._snapshot(value)
        self._pending[key] = value
        self._mutations += 1
        self.save_variables.allVariables[key] = value
        if write:
            self.write()


    def delete(self, variable, namespace=None, write=False):
        """
        Delete a variable with optional namespacing and optional batch flush.
        """
        key = self.namespace(variable, namespace)
        self._pending[key] = _DELETED
        self._mutations += 1
        _ = self.save_variables.allVariables.pop(key, None)
        if write:
            self.write()


    def _apply_pending(self):
        """
        Push unpersisted deltas back into klipper's dict.

        Gets them into the next SAVE_VARIABLE snapshot, and repairs the dict after any
        re-read has replaced it - ours or one triggered by a macro elsewhere.
        """
        all_variables = self.save_variables.allVariables
        for key, value in self._pending.items():
            if value is _DELETED:
                all_variables.pop(key, None)
            else:
                all_variables[key] = value


    def _reconcile(self):
        """
        Drop journal entries the post-write re-read proves are on disk.

        SAVE_VARIABLE ends by re-reading the file, so klipper's dict now IS the file
        contents - comparing against it verifies the write rather than assuming it.
        """
        all_variables = self.save_variables.allVariables
        for key, value in list(self._pending.items()):
            if value is _DELETED:
                if key not in all_variables:
                    del self._pending[key]
            elif key in all_variables and all_variables[key] == value:
                del self._pending[key]
        self._apply_pending() # Whatever is left is unpersisted; keep it readable


    def write(self):
        """
        Request a flush. The physical write happens on a reactor timer.

        SAVE_VARIABLE pauses, so issuing it inline needs the gcode mutex - and we are
        called from both locked (gcode command) and unlocked (bare reactor timer)
        contexts. Deferring every write to one place means run_script, which takes the
        mutex itself, is always correct and no caller has to know where it is running.
        """
        if not self._can_write_variables or self._flush_timer is not None:
            return
        reactor = self.printer.get_reactor()
        self._flush_timer = reactor.register_timer(self._flush, reactor.NOW)


    def _flush(self, eventtime):
        """
        Timer callback that performs the coalesced write.

        Retries rather than blocking when a command holds the gcode mutex: run_script
        would park this callback mid-flight for the length of that command, and a state
        save has no deadline.
        """
        reactor = self.printer.get_reactor()
        if self._can_write_variables and not self.printer.is_shutdown():
            if self.gcode.get_mutex().test():
                return eventtime + self.FLUSH_RETRY_DELAY # Leave the timer armed
            try:
                self._write_now()
            except Exception:
                # A failed save must not take the printer down, nor strand _flush_timer
                # set, which would stop all future flushes. Nothing is lost - the journal
                # keeps every unconfirmed delta for the next attempt.
                logging.exception("MMU: error flushing save_variables")
        reactor.unregister_timer(self._flush_timer)
        self._flush_timer = None
        return reactor.NEVER


    def _write_now(self):
        """
        Persist by bumping the MMU vars revision, which rewrites the whole file.

        Retries only when the mutation counter shows something was set while we were
        paused. Retrying on any mismatch would be a write-amplification trap: a value
        klipper cannot round-trip would mismatch forever and treble every flush.
        """
        if self._writing:
            return # Re-entrant request; the loop below will pick the new deltas up
        self._writing = True
        try:
            for _ in range(self.MAX_FLUSH_ATTEMPTS):
                self._apply_pending()
                mutations = self._mutations
                mmu_vars_revision = self.save_variables.allVariables.get(VARS_MMU_REVISION, 0) + 1
                self.gcode.run_script(
                    "SAVE_VARIABLE VARIABLE=%s VALUE=%d" % (VARS_MMU_REVISION, mmu_vars_revision)
                )
                self._reconcile()
                if not self._pending or mutations == self._mutations:
                    break # Nothing left, or nothing raced with us so retrying is futile
            if self._pending:
                logging.info(
                    "MMU: %d save_variable(s) not confirmed on disk after %d attempt(s): %s"
                    % (len(self._pending), self.MAX_FLUSH_ATTEMPTS, sorted(self._pending))
                )
        finally:
            self._writing = False


    def handle_disconnect(self):
        """
        Last chance to persist anything still pending, since flushes are deferred.

        Ignores _can_write_variables on purpose: it means "not yet", but at disconnect
        there is no later flush, and a suspended batch is the largest thing we could
        drop. Best-effort - the reactor has stopped, so a pause here cannot complete.
        """
        if not self._pending:
            return
        try:
            self._can_write_variables = True
            self._write_now()
        except Exception:
            logging.exception("MMU: unable to flush save_variables on disconnect")


    def upgrade(self, variable, namespace):
        """
        Move a variable to a namespaced key (if it exists), then delete the old key.
        Used for v3 -> v4 upgrade
        """
        val = self.get(variable, None)
        if val is not None:
            self.set(variable, val, namespace)
            self.delete(variable)


    @contextlib.contextmanager
    def wrap_suspend_write_variables(self):
        """
        Temporarily suspend writes to save_variables, then re-enable and flush on exit.
        """
        self._can_write_variables = False
        try:
            yield self
        finally:
            self._can_write_variables = True
            self.write()


# -----------------------------------------------------------------------------------------------------------
# CONTEXT MANAGER FOR DEBUGGING STEPPER MOVEMENT
# -----------------------------------------------------------------------------------------------------------

class DebugStepperMovement:
    """
    Context manager for debugging synced gear/extruder/rail movement.
    Add this around move logic:
        with DebugStepperMovement(self):
           <synced_move>
    """

    def __init__(self, mmu, debug=False):
        """
        Create a movement debugger; when debug is False it does nothing.
        """
        self.mmu = mmu
        self.debug = debug


    def __enter__(self):
        """
        Capture starting positions/steps if debugging is enabled.
        """
        if self.debug:
            g_stepper = self.mmu.drive().mmu_gear_stepper
            e_stepper = self.mmu.drive().mmu_extruder_stepper

            self.g_steps0 = g_stepper.stepper.get_mcu_position()
            self.g_pos0 = g_stepper.stepper.get_commanded_position()
            self.g_manual_pos0 = g_stepper.commanded_pos
            self.g_mode_pos0 = g_stepper.get_mode_position()

            self.e_steps0 = e_stepper.stepper.get_mcu_position()
            self.e_pos0 = e_stepper.stepper.get_commanded_position()
            self.e_manual_pos0 = e_stepper.commanded_pos
            self.e_mode_pos0 = e_stepper.get_mode_position()


    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Wait for completion and log deltas if debugging is enabled.
        """
        if self.debug:
            self.mmu.log_always("Waiting for movement to complete...")
            self.mmu.toolhead.wait_moves()

            g_stepper = self.mmu.drive().mmu_gear_stepper
            e_stepper = self.mmu.drive().mmu_extruder_stepper

            g_steps1 = g_stepper.stepper.get_mcu_position()
            g_pos1 = g_stepper.stepper.get_commanded_position()
            g_manual_pos1 = g_stepper.commanded_pos
            g_mode_pos1 = g_stepper.get_mode_position()

            e_steps1 = e_stepper.stepper.get_mcu_position()
            e_pos1 = e_stepper.stepper.get_commanded_position()
            e_manual_pos1 = e_stepper.commanded_pos
            e_mode_pos1 = e_stepper.get_mode_position()

            self.mmu.log_always(
                f"Gear steps: {g_steps1 - self.g_steps0:d} = "
                f"{(g_steps1 - self.g_steps0) * g_stepper.stepper.get_step_dist():.4f}mm, "
                f"stepper.cmd_pos movement: {g_pos1 - self.g_pos0:.4f}mm, "
                f"cmd_pos movement: {g_manual_pos1 - self.g_manual_pos0:.4f}mm, "
                f"mode_pos: {g_mode_pos1 - self.g_mode_pos0:.4f}mm"
            )

            self.mmu.log_always(
                f"Extruder steps: {e_steps1 - self.e_steps0:d} = "
                f"{(e_steps1 - self.e_steps0) * e_stepper.stepper.get_step_dist():.4f}mm, "
                f"stepper.cmd_pos movement: {e_pos1 - self.e_pos0:.4f}mm, "
                f"cmd_pos movement: {e_manual_pos1 - self.e_manual_pos0:.4f}mm, "
                f"mode_pos: {e_mode_pos1 - self.e_mode_pos0:.4f}mm"
            )



# -----------------------------------------------------------------------------------------------------------
# PURGE VOLUME LOGIC
# -----------------------------------------------------------------------------------------------------------

class PurgeVolCalculator:
    """
    Compute purge volume for a color change using RGB/HSV-based heuristics.
    """

    def __init__(self, min_purge_vol, max_purge_vol, multiplier):
        """
        Initialize calculator parameters and scaling.
        """
        self.min_purge_vol = min_purge_vol
        self.max_purge_vol = max_purge_vol
        self.multiplier = multiplier


    def calc_purge_vol_by_rgb(self, src_r, src_g, src_b, dst_r, dst_g, dst_b):
        """
        Calculate purge volume for a transition from source RGB to destination RGB.
        """
        src_r_f = float(src_r) / 255.0
        src_g_f = float(src_g) / 255.0
        src_b_f = float(src_b) / 255.0
        dst_r_f = float(dst_r) / 255.0
        dst_g_f = float(dst_g) / 255.0
        dst_b_f = float(dst_b) / 255.0

        from_hsv_h, from_hsv_s, from_hsv_v = self.RGB2HSV(src_r_f, src_g_f, src_b_f)
        to_hsv_h, to_hsv_s, to_hsv_v = self.RGB2HSV(dst_r_f, dst_g_f, dst_b_f)
        hs_dist = self.DeltaHS_BBS(from_hsv_h, from_hsv_s, from_hsv_v, to_hsv_h, to_hsv_s, to_hsv_v)
        from_lumi = self.get_luminance(src_r_f, src_g_f, src_b_f)
        to_lumi = self.get_luminance(dst_r_f, dst_g_f, dst_b_f)

        lumi_purge = 0.0
        if to_lumi >= from_lumi:
            lumi_purge = math.pow(to_lumi - from_lumi, 0.7) * 560.0
        else:
            lumi_purge = (from_lumi - to_lumi) * 80.0

        inter_hsv_v = 0.67 * to_hsv_v + 0.33 * from_hsv_v
        hs_dist = min(inter_hsv_v, hs_dist)
        hs_purge = 230.0 * hs_dist

        purge_volume = self.calc_triangle_3rd_edge(hs_purge, lumi_purge, 120.0)
        purge_volume = max(0.0, purge_volume * self.multiplier)
        purge_volume = min(self.max_purge_vol, max(self.min_purge_vol, purge_volume))
        purge_volume = int(round(purge_volume))

        return purge_volume


    def calc_purge_vol_by_hex(self, src_clr, dst_clr):
        """
        Calculate purge volume for a transition from source hex to destination hex.
        """
        src_rgb = self.hex_to_rgb(src_clr)
        dst_rgb = self.hex_to_rgb(dst_clr)
        return self.calc_purge_vol_by_rgb(*(src_rgb + dst_rgb))


    @staticmethod
    def RGB2HSV(r, g, b):
        """
        Convert RGB floats in [0, 1] to HSV (h in degrees, s/v in [0, 1]).
        """
        Cmax = max(r, g, b)
        Cmin = min(r, g, b)
        delta = Cmax - Cmin

        if abs(delta) < 0.001:
            h = 0.0
        elif Cmax == r:
            h = 60.0 * math.fmod((g - b) / delta, 6.0)
        elif Cmax == g:
            h = 60.0 * ((b - r) / delta + 2)
        else:
            h = 60.0 * ((r - g) / delta + 4)
        s = 0.0 if abs(Cmax) < 0.001 else delta / Cmax
        v = Cmax
        return h, s, v


    @staticmethod
    def to_radians(degree):
        """
        Convert degrees to radians.
        """
        return degree / 180.0 * math.pi


    @staticmethod
    def get_luminance(r, g, b):
        """
        Return a simple weighted luminance estimate from RGB floats.
        """
        return r * 0.3 + g * 0.59 + b * 0.11


    @staticmethod
    def calc_triangle_3rd_edge(edge_a, edge_b, degree_ab):
        """
        Compute the third triangle edge using law of cosines (angle in degrees).
        """
        return math.sqrt(edge_a * edge_a + edge_b * edge_b - 2 * edge_a * edge_b * math.cos(PurgeVolCalculator.to_radians(degree_ab)))


    @staticmethod
    def DeltaHS_BBS(h1, s1, v1, h2, s2, v2):
        """
        Compute a bounded distance in HS space using hue angle and scaled SV.
        """
        h1_rad = PurgeVolCalculator.to_radians(h1)
        h2_rad = PurgeVolCalculator.to_radians(h2)

        dx = math.cos(h1_rad) * s1 * v1 - math.cos(h2_rad) * s2 * v2
        dy = math.sin(h1_rad) * s1 * v1 - math.sin(h2_rad) * s2 * v2
        dxy = math.sqrt(dx * dx + dy * dy)

        return min(1.2, dxy)


    @staticmethod
    def hex_to_rgb(hex_color):
        """
        Convert a hex color string (#RGB, #RRGGBB, #RRGGBBAA) to an (r, g, b) tuple.
        """
        hex_color = hex_color.lstrip('#')
        if len(hex_color) == 3:
            hex_color = ''.join([c * 2 for c in hex_color])
        if len(hex_color) == 8:
            hex_color = hex_color[:6]
        if len(hex_color) != 6:
            raise ValueError("Invalid hex color code, it should be 3, 6 or 8 digits long")
        color_value = int(hex_color, 16)
        r = (color_value >> 16) & 0xFF
        g = (color_value >> 8) & 0xFF
        b = color_value & 0xFF
        return r, g, b



# -----------------------------------------------------------------------------------------------------------
# COLOR UTILS
# -----------------------------------------------------------------------------------------------------------

class MmuColorUtils:
    """
    Collection of color conversion and other utilities
    """

    # Standard symbolic color names
    # cspell:disable # technical color names should not be spell checked
    W3C_COLORS = {
        'aliceblue': '#F0F8FF',
        'antiquewhite': '#FAEBD7',
        'aqua': '#00FFFF',
        'aquamarine': '#7FFFD4',
        'azure': '#F0FFFF',
        'beige': '#F5F5DC',
        'bisque': '#FFE4C4',
        'black': '#000000',
        'blanchedalmond': '#FFEBCD',
        'blue': '#0000FF',
        'blueviolet': '#8A2BE2',
        'brown': '#A52A2A',
        'burlywood': '#DEB887',
        'cadetblue': '#5F9EA0',
        'chartreuse': '#7FFF00',
        'chocolate': '#D2691E',
        'coral': '#FF7F50',
        'cornflowerblue': '#6495ED',
        'cornsilk': '#FFF8DC',
        'crimson': '#DC143C',
        'cyan': '#00FFFF',
        'darkblue': '#00008B',
        'darkcyan': '#008B8B',
        'darkgoldenrod': '#B8860B',
        'darkgray': '#A9A9A9',
        'darkgreen': '#006400',
        'darkgrey': '#A9A9A9',
        'darkkhaki': '#BDB76B',
        'darkmagenta': '#8B008B',
        'darkolivegreen': '#556B2F',
        'darkorange': '#FF8C00',
        'darkorchid': '#9932CC',
        'darkred': '#8B0000',
        'darksalmon': '#E9967A',
        'darkseagreen': '#8FBC8F',
        'darkslateblue': '#483D8B',
        'darkslategray': '#2F4F4F',
        'darkslategrey': '#2F4F4F',
        'darkturquoise': '#00CED1',
        'darkviolet': '#9400D3',
        'deeppink': '#FF1493',
        'deepskyblue': '#00BFFF',
        'dimgray': '#696969',
        'dimgrey': '#696969',
        'dodgerblue': '#1E90FF',
        'firebrick': '#B22222',
        'floralwhite': '#FFFAF0',
        'forestgreen': '#228B22',
        'fuchsia': '#FF00FF',
        'gainsboro': '#DCDCDC',
        'ghostwhite': '#F8F8FF',
        'gold': '#FFD700',
        'goldenrod': '#DAA520',
        'gray': '#808080',
        'green': '#008000',
        'greenyellow': '#ADFF2F',
        'grey': '#808080',
        'honeydew': '#F0FFF0',
        'hotpink': '#FF69B4',
        'indianred': '#CD5C5C',
        'indigo': '#4B0082',
        'ivory': '#FFFFF0',
        'khaki': '#F0E68C',
        'lavender': '#E6E6FA',
        'lavenderblush': '#FFF0F5',
        'lawngreen': '#7CFC00',
        'lemonchiffon': '#FFFACD',
        'lightblue': '#ADD8E6',
        'lightcoral': '#F08080',
        'lightcyan': '#E0FFFF',
        'lightgoldenrodyellow': '#FAFAD2',
        'lightgray': '#D3D3D3',
        'lightgreen': '#90EE90',
        'lightgrey': '#D3D3D3',
        'lightpink': '#FFB6C1',
        'lightsalmon': '#FFA07A',
        'lightseagreen': '#20B2AA',
        'lightskyblue': '#87CEFA',
        'lightslategray': '#778899',
        'lightslategrey': '#778899',
        'lightsteelblue': '#B0C4DE',
        'lightyellow': '#FFFFE0',
        'lime': '#00FF00',
        'limegreen': '#32CD32',
        'linen': '#FAF0E6',
        'magenta': '#FF00FF',
        'maroon': '#800000',
        'mediumaquamarine': '#66CDAA',
        'mediumblue': '#0000CD',
        'mediumorchid': '#BA55D3',
        'mediumpurple': '#9370DB',
        'mediumseagreen': '#3CB371',
        'mediumslateblue': '#7B68EE',
        'mediumspringgreen': '#00FA9A',
        'mediumturquoise': '#48D1CC',
        'mediumvioletred': '#C71585',
        'midnightblue': '#191970',
        'mintcream': '#F5FFFA',
        'mistyrose': '#FFE4E1',
        'moccasin': '#FFE4B5',
        'navajowhite': '#FFDEAD',
        'navy': '#000080',
        'oldlace': '#FDF5E6',
        'olive': '#808000',
        'olivedrab': '#6B8E23',
        'orange': '#FFA500',
        'orangered': '#FF4500',
        'orchid': '#DA70D6',
        'palegoldenrod': '#EEE8AA',
        'palegreen': '#98FB98',
        'paleturquoise': '#AFEEEE',
        'palevioletred': '#DB7093',
        'papayawhip': '#FFEFD5',
        'peachpuff': '#FFDAB9',
        'peru': '#CD853F',
        'pink': '#FFC0CB',
        'plum': '#DDA0DD',
        'powderblue': '#B0E0E6',
        'purple': '#800080',
        'rebeccapurple': '#663399',
        'red': '#FF0000',
        'rosybrown': '#BC8F8F',
        'royalblue': '#4169E1',
        'saddlebrown': '#8B4513',
        'salmon': '#FA8072',
        'sandybrown': '#F4A460',
        'seagreen': '#2E8B57',
        'seashell': '#FFF5EE',
        'sienna': '#A0522D',
        'silver': '#C0C0C0',
        'skyblue': '#87CEEB',
        'slateblue': '#6A5ACD',
        'slategray': '#708090',
        'slategrey': '#708090',
        'snow': '#FFFAFA',
        'springgreen': '#00FF7F',
        'steelblue': '#4682B4',
        'tan': '#D2B48C',
        'teal': '#008080',
        'thistle': '#D8BFD8',
        'tomato': '#FF6347',
        'turquoise': '#40E0D0',
        'violet': '#EE82EE',
        'wheat': '#F5DEB3',
        'white': '#FFFFFF',
        'whitesmoke': '#F5F5F5',
        'yellow': '#FFFF00',
        'yellowgreen': '#9ACD32',
    }
    # cspell:enable


    @staticmethod
    def format_color(color):
        """
        Format color string for display
        """
        x = re.search(r"^([a-f\d]{6})(ff)?$", color, re.IGNORECASE)
        if x is not None:
            return '#' + x.group(1).upper()

        x = re.search(r"^([a-f\d]{6}([a-f\d]{2})?)$", color, re.IGNORECASE)
        if x is not None:
            return '#' + x.group().upper()

        return color


    @staticmethod
    def color_to_rgb_hex(color, default="000000"):
        """
        Returns hex color format without leading '#', e.g. ff00e080.
        Supports alpha channel.
        """
        if not color:
            color = default
        else:
            color = color.lower()
            if color in MmuColorUtils.W3C_COLORS:
                color = MmuColorUtils.W3C_COLORS[color]

        rgb_hex = color.lstrip('#').lower()
        return rgb_hex[0:8]


    @staticmethod
    def color_to_rgb_tuple(color, fraction=True):
        """
        Returns RGB tuple as fractions, e.g. (0.32, 0.56, 1.00),
        or integers, e.g. (82, 143, 255). Alpha channel is ignored.
        """
        rgb_hex = MmuColorUtils.color_to_rgb_hex(color)[:6]
        length = len(rgb_hex)

        if length != 6:
            return (0.0, 0.0, 0.0) if fraction else (0, 0, 0)

        if fraction:
            return tuple(round(int(rgb_hex[i:i + 2], 16) / 255, 3) for i in (0, 2, 4))

        return tuple(int(rgb_hex[i:i + 2], 16) for i in (0, 2, 4))


    @staticmethod
    def validate_color(color):
        """
        Return validated color string or None if invalid.
        """
        color = color.lower()
        if color == "":
            return ""

        # Try W3C named color
        if color in MmuColorUtils.W3C_COLORS:
            return color

        # Try RGB/RGBA hex color
        color = color.lstrip('#')
        x = re.search(r"^([a-f\d]{6}([a-f\d]{2})?)$", color, re.IGNORECASE)
        if x is not None and x.group() == color:
            return color

        return None


    @staticmethod
    def find_closest_color(ref_color, color_list):
        """
        Find closest color in color_list to ref_color.
        Example:
          color_list = ['123456', 'abcdef', '789abc', '4a7d9f', '010203']
          find_closest_color('4b7d8e', color_list) returns ('4a7d9f', distance)
        """
        def weighted_euclidean_distance(color1, color2, weights=(0.3, 0.59, 0.11)):
            return sum(weights[i] * (a - b) ** 2 for i, (a, b) in enumerate(zip(color1, color2)))

        ref_rgb = MmuColorUtils.color_to_rgb_tuple(ref_color, fraction=False)
        min_distance = float('inf')
        closest_color = None

        for color in color_list:
            color_rgb = MmuColorUtils.color_to_rgb_tuple(color, fraction=False)
            distance = weighted_euclidean_distance(ref_rgb, color_rgb)
            if distance < min_distance:
                min_distance = distance
                closest_color = color

        return closest_color, min_distance
