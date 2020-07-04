import json
from os import listdir
from os.path import dirname
from os.path import exists as lexists
from os.path import realpath
from pipes import quote
from posixpath import join

import requests
from dotenv import dotenv_values
from jinja2 import Environment, FileSystemLoader

from fabric.api import abort, cd, lcd, local, puts, run, sudo, task
from fabric.colors import blue, cyan, green, red, yellow
from fabric.context_managers import shell_env
from fabric.contrib.files import exists
from fabric.operations import prompt
from fabric.state import env
from fabtools import nodejs, require
from fabtools import service as ft_service
from fabtools import supervisor, user
from fabtools.files import watch

QA = 'qa'
DEV = 'dev'
STAG = 'stag'
BETA = 'beta'
PROD = 'prod'
DATA_FILE = '.infra.json'
confs = Environment(loader=FileSystemLoader('confs'))
nginx_client = confs.get_template('nginx_client.conf')
nginx_django = confs.get_template('nginx_django.conf')
env.projects_path = dirname(dirname(realpath(__file__)))
SSH_USERS = [
    'dhilipsiva', 'rs-ds', 'jinchuuriki91', 'aadil-reckonsys', 'govindsharma7',
    'gururaj26', 'samyadh', 'praneethreckonsys', 'DHEERAJDGP']


class EnvNotSetup(Exception):
    pass


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


@task
def list_apps():
    apps = []
    for item in listdir(env.projects_path):
        infra_file = join(env.projects_path, item, DATA_FILE)
        if lexists(infra_file):
            apps.append(item)
    info('Available Apps: %s' % apps)


def setup_shell_envs():
    env.shell_envs_supervisor = ''
    env.shell_envs_dict = {'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8'}
    if env.environment == 'prod':
        path = '__KEYS__/%s/%s.env' % (env.app, env.environment)
    else:
        path = '%s/envs/%s.env' % (env.app, env.environment)
    env_file = join(env.projects_path, path)
    if not lexists(env_file):
        return error('No ENV file: %s' % env_file)
    env.shell_envs_dict.update(dotenv_values(env_file))
    lines = [
        '%s="%s"' % (key, value) for key, value in env.shell_envs_dict.items()]
    env.shell_envs_supervisor = ','.join(lines)


def setup_env(environment, app):
    '''
    Setup environment
    '''
    if app is None:
        list_apps()
        raise EnvNotSetup('Please specify app')
        abort('')
    env.app = app
    env.environment = environment
    env.app_path_local = join(env.projects_path, env.app)
    infra_file = join(env.app_path_local, DATA_FILE)
    env.infra_data = json.loads(open(infra_file).read())
    host = env.infra_data['hosts'][env.environment]
    env.user = host.get('user', 'root')
    ssh_port = host.get('ssh_port', 22)
    first_host = host['domains'][0]
    env.app_user = env.user
    env.host_string = '%s@%s:%s' % (env.user, first_host, ssh_port)
    env.home_path = user.home_directory(env.user)
    env.var_static_app = join(env.home_path, 'static', env.app)
    env.apps_path = join(env.home_path, 'apps')
    env.logs_path = join(env.home_path, 'logs')
    env.app_logs_path = join(env.logs_path, env.app)
    env.pipenv_path = join(env.home_path, '.local/bin/pipenv')
    env.app_path = join(env.apps_path, env.app)
    env.log_path = join(env.logs_path, env.app)
    setup_shell_envs()


@task
def vagrant(app=None):
    setup_env(DEV, app)


@task
def qa(app=None):
    setup_env(QA, app)


@task
def dev(app=None):
    setup_env(DEV, app)


@task
def beta(app=None):
    setup_env(BETA, app)


@task
def stag(app=None):
    setup_env(STAG, app)


@task
def prod(app=None):
    setup_env(PROD, app)


