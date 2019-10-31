import os
import sys
import json
import time
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
        config = util.template("pubpublica.json")

        context = {}
        context.update(config.get("BUILD", {}))

        local_config_path = os.path.abspath(context.get("LOCAL_CONFIG_PATH"))
        context.update({"LOCAL_CONFIG_PATH": local_config_path})

        local_app_path = os.path.abspath(context.get("LOCAL_APP_PATH"))
        context.update({"LOCAL_APP_PATH": local_app_path})

        context.update(config.get("PROVISION", {}))
        context.update(config.get("DEPLOY", {}))

        if pubpublica_config := config.get("PUBPUBLICA"):
            context.update({"PUBPUBLICA": pubpublica_config})

        if flask_config := config.get("FLASK"):
            context.update({"FLASK": flask_config})

        if redis_config := config.get("REDIS"):
            context.update({"REDIS": redis_config})

        version_file = os.path.join(local_app_path, "__version__.py")
        version = util.version(version_file)
        context.update({"LOCAL_VERSION": version})

        commit = git.latest_commit_hash(c, local_app_path)
        context.update({"COMMIT_HASH": commit})
        context.update({"SHORT_COMMIT_HASH": commit[:7]})

        timestamp = util.timestamp()
        context.update({"TIMESTAMP": timestamp})

        return context


def check_local_git_repo(c, ctx):
    with Guard("· checking local git repo..."):
        root = ctx.get("LOCAL_APP_PATH")
        dirty = git.is_dirty(c, root)

        if dirty is None:
            raise GuardWarning(f"{root} is not a git repository")

        if dirty:
            raise GuardWarning("local git repository is dirty")


def check_deployment(c, ctx):
    with Guard("· checking deployment..."):
        app_path = ctx.get("APP_PATH")
        id_file = ctx.get("DEPLOYED_ID_FILE")
        deployment_file = os.path.join(app_path, id_file)
        id = fs.read_file(c, deployment_file)

        if not id:
            raise GuardWarning("unable to find deployed id")

        ctx.update({"DEPLOYED_ARTIFACT_ID": id})


def check_versions(c, ctx):
    with Guard("· checking versions..."):
        production_path = ctx.get("PRODUCTION_PATH")
        remote_ver_file = os.path.join(production_path, "__version__.py")
        v_remote = fs.read_file(c, remote_ver_file)

        if not v_remote:
            raise GuardWarning("unable to retrieve deployed version")

        ctx.update({"REMOTE_VERSION": v_remote})

        v_local = ctx.get("LOCAL_VERSION")
        if not util.version_newer(v_local, v_remote):
            raise GuardWarning(f"{v_local} is older or equal to deployed {v_remote}")


def check_dependencies(c, ctx):
    with Guard("· checking dependencies..."):
        deps = ctx.get("DEPENDENCIES") or []
        for dep in deps:
            if not apt.is_installed(c, dep):
                raise Exception(f"{dep} is not installed.")


def pack_project(c, ctx):
    def _tar_filter(info):
        if "__pycache__" in info.name:
            return None
        return info

    with Guard("· packing..."):
        commit = ctx.get("SHORT_COMMIT_HASH")
        version = ctx.get("LOCAL_VERSION")
        timestamp = ctx.get("TIMESTAMP")
        date = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d")

        app_path = ctx.get("APP_PATH")
        local_app_path = ctx.get("LOCAL_APP_PATH")

        artifact_name = f"pubpublica--{date}--{version}--{commit}"
        artifact_ext = ".tar.gz"
        artifact_file = artifact_name + artifact_ext

        artifact_dir = os.path.abspath("build/")
        artifact_path = os.path.join(artifact_dir, artifact_file)

        includes = ctx.get("INCLUDES") or []
        paths = [ os.path.join(local_app_path, i) for i in includes ]

        with tarfile.open(artifact_path, "w:gz") as tar:
            for name, path in zip(includes, paths):
                tar.add(path, arcname=name, filter=_tar_filter)

        ctx.update({"ARTIFACT_ID": artifact_name})
        ctx.update({"ARTIFACT_FILE": artifact_file})
        ctx.update({"ARTIFACT_LOCAL_PATH": artifact_path})

        md5 = hashlib.md5()
        block_size = 65536
        with open(artifact_path, "rb") as f:
            while data := f.read(block_size):
                md5.update(data)

        ctx.update({"ARTIFACT_MD5": md5.hexdigest()})

        deploy_path = os.path.join(app_path, artifact_name)
        ctx.update({"DEPLOY_PATH": deploy_path})


