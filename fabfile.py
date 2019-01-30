import json
from os import listdir
from pipes import quote
from posixpath import join
from os.path import dirname, realpath, exists as lexists

from fabric.state import env
from fabric.contrib.files import exists
from fabric.colors import blue, green, red, yellow, cyan
from fabric.api import (
    cd, run, puts, task, lcd, local, abort, sudo, hide, settings)

import requests
from fabtools.files import watch
from fabtools.utils import run_as_root
from fabtools import user, require, supervisor, nodejs, service as ft_service


DATA_FILE = '.infra.json'
SSH_USERS = ['dhilipsiva', 'rs-ds', 'jinchuuriki91', 'aadil-reckonsys']
env.projects_path = dirname(dirname(realpath(__file__)))

NGX_STATIC_TPL = '''
server {
    listen 80;
    index index.html;
    server_name %(server_name)s;
    root %(var_static_app)s;
    access_log /var/log/nginx/%(server_name)s-access.log;
    error_log /var/log/nginx/%(server_name)s-error.log;

    location / {
        # try_files $uri $uri/ /index.html;
    }

    %(extra_ngx_config)s
}
'''

NGX_SERVER_TPL = """
server {
    listen 80;
    server_name %(server_name)s;
    gzip_vary on;
    root %(var_static_app)s;
    try_files $uri @proxied;
    error_log /var/log/nginx/%(server_name)s-access.log;
    access_log /var/log/nginx/%(server_name)s-error.log;

    location /static {
        alias %(var_static_app)s;
    }

    location @proxied {
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Host $http_host;
        proxy_redirect off;
        proxy_pass %(proxy_url)s;
    }

    %(extra_ngx_config)s
}
"""


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
    env.infra_file = join(env.app_path_local, DATA_FILE)
    data = get_infra_data()
    host = data["hosts"][env.environment]
    env.user = host['user']
    env.app_user = env.user
    env.host_string = '%s@%s' % (host['user'], host['host'])
    env.var_static_app = join('/var/static', env.app)
    env.home_path = user.home_directory(env.user)
    env.apps_path = join(env.home_path, 'apps')
    # env.logs_path = join(env.home_path, 'logs')
    env.logs_path = '/var/log'
    env.app_logs_path = join(env.logs_path, env.app)
    env.pipenv_path = join(env.home_path, '.local/bin/pipenv')
    env.app_path = join(env.apps_path, env.app)
    env.log_path = join(env.logs_path, env.app)
    return data


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


def get_infra_data():
    return json.loads(open(env.infra_file).read())


def read_environment():
    env_file = join(
        env.projects_path, '__KEYS__/%s/%s.env' % (env.app, env.environment))
    if not lexists(env_file):
        error('No ENV file: %s' % env_file)
        return ''
    return ",".join(open(env_file).readlines())


def supervisor_process(name, **kwargs):
    lines = []
    params = {}
    params.update(kwargs)
    params.setdefault('autorestart', 'true')
    params.setdefault('redirect_stderr', 'true')
    lines.append('[program:%(name)s]' % locals())
    for key, value in sorted(params.items()):
        lines.append("%s=%s" % (key, value))
    filename = '/etc/supervisor/conf.d/%(name)s.conf' % locals()
    with watch(filename, callback=supervisor.update_config, use_sudo=True):
        require.file(filename, contents='\n'.join(lines), use_sudo=True)


def setup_service_django(service):
    _name = service['name']
    args = service['args'][env.environment]
    service_name = "%s_%s" % (env.app, _name)
    command = "%s run gunicorn -c guniconfig.py %s %s" % (
        env.pipenv_path, args['wsgi_app'], args['port'])
    stderr_logfile = join(env.log_path, _name + '_supervisor_error.log')
    stdout_logfile = join(env.log_path, _name + '_supervisor_access.log')
    supervisor_process(
        service_name,
        command=command,
        directory=env.app_path,
        environment=read_environment(),
        # user=env.app_user,
        user=env.user,
        stdout_logfile=stdout_logfile,
        stderr_logfile=stderr_logfile
    )
    for domain in args['domains']:
        require.nginx.site(
            domain, proxy_url='http://127.0.0.1:%s' % args['port'],
            docroot=env.app_path, template_contents=NGX_SERVER_TPL,
            var_static_app=env.var_static_app,
            extra_ngx_config=service.get('extra_ngx_config', '')
        )


def setup_service_angular(service):
    args = service['args'][env.environment]
    for domain in args['domains']:
        require.nginx.site(
            domain, template_contents=NGX_STATIC_TPL, docroot=env.app_path,
            extra_ngx_config=service.get('extra_ngx_config', ''),
            var_static_app=env.var_static_app)


def setup_service(framework, service):
    _setup_service = {
        "django": setup_service_django,
        "angular": setup_service_angular,
    }.get(framework)
    return _setup_service(service)


def setup_services(data):
    for service in data['services']:
        framework = service['framework']
        setup_service(framework, service)


def ensure_deps_python():
    require.deb.packages(['python3-pip'])
    run('pip3 install --user pipenv')


def ensure_deps_node():
    sudo('curl -sL https://deb.nodesource.com/setup_10.x | bash -')
    require.deb.packages(['nodejs'])
    for package in ['yarn', '@angular/cli']:
        nodejs.install_package(package)


def ensure_deps(language):
    _ensure_deps = {
        'python': ensure_deps_python,
        'node': ensure_deps_node,
    }.get(language)
    return _ensure_deps()


def ensure_packages_python():
    run("%s install -d" % env.pipenv_path)


def ensure_packages_node():
    nodejs.install_dependencies(npm='yarn')


def ensure_packages(language):
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
    run("yarn build-var")


def one_offs(language):
    _one_offs = {
        'python': one_offs_python,
        'node': one_offs_node,
    }.get(language)
    return _one_offs


@task
def setup():
    info('Starting Deployment for %s in %s' % (env.app, env.host_string))
    data = setup_env()
    language = data['language']
    info('Starting Deployment for %s in %s' % (env.app, env.host_string))
    require.deb.uptodate_index()
    require.deb.packages(['supervisor'])
    ensure_deps(language)
    require.users.user(env.app_user, system=True)
    require.files.directories([
        env.app_path, env.var_static_app, env.app_logs_path])
    require.file('/var/.htpasswd', source='.htpasswd')
    git_push()
    with cd(env.app_path):
        ensure_packages(language)
        one_offs(language)
    setup_services(data)


@task
def deploy():
    data = setup_env()
    language = data['language']
    git_push()
    with cd(env.app_path):
        ensure_packages(language)
        one_offs(language)
    supervisor.update_config()
    supervisor.restart_process('all')
    ft_service.restart('nginx')


@task
def ping():
    setup_env()
    run("echo pong")
