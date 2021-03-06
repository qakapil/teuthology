"""
Internal tasks are tasks that are started from the teuthology infrastructure.
Note that there is no corresponding task defined for this module.  All of
the calls are made from other modules, most notably teuthology/run.py
"""
from cStringIO import StringIO
import contextlib
import logging
import os
import time
import yaml
import re
import subprocess

from teuthology import lockstatus
from teuthology import lock
from teuthology import misc as teuthology
from teuthology.parallel import parallel
from ..orchestra import cluster, remote, run

log = logging.getLogger(__name__)


@contextlib.contextmanager
def base(ctx, config):
    """
    Create the test directory that we will be using on the remote system
    """
    log.info('Creating test directory...')
    testdir = teuthology.get_testdir(ctx)
    run.wait(
        ctx.cluster.run(
            args=[
                'mkdir', '-m0755', '--',
                testdir,
                ],
            wait=False,
            )
        )
    try:
        yield
    finally:
        log.info('Tidying up after the test...')
        # if this fails, one of the earlier cleanups is flawed; don't
        # just cram an rm -rf here
        run.wait(
            ctx.cluster.run(
                args=[
                    'rmdir',
                    '--',
                    testdir,
                    ],
                wait=False,
                ),
            )


@contextlib.contextmanager
def lock_machines(ctx, config):
    """
    Lock machines.  Called when the teuthology run finds and locks
    new machines.  This is not called if the one has teuthology-locked
    machines and placed those keys in the Targets section of a yaml file.
    """
    log.info('Locking machines...')
    assert isinstance(config[0], int), 'config[0] must be an integer'
    machine_type = config[1]
    machine_types = teuthology.get_multi_machine_types(machine_type)
    how_many = config[0]

    while True:
        # make sure there are enough machines up
        machines = lock.list_locks()
        if machines is None:
            if ctx.block:
                log.warn('error listing machines, trying again')
                time.sleep(20)
                continue
            else:
                assert 0, 'error listing machines'

        is_up = lambda machine: machine['up'] and machine['type'] in machine_types  # noqa
        num_up = len(filter(is_up, machines))
        assert num_up >= how_many, 'not enough machines are up'

        # make sure there are machines for non-automated jobs to run
        is_up_and_free = lambda machine: machine['up'] and machine['locked'] == 0 and machine['type'] in machine_types  # noqa
        up_and_free = filter(is_up_and_free, machines)
        num_free = len(up_and_free)
        if num_free < 6 and ctx.owner.startswith('scheduled'):
            if ctx.block:
                log.info(
                    'waiting for more machines to be free (need %s see %s)...',
                    how_many,
                    num_free,
                )
                time.sleep(10)
                continue
            else:
                assert 0, 'not enough machines free'

        newly_locked = lock.lock_many(ctx, how_many, machine_type, ctx.owner,
                                      ctx.archive)
        if len(newly_locked) == how_many:
            vmlist = []
            for lmach in newly_locked:
                if teuthology.is_vm(lmach):
                    vmlist.append(lmach)
            if vmlist:
                log.info('Waiting for virtual machines to come up')
                keyscan_out = ''
                loopcount = 0
                while len(keyscan_out.splitlines()) != len(vmlist):
                    loopcount += 1
                    time.sleep(10)
                    keyscan_out, current_locks = lock.keyscan_check(ctx,
                                                                    vmlist)
                    log.info('virtual machine is still unavailable')
                    if loopcount == 40:
                        loopcount = 0
                        log.info('virtual machine(s) still not up, ' +
                                 'recreating unresponsive ones.')
                        for guest in vmlist:
                            if guest not in keyscan_out:
                                log.info('recreating: ' + guest)
                                lock.destroy_if_vm(ctx, 'ubuntu@' + guest)
                                lock.create_if_vm(ctx, 'ubuntu@' + guest)
                if lock.update_keys(ctx, keyscan_out, current_locks):
                    log.info("Error in virtual machine keys")
                newscandict = {}
                for dkey in newly_locked.iterkeys():
                    stats = lockstatus.get_status(ctx, dkey)
                    newscandict[dkey] = stats['sshpubkey']
                ctx.config['targets'] = newscandict
            else:
                ctx.config['targets'] = newly_locked
            # FIXME: Ugh.
            log.info('\n  '.join(['Locked targets:', ] + yaml.safe_dump(ctx.config['targets'], default_flow_style=False).splitlines()))
            break
        elif not ctx.block:
            assert 0, 'not enough machines are available'

        log.warn('Could not lock enough machines, waiting...')
        time.sleep(10)
    try:
        yield
    finally:
        if ctx.config.get('unlock_on_failure', False) or \
           ctx.summary.get('success', False):
            log.info('Unlocking machines...')
            for machine in ctx.config['targets'].iterkeys():
                lock.unlock_one(ctx, machine, ctx.owner)

