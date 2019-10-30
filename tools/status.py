import os
import sys
import json
import getpass

import click
from paramiko.config import SSHConfig
from fabric import Config, Connection
from fabrikant import fs, system, access
from fabrikant.apps import git, systemd, ufw

from termcolor import colored

import log
import util
from util import Guard


def grey(s):
    return colored(s, "white")


def green(s):
    return colored(s, "green")


def yellow(s):
    return colored(s, "yellow")


def red(s):
    return colored(s, "red")


def build_context(c):
    config = util.template("pubpublica.json")

    ctx = {}
    ctx.update(config.get("DEPLOY"))
    ctx.update(config.get("PUBPUBLICA"))
    ctx.pop("INCLUDES", None)
    ctx.pop("SOCKET_PATH", None)

    return ctx


def gather_info(c, ctx):
    deployed_path = ctx.get("PRODUCTION_PATH")
    pubpublica_config_file = ctx.get("PUBPUBLICA_CONFIG_FILE")
    pubpublica_config_file_path = os.path.join(deployed_path, pubpublica_config_file)
    pubpublica_config = fs.read_file(c, pubpublica_config_file_path, sudo=True)
    pubpublica = json.loads(pubpublica_config)

    deployed_id = pubpublica.get("ARTIFACT_ID")
    print(f"ID:\t {grey(deployed_id)}")
    ctx.update({"DEPLOYED_ID": deployed_id})

    deployed_md5 = pubpublica.get("ARTIFACT_MD5")
    print(f"MD5:\t {grey(deployed_md5)}")
    ctx.update({"DEPLOYED_MD5": deployed_md5})

    deployed_timestamp = pubpublica.get("TIMESTAMP").replace("T", " ")
    print(f"date:\t {grey(deployed_timestamp)}")
    ctx.update({"DEPLOYED_TIMESTAMP": deployed_timestamp})

    deployed_commit = pubpublica.get("COMMIT_HASH")
    print(f"commit:\t {grey(deployed_commit)}")
    ctx.update({"DEPLOYED_COMMIT": deployed_commit})


def color_by_predicate(pred, true, false):
    if pred:
        return green(true)
    else:
        return red(false)


def color_by_range(val, s, low=0.25, mid=0.50, high=0.75):
    f = float(val)
    if f < 0.25:
        return green(s)
    elif f < 0.50:
        return yellow(s)
    elif f < 0.75:
        return yellow(s)
    else:
        return red(s)


def service_status(c, service):
    activity = green("active") if systemd.is_active(c, service) else red("inactive")
    return f"{service}: {activity}"


def ufw_status(c):
    ufw_on = green("active") if systemd.is_active(c, "ufw") else red("inactive")
    ufw_fw = green("enabled") if ufw.enabled(c, sudo=True) else red("disabled")

    return f"{ufw_on} + {ufw_fw}"


def avg_cpu_load(c):
    load_avg = c.run("cat /proc/loadavg | cut -d' ' -f1-3").stdout.strip()
    loads = [color_by_range(l, l) for l in load_avg.split()]
    return " ".join(loads)


def memory_load(c):
    mem = c.run("free -m | sed -n 2p | awk '{print $3 \" \" $2}'").stdout.strip()
    mem = mem.split()

    free = int(mem[0])
    total = int(mem[1])

    pct = int((free / total) * 100.0)

    return color_by_range(float(free / total), f"{free}mb / {total}mb ({pct}%)")


def count_compute_units(c):
    processes = c.run("ps -e | wc -l").stdout.strip()
    threads = c.run("ps -eT | wc -l").stdout.strip()
    return processes, threads


@click.command()
@click.argument("host")
def entry(host):

    try:
        c = util.connect(host, True)

        print("----------")

        ctx = build_context(c)
        gather_info(c, ctx)

        print("----------")
        print(c.run("uname -a").stdout.strip())
        print("----------")

        load_avg = avg_cpu_load(c)
        print(f"load average: \t{load_avg}")

        mem_load = memory_load(c)
        print(f"memory load: \t{mem_load}")

        processes, threads = count_compute_units(c)
        print(f"processes: \t{processes}")
        print(f"threads: \t{threads}")

        print("----------")
        print(service_status(c, "nginx"))
        print(service_status(c, "redis"))
        print(service_status(c, "pubpublica"))
        print("----------")
        print("ufw: " + ufw_status(c))
        print(service_status(c, "fail2ban"))
    except KeyboardInterrupt:
        pass
    except Exception as err:
        print(err)


if __name__ == "__main__":
    entry()
