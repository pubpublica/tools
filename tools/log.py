import sys
from termcolor import colored


def info(s):
    print(colored(s, "white", attrs=["bold"]))


def success(s):
    print(colored(s, "green", attrs=["bold"]))


def warning(s):
    print(colored(s, "yellow", attrs=["bold"]))


def error(s):
    print(colored(s, "red", attrs=["bold"]), file=sys.stderr)