def save_config(ctx, config):
    """
    Store the config in a yaml file
    """
    log.info('Saving configuration')
    if ctx.archive is not None:
        with file(os.path.join(ctx.archive, 'config.yaml'), 'w') as f:
            yaml.safe_dump(ctx.config, f, default_flow_style=False)

def check_lock(ctx, config):
    """
    Check lock status of remote machines.
    """
    if ctx.config.get('check-locks') == False:
        log.info('Lock checking disabled.')
        return
    log.info('Checking locks...')
    for machine in ctx.config['targets'].iterkeys():
        status = lockstatus.get_status(ctx, machine)
        log.debug('machine status is %s', repr(status))
        assert status is not None, \
            'could not read lock status for {name}'.format(name=machine)
        assert status['up'], 'machine {name} is marked down'.format(name=machine)
        assert status['locked'], \
            'machine {name} is not locked'.format(name=machine)
        assert status['locked_by'] == ctx.owner, \
            'machine {name} is locked by {user}, not {owner}'.format(
            name=machine,
            user=status['locked_by'],
            owner=ctx.owner,
            )

@contextlib.contextmanager
def timer(ctx, config):
    """
    Start the timer used by teuthology
    """
    log.info('Starting timer...')
    start = time.time()
    try:
        yield
    finally:
        duration = time.time() - start
        log.info('Duration was %f seconds', duration)
        ctx.summary['duration'] = duration

def connect(ctx, config):
    """
    Open a connection to a remote host.
    """
    log.info('Opening connections...')
    remotes = []
    machs = []
    for name in ctx.config['targets'].iterkeys():
        machs.append(name)
    for t, key in ctx.config['targets'].iteritems():
        log.debug('connecting to %s', t)
        try:
            if ctx.config['sshkeys'] == 'ignore':
                key = None
        except (AttributeError, KeyError):
            pass
        if key.startswith('ssh-rsa ') or key.startswith('ssh-dss '):
            if teuthology.is_vm(t):
                key = None
        remotes.append(
            remote.Remote(name=t, host_key=key, keep_alive=True, console=None))
    ctx.cluster = cluster.Cluster()
    if 'roles' in ctx.config:
        for rem, roles in zip(remotes, ctx.config['roles']):
            assert all(isinstance(role, str) for role in roles), \
                "Roles in config must be strings: %r" % roles
            ctx.cluster.add(rem, roles)
            log.info('roles: %s - %s' % (rem, roles))
    else:
        for rem in remotes:
            ctx.cluster.add(rem, rem.name)


def serialize_remote_roles(ctx, config):
    """
    Provides an explicit mapping for which remotes have been assigned what roles
    So that other software can be loosely coupled to teuthology
    """
    if ctx.archive is not None:
        with file(os.path.join(ctx.archive, 'info.yaml'), 'r+') as info_file:
            info_yaml = yaml.safe_load(info_file)
            info_file.seek(0)
            info_yaml['cluster'] = dict([(rem.name, {'roles': roles}) for rem, roles in ctx.cluster.remotes.iteritems()])
            yaml.safe_dump(info_yaml, info_file, default_flow_style=False)


def check_ceph_data(ctx, config):
    """
    Check for old /var/lib/ceph directories and detect staleness.
    """
    log.info('Checking for old /var/lib/ceph...')
    processes = ctx.cluster.run(
        args=[
            'test', '!', '-e', '/var/lib/ceph',
            ],
        wait=False,
        )
    failed = False
    for proc in processes:
        try:
            proc.wait()
        except run.CommandFailedError:
            log.error('Host %s has stale /var/lib/ceph, check lock and nuke/cleanup.', proc.remote.shortname)
            failed = True
    if failed:
        raise RuntimeError('Stale /var/lib/ceph detected, aborting.')

def check_conflict(ctx, config):
    """
    Note directory use conflicts and stale directories.
    """
    log.info('Checking for old test directory...')
    testdir = teuthology.get_testdir(ctx)
    processes = ctx.cluster.run(
        args=[
            'test', '!', '-e', testdir,
            ],
        wait=False,
        )
    failed = False
    for proc in processes:
        try:
            proc.wait()
        except run.CommandFailedError:
            log.error('Host %s has stale test directory %s, check lock and cleanup.', proc.remote.shortname, testdir)
            failed = True
    if failed:
        raise RuntimeError('Stale jobs detected, aborting.')