def transfer_project(c, ctx):
    with Guard("· transferring..."):
        local_artifact = ctx.get("ARTIFACT_LOCAL_PATH")
        if not local_artifact:
            raise Exception("no artifact to deployed")

        if not os.path.isfile(local_artifact):
            raise Exception("artifact to be deployed is not a file")

        deploy_path = ctx.get("DEPLOY_PATH")
        if not fs.create_directory(c, deploy_path, sudo=True):
            raise Exception("unable to create {deploy_path} on server")

        artifact_file = ctx.get("ARTIFACT_FILE")
        artifact_path = os.path.join(deploy_path, artifact_file)

        temp_path = "/tmp"
        remote_artifact = os.path.join(temp_path, artifact_file)

        # if transfer fails, an exception is raised
        c.put(local_artifact, remote=remote_artifact)
        fs.move(c, remote_artifact, artifact_path, sudo=True)


def unpack_project(c, ctx):
    with Guard("· unpacking..."):
        deploy_path = ctx.get("DEPLOY_PATH")
        artifact = ctx.get("ARTIFACT_FILE")
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


def setup_flask(c, ctx):
    # TODO: merge with setup_pubpublica?
    # TODO: find some other approach for rendering and saving config files enmasse
    print("setting up flask")

    if not (cfg := ctx.get("FLASK") or {}):
        log.warning("unable to locate flask config")

    local_config_path = ctx.get("LOCAL_CONFIG_PATH")
    if not os.path.isdir(local_config_path):
        raise Exception(f"local config path {local_config_path} does not exist")

    if not (deploy_path := ctx.get("DEPLOY_PATH")):
        raise Exception("dont know where the app is located")

    if not (config_file := cfg.get("FLASK_CONFIG_FILE")):
        raise Exception("dont know where the flask config file is located")

    with Guard("· building config files..."):
        if path := cfg.get("FLASK_SECRET_KEY_PATH"):
            pw = PASS.get(path)
            cfg.update({"FLASK_SECRET_KEY": pw})
            cfg.pop("FLASK_SECRET_KEY_PATH", None)

        config_path = ctx.get("LOCAL_CONFIG_PATH")
        config_file = cfg.get("FLASK_CONFIG_FILE")
        flask_template = os.path.join(config_path, config_file)
        rendered_config = util.template(flask_template, cfg)

    with Guard("· writing config files..."):
        config_contents = json.dumps(rendered_config, indent=4)
        remote_config_file_path = os.path.join(deploy_path, config_file)
        tmpfile = remote_config_file_path + ".new"
        fs.write_file(c, config_contents, tmpfile, overwrite=True, sudo=True)
        fs.move(c, tmpfile, remote_config_file_path, sudo=True)


def setup_redis(c, ctx):
    print("setting up redis")

    if not (cfg := ctx.get("REDIS") or {}):
        log.warning("unable to locate redis config")

    local_config_path = ctx.get("LOCAL_CONFIG_PATH")
    if not os.path.isdir(local_config_path):
        raise Exception(f"local config path {local_config_path} does not exist")

    if not (deploy_path := ctx.get("DEPLOY_PATH")):
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


def setup_nginx(c, ctx):
    # TODO: copy over nginx settings
    print("setting up nginx")

    with Guard("· building config files..."):
        if not (cfg := ctx.get("NGINX") or {}):
            pass

        config_path = ctx.get("LOCAL_CONFIG_PATH")
        nginx_template = os.path.join(config_path, ".nginx")
        nginx_config = util.template(nginx_template, cfg)

    with Guard("· writing config files..."):
        pass


def setup_pubpublica_access(c, ctx):
    if not (deploy_path := ctx.get("DEPLOY_PATH")):
        raise Exception("unable to locate deployed app")

    user = ctx.get("USER")
    group = ctx.get("GROUP")

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


