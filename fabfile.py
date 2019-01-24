import json
import posixpath
from pipes import quote
from os.path import dirname, realpath

from fabric.state import env
from fabric.contrib.files import exists
from fabric.api import cd, run, puts, task, lcd, local
from fabric.colors import blue, green, red, yellow, cyan

import requests
from fabtools import user, require
from fabtools.vagrant import vagrant

vagrant = vagrant  # silence flake8

SSH_USERS = ['rs-ds']


def log(message, wrapper=blue):
    return puts(wrapper(message))


def info(message):
    return log(message, cyan)


def success(message):
    return log(message, green)


def warn(message):
    return log(message, yellow)


def error(message):
    return log(message, red)


class EnvNotSetup(Exception):
    pass


def setup_env(app):
    '''
    Setup environment
    '''
    env.app = app
    env.app_user = app
    env.home_path = user.home_directory(env.user)
    env.apps_path = posixpath.join(env.home_path, 'apps')
    env.logs_path = posixpath.join(env.home_path, 'logs')
    env.envs_path = posixpath.join(env.home_path, 'envs')
    env.app_path = posixpath.join(env.apps_path, env.app)
    env.log_path = posixpath.join(env.logs_path, env.app)
    env.env_path = posixpath.join(env.envs_path, env.app)
    env.projects_path = dirname(dirname(realpath(__file__)))
    env.app_path_local = posixpath.join(env.projects_path, env.app)
    env.pipenv_path = posixpath.join(env.home_path, '.local/bin/pipenv')


@task
def transportsimple_server():
    setup_env('transportsimple_server')


@task
def sync_auth_keys():
    """
    Add multiple public keys to the user's authorized SSH keys from GitHub.
    """
    if env.user == 'vagrant':
        return error("Did not run sync_auth_keys on vagrant!!! Bad Idea.")
    ssh_dir = posixpath.join(user.home_directory(env.user), '.ssh')
    require.files.directory(ssh_dir, mode='700')
    authorized_keys_filename = posixpath.join(ssh_dir, 'authorized_keys')
    require.files.file(
        authorized_keys_filename, mode='600')
    run('cat /dev/null > %s' % quote(authorized_keys_filename))
    for gh_user in SSH_USERS:
        info("Fetching public keys from GitHub")
        r = requests.get("https://api.github.com/users/%s/keys" % gh_user)
        for key in r.json():
            run("echo %s >> %s"
                % (quote(key["key"]), quote(authorized_keys_filename)))
        success("Public keys synced")


def git_head_rev():
    """
    find the commit that is currently checked out
    """
    return local('git rev-parse HEAD', capture=True)


def git_init():
    """
    create a git repository if necessary
    """
    if exists('%s/.git' % env.app_path):
        return
    info('Creating new git repository ' + env.app_path)
    with cd(env.app_path):
        if run('git init').failed:
            run('git init-db')
        run('git config receive.denyCurrentBranch ignore')


def git_reset(commit=None):
    """
    reset the working directory to a specific commit [remote]
    """
    with cd(env.app_path):
        commit = commit or git_head_rev()
        info('Resetting to commit ' + commit)
        run('git reset --hard %s' % commit)


def git_seed(commit=None):
    """
    seed a git repository (and create if necessary)
    """
    git_init()
    with lcd(env.app_path_local):
        commit = commit or git_head_rev()
        info('Pushing commit ' + commit)
        local('git push git+ssh://%s%s %s:refs/heads/master -f' % (
            env.host_string, env.app_path, commit))
        git_reset(commit)


def get_infra_data():
    infra_file = posixpath.join(env.app_path_local, '.infra.json')
    return json.loads(open(infra_file).read())


def setup_service_django(service):
    args = service['args']
    _name = service['name']
    service_name = "%s_%s" % (env.app, _name)
    command = "%s run gunicorn -c guniconfig.py %s %s" % (
        env.pipenv_path, args['wsgi_app'], args['port'])
    supervisor_log = posixpath.join(env.log_path, _name + '_supervisor.log')
    require.supervisor.process(
        service_name,
        command=command,
        directory=env.app_path,
        # user=env.app_user,
        user=env.user,
        stdout_logfile=supervisor_log,
    )
    for domain in args['domains']:
        require.nginx.proxied_site(
            domain, proxy_url='http://127.0.0.1:%s' % args['port'],
            docroot=env.app_path,
        )


service_types = {
    "django": setup_service_django,
}


@task
def setup_services():
    data = get_infra_data()
    for service in data['services']:
        setup_service = service_types[service['type']]
        setup_service(service)


@task
def setup():
    if not hasattr(env, 'app'):
        raise EnvNotSetup("Please setup the env (setup_env)")
    info('Starting Deployment for %s in %s' % (env.app, env.host_string))
    require.deb.uptodate_index()
    require.deb.packages(['python3-pip'])
    run('pip3 install --user pipenv')
    require.users.user(env.app_user, system=True)
    require.files.directories([
        env.app_path,
        env.log_path,
        env.env_path,
    ])
    git_seed()
    with cd(env.app_path):
        run("pipenv install")
        run("pipenv run ./manage.py migrate")
    setup_services()
