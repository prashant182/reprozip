# Copyright (C) 2014 New York University
# This file is part of ReproZip which is released under the Revised BSD License
# See file LICENSE for full license details.

"""Docker plugin for reprounzip.

This files contains the 'docker' unpacker, which builds a Dockerfile from a
reprozip pack. You can then build a container and run it with Docker.

See http://www.docker.io/
"""

from __future__ import unicode_literals

import argparse
import logging
import os
import pickle
from rpaths import Path, PosixPath
import subprocess
import sys
import tarfile

from reprounzip.common import Package
from reprounzip.unpackers.common import COMPAT_OK, COMPAT_MAYBE, \
    composite_action, target_must_exist, make_unique_name, shell_escape, \
    load_config, select_installer, join_root, FileUploader, FileDownloader
from reprounzip.utils import unicode_, irange, iteritems


def docker_escape(s):
    return '"%s"' % (s.replace('\\', '\\\\')
                      .replace('"', '\\"'))


def select_image(runs):
    distribution, version = runs[0]['distribution']
    distribution = distribution.lower()
    architecture = runs[0]['architecture']

    if architecture == 'i686':
        sys.stderr.write("Warning: wanted architecture was i686, but we'll "
                         "use x86_64 with Docker")
    elif architecture != 'x86_64':
        sys.stderr.write("Error: unsupported architecture %s\n" % architecture)
        sys.exit(1)

    # Ubuntu
    if distribution == 'ubuntu':
        if version != '12.04':
            sys.stderr.write("Warning: using Ubuntu 12.04 'Precise' instead "
                             "of '%s'\n" % version)
        return 'ubuntu', 'ubuntu:12.04'

    # Debian
    elif distribution != 'debian':
        sys.stderr.write("Warning: unsupported distribution %s, using Debian"
                         "\n" % distribution)
        distribution = 'debian', '7'

    if version == '6' or version.startswith('squeeze'):
        return 'debian', 'debian:squeeze'
    if version == '8' or version.startswith('jessie'):
        return 'debian', 'debian:jessie'
    else:
        if version != '7' and not version.startswith('wheezy'):
            sys.stderr.write("Warning: using Debian 7 'Wheezy' instead of '%s'"
                             "\n" % version)
        return 'debian', 'debian:wheezy'


def write_dict(filename, dct):
    to_write = {'unpacker': 'docker'}
    to_write.update(dct)
    with filename.open('wb') as fp:
        pickle.dump(to_write, fp, pickle.HIGHEST_PROTOCOL)


def read_dict(filename):
    with filename.open('rb') as fp:
        dct = pickle.load(fp)
    assert dct['unpacker'] == 'docker'
    return dct


def docker_setup_create(args):
    """Sets up the experiment to be run in a Docker-built container.
    """
    pack = Path(args.pack[0])
    target = Path(args.target[0])
    if target.exists():
        sys.stderr.write("Error: Target directory exists\n")
        sys.exit(1)

    # Loads config
    runs, packages, other_files = load_config(pack)

    if args.base_image:
        target_distribution = None
        base_image = args.base_image[0]
    else:
        target_distribution, base_image = select_image(runs)

    logging.debug("Base image: %s, distribution: %s" % (
                  base_image,
                  target_distribution or "unknown"))

    target.mkdir(parents=True)
    pack.copyfile(target / 'experiment.rpz')

    # Writes Dockerfile
    with (target / 'Dockerfile').open('w',
                                      encoding='utf-8', newline='\n') as fp:
        fp.write('FROM %s\n\n' % base_image)
        fp.write('COPY experiment.rpz /reprozip_experiment.rpz\n\n')
        fp.write('RUN \\\n')

        # Installs missing packages
        packages = [pkg for pkg in packages if not pkg.packfiles]
        # FIXME : Right now, we need 'sudo' to be available (and it's not
        # necessarily in the base image)
        packages += [Package('sudo', None, packfiles=False)]
        if packages:
            installer = select_installer(pack, runs, target_distribution)
            # Updates package sources
            fp.write('    %s && \\\n' % installer.update_script())
            # Installs necessary packages
            fp.write('    %s && \\\n' % installer.install_script(packages))
        logging.info("Dockerfile will install the %d software packages that "
                     "were not packed" % len(packages))

        # Untar
        paths = set()
        pathlist = []
        dataroot = PosixPath('DATA')
        # Adds intermediate directories, and checks for existence in the tar
        tar = tarfile.open(str(pack), 'r:*')
        for f in other_files:
            path = PosixPath('/')
            for c in f.path.components[1:]:
                path = path / c
                if path in paths:
                    continue
                paths.add(path)
                datapath = join_root(dataroot, path)
                try:
                    tar.getmember(str(datapath))
                except KeyError:
                    logging.info("Missing file %s" % datapath)
                else:
                    pathlist.append(unicode_(datapath))
        tar.close()
        # FIXME : for some reason we need reversed() here, I'm not sure why.
        # Need to read more of tar's docs.
        # TAR bug: --no-overwrite-dir removes --keep-old-files
        fp.write('    cd / && tar zpxf /reprozip_experiment.rpz '
                 '--numeric-owner --strip=1 %s\n' %
                 ' '.join(shell_escape(p) for p in reversed(pathlist)))

    # Meta-data for reprounzip
    write_dict(target / '.reprounzip', {})


