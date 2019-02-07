import json
from os import listdir
from pipes import quote
from posixpath import join
from os.path import dirname, realpath, exists as lexists

import requests
from jinja2 import Environment, FileSystemLoader

from fabric.state import env
from fabric.contrib.files import exists
from fabric.colors import blue, green, red, yellow, cyan
from fabric.api import (
    cd, run, puts, task, lcd, local, abort, sudo, hide, settings)

from fabtools.files import watch
from fabtools.utils import run_as_root
from fabtools import user, require, supervisor, nodejs, service as ft_service

confs = Environment(loader=FileSystemLoader('confs'))

DATA_FILE = '.infra.json'
SSH_USERS = [
    'dhilipsiva', 'rs-ds', 'jinchuuriki91', 'aadil-reckonsys', 'govindsharma7']
env.projects_path = dirname(dirname(realpath(__file__)))

nginx_client = confs.get_template('nginx_client.conf')
nginx_django = confs.get_template('nginx_django.conf')


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


@task
def list_apps():
    apps = []
    for item in listdir(env.projects_path):
        infra_file = join(env.projects_path, item, DATA_FILE)
        if lexists(infra_file):
            apps.append(item)
    info("Available Apps: %s" % apps)


def process_status(name):
    """
    Get the status of a supervisor process.
    """
    with settings(
            hide('running', 'stdout', 'stderr', 'warnings'), warn_only=True):
        res = run_as_root(
            "supervisorctl --no-pager status %(name)s" % locals())
        if res.startswith("No such process"):
            return None
        else:
            return res.split()[1]


def setup_env():
    '''
    Setup environment
    '''
    if not (hasattr(env, 'app') and hasattr(env, 'environment')):
        raise EnvNotSetup("Please specify app and environment")
        abort("")
    env.app_path_local = join(env.projects_path, env.app)
    infra_file = join(env.app_path_local, DATA_FILE)
    env.infra_data = json.loads(open(infra_file).read())
    host = env.infra_data["hosts"][env.environment]
    env.user = host.get('user', 'root')
    ssh_port = host.get('ssh_port', 22)
    first_host = host['domains'][0]
    env.app_user = env.user
    env.host_string = '%s@%s:%s' % (env.user, first_host, ssh_port)
    env.var_static_app = join('/var/static', env.app)
    env.home_path = user.home_directory(env.user)
    env.apps_path = join(env.home_path, 'apps')
    env.logs_path = '/var/log'
    env.app_logs_path = join(env.logs_path, env.app)
    env.pipenv_path = join(env.home_path, '.local/bin/pipenv')
    env.app_path = join(env.apps_path, env.app)
    env.log_path = join(env.logs_path, env.app)


@task
def vagrant(app=None):
    env.environment = 'dev'


@task
def staging():
    env.environment = 'stag'


@task
def transportsimple():
    env.app = 'transportsimple'


@task
def transportsimple_web():
    env.app = 'transportsimple_web'


@task
def golden_sherpa():
    env.app = 'golden_sherpa'


@task
def golden_sherpa_client():
    env.app = 'golden_sherpa_client'


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
    require.files.file(authorized_keys_filename, mode='600')
    run('cat /dev/null > %s' % quote(authorized_keys_filename))
    info("Fetching public keys from GitHub")
    for gh_user in SSH_USERS:
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


def read_environment():
    if env.environment == 'prod':
        path = '__KEYS__/%s/%s.env' % (env.app, env.environment)
    else:
        path = '%s/envs/%s.env' % (env.app, env.environment)
    env_file = join(env.projects_path, path)
    if not lexists(env_file):
        error('No ENV file: %s' % env_file)
        return ''
    lines = [line.strip() for line in open(env_file).readlines()]
    return ",".join(lines)


def supervisor_process(service):
    lines = []
    _name = service['name']
    args = service['args']
    service_name = "%s_%s" % (env.app, _name)
    wsgi_app = args.get('wsgi_app', _name)
    wsgi_app = "%s.wsgi:application" % _name
    command = "%s run gunicorn -c guniconfig.py %s %s" % (
        env.pipenv_path, wsgi_app, args['port'])
    stderr_logfile = join(env.log_path, _name + '_supervisor_error.log')
    stdout_logfile = join(env.log_path, _name + '_supervisor_access.log')
    params = dict(
        command=command, directory=env.app_path, stderr_logfile=stderr_logfile,
        environment=read_environment(), stdout_logfile=stdout_logfile,
        autorestart=args.get('autorestart', 'true'),
        redirect_stderr=args.get('redirect_stderr', 'true'),
        # user=env.app_user,
    )
    lines.append('[program:%(service_name)s]' % locals())
    for key, value in sorted(params.items()):
        lines.append("%s=%s" % (key, value))
    filename = '/etc/supervisor/conf.d/%(service_name)s.conf' % locals()
    with watch(filename, callback=supervisor.update_config, use_sudo=True):
        require.file(filename, contents='\n'.join(lines), use_sudo=True)