@contextlib.contextmanager
def archive(ctx, config):
    """
    Handle the creation and deletion of the archive directory.
    """
    log.info('Creating archive directory...')
    archive_dir = teuthology.get_archive_dir(ctx)
    run.wait(
        ctx.cluster.run(
            args=[
                'install', '-d', '-m0755', '--', archive_dir,
                ],
            wait=False,
            )
        )

    try:
        yield
    except Exception:
        # we need to know this below
        ctx.summary['success'] = False
        raise
    finally:
        if ctx.archive is not None and \
                not (ctx.config.get('archive-on-error') and ctx.summary['success']):
            log.info('Transferring archived files...')
            logdir = os.path.join(ctx.archive, 'remote')
            if (not os.path.exists(logdir)):
                os.mkdir(logdir)
            for rem in ctx.cluster.remotes.iterkeys():
                path = os.path.join(logdir, rem.shortname)
                teuthology.pull_directory(rem, archive_dir, path)

        log.info('Removing archive directory...')
        run.wait(
            ctx.cluster.run(
                args=[
                    'rm',
                    '-rf',
                    '--',
                    archive_dir,
                    ],
                wait=False,
                ),
            )


@contextlib.contextmanager
def sudo(ctx, config):
    """
    Enable use of sudo
    """
    log.info('Configuring sudo...')
    sudoers_file = '/etc/sudoers'
    backup_ext = '.orig.teuthology'
    tty_expr = r's/^\([^#]*\) \(requiretty\)/\1 !\2/g'
    pw_expr = r's/^\([^#]*\) !\(visiblepw\)/\1 \2/g'

    run.wait(
        ctx.cluster.run(
            args="sudo sed -i{ext} -e '{tty}' -e '{pw}' {path}".format(
                ext=backup_ext, tty=tty_expr, pw=pw_expr,
                path=sudoers_file
            ),
            wait=False,
        )
    )
    try:
        yield
    finally:
        log.info('Restoring {0}...'.format(sudoers_file))
        ctx.cluster.run(
            args="sudo mv -f {path}{ext} {path}".format(
                path=sudoers_file, ext=backup_ext
            )
        )


@contextlib.contextmanager
def coredump(ctx, config):
    """
    Stash a coredump of this system if an error occurs.
    """
    log.info('Enabling coredump saving...')
    archive_dir = teuthology.get_archive_dir(ctx)
    run.wait(
        ctx.cluster.run(
            args=[
                'install', '-d', '-m0755', '--',
                '{adir}/coredump'.format(adir=archive_dir),
                run.Raw('&&'),
                'sudo', 'sysctl', '-w', 'kernel.core_pattern={adir}/coredump/%t.%p.core'.format(adir=archive_dir),
                ],
            wait=False,
            )
        )

    try:
        yield
    finally:
        run.wait(
            ctx.cluster.run(
                args=[
                    'sudo', 'sysctl', '-w', 'kernel.core_pattern=core',
                    run.Raw('&&'),
                    # don't litter the archive dir if there were no cores dumped
                    'rmdir',
                    '--ignore-fail-on-non-empty',
                    '--',
                    '{adir}/coredump'.format(adir=archive_dir),
                    ],
                wait=False,
                )
            )

        # set success=false if the dir is still there = coredumps were
        # seen
        for rem in ctx.cluster.remotes.iterkeys():
            r = rem.run(
                args=[
                    'if', 'test', '!', '-e', '{adir}/coredump'.format(adir=archive_dir), run.Raw(';'), 'then',
                    'echo', 'OK', run.Raw(';'),
                    'fi',
                    ],
                stdout=StringIO(),
                )
            if r.stdout.getvalue() != 'OK\n':
                log.warning('Found coredumps on %s, flagging run as failed', rem)
                ctx.summary['success'] = False
                if 'failure_reason' not in ctx.summary:
                    ctx.summary['failure_reason'] = \
                        'Found coredumps on {rem}'.format(rem=rem)