def setup_pubpublica_virtualenv(c, ctx):
    if not (deploy_path := ctx.get("DEPLOY_PATH")):
        raise Exception("unable to locate deployed app")

    venv_dir = os.path.join(deploy_path, "venv")

    with Guard("· creating virtual environment..."):
        create_venv = f"python3.8 -m venv {venv_dir}"
        ret = c.sudo(create_venv, hide=True, warn=True)
        if not ret.ok:
            raise Exception(f"failed creating virtual environment: {ret}")

    with Guard("· updating virtual environment..."):
        pip_file = os.path.join(venv_dir, "bin", "pip3.8")
        requirements_file = os.path.join(deploy_path, "requirements.txt")
        pip_install = f"{pip_file} install -r {requirements_file}"

        ret = c.sudo(pip_install, hide=True, warn=True)
        if not ret.ok:
            raise Exception(f"failed to update the virtual environment: {ret}")


def setup_pubpublica(c, ctx):
    print("setting up pubpublica")

    if not (cfg := ctx.get("PUBPUBLICA") or {}):
        log.warning("unable to locate pubpublica config")

    local_config_path = ctx.get("LOCAL_CONFIG_PATH")
    if not os.path.isdir(local_config_path):
        raise Exception(f"local config path {local_config_path} does not exist")

    if not (app_path := ctx.get("APP_PATH")):
        raise Exception("dont know where the app is located")

    if not (deploy_path := ctx.get("DEPLOY_PATH")):
        raise Exception("unable to locate deployed app")

    if not (config_file := cfg.get("PUBPUBLICA_CONFIG_FILE")):
        raise Exception("dont know where the config_file is located")

    remote_config_file_path = os.path.join(deploy_path, config_file)

    with Guard("· building config files..."):
        template_path = os.path.join(local_config_path, config_file)
        rendered_config = util.template(template_path, {**ctx, **cfg})

    with Guard("· writing config files..."):
        config_string = json.dumps(rendered_config, indent=4)
        tmpfile = remote_config_file_path + ".new"
        fs.write_file(c, config_string, tmpfile, overwrite=True, sudo=True)
        fs.move(c, tmpfile, remote_config_file_path, sudo=True)

    setup_pubpublica_virtualenv(c, ctx)
    setup_pubpublica_access(c, ctx)

    with Guard("· linking production to new deployment..."):
        production_path = ctx.get("PRODUCTION_PATH")
        if not fs.create_symlink(c, deploy_path, production_path, force=True, sudo=True):
            raise Exception(f"failed to link {production_path} to newly deployed app")

    version_file = ctx.get("DEPLOYED_ID_FILE")
    new_version = ctx.get("ARTIFACT_ID")
    version_file_path = os.path.join(app_path, version_file)
    if not fs.write_file(c, new_version, version_file_path, overwrite=True, sudo=True):
        raise Exception("unable to write new deployment version to {version_file}")


def pre_deploy(c, local, context):
    log.info("PRE DEPLOY")
    context.update({"DEPLOY_START_TIME": util.timestamp()})
    check_local_git_repo(local, context)
    check_deployment(c, context)
    check_versions(c, context)
    check_dependencies(c, context)


def deploy(c, context):
    short_commit_hash = context.get("SHORT_COMMIT_HASH")
    log.info(f"DEPLOYING: {short_commit_hash}")

    pack_project(c, context)
    transfer_project(c, context)
    unpack_project(c, context)

    setup_redis(c, context)
    setup_nginx(c, context)

    setup_flask(c, context)
    setup_pubpublica(c, context)


def post_deploy(c, context):
    log.info("POST DEPLOY")

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

        PASS.unlock()           # TODO: only open if needed

        context = build_context(local)

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

        start_time = datetime.now()

        pre_deploy(c, local, context)
        deploy(c, context)
        post_deploy(c, context)

        # util.print_json(context)

        end_time = datetime.now()

        elapsed = end_time - start_time
        total_seconds = int(elapsed.total_seconds())
        hours, remainder = divmod(total_seconds,60*60)
        minutes, seconds = divmod(remainder,60)

        log.success(f"deployment complete, took {hours:02d}:{minutes:02d}:{seconds:02d}")
    except KeyboardInterrupt:
        pass
    except Exception as err:
        log.error(err)


if __name__ == "__main__":
    entry()
