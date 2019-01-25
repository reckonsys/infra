import json
from os import listdir
from pipes import quote
from posixpath import join
from os.path import dirname, realpath, exists as lexists

from fabric.state import env
from fabric.contrib.files import exists
from fabric.colors import blue, green, red, yellow, cyan
from fabric.api import cd, run, puts, task, lcd, local, abort, sudo

import requests
from fabtools import vagrant as _vagrant
from fabtools import user, require, supervisor, nodejs

DATA_FILE = '.infra.json'
SSH_USERS = ['rs-ds']
env.projects_path = dirname(dirname(realpath(__file__)))


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
    env.apps_path = join(env.home_path, 'apps')
    env.logs_path = join(env.home_path, 'logs')
    env.envs_path = join(env.home_path, 'envs')
    env.pipenv_path = join(env.home_path, '.local/bin/pipenv')
    env.app_path = join(env.apps_path, env.app)
    env.log_path = join(env.logs_path, env.app)
    env.env_path = join(env.envs_path, env.app)
    env.app_path_local = join(env.projects_path, env.app)
    env.infra_file = join(env.app_path_local, DATA_FILE)


@task
def get_apps():
    apps = []
    for item in listdir(env.projects_path):
        infra_file = join(env.projects_path, item, DATA_FILE)
        if lexists(infra_file):
            apps.append(item)
    return apps


@task
def vagrant(app=None):
    _vagrant.vagrant()
    env.environment = 'dev'
    if app is None:
        apps = get_apps()
        info("Usage: fab vagrant:<app> deploy")
        info("Available Apps: %s" % apps)
        abort("Please specify an app to deploy!")
    setup_env(app)


@task
def sync_auth_keys():
    """
    Add multiple public keys to the user's authorized SSH keys from GitHub.
    """
    if env.user == 'vagrant':
        return error("Did not run sync_auth_keys on vagrant!!! Bad Idea.")
    ssh_dir = join(user.home_directory(env.user), '.ssh')
    require.files.directory(ssh_dir, mode='700')
    authorized_keys_filename = join(ssh_dir, 'authorized_keys')
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


def git_push(commit=None):
    """
    push to a git repository (or create if necessary)
    """
    git_init()
    with lcd(env.app_path_local):
        commit = commit or git_head_rev()
        info('Pushing commit ' + commit)
        local('git push git+ssh://%s%s %s:refs/heads/master -f' % (
            env.host_string, env.app_path, commit))
        git_reset(commit)


def get_infra_data():
    return json.loads(open(env.infra_file).read())


def read_environment():
    env_file = join(
        env.projects_path, '__KEYS__/%s/%s.env' % (env.app, env.environment))
    if not lexists(env_file):
        error('No ENV file: %s' % env_file)
        return ''
    return ",".join(open(env_file).readlines())


def setup_service_django(service):
    args = service['args']
    _name = service['name']
    service_name = "%s_%s" % (env.app, _name)
    command = "%s run gunicorn -c guniconfig.py %s %s" % (
        env.pipenv_path, args['wsgi_app'], args['port'])
    supervisor_log = join(env.log_path, _name + '_supervisor.log')
    require.supervisor.process(
        service_name,
        command=command,
        directory=env.app_path,
        environment=read_environment(),
        # user=env.app_user,
        user=env.user,
        stdout_logfile=supervisor_log,
    )
    for domain in args['domains']:
        require.nginx.proxied_site(
            domain, proxy_url='http://127.0.0.1:%s' % args['port'],
            docroot=env.app_path,
        )


def setup_service_angular(service):
    args = service['args']
    _name = service['name']
    service_name = "%s_%s" % (env.app, _name)
    command = "ng build"
    supervisor_log = join(env.log_path, _name + '_supervisor.log')
    require.supervisor.process(
        service_name, command=command, directory=env.app_path,
        environment=read_environment(), user=env.user, startretries=1,
        stdout_logfile=supervisor_log, startsecs=0, autorestart=False,
        # user=env.app_user,
    )
    for domain in args['domains']:
        CONFIG_TPL = '''
            server {
                listen      80;
                server_name %(server_name)s;
                root        %(docroot)s;
                access_log  /var/log/nginx/%(server_name)s.log;
            }'''
        require.nginx.site(
            domain, template_contents=CONFIG_TPL, docroot=env.app_path)


setup_service = {
    "django": setup_service_django,
    "angular": setup_service_angular,
}


def setup_services(data):
    for service in data['services']:
        framework = service['framework']
        setup_service[framework](service)


def ensure_deps_python():
    require.deb.packages(['python3-pip'])
    run('pip3 install --user pipenv')


def ensure_deps_node():
    sudo('curl -sL https://deb.nodesource.com/setup_10.x | bash -')
    require.deb.packages(['nodejs'])
    for package in ['yarn', '@angular/cli']:
        nodejs.install_package(package)


ensure_deps = {
    'python': ensure_deps_python,
    'node': ensure_deps_node,
}


def ensure_packages_python():
    run("pipenv install")
    run("pipenv run ./manage.py migrate")


def ensure_packages_node():
    nodejs.install_dependencies(npm='yarn')


ensure_packages = {
    'python': ensure_packages_python,
    'node': ensure_packages_node,
}


@task
def setup():
    if not hasattr(env, 'app'):
        raise EnvNotSetup("Please setup the env (setup_env)")
    data = get_infra_data()
    language = data['language']
    info('Starting Deployment for %s in %s' % (env.app, env.host_string))
    require.deb.uptodate_index()
    ensure_deps[language]()
    require.users.user(env.app_user, system=True)
    require.files.directories([env.app_path, env.log_path, env.env_path])
    git_push()
    with cd(env.app_path):
        ensure_packages[language]()
    setup_services(data)


@task
def deploy():
    git_push()
    supervisor.reload_config()


@task
def echo_test():
    _vagrant.vagrant()
