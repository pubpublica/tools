import os
import sys
import json
from datetime import datetime
import getpass
import tarfile
import hashlib
from dataclasses import dataclass

from libpass import PasswordStore

import click
import invoke
from invoke.context import Context
import fabric
from fabric import Config, Connection
from fabrikant import fs, system, access
from fabrikant.apps import git, systemd, apt

from config import config

import log
import util
from util import Guard, GuardWarning

PASS = PasswordStore()

# TODO: async workflow: (uvloop?)
# (async paramiko commands may not be a good idea?)
# build context
# ASYNC checks
# pack, upload, unpack
# ASYNC config rendering
# chown + chgrp + chmod
# symlink
# restart services


def build_context(c):
    with Guard("· gathering build information..."):
        context = config.get("DEPLOY") or {}
        context.update(config.get("PROVISION", {}))
        context.update(config.get("BUILD", {}))

        root = os.getcwd()
        context.update({"LOCAL_ROOT": root})

        version = util.version()
        context.update({"LOCAL_VERSION": version})

        commit = git.latest_commit_hash(c, ".")
        context.update({"COMMIT_HASH": commit})
        context.update({"SHORT_COMMIT_HASH": commit[:7]})

        timestamp = util.timestamp()
        context.update({"TIMESTAMP": timestamp})

        return context


def check_local_git_repo(c, context):
    with Guard("· checking git repo..."):
        root = context.get("LOCAL_ROOT")
        dirty = git.is_dirty(c, root)

        if dirty is None:
            raise GuardWarning(f"{root} is not a repository")

        if dirty:
            raise GuardWarning("repository is dirty")


def check_deployment(c, context):
    with Guard("· checking deployment..."):
        app_path = context.get("APP_PATH")
        id_file = context.get("DEPLOYED_ID_FILE")
        deployment_file = os.path.join(app_path, id_file)
        id = fs.read_file(c, deployment_file)

        if not id:
            raise GuardWarning("unable to find deployed id")

        context.update({"DEPLOYED_ARTIFACT_ID": id})


def check_versions(c, context):
    with Guard("· checking versions..."):
        production_path = context.get("PRODUCTION_PATH")
        remote_ver_file = os.path.join(production_path, "__version__.py")
        v_remote = fs.read_file(c, remote_ver_file)

        if not v_remote:
            raise GuardWarning("unable to retrieve deployed version")

        context.update({"REMOTE_VERSION": v_remote})

        v_local = context.get("LOCAL_VERSION")
        if not util.version_newer(v_local, v_remote):
            raise GuardWarning(f"{v_local} is older or equal to deployed {v_remote}")


def check_dependencies(c, context):
    with Guard("· checking dependencies..."):
        deps = context.get("DEPENDENCIES") or []
        for dep in deps:
            if not apt.is_installed(c, dep):
                raise Exception(f"{dep} is not installed.")


def pack_project(c, context):
    def _tar_filter(info):
        if "__pycache__" in info.name:
            return None
        return info

    with Guard("· packing..."):
        includes = context.get("INCLUDES") or []
        commit = context.get("SHORT_COMMIT_HASH")
        version = context.get("LOCAL_VERSION")
        timestamp = context.get("TIMESTAMP")
        date = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d")

        app_path = context.get("APP_PATH")

        artifact_name = f"pubpublica--{date}--{version}--{commit}"
        artifact_ext = ".tar.gz"
        artifact_file = artifact_name + artifact_ext

        artifact_dir = os.path.abspath("build/")
        artifact_path = os.path.join(artifact_dir, artifact_file)

        with tarfile.open(artifact_path, "w:gz") as tar:
            for f in includes:
                tar.add(f, filter=_tar_filter)

        context.update({"ARTIFACT_ID": artifact_name})
        context.update({"ARTIFACT_FILE": artifact_file})
        context.update({"ARTIFACT_LOCAL_PATH": artifact_path})

        md5 = hashlib.md5()
        block_size = 65536
        with open(artifact_path, "rb") as f:
            while data := f.read(block_size):
                md5.update(data)

        context.update({"ARTIFACT_MD5": md5.hexdigest()})

        deploy_path = os.path.join(app_path, artifact_name)
        context.update({"DEPLOY_PATH": deploy_path})


def transfer_project(c, context):
    with Guard("· transferring..."):
        local_artifact = context.get("ARTIFACT_LOCAL_PATH")
        if not local_artifact:
            raise Exception("no artifact to deployed")

        if not os.path.isfile(local_artifact):
            raise Exception("artifact to be deployed is not a file")

        deploy_path = context.get("DEPLOY_PATH")
        if not fs.create_directory(c, deploy_path, sudo=True):
            raise Exception("unable to create {deploy_path} on server")

        artifact_file = context.get("ARTIFACT_FILE")
        artifact_path = os.path.join(deploy_path, artifact_file)

        temp_path = "/tmp"
        remote_artifact = os.path.join(temp_path, artifact_file)

        # if transfer fails, an exception is raised
        c.put(local_artifact, remote=remote_artifact)
        fs.move(c, remote_artifact, artifact_path, sudo=True)