@task
def sync_auth_keys():
    '''
    Add multiple public keys to the user's authorized SSH keys from GitHub.
    '''
    if env.user == 'vagrant':
        return error('Did not run sync_auth_keys on vagrant!!! Bad Idea.')
    ssh_dir = join(user.home_directory(env.user), '.ssh')
    require.files.directory(ssh_dir, mode='700')
    authorized_keys_filename = join(ssh_dir, 'authorized_keys')
    require.files.file(authorized_keys_filename, mode='600')
    run('cat /dev/null > %s' % quote(authorized_keys_filename))
    info('Fetching public keys from GitHub')
    for gh_user in SSH_USERS:
        r = requests.get('https://api.github.com/users/%s/keys' % gh_user)
        for key in r.json():
            run('echo %s >> %s'
                % (quote(key['key']), quote(authorized_keys_filename)))
    success('Public keys synced')


def git_head_rev():
    '''
    find the commit that is currently checked out
    '''
    return local('git rev-parse HEAD', capture=True)


def git_init():
    '''
    create a git repository if necessary
    '''
    if exists('%s/.git' % env.app_path):
        return
    info('Creating new git repository ' + env.app_path)
    with cd(env.app_path):
        if run('git init').failed:
            run('git init-db')
        run('git config receive.denyCurrentBranch ignore')


def git_reset(commit=None):
    '''
    reset the working directory to a specific commit [remote]
    '''
    with cd(env.app_path):
        commit = commit or git_head_rev()
        info('Resetting to commit ' + commit)
        run('git reset --hard %s' % commit)


def git_push(commit=None):
    '''
    push to a git repository (or create if necessary)
    '''
    git_init()
    with lcd(env.app_path_local):
        commit = commit or git_head_rev()
        info('Pushing commit ' + commit)
        local('git push git+ssh://%s%s %s:refs/heads/master -f' % (
            env.host_string, env.app_path, commit))
        git_reset(commit)


def supervisor_process(service):
    _name = service['name']
    args = service['args']
    service_name = '%s_%s' % (env.app, _name)
    lines = ['[program:%(service_name)s]' % locals()]
    stderr_logfile = join(env.log_path, _name + '_supervisor_error.log')
    stdout_logfile = join(env.log_path, _name + '_supervisor_access.log')
    if service['framework'] == 'django':
        # wsgi_app = args.get('wsgi_app', _name)
        wsgi_app = '%s.wsgi:application' % env.app
        command = '%s run gunicorn -c guniconfig.py %s %s' % (
            env.pipenv_path, wsgi_app, args['port'])
    if service['framework'] == 'flask':
        command = '%s run flask run -p %s' % (env.pipenv_path, args['port'])
    elif service['framework'] == 'celery':
        command = '%s run celery -A %s worker --loglevel=info -E --concurrency=10' % (  # NOQA
            env.pipenv_path, env.app)
    elif service['framework'] == 'celery_beat':
        command = '%s run celery -A %s beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler' % (  # NOQA
            env.pipenv_path, env.app)
    params = dict(
        command=command, directory=env.app_path, stderr_logfile=stderr_logfile,
        environment=env.shell_envs_supervisor, stdout_logfile=stdout_logfile,
        autorestart=args.get('autorestart', 'true'), user=env.user,
        redirect_stderr=args.get('redirect_stderr', 'true'),
        # user=env.app_user,  # FIXME: Services should start as a system user
    )
    for key, value in sorted(params.items()):
        lines.append('%s=%s' % (key, value))
    filename = '/etc/supervisor/conf.d/%(service_name)s.conf' % locals()
    with watch(filename, callback=supervisor.update_config, use_sudo=True):
        require.file(filename, contents='\n'.join(lines), use_sudo=True)


def nginx_conf(service, template):
    kwargs = {}
    params = {}
    args = service.get('args', {})
    if service['framework'] in ['django', 'flask']:
        kwargs = dict(proxy_url='http://127.0.0.1:%s' % args['port'])
    params = {
        'ssl': args.get('ssl', 'certbot'),
        'htpasswd': args.get('htpasswd', False),
        'extra_nginx_confs': args.get('extra_nginx_confs', ''),
        'nginx_cors': args.get('nginx_cors')
    }
    _env = env.infra_data['hosts'][env.environment]
    for domain in _env['domains']:
        tpl = template.render(**params)
        require.nginx.site(
            domain, docroot=env.app_path, template_contents=tpl,
            var_static_app=env.var_static_app, **kwargs)


