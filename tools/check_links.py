import os
import sys

import click
import requests

import log
import util


def check_link(url):
    try:
        r = requests.head(url, allow_redirects=True)
        return r.status_code
    except requests.ConnectionError:
        return None


@click.command()
@click.argument("path")
def entry(path):
    cwd = os.getcwd()
    dir = os.path.join(cwd, path)

    items = {}
    for file in os.listdir(dir):
        path = os.path.join(dir, file)
        item = util.template(path)

        links = []
        for l in ["sitelink", "directlink", "summarylink"]:
            if item.get(l):
                links.append(item.get(l))

        items.update({file: links})

    exit_code = 0
    for file, links in items.items():
        print()
        print(file)
        for link in links:
            code = check_link(link)

            if not code:
                log.error(f"XXX: {link}")
                exit_code += 1
            elif code == 404:
                log.error(f"{code}: {link}")
                exit_code += 1
            else:
                print(f"{code}: {link}")

    sys.exit(exit_code)


if __name__ == "__main__":
    entry()