def setup_service_django(service):
    supervisor_process(service)
    args = service['args']
    _env = env.infra_data['hosts'][env.environment]
    for domain in _env['domains']:
        template = nginx_django.render()
        require.nginx.site(
            domain, proxy_url='http://127.0.0.1:%s' % args['port'],
            docroot=env.app_path, template_contents=template,
            var_static_app=env.var_static_app,
        )


def setup_service_angular(service):
    _env = env.infra_data['hosts'][env.environment]
    for domain in _env['domains']:
        require.nginx.site(
            domain, template_contents=nginx_client, docroot=env.app_path,
            extra_ngx_config=service.get('extra_ngx_config', ''),
            var_static_app=env.var_static_app)


def setup_service(service):
    framework = service['framework']
    _setup_service = {
        "django": setup_service_django,
        "angular": setup_service_angular,
    }.get(framework)
    return _setup_service(service)


def setup_services():
    for service in env.infra_data['services']:
        setup_service(service)


def ensure_deps_python():
    require.deb.packages(['python3-pip'])
    run('pip3 install --user pipenv')


def ensure_deps_node():
    sudo('curl -sL https://deb.nodesource.com/setup_10.x | bash -')
    require.deb.packages(['nodejs'])
    for package in ['yarn', '@angular/cli']:
        nodejs.install_package(package)


def ensure_deps():
    language = env.infra_data['language']
    _ensure_deps = {
        'python': ensure_deps_python,
        'node': ensure_deps_node,
    }.get(language)
    return _ensure_deps()


def ensure_packages_python():
    run("%s install -d" % env.pipenv_path)


def ensure_packages_node():
    nodejs.install_dependencies(npm='yarn')


def ensure_packages():
    language = env.infra_data['language']
    _ensure_packages = {
        'python': ensure_packages_python,
        'node': ensure_packages_node,
    }.get(language)
    return _ensure_packages()


def one_offs_python():
    run("%s run ./manage.py migrate" % env.pipenv_path)
    # run("%s run ./manage.py seed_db" % env.pipenv_path)
    run("%s run ./manage.py collectstatic" % env.pipenv_path)


def one_offs_node():
    run("yarn build:%s" % env.environment)


def one_offs():
    language = env.infra_data['language']
    _one_offs = {
        'python': one_offs_python,
        'node': one_offs_node,
    }.get(language)
    return _one_offs


@task
def setup_certbot():
    setup_env()
    require.deb.uptodate_index()
    require.deb.packages(['software-properties-common'])
    sudo('add-apt-repository universe')
    sudo('add-apt-repository ppa:certbot/certbot')
    require.deb.uptodate_index()
    sudo('apt-get install certbot python-certbot-nginx')
    sudo('certbot --nginx')


@task
def setup():
    setup_env()
    info('[setup] Starting Setup: %s -> %s' % (env.app, env.host_string))
    require.deb.uptodate_index()
    require.deb.packages(['supervisor'])
    ensure_deps()
    require.users.user(env.app_user, system=True)
    require.files.directories([
        env.app_path, env.var_static_app, env.app_logs_path])
    require.file('/var/.htpasswd', source='.htpasswd')
    git_push()
    with cd(env.app_path):
        ensure_packages()
        one_offs()
    setup_services()
    success('[setup] Finished Setup: %s -> %s' % (env.app, env.host_string))


@task
def deploy():
    setup_env()
    info('[deploy] Starting Deploy: %s -> %s' % (env.app, env.host_string))
    git_push()
    with cd(env.app_path):
        ensure_packages()
        one_offs()
    supervisor.update_config()
    supervisor.restart_process('all')
    ft_service.restart('nginx')
    success('[deploy] Finished Deploy: %s -> %s' % (env.app, env.host_string))


@task
def ping():
    setup_env()
    info('[ping] Starting Ping: %s -> %s' % (env.app, env.host_string))
    run("echo pong")
    success('[ping] Finished Ping: %s -> %s' % (env.app, env.host_string))