@contextlib.contextmanager
def syslog(ctx, config):
    """
    start syslog / stop syslog on exit.
    """
    if ctx.archive is None:
        # disable this whole feature if we're not going to archive the data anyway
        yield
        return

    log.info('Starting syslog monitoring...')

    archive_dir = teuthology.get_archive_dir(ctx)
    run.wait(
        ctx.cluster.run(
            args=[
                'mkdir', '-m0755', '--',
                '{adir}/syslog'.format(adir=archive_dir),
                ],
            wait=False,
            )
        )

    CONF = '/etc/rsyslog.d/80-cephtest.conf'
    conf_fp = StringIO('''
kern.* -{adir}/syslog/kern.log;RSYSLOG_FileFormat
*.*;kern.none -{adir}/syslog/misc.log;RSYSLOG_FileFormat
'''.format(adir=archive_dir))
    try:
        for rem in ctx.cluster.remotes.iterkeys():
            teuthology.sudo_write_file(
                remote=rem,
                path=CONF,
                data=conf_fp,
                )
            conf_fp.seek(0)
        run.wait(
            ctx.cluster.run(
                args=[
                    'sudo',
                    'service',
                    # a mere reload (SIGHUP) doesn't seem to make
                    # rsyslog open the files
                    'rsyslog',
                    'restart',
                    ],
                wait=False,
                ),
            )

        yield
    finally:
        log.info('Shutting down syslog monitoring...')

        run.wait(
            ctx.cluster.run(
                args=[
                    'sudo',
                    'rm',
                    '-f',
                    '--',
                    CONF,
                    run.Raw('&&'),
                    'sudo',
                    'service',
                    'rsyslog',
                    'restart',
                    ],
                wait=False,
                ),
            )
        # race condition: nothing actually says rsyslog had time to
        # flush the file fully. oh well.

        log.info('Checking logs for errors...')
        for rem in ctx.cluster.remotes.iterkeys():
            log.debug('Checking %s', rem.name)
            r = rem.run(
                args=[
                    'egrep', '--binary-files=text',
                    '\\bBUG\\b|\\bINFO\\b|\\bDEADLOCK\\b',
                    run.Raw('{adir}/syslog/*.log'.format(adir=archive_dir)),
                    run.Raw('|'),
                    'grep', '-v', 'task .* blocked for more than .* seconds',
                    run.Raw('|'),
                    'grep', '-v', 'lockdep is turned off',
                    run.Raw('|'),
                    'grep', '-v', 'trying to register non-static key',
                    run.Raw('|'),
                    'grep', '-v', 'DEBUG: fsize',  # xfs_fsr
                    run.Raw('|'),
                    'grep', '-v', 'CRON',  # ignore cron noise
                    run.Raw('|'),
                    'grep', '-v', 'BUG: bad unlock balance detected', # #6097
                    run.Raw('|'),
                    'grep', '-v', 'inconsistent lock state', # FIXME see #2523
                    run.Raw('|'),
                    'grep', '-v', '*** DEADLOCK ***', # part of lockdep output
                    run.Raw('|'),
                    'grep', '-v', 'INFO: possible irq lock inversion dependency detected', # FIXME see #2590 and #147
                    run.Raw('|'),
                    'grep', '-v', 'INFO: NMI handler (perf_event_nmi_handler) took too long to run',
                    run.Raw('|'),
                    'grep', '-v', 'INFO: recovery required on readonly',
                    run.Raw('|'),
                    'head', '-n', '1',
                    ],
                stdout=StringIO(),
                )
            stdout = r.stdout.getvalue()
            if stdout != '':
                log.error('Error in syslog on %s: %s', rem.name, stdout)
                ctx.summary['success'] = False
                if 'failure_reason' not in ctx.summary:
                    ctx.summary['failure_reason'] = \
                        "'{error}' in syslog".format(error=stdout)

        log.info('Compressing syslogs...')
        run.wait(
            ctx.cluster.run(
                args=[
                    'find',
                    '{adir}/syslog'.format(adir=archive_dir),
                    '-name',
                    '*.log',
                    '-print0',
                    run.Raw('|'),
                    'sudo',
                    'xargs',
                    '-0',
                    '--no-run-if-empty',
                    '--',
                    'gzip',
                    '--',
                    ],
                wait=False,
                ),
            )

def vm_setup(ctx, config):
    """
    Look for virtual machines and handle their initialization
    """
    with parallel() as p:
        editinfo = os.path.join(os.path.dirname(__file__),'edit_sudoers.sh')
        for rem in ctx.cluster.remotes.iterkeys():
            mname = re.match(".*@([^\.]*)\.?.*", str(rem)).group(1)
            if teuthology.is_vm(mname):
                r = rem.run(args=['test', '-e', '/ceph-qa-ready',],
                        stdout=StringIO(),
                        check_status=False,)
                if r.returncode != 0:
                    p1 = subprocess.Popen(['cat', editinfo], stdout=subprocess.PIPE)
                    p2 = subprocess.Popen(['ssh', '-t', '-t', str(rem), 'sudo', 'sh'], stdin=p1.stdout, stdout=subprocess.PIPE)
                    _, err = p2.communicate()
                    if err:
                        log.info("Edit of /etc/sudoers failed: %s", err)
                    p.spawn(_handle_vm_init, rem)

def _handle_vm_init(remote_):
    """
    Initialize a remote vm by downloading and running ceph_qa_chef.
    """
    log.info('Running ceph_qa_chef on %s', remote_)
    remote_.run(args=['wget', '-q', '-O-',
            'http://ceph.com/git/?p=ceph-qa-chef.git;a=blob_plain;f=solo/solo-from-scratch;hb=HEAD',
            run.Raw('|'),
            'sh',
        ])

