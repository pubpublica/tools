import os
import sys
import json
import getpass
import datetime
import re

import packaging.version

from termcolor import colored
from fabric import Config, Connection

from jinja2 import Template

import log


def timestamp():
    return datetime.datetime.utcnow().isoformat()


def version():
    with open("__version__.py", "r") as f:
        version = f.read().strip()
        match = re.search("^\d+\.\d+\.\d+$", version)

        if not match:
            raise Exception("__version__.py is malformed")

        return version


def version_newer(a, b):
    a = packaging.version.parse(a)
    b = packaging.version.parse(b)
    return a > b


def template(file, config={}):
    if not os.path.isfile(file):
        return {}

    with open(file, "r") as f:
        contents = f.read()

    template = Template(contents)
    rendering = template.render(config)
    return json.loads(rendering)


def connect(host, sudo=False):
    config = Config()

    settings = {"hide": True, "warn": True}
    config["sudo"].update(settings)
    config["run"].update(settings)

    if sudo:
        sudo_pass = getpass.getpass(f"sudo password on {host}: ")
        config["sudo"].update({"password": sudo_pass})

    c = Connection(host, config=config)

    return c


def print_json(j):
    print(json.dumps(j, indent=4))


class GuardWarning(Exception):
    pass


class Guard:
    def __init__(self, str):
        self.str = str

    def __enter__(self):
        sys.stdout.write(self.str)
        sys.stdout.flush()

    def __exit__(self, type, value, traceback):
        handled = False

        if not (type and value and traceback):
            log.success("OK")
            handled = True
        elif type == GuardWarning:
            log.warning("WARNING")
            log.warning(value)
            handled = True
        else:
            log.error("FAILED")

        sys.stdout.flush()
        return handled
