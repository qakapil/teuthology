import contextlib
import copy
import logging
import time
import os

from cStringIO import StringIO
from teuthology import misc as teuthology
from teuthology import contextutil
from teuthology.parallel import parallel
from ..orchestra import run

log = logging.getLogger(__name__)

# Should the RELEASE value get extracted from somewhere?
RELEASE = "1-0"

# This is intended to be a complete listing of ceph packages. If we're going
# to hardcode this stuff, I don't want to do it in more than once place.
rpm_packages = {'ceph': [
    'ceph-debuginfo',
    'ceph-radosgw',
    'ceph-test',
    'ceph-devel',
    'ceph',
    'ceph-fuse',
    #'rest-bench',
    #'libcephfs_jni1',
    'libcephfs1',
    'python-ceph',
]}





def purge_data(ctx):
    """
    Purge /var/lib/ceph on every remote in ctx.

    :param ctx: the argparse.Namespace object
    """
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            p.spawn(_purge_data, remote)
            


def _purge_data(remote):
    """
    Purge /var/lib/ceph on remote.

    :param remote: the teuthology.orchestra.remote.Remote object
    """
    log.info('Purging /var/lib/ceph on %s', remote)
    remote.run(args=[
        'sudo',
        'rm', '-rf', '--one-file-system', '--', '/var/lib/ceph',
        run.Raw('||'),
        'true',
        run.Raw(';'),
        'test', '-d', '/var/lib/ceph',
        run.Raw('&&'),
        'sudo',
        'find', '/var/lib/ceph',
        '-mindepth', '1',
        '-maxdepth', '2',
        '-type', 'd',
        '-exec', 'umount', '{}', ';',
        run.Raw(';'),
        'sudo',
        'rm', '-rf', '--one-file-system', '--', '/var/lib/ceph',
    ])



def _remove_sources_list_rpm(remote, proj):
    """
    Removes /etc/yum.repos.d/{proj}.repo, /var/lib/{proj}, and /var/log/{proj}.

    :param remote: the teuthology.orchestra.remote.Remote object
    :param proj: the project whose sources.list needs removing
    """
    remote.run(
        args=[
            'sudo', 'rm', '-f', '/etc/zypp/repos.d/{proj}.repo'.format(
                proj=proj),
            run.Raw('||'),
            'true',
        ],
        stdout=StringIO(),
    )
    # FIXME
    # There probably should be a way of removing these files that is
    # implemented in the yum/rpm remove procedures for the ceph package.
    # FIXME but why is this function doing these things?
    remote.run(
        args=[
            'sudo', 'rm', '-fr', '/var/lib/{proj}'.format(proj=proj),
            run.Raw('||'),
            'true',
        ],
        stdout=StringIO(),
    )
    remote.run(
        args=[
            'sudo', 'rm', '-fr', '/var/log/{proj}'.format(proj=proj),
            run.Raw('||'),
            'true',
        ],
        stdout=StringIO(),
    )





def remove_sources(ctx, config):
    """
    Removes repo source files from each remote in ctx.

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """
    log.info("Removing {proj} sources lists".format(
        proj=config.get('project', 'ceph')))
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            system_type = teuthology.get_system_type(remote)
            p.spawn(_remove_sources_list_rpm,
                     remote, config.get('project', 'ceph'))
            p.spawn(_remove_sources_list_rpm,
                     remote, 'calamari')






def _remove_rpm(ctx, config, remote, rpm):
    """
    Removes RPM packages from remote

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    :param remote: the teuthology.orchestra.remote.Remote object
    :param rpm: list of packages names to remove
    """
    for pkg in rpm:
        pk_err_mess = StringIO()
        remote.run(args=['sudo', 'zypper', '--non-interactive', 
                    '--no-gpg-checks', '--quiet', 'remove', pkg, ],
                    stderr=pk_err_mess)
    
    
    
def remove_packages(ctx, config, pkgs):
    """
    Removes packages from each remote in ctx.

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    :param pkgs: list of packages names to remove
    """
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            system_type = teuthology.get_system_type(remote)
            p.spawn(_remove_rpm, 
                    ctx, config, remote, pkgs[system_type])
            
            
            

def _get_os_version(ctx, remote, config):
    os_alias = {'openSUSE project':'openSUSE_',
               'SUSE LINUX':'SLE_'}
    
    r = remote.run(
        args=['lsb_release', '-is'],
        stdout=StringIO(),
    )
    os_name = r.stdout.getvalue().strip()
    log.info("os name is %s" % (os_name))
    
    assert os_name in os_alias.keys(), \
        "unknown os found %s" %(os_name)
    
    version_alias = {'11':'11_SP3/', '12':'12/', '13.1':'13.1/'}
    r = remote.run(
        args=['lsb_release', '-rs'],
        stdout=StringIO(),
    )
    
    os_version = r.stdout.getvalue().strip()
    log.info("os version is %s" % (os_version))
    assert os_version in version_alias.keys(), \
        "unknown os found %s" %(os_version)
        
    retval = os_alias[os_name]+version_alias[os_version]
    log.info("os is %s" % (retval))
    return retval
    
    
    
