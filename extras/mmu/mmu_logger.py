# Happy Hare MMU Software
# Logging helpers
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

import logging, logging.handlers, threading, os.path, re, queue, atexit, traceback

# Happy Hare imports
from .mmu_constants import *


# Color formatting in console output
CONSOLE_COLOR_TOKENS_RE = re.compile(r"\{\{[^{}]*\}\}|\{[^{}]*\}")
CONSOLE_COLOR_SPAN_RE = re.compile(r"\{\{([0-9a-fA-F]{3,8})\}\}|\{\{\}\}")

class MmuLogger:
    """
    Asynchronous rotating file logger for Happy Hare MMU.
    """

    def __init__(self, mmu):
        """
        Configure logger and background queue listener.
        """
        self.mmu = mmu
        self.queue_listener = None
        self.file_logger = None
        self.file_logging_enabled = self.mmu.p.log_file_level >= 0

        if self.file_logging_enabled:
            logfile_path = self.mmu.printer.start_args['log_file']
            dirname = os.path.dirname(logfile_path)
            if not dirname:
                mmu_log = '/tmp/mmu.log'
            else:
                mmu_log = os.path.join(dirname, 'mmu.log')

            logging.info("MMU: Log: %s" % mmu_log)

            name = os.path.splitext(os.path.basename(mmu_log))[0]
            self.file_logger = logging.getLogger(name)

            if not any(isinstance(h, QueueHandler) for h in self.file_logger.handlers):
                handler = logging.handlers.TimedRotatingFileHandler(
                    mmu_log,
                    when='midnight',
                    backupCount=3
                )
                handler.setFormatter(
                    MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S')
                )
                self.queue_listener = QueueListener(handler)
                self.file_logger.addHandler(
                    QueueHandler(self.queue_listener.bg_queue)
                )

            self.file_logger.setLevel(logging.INFO)
            self.file_logger.propagate = False
            atexit.register(self.shutdown)

            self.log("\n\n\nMMU Startup -----------------------------------------------\n")

    def log(self, message):
        if self.file_logging_enabled and self.file_logger is not None:
            self.file_logger.info(
                message.replace(UI_SPACE, ' ').replace(UI_SEPARATOR, ' ')
            )

    def shutdown(self):
        if self.queue_listener is not None:
            self.queue_listener.stop()

    def _color_message(self, msg):
        # Fast path
        if "{" not in msg:
            return msg, msg

        # 1) Plain msg cleanup
        plain_msg = CONSOLE_COLOR_TOKENS_RE.sub("", msg)
        want_color = self.mmu.p.console_show_colored_text
        if not want_color:
            return plain_msg, plain_msg

        # 2) Replace fixed {0}..{6} tokens
        html_msg = msg
        if (
            "{0}" in html_msg or "{1}" in html_msg or "{2}" in html_msg or
            "{3}" in html_msg or "{4}" in html_msg or "{5}" in html_msg or
            "{6}" in html_msg
        ):
            html_msg = (
                html_msg
                .replace("{0}", "</span>")                      # Color off
                .replace("{1}", '<span style="color:#C0C0C0">') # Grey
                .replace("{2}", '<span style="color:#FF69B4">') # Redish
                .replace("{3}", '<span style="color:#90EE90">') # Greenish
                .replace("{4}", '<span style="color:#87CEEB">') # Cyan
                .replace("{5}", "<b>")                          # Bold on
                .replace("{6}", "</b>")                         # Bold off
            )

        # 3) Replace dynamic {{RRGGBB}} and {{}} tokens if present
        if "{{" in html_msg:
            def repl(match):
                hex_color = match.group(1)
                if hex_color:
                    return '<span style="color:#%s">' % hex_color
                return "</span>"

            html_msg = CONSOLE_COLOR_SPAN_RE.sub(repl, html_msg)

        return html_msg, plain_msg

    def log_to_file(self, msg, prefix='> '):
        self.log("%s%s" % (prefix, msg))

    def log_assertion(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)

        # Capture stack trace (exclude this frame for cleaner output)
        stack = "".join(traceback.format_stack()[:-1])

        self.log(msg)
        self.log("Stack trace:\n%s" % stack)

        self.mmu.gcode.respond_raw("!! Happy Hare Assertion: %s" % html_msg)

    def log_error(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        self.log(msg)
        self.mmu.gcode.respond_raw("!! %s" % html_msg)

    def log_warning(self, msg):
        self.log_always("{2}%s{0}" % msg, color=True)

    def log_always(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        self.log(msg)
        self.mmu.gcode.respond_info(html_msg)

    def log_info(self, msg, color=False):
        html_msg, msg = self._color_message(msg) if color else (msg, msg)
        if self.mmu.p.log_file_level > 0:
            self.log(msg)
        if self.mmu.p.log_level > 0:
            self.mmu.gcode.respond_info(html_msg)

    def log_debug(self, msg):
        msg = "%s DEBUG: %s" % (UI_SEPARATOR, msg)
        if self.mmu.p.log_file_level > 1:
            self.log(msg)
        if self.mmu.p.log_level > 1:
            self.mmu.gcode.respond_info(msg)

    def log_trace(self, msg):
        msg = "%s %s TRACE: %s" % (UI_SEPARATOR, UI_SEPARATOR, msg)
        if self.mmu.p.log_file_level > 2:
            self.log(msg)
        if self.mmu.p.log_level > 2:
            self.mmu.gcode.respond_info(msg)

    def log_stepper(self, msg):
        msg = "%s %s %s STEPPER: %s" % (UI_SEPARATOR, UI_SEPARATOR, UI_SEPARATOR, msg)
        if self.mmu.p.log_file_level > 3:
            self.log(msg)
        if self.mmu.p.log_level > 3:
            self.mmu.gcode.respond_info(msg)

    def log_enabled(self, level):
        return self.mmu.p.log_file_level >= level or self.mmu.p.log_level >= level


class QueueHandler(logging.Handler):
    """
    Handler that pushes log records into a queue.
    """

    def __init__(self, log_queue):
        super(QueueHandler, self).__init__()
        self.queue = log_queue

    def emit(self, record):
        try:
            self.queue.put_nowait(record)
        except Exception:
            self.handleError(record)


class QueueListener:
    """
    Consumes queued records and writes them via a handler.
    """

    def __init__(self, handler):
        """Start background logging thread."""
        self.bg_queue = queue.Queue()
        self.handler = handler
        self.bg_thread = threading.Thread(target=self._bg_thread)
        self.bg_thread.daemon = True
        self.bg_thread.start()

    def _bg_thread(self):
        while True:
            record = self.bg_queue.get(True)
            if record is None:
                break
            self.handler.handle(record)

    def stop(self):
        self.bg_queue.put_nowait(None)
        self.bg_thread.join()


# Class to improve formatting of multi-line messages
class MultiLineFormatter(logging.Formatter):
    """
    Formatter that indents multi-line log messages.
    """

    def format(self, record):
        indent = ' ' * 9
        formatted_message = super(MultiLineFormatter, self).format(record)
        if record.exc_text:
            # Don't modify exception stack traces
            return formatted_message
        return formatted_message.replace('\n', '\n' + indent)