@target_must_exist
def docker_setup_build(args):
    """Builds the container from the Dockerfile
    """
    target = Path(args.target[0])
    unpacked_info = read_dict(target / '.reprounzip')
    if 'initial_image' in unpacked_info:
        sys.stderr.write("Image already built\n")
        sys.exit(1)

    image = make_unique_name(b'reprounzip_image_')

    retcode = subprocess.call(['docker', 'build', '-t', image, '.'],
                              cwd=target.path)
    if retcode != 0:
        sys.stderr.write("docker build failed with code %d\n" % retcode)
        sys.exit(1)
    logging.info("Initial image created: %s" % image.decode('ascii'))

    unpacked_info['initial_image'] = image
    write_dict(target / '.reprounzip', unpacked_info)


@target_must_exist
def docker_run(args):
    """Runs the experiment in the container.
    """
    target = Path(args.target[0])
    unpacked_info = read_dict(target / '.reprounzip')
    run = args.run
    cmdline = args.cmdline

    # Loads config
    runs, packages, other_files = load_config(target / 'experiment.rpz')

    if run is None and len(runs) == 1:
        run = 0

    # --cmdline without arguments: display the original command line
    if cmdline == []:
        if run is None:
            sys.stderr.write("Error: There are several runs in this pack -- "
                             "you have to choose which\none to use with "
                             "--cmdline\n")
            sys.exit(1)
        print("Original command-line:")
        print(' '.join(shell_escape(arg) for arg in runs[run]['argv']))
        sys.exit(0)

    if run is None:
        run = irange(len(runs))
    else:
        run = (int(run),)

    # Destroy previous container
    if 'ran_container' in unpacked_info:
        container = unpacked_info.pop('ran_container')
        logging.info("Destroying previous container %s" %
                     container.decode('ascii'))
        retcode = subprocess.call(['docker', 'rm', '-f', container])
        if retcode != 0:
            sys.stderr.write("Error deleting previous container %s\n" %
                             container.decode('ascii'))
        write_dict(target / '.reprounzip', unpacked_info)

    temp_image = None

    # If staging container exists, make an image from it
    if 'staging_container' in unpacked_info:
        stage = unpacked_info['staging_container']
        logging.debug("Creating temp image from staging container %s" %
                      stage.decode('ascii'))
        temp_image = make_unique_name(b'reprounzip_temp_')
        subprocess.check_call(['docker', 'commit', stage, temp_image])
        image = temp_image
        logging.debug("Image created: %s" % image.decode('ascii'))
    # Else, use the initial image directly
    elif 'initial_image' in unpacked_info:
        image = unpacked_info['initial_image']
        logging.debug("Running from initial image %s" % image.decode('ascii'))
    else:
        sys.stderr.write("Image doesn't exist yet, have you run "
                         "setup/build?\n")
        sys.exit(1)

    # Name of new container
    container = make_unique_name(b'reprounzip_run_')

    cmds = []
    for run_number in run:
        run_dict = runs[run_number]
        cmd = 'cd %s && ' % shell_escape(run_dict['workingdir'])
        cmd += '/usr/bin/env -i '
        cmd += ' '.join('%s=%s' % (k, shell_escape(v))
                        for k, v in iteritems(run_dict['environ']))
        cmd += ' '
        # FIXME : Use exec -a or something if binary != argv[0]
        if cmdline is None:
            argv = [run_dict['binary']] + run_dict['argv'][1:]
        else:
            argv = cmdline
        cmd += ' '.join(shell_escape(a) for a in argv)
        uid = run_dict.get('uid', 1000)
        cmd = 'sudo -u \'#%d\' sh -c %s\n' % (uid, shell_escape(cmd))
        cmds.append(cmd)
    cmds = ' && '.join(cmds)

    # Run command in container
    logging.info("Starting container %s" % container.decode('ascii'))
    subprocess.check_call(['docker', 'run', b'--name=' + container,
                           '-i', '-t', image,
                           '/bin/sh', '-c', cmds])

    # Store container name (so we can download output files)
    unpacked_info['ran_container'] = container
    write_dict(target / '.reprounzip', unpacked_info)

    if temp_image is not None:
        # TODO : Can't do that, need to live until container is destroyed
        logging.debug("Removing temp image %s" % temp_image.decode('ascii'))
        subprocess.check_call(['docker', 'rmi', temp_image])