def _add_repo(remote,baseurl,reponame):
    err_mess = StringIO()
    try:
        remote.run(args=['sudo', 'zypper', 'removerepo', reponame, ], stderr=err_mess, )
    except Exception:
        cmp_msg = 'Repository ceph not found by alias, number or URI.'
        if cmp_msg != err_mess.getvalue().strip():
            raise
        
    r = remote.run(
        args=['sudo', 'zypper', '--gpg-auto-import-keys',
               '--non-interactive', 'refresh'],
        stdout=StringIO(),
    )
        
    r = remote.run(
        args=['sudo', 'zypper', 'ar', baseurl, reponame],
        stdout=StringIO(),
    )
    
    r = remote.run(
        args=['sudo', 'zypper', '--gpg-auto-import-keys',
               '--non-interactive', 'refresh'],
        stdout=StringIO(),
    )



    
def _update_rpm_package_list_and_install(ctx, remote, rpm, config):
    baseparms = _get_os_version(ctx, remote, config)
    log.info("Installing packages: {pkglist} on remote os {os}".format(
        pkglist=", ".join(rpm), os=baseparms))
    host = ctx.teuthology_config.get('gitbuilder_host',
            'download.suse.de/ibs/Devel:/Storage:/0.5:/Staging/')
    baseurl = host+baseparms
    _add_repo(remote,baseurl,'ceph')
    
    for pkg in rpm:
        pk_err_mess = StringIO()
        remote.run(args=['sudo', 'zypper', '--non-interactive', 
                    '--no-gpg-checks', '--quiet', 'install', pkg, ],
                    stderr=pk_err_mess)
    
    
    
    
    
    
def install_packages(ctx, pkgs, config):
    """
    Installs packages on each remote in ctx.

    :param ctx: the argparse.Namespace object
    :param pkgs: list of packages names to install
    :param config: the config dict
    """
  
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            p.spawn(
                _update_rpm_package_list_and_install,
                ctx, remote, pkgs['rpm'], config)


@contextlib.contextmanager
def install(ctx, config):
    """
    The install task. Installs packages for a given project on all hosts in
    ctx. May work for projects besides ceph, but may not. Patches welcomed!

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """

    project = config.get('project', 'ceph')

    global rpm_packages
    rpm = rpm_packages.get(project, [])

    # pull any additional packages out of config
    extra_pkgs = config.get('extra_packages')
    log.info('extra packages: {packages}'.format(packages=extra_pkgs))
    rpm += extra_pkgs

    # the extras option right now is specific to the 'ceph' project
    extras = config.get('extras')
    if extras is not None:
        rpm = ['ceph-fuse', 'librbd1', 'librados2', 'ceph-test', 'python-ceph']

    # install lib deps (so we explicitly specify version), but do not
    # uninstall them, as other packages depend on them (e.g., kvm)
    proj_install_debs = {'ceph': [
        'librados2',
        'librados2-dbg',
        'librbd1',
        'librbd1-dbg',
    ]}

    proj_install_rpm = {'ceph': [
        'librbd1',
        'librados2',
    ]}

    install_debs = proj_install_debs.get(project, [])
    install_rpm = proj_install_rpm.get(project, [])

    install_info = {
        "rpm": rpm + install_rpm}
    remove_info = {
        "rpm": rpm}
    install_packages(ctx, install_info, config)
    try:
        yield
    finally:
        remove_packages(ctx, config, remove_info)
        remove_sources(ctx, config)
        if project == 'ceph':
            purge_data(ctx)




@contextlib.contextmanager
def task(ctx, config):
    """
    Install packages for a given project.

    tasks:
    - install:
        project: ceph
        branch: bar
    - install:
        project: samba
        branch: foo
        extra_packages: ['samba']

    Overrides are project specific:

    overrides:
      install:
        ceph:
          sha1: ...

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """
    if config is None:
        config = {}
    assert isinstance(config, dict), \
        "task install only supports a dictionary for configuration"

    project, = config.get('project', 'ceph'),
    log.debug('project %s' % project)
    overrides = ctx.config.get('overrides')
    if overrides:
        install_overrides = overrides.get('install', {})
        teuthology.deep_merge(config, install_overrides.get(project, {}))
    log.debug('config %s' % config)

    # Flavor tells us what gitbuilder to fetch the prebuilt software
    # from. It's a combination of possible keywords, in a specific
    # order, joined by dashes. It is used as a URL path name. If a
    # match is not found, the teuthology run fails. This is ugly,
    # and should be cleaned up at some point.

    flavor = config.get('flavor', 'basic')

    if config.get('path'):
        # local dir precludes any other flavors
        flavor = 'local'
        log.debug('config:path ' + config.get('path'))
    else:
        if config.get('valgrind'):
            log.info(
                'Using notcmalloc flavor and running some daemons under valgrind')
            flavor = 'notcmalloc'
        else:
            if config.get('coverage'):
                log.info('Recording coverage for this run.')
                flavor = 'gcov'

    ctx.summary['flavor'] = flavor

    with contextutil.nested(
        lambda: install(ctx=ctx, config=dict(
            branch=config.get('branch'),
            tag=config.get('tag'),
            sha1=config.get('sha1'),
            flavor=flavor,
            extra_packages=config.get('extra_packages', []),
            extras=config.get('extras', None),
            wait_for_package=ctx.config.get('wait_for_package', False),
            project=project,
        )),
    ):
        yield
