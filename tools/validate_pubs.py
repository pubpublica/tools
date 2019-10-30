import os
import sys
import json

from jsonschema import validate, ValidationError
import click

import log
import util

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "date": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "sitelink": {"type": "string"},
        "summarylink": {"type": "string"},
        "directlink": {"type": "string"},
    },
    "required": ["title", "authors", "date", "tags", "description", "sitelink"],
}


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

    failed = 0
    for file in os.listdir(dir):
        try:
            path = os.path.join(dir, file)
            with open(path, "r") as f:
                contents = json.load(f)
                validate(contents, SCHEMA)
        except ValidationError as err:
            failed += 1
            log.warning(f"{file}: {err.message}")

    if not failed:
        log.success("all publications valid")
    else:
        log.warning(f"{failed} publications failed validation")

    sys.exit(failed)


if __name__ == "__main__":
    entry()
