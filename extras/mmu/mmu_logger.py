# Happy Hare MMU Software
# Logging helpers
#
# Copyright (C) 2022-2025  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, logging.handlers, threading, os, queue, atexit

# MMU subcomponent clases
from .mmu_shared import *

class MmuLogger:
    def __init__(self, logfile_path):
        name = os.path.splitext(os.path.basename(logfile_path))[0]
        self.logger = logging.getLogger(name)

        self.queue_listener = None
        if not any(isinstance(h, QueueHandler) for h in self.logger.handlers):
            handler = logging.handlers.TimedRotatingFileHandler(logfile_path, when='midnight', backupCount=3)
            handler.setFormatter(MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
            self.queue_listener = QueueListener(handler)
            self.logger.addHandler(QueueHandler(self.queue_listener.bg_queue))

        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        atexit.register(self.shutdown)

    def log(self, message):
        self.logger.info(message.replace(UI_SPACE, ' ').replace(UI_SEPARATOR, ' '))

    def shutdown(self):
        if self.queue_listener is not None:
            self.queue_listener.stop()

# Poll log queue on background thread and log each message to logfile
class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super(QueueHandler, self).__init__()
        self.queue = log_queue

    def emit(self, record):
        try:
            self.queue.put_nowait(record)
        except Exception:
            self.handleError(record)

class QueueListener:
    def __init__(self, handler):
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
    def format(self, record):
        indent = ' ' * 9
        formatted_message = super(MultiLineFormatter, self).format(record)
        if record.exc_text:
            # Don't modify exception stack traces
            return formatted_message
        return formatted_message.replace('\n', '\n' + indent)