def unpack_project(c, context):
    with Guard("· unpacking..."):
        deploy_path = context.get("DEPLOY_PATH")
        artifact = context.get("ARTIFACT_FILE")
        artifact_path = os.path.join(deploy_path, artifact)

        cmd = f"tar -C {deploy_path} -xzf {artifact_path}"
        unpack = c.sudo(cmd, hide=True, warn=True)

        if not unpack.ok:
            raise Exception(f"failed to unpack project: {unpack.stderr}")

        if not fs.remove(c, artifact_path, sudo=True):
            raise GuardWarning("failed to remove artifact after unpacking")


def restart_service(c, service):
    with Guard(f"· restarting {service} service..."):
        if not systemd.restart(c, service, sudo=True):
            raise GuardWarning(f"Failed to restart the {service} service")


def setup_flask(c, context):
    # TODO: merge with setup_pubpublica?
    # TODO: find some other approach for rendering and saving config files enmasse
    print("setting up flask")

    if not (cfg := config.get("FLASK") or {}):
        log.warning("unable to locate flask config")

    local_config_path = context.get("LOCAL_CONFIG_PATH")
    if not os.path.isdir(local_config_path):
        raise Exception(f"local config path {local_config_path} does not exist")

    if not (deploy_path := context.get("DEPLOY_PATH")):
        raise Exception("dont know where the app is located")

    if not (config_file := cfg.get("FLASK_CONFIG_FILE")):
        raise Exception("dont know where the flask config file is located")

    with Guard("· building config files..."):
        if path := cfg.get("FLASK_SECRET_KEY_PATH"):
            pw = PASS.get(path)
            cfg.update({"FLASK_SECRET_KEY": pw})
            cfg.pop("FLASK_SECRET_KEY_PATH", None)

        config_path = context.get("LOCAL_CONFIG_PATH")
        config_file = cfg.get("FLASK_CONFIG_FILE")
        flask_template = os.path.join(config_path, config_file)
        rendered_config = util.template(flask_template, cfg)

    with Guard("· writing config files..."):
        config_contents = json.dumps(rendered_config, indent=4)
        remote_config_file_path = os.path.join(deploy_path, config_file)
        tmpfile = remote_config_file_path + ".new"
        fs.write_file(c, config_contents, tmpfile, overwrite=True, sudo=True)
        fs.move(c, tmpfile, remote_config_file_path, sudo=True)


def setup_redis(c, context):
    print("setting up redis")

    if not (cfg := config.get("REDIS") or {}):
        log.warning("unable to locate redis config")

    local_config_path = context.get("LOCAL_CONFIG_PATH")
    if not os.path.isdir(local_config_path):
        raise Exception(f"local config path {local_config_path} does not exist")

    if not (deploy_path := context.get("DEPLOY_PATH")):
        raise Exception("dont know where the app is located")

    if not (config_file := cfg.get("REDIS_CONFIG_FILE")):
        raise Exception("dont know where the redis config file is located")

    with Guard("· building config files..."):
        if password_path := cfg.get("REDIS_PASSWORD_PATH"):
            pw = PASS.get(password_path)
            cfg.update({"REDIS_PASSWORD": pw})
            cfg.pop("REDIS_PASSWORD_PATH", None)

        redis_template = os.path.join(local_config_path, config_file)
        rendered_config = util.template(redis_template, cfg)

    with Guard("· writing config files..."):
        # TODO: extract this 'dump json to string, create remote tmp file, write contents, overwrite remote file' flow
        config_contents = json.dumps(rendered_config, indent=4)
        remote_config_file_path = os.path.join(deploy_path, config_file)
        tmpfile = remote_config_file_path + ".new"
        fs.write_file(c, config_contents, tmpfile, overwrite=True, sudo=True)
        fs.move(c, tmpfile, remote_config_file_path, sudo=True)


def setup_nginx(c, context):
    # TODO: copy over nginx settings
    print("setting up nginx")

    with Guard("· building config files..."):
        cfg = config.get("NGINX") or {}

        config_path = context.get("LOCAL_CONFIG_PATH")
        nginx_template = os.path.join(config_path, ".nginx")
        nginx_config = util.template(nginx_template, cfg)

    with Guard("· writing config files..."):
        pass


def setup_pubpublica_access(c, context):
    if not (deploy_path := context.get("DEPLOY_PATH")):
        raise Exception("unable to locate deployed app")

    user = context.get("USER")
    group = context.get("GROUP")

    with Guard("· creating user and group..."):
        if user:
            if not system.create_user(c, user, sudo=True):
                raise Exception(f"failed to create user '{user}'")

        if group:
            if not system.create_group(c, group, sudo=True):
                raise Exception(f"failed to create group '{group}")

            if not system.group_add_user(c, group, user, sudo=True):
                raise Exception(f"failed to add user '{user} to group '{group}")

    with Guard("· changing permissions..."):
        if user:
            if not access.change_owner(c, deploy_path, user, recursive=True, sudo=True):
                raise Exception(f"failed to change owner of deployment")

        if group:
            if not access.change_group(c, deploy_path, group, recursive=True, sudo=True):
                raise Exception(f"failed to change group of deployment")


