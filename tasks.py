from invoke import task
from fabric import Connection


@task
def echo(c, app, env):
    print(c, app, env)
    cxn = Connection('beta.goldensherpa.com', user='root')
    result = cxn.run('uname -s', hide=True)
    print(
        f'Ran {result.command!r} on {result.connection.host},'
        f' got stdout:\n{result.stdout}')

@task
def sync_auth_keys(c, app, env):
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