class ContainerUploader(FileUploader):
    def __init__(self, target, input_files, files, unpacked_info):
        self.unpacked_info = unpacked_info
        FileUploader.__init__(self, target, input_files, files)

    def prepare_upload(self, files):
        # Checks whether the staging container exists
        if 'staging_container' in self.unpacked_info:
            self.stage = self.unpacked_info['staging_container']
        # Else we have to create it
        elif 'initial_image' in self.unpacked_info:
            self.stage = make_unique_name(b'reprounzip_staging_')
            initial_image = self.unpacked_info['initial_image']
            subprocess.check_call(['docker', 'run', '--name=' + self.stage,
                                   initial_image, '/bin/sh', '-c', ''])  # nop
            self.unpacked_info['staging_container'] = self.stage
            write_dict(self.target / '.reprounzip', self.unpacked_info)
        else:
            sys.stderr.write("Image doesn't exist yet, have you run "
                             "setup/build?\n")
            sys.exit(1)

    def upload_file(self, local_path, input_path):
        # FIXME : owner/group and permissions
        subprocess.check_call(['docker', 'cp', local_path.path,
                               self.stage + b':' + input_path.path])


@target_must_exist
def docker_upload(args):
    """Replaces an input file in the container.
    """
    target = Path(args.target[0])
    files = args.file
    unpacked_info = read_dict(target / '.reprounzip')
    input_files = unpacked_info.setdefault('input_files', {})

    try:
        ContainerUploader(target, input_files, files, unpacked_info)
    finally:
        write_dict(target / '.reprounzip', unpacked_info)


class ContainerDownloader(FileDownloader):
    def __init__(self, target, files, container):
        self.container = container
        FileDownloader.__init__(self, target, files)

    def download(self, remote_path, local_path):
        # Docker copies to a file in the specified directory, cannot just take
        # a file name (#4272)
        tmpdir = Path.tempdir(prefix='reprozip_docker_output_')
        try:
            subprocess.check_call(['docker', 'cp',
                                   self.container + b':' + remote_path.path,
                                   tmpdir.path])
            (tmpdir / remote_path.name).copyfile(local_path)
        finally:
            tmpdir.rmtree()


@target_must_exist
def docker_download(args):
    """Gets an output file out of the container.
    """
    target = Path(args.target[0])
    files = args.file
    unpacked_info = read_dict(target / '.reprounzip')

    if 'ran_container' not in unpacked_info:
        sys.stderr.write("Container does not exist. Have you run the "
                         "experiment?\n")
        sys.exit(1)
    container = unpacked_info['ran_container']
    logging.debug("Downloading from container %s" % container.decode('ascii'))

    ContainerDownloader(target, files, container)


@target_must_exist
def docker_destroy_docker(args):
    """Destroys the container and images.
    """
    target = Path(args.target[0])
    unpacked_info = read_dict(target / '.reprounzip')
    if 'initial_image' not in unpacked_info:
        sys.stderr.write("Image not created\n")
        sys.exit(1)

    if 'ran_container' in unpacked_info:
        container = unpacked_info.pop('ran_container')
        retcode = subprocess.call(['docker', 'rm', '-f', container])
        if retcode != 0:
            sys.stderr.write("Error deleting container %s\n" %
                             container.decode('ascii'))

    if 'staging_container' in unpacked_info:
        stage = unpacked_info.pop('staging_container')
        retcode = subprocess.call(['docker', 'rm', '-f', stage])
        if retcode != 0:
            sys.stderr.write("Error deleting container %s\n" %
                             stage.decode('ascii'))

    image = unpacked_info.pop('initial_image')
    retcode = subprocess.call(['docker', 'rmi', image])
    if retcode != 0:
        sys.stderr.write("Error deleting image %s\n" % image.decode('ascii'))


