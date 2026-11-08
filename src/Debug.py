# Copyright (C) 2018-2026 by xcentaurix
# License: GNU General Public License v3.0


ID = "SKF"


def initLogging():
    return


class Logger():

    def __init__(self):
        return

    def debug(self, msg, *args):
        print(ID + ": " + msg % args)

    def info(self, msg, *args):
        print(ID + ": " + msg % args)

    def warning(self, msg, *args):
        print(ID + ": " + msg % args)

    def error(self, msg, *args):
        print(ID + ": " + msg % args)


logger = Logger()