def setup_service_django(service):
    supervisor_process(service)
    nginx_conf(service, nginx_django)


def setup_service_celery(service):
    supervisor_process(service)


def setup_service_angular(service):
    nginx_conf(service, nginx_client)


def setup_service(service):
    framework = service['framework']
    _setup_service = {
        'django': setup_service_django,
        'flask': setup_service_django,
        'celery': setup_service_celery,
        'angular': setup_service_angular,
    }.get(framework)
    return _setup_service(service)


def setup_services():
    for service in env.infra_data['services']:
        setup_service(service)


def ensure_deps_python():
    require.deb.packages(['python3-pip'])
    run('pip3 install --user pipenv')
    # sudo('-H pip3 install -U pipenv')


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
    run('%s install -d' % env.pipenv_path)


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
    info("Executing: one_offs_python")
    if env.infra_data.get('one_offs_python'):
        for command in env.infra_data.get('one_offs_python', []):
            run('{0} run ./manage.py {1}'.format(env.pipenv_path, command))
    else:
        run('%s run ./manage.py migrate' % env.pipenv_path)
    run('%s run ./manage.py collectstatic --no-input' % env.pipenv_path)
    for command in env.infra_data.get('more_one_offs_python', []):
        run('{0} run ./manage.py {1}'.format(env.pipenv_path, command))


def one_offs_node():
    run('yarn build:%s' % env.environment)


def one_offs():
    info("Detecting: one_offs")
    language = env.infra_data['language']
    _one_offs = {
        'python': one_offs_python,
        'node': one_offs_node,
    }.get(language)
    return _one_offs()


@task
def setup_certbot():
    require.file('/var/do.ini', source='../__KEYS__/DO_staging.ini')
    require.deb.uptodate_index()
    require.deb.packages([
        'software-properties-common', 'python3-certbot-dns-digitalocean'])
    sudo('add-apt-repository universe')
    sudo('add-apt-repository ppa:certbot/certbot')
    require.deb.uptodate_index()
    sudo('apt-get install certbot python-certbot-nginx')
    sudo(
        'certbot certonly -a dns-digitalocean -i nginx'
        ' -d "*.reckonsys.xyz" -d reckonsys.xyz'
        ' --server https://acme-v02.api.letsencrypt.org/directory')


@task
def setup_redis():
    require.redis.instance('0')


@task
def setup_postgres():
    dbuser = prompt("Please enter a username:")
    password = prompt("Please enter the password:")
    dbname = prompt("Please enter the DB name:")
    require.files.directory('/var/lib/locales/supported.d/')
    require.postgres.server()
    require.postgres.user(dbuser, password=password, encrypted_password=True)
    require.postgres.database(dbname, owner=dbuser)


@task
def setup():
    info('[setup] Starting Setup: %s -> %s' % (env.app, env.host_string))
    require.deb.uptodate_index()
    require.deb.packages(['supervisor', 'libgraphviz-dev'])
    ensure_deps()
    require.users.user(env.app_user, system=True)
    require.files.directories([
        env.app_path, env.var_static_app, env.app_logs_path])
    # require.file('/var/.htpasswd', source='.htpasswd')
    git_push()
    with cd(env.app_path), shell_env(**env.shell_envs_dict):
        ensure_packages()
        one_offs()
    setup_services()
    success('[setup] Finished Setup: %s -> %s' % (env.app, env.host_string))


@task
def deploy():
    info('[deploy] Starting Deploy: %s -> %s' % (env.app, env.host_string))
    git_push()
    with cd(env.app_path), shell_env(**env.shell_envs_dict):
        ensure_packages()
        one_offs()
    supervisor.update_config()
    supervisor.restart_process('all')
    ft_service.restart('nginx')
    success('[deploy] Finished Deploy: %s -> %s' % (env.app, env.host_string))


@task
def ping():
    info('[ping] Starting Ping: %s -> %s' % (env.app, env.host_string))
    run('echo pong')
    success('[ping] Finished Ping: %s -> %s' % (env.app, env.host_string))