@target_must_exist
def docker_destroy_dir(args):
    """Destroys the directory.
    """
    target = Path(args.target[0])
    read_dict(target / '.reprounzip')

    target.rmtree()


def test_has_docker(pack, **kwargs):
    pathlist = os.environ['PATH'].split(os.pathsep) + ['.']
    pathexts = os.environ.get('PATHEXT', '').split(os.pathsep)
    for path in pathlist:
        for ext in pathexts:
            fullpath = os.path.join(path, 'docker') + ext
            if os.path.isfile(fullpath):
                return COMPAT_OK
    return COMPAT_MAYBE, "docker not found in PATH"


def setup(parser):
    """Runs the experiment in a Docker container

    You will need Docker to be installed on your machine if you want to run the
    experiment.

    setup   setup/create    creates Dockerfile (--pack is required)
            setup/build     builds the container from the Dockerfile
    upload                  replaces input files in the container
                            (without arguments, lists input files)
    run                     runs the experiment in the container
    download                gets output files from the container
                            (without arguments, lists output files)
    destroy destroy/docker  destroys the container and associated images
            destroy/dir     removes the unpacked directory

    For example:

        $ reprounzip docker setup --pack mypack.rpz experiment; cd experiment
        $ reprounzip docker run .
        $ reprounzip docker download . results:/home/user/theresults.txt
        $ cd ..; reprounzip docker destroy experiment
    """
    subparsers = parser.add_subparsers(title="actions",
                                       metavar='', help=argparse.SUPPRESS)
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument('target', nargs=1, help="Directory to create")

    # setup/create
    opt_setup = argparse.ArgumentParser(add_help=False)
    opt_setup.add_argument('--pack', nargs=1, help="Pack to extract")
    opt_setup.add_argument('--base-image', nargs=1, help="Base image to use")
    parser_setup_create = subparsers.add_parser('setup/create',
                                                parents=[options, opt_setup])
    parser_setup_create.set_defaults(func=docker_setup_create)

    # setup/build
    parser_setup_build = subparsers.add_parser('setup/build',
                                               parents=[options])
    parser_setup_build.set_defaults(func=docker_setup_build)

    # setup
    parser_setup = subparsers.add_parser('setup', parents=[options, opt_setup])
    parser_setup.set_defaults(func=composite_action(docker_setup_create,
                                                    docker_setup_build))

    # upload
    parser_upload = subparsers.add_parser('upload', parents=[options])
    parser_upload.add_argument('file', nargs=argparse.ZERO_OR_MORE,
                               help="<path>:<input_file_name")
    parser_upload.set_defaults(func=docker_upload)

    # run
    parser_run = subparsers.add_parser('run', parents=[options])
    parser_run.add_argument('run', default=None, nargs='?')
    parser_run.add_argument('--cmdline', nargs=argparse.REMAINDER,
                            help=("Command line to run"))
    parser_run.set_defaults(func=docker_run)

    # download
    parser_download = subparsers.add_parser('download', parents=[options])
    parser_download.add_argument('file', nargs=argparse.ZERO_OR_MORE,
                                 help="<output_file_name>:<path>")
    parser_download.set_defaults(func=docker_download)

    # destroy/docker
    parser_destroy_docker = subparsers.add_parser('destroy/docker',
                                                  parents=[options])
    parser_destroy_docker.set_defaults(func=docker_destroy_docker)

    # destroy/dir
    parser_destroy_dir = subparsers.add_parser('destroy/dir',
                                               parents=[options])
    parser_destroy_dir.set_defaults(func=docker_destroy_dir)

    # destroy
    parser_destroy = subparsers.add_parser('destroy', parents=[options])
    parser_destroy.set_defaults(func=composite_action(docker_destroy_docker,
                                                      docker_destroy_dir))

    return {'test_compatibility': test_has_docker}