def setup_pubpublica_virtualenv(c, context):
    if not (deploy_path := context.get("DEPLOY_PATH")):
        raise Exception("unable to locate deployed app")

    with Guard("· creating virtual environment..."):
        venv_dir = os.path.join(deploy_path, "venv")
        create_venv = f"python3.8 -m venv {venv_dir}"

        ret = c.sudo(create_venv, hide=True, warn=True)
        if not ret.ok:
            raise Exception(f"failed creating virtual environment: {ret}")

    with Guard("· updating virtual environment..."):
        venv_dir = os.path.join(deploy_path, "venv")
        pip_file = os.path.join(venv_dir, "bin", "pip3.8")
        requirements_file = os.path.join(deploy_path, "requirements.txt")
        pip_install = f"{pip_file} install -r {requirements_file}"

        ret = c.sudo(pip_install, hide=True, warn=True)
        if not ret.ok:
            raise Exception(f"failed to update the virtual environment: {ret}")


def setup_pubpublica(c, context):
    print("setting up pubpublica")

    if not (cfg := config.get("PUBPUBLICA") or {}):
        log.warning("unable to locate pubpublica config")

    ctx = {**context, **cfg}

    local_config_path = context.get("LOCAL_CONFIG_PATH")
    if not os.path.isdir(local_config_path):
        raise Exception(f"local config path {local_config_path} does not exist")

    if not (app_path := context.get("APP_PATH")):
        raise Exception("dont know where the app is located")

    if not (deploy_path := context.get("DEPLOY_PATH")):
        raise Exception("unable to locate deployed app")

    if not (config_file := cfg.get("PUBPUBLICA_CONFIG_FILE")):
        raise Exception("dont know where the config_file is located")

    remote_config_file_path = os.path.join(deploy_path, config_file)

    with Guard("· building config files..."):
        template_path = os.path.join(local_config_path, config_file)
        rendered_config = util.template(template_path, ctx)

    with Guard("· writing config files..."):
        config_string = json.dumps(rendered_config, indent=4)
        tmpfile = remote_config_file_path + ".new"
        fs.write_file(c, config_string, tmpfile, overwrite=True, sudo=True)
        fs.move(c, tmpfile, remote_config_file_path, sudo=True)

    setup_pubpublica_virtualenv(c, ctx)
    setup_pubpublica_access(c, ctx)

    with Guard("· linking production to new deployment..."):
        production_path = context.get("PRODUCTION_PATH")
        if not fs.create_symlink(c, deploy_path, production_path, force=True, sudo=True):
            raise Exception(f"failed to link {production_path} to newly deployed app")

    version_file = context.get("DEPLOYED_ID_FILE")
    new_version = context.get("ARTIFACT_ID")
    version_file_path = os.path.join(app_path, version_file)
    if not fs.write_file(c, new_version, version_file_path, overwrite=True, sudo=True):
        raise Exception("unable to write new deployment version to {version_file}")


def pre_deploy(c, local, context):
    print("PRE DEPLOY")
    context.update({"DEPLOY_START_TIME": util.timestamp()})
    check_local_git_repo(local, context)
    check_deployment(c, context)
    check_versions(c, context)
    check_dependencies(c, context)


def deploy(c, context):
    print("DEPLOY")
    pack_project(c, context)
    transfer_project(c, context)
    unpack_project(c, context)

    setup_redis(c, context)
    setup_nginx(c, context)

    setup_flask(c, context)
    setup_pubpublica(c, context)


def post_deploy(c, context):
    print("POST DEPLOY")

    # TODO: only restart services whoose config has changed
    restart_service(c, "redis")
    restart_service(c, "nginx")
    restart_service(c, "pubpublica")

    context.update({"DEPLOY_END_TIME": util.timestamp()})


@click.command()
@click.argument("host")
@click.option("-d", "--dry-run", is_flag=True)
def entry(host, dry_run):
    try:
        local = Context()
        c = util.connect(host, sudo=True)

        context = build_context(local)

        # TODO: only open if needed
        PASS.unlock()

        if dry_run:
            print("DRY RUN")

            @dataclass
            class success:
                ok: bool = True
                exited: int = 0
                stdout: str = ""

            def just_print(*args, **kwargs):
                args = " ".join(args)
                print(f"{args}")
                return success()

            c.run = just_print
            c.sudo = just_print
            c.put = just_print

        # TODO: validate context with jsonschema

        pre_deploy(c, local, context)
        deploy(c, context)
        post_deploy(c, context)

        # util.print_json(context)

        log.success("deployment complete")
    except KeyboardInterrupt:
        pass
    except Exception as err:
        log.error(err)


if __name__ == "__main__":
    entry()