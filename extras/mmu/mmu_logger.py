# Happy Hare MMU Software
# Logging helpers
#
# Copyright (C) 2022  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#

# PAUL
#import logging, logging.handlers, threading, queue, os
#class MmuLogger(logging.Handler):
#    def __init__(self, logfile_path):
#        name = os.path.splitext(os.path.basename(logfile_path))[0]
#        self.queue_listener = QueueListener(logfile_path)
#        self.queue_listener.setFormatter(MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
#        queue_handler = QueueHandler(self.queue_listener.bg_queue)
#        #queue_handler.setFormatter(MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S')) # PAUL this instead of setting on queue_listener
#        self.logger = logging.getLogger(name)
#        self.logger.setLevel(logging.INFO)
#        self.logger.addHandler(queue_handler)
#
#    def log(self, message):
#        self.logger.info(message)
#
#    def shutdown(self):
#        if self.queue_listener is not None:
#            self.queue_listener.stop()
#
## Forward all messages through a queue (polled by background thread)
#class QueueHandler(logging.Handler):
#    def __init__(self, queue):
#        logging.Handler.__init__(self)
#        self.queue = queue
#
#    def emit(self, record):
#        try:
#            self.queue.put_nowait(record)
#        except Exception:
#            self.handleError(record)
#
## PAUL
##        try:
##            self.format(record)
##            record.msg = record.message
##            record.args = None
##            record.exc_info = None
##            self.queue.put_nowait(record)
##        except Exception:
##            self.handleError(record)
#
## Poll log queue on background thread and log each message to logfile
#class QueueListener(logging.handlers.TimedRotatingFileHandler):
#    def __init__(self, filename):
#        logging.handlers.TimedRotatingFileHandler.__init__(self, filename, when='midnight', backupCount=5)
#        self.bg_queue = queue.Queue()
#        self.bg_thread = threading.Thread(target=self._bg_thread)
#        self.bg_thread.start()
#
#    def _bg_thread(self):
#        while True:
#            record = self.bg_queue.get(True)
#            if record is None:
#                break
#            self.handle(record)
#
#    def stop(self):
#        self.bg_queue.put_nowait(None)
#        self.bg_thread.join()
#
## Class to improve formatting of multi-line messages
#class MultiLineFormatter(logging.Formatter):
#    def format(self, record):
#        indent = ' ' * 9
#        lines = super(MultiLineFormatter, self).format(record)
#        return lines.replace('\n', '\n' + indent)
#

import logging, logging.handlers, threading, os, atexit

# Not sure this is needed for python 2..
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

class MmuLogger:
    def __init__(self, logfile_path):
        name = os.path.splitext(os.path.basename(logfile_path))[0]
        handler = logging.handlers.TimedRotatingFileHandler(logfile_path, when='midnight', backupCount=3)
        handler.setFormatter(MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))

        self.queue_listener = QueueListener(handler)
        queue_handler = QueueHandler(self.queue_listener.bg_queue)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(queue_handler)

        # Ensure we shutdown on exit
        atexit.register(self.shutdown)

    def log(self, message):
        self.logger.info(message)

    def shutdown(self):
        self.logger.info("Shutting down the MMU logger")
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
