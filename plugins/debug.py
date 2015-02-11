#
# Copyright (C) 2015  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

from __future__ import absolute_import
from __future__ import unicode_literals
from dnfpluginscore import _, logger

import dnf
import dnf.cli
import dnfpluginscore
import dnfpluginscore.lib
import gzip
import hawkey
import os
import subprocess
import sys
import time


DEBUG_VERSION = "dnf-debug-dump version 1\n"

class Debug(dnf.Plugin):

    name = 'debug'

    def __init__(self, base, cli):
        super(Debug, self).__init__(base, cli)
        self.base = base
        self.cli = cli
        if self.cli is not None:
            self.cli.register_command(DebugDumpCommand)
            self.cli.register_command(DebugRestoreCommand)


class DebugDumpCommand(dnf.cli.Command):

    aliases = ['debug-dump']
    summary = _('dump information about installed rpm packages to file')
    usage = '[%s] [%s]' % (_('OPTIONS'), _('KEYWORDS'))

    def run(self, args):
        """create debug txt file and compress it, if no filename specified
           use dnf_debug_dump-<timestamp>.txt.gz by default"""
        parser = dnfpluginscore.ArgumentParser(self.aliases[0])
        parser.add_argument(
            '--norepos', action="store_true", default=False,
            help=_('do not attempt to dump the repository contents.'))
        parser.add_argument(
            'filename', nargs='?',
            help=_('optional name of dump file'))
        opts = parser.parse_args(args)

        if opts.help_cmd:
            print(parser.format_help())
            return

        filename = opts.filename
        if not filename:
            now = time.strftime("%Y-%m-%d_%T", time.localtime(time.time()))
            filename = 'dnf_debug_dump-%s-%s.txt.gz' % (os.uname()[1], now)

        filename = os.path.abspath(filename)
        if filename.endswith('.gz'):
            fobj = gzip.GzipFile(filename, 'w')
        else:
            fobj = open(filename, 'w')

        fobj.write(DEBUG_VERSION)
        self.dump_system_info(fobj)
        self.dump_dnf_config_info(fobj)
        self.dump_rpm_problems(fobj)
        self.dump_packages(fobj, not opts.norepos)
        self.dump_rpmdb_versions(fobj)
        fobj.close()

        print(_("Output written to: %s") % filename)

    @staticmethod
    def dump_system_info(fobj):
        fobj.write("%%%%SYSTEM INFO\n")
        uname = os.uname()
        rpm_ver = subprocess.check_output(["rpm", "--version"]).strip()
        fobj.write("  uname: %s, %s\n" % (uname[2], uname[4]))
        fobj.write("  rpm ver: %s\n" % rpm_ver)
        fobj.write("  python ver: %s\n" % sys.version.replace('\n', ''))
        return

    def dump_dnf_config_info(self, fobj):
        var = self.base.conf.substitutions
        plugins = ",".join([p.name for p in self.base.plugins.plugins])
        fobj.write("%%%%DNF INFO\n")
        fobj.write("  arch: %s\n" % var['arch'])
        fobj.write("  basearch: %s\n" % var['basearch'])
        fobj.write("  releasever: %s\n" % var['releasever'])
        fobj.write("  dnf ver: %s\n" % dnf.const.VERSION)
        fobj.write("  enabled plugins: %s\n" % plugins)
        fobj.write("  global excludes: %s\n" % ",".join(self.base.conf.exclude))
        return

    def dump_rpm_problems(self, fobj):
        fobj.write("%%%%RPMDB PROBLEMS\n")
        (missing, conflicts) = rpm_problems(self.base)
        fobj.writelines(["Package %s requires %s\n" % (unicode(pkg), unicode(req))
                         for (req, pkg) in missing])
        fobj.writelines(["Package %s conflicts with %s\n" % (unicode(pkg),
                                                             unicode(conf))
                         for (conf, pkg) in conflicts])


    def dump_packages(self, fobj, load_repos):
        self.base.fill_sack(load_system_repo=True,
                            load_available_repos=load_repos)
        q = self.base.sack.query()
        # packages from rpmdb
        fobj.write("%%%%RPMDB\n")
        for p in sorted(q.installed()):
            fobj.write('  %s\n' % pkgspec(p))

        if not load_repos:
            return

        fobj.write("%%%%REPOS\n")
        available = q.available()
        for repo in sorted(self.base.repos.iter_enabled(), key=lambda x: x.id):
            try:
                url = None
                if repo.metalink is not None:
                    url = repo.metalink
                elif repo.mirrorlist is not None:
                    url = repo.mirrorlist
                elif len(repo.baseurl) > 0:
                    url = repo.baseurl[0]
                fobj.write('%%%s - %s\n' % (repo.id, url))
                fobj.write('  excludes: %s\n' % ','.join(repo.exclude))
                for po in sorted(available.filter(reponame=repo.id)):
                    fobj.write('  %s\n' % pkgspec(po))

            except dnf.exceptions.Error as e:
                fobj.write("Error accessing repo %s: %s\n" % (repo, str(e)))
                continue
        return

    def dump_rpmdb_versions(self, fobj):
        fobj.write("%%%%RPMDB VERSIONS\n")
        version = self.base.sack.rpmdb_version(self.base.yumdb)
        fobj.write('  all: %s\n' % version)
        return


class DebugRestoreCommand(dnf.cli.Command):

    aliases = ['debug-restore']
    summary = _('restore packages recorded in debug-dump file')
    usage = '[%s] [%s]' % (_('OPTIONS'), _('KEYWORDS'))

    def run(self, args):
        """Execute the command action here."""

        parser = dnfpluginscore.ArgumentParser(self.aliases[0])
        parser.add_argument(
            '--output', action="store_true",
            help=_('output commands that would be run to stdout.'))
        parser.add_argument(
            '--install-latest', action='store_true',
            help=_('Install the latest version of recorded packages.'))
        parser.add_argument(
            '--ignore-arch', action='store_true',
            help=_('Ignore architecture and install missing packages matching'
                   + 'the name, epoch, version and release.'))
        parser.add_argument(
            '--filter-types', metavar='[install, remove, replace]',
            default='install, remove, replace',
            help=_('limit to specified type'))
        parser.add_argument(
            'filename', nargs=1, help=_('name of dump file'))

        opts = parser.parse_args(args)

        if opts.help_cmd:
            print(parser.format_help())
            return

        if opts.filter_types:
            opts.filter_types = set(
                opts.filter_types.replace(",", " ").split())

        self.base.fill_sack(load_system_repo=True,
                            load_available_repos=True)
        installed = self.base.sack.query().installed()
        dump_pkgs = self.read_dump_file(opts.filename[0])

        self.process_installed(installed, dump_pkgs, opts)

        self.process_dump(dump_pkgs, opts)

        if not opts.output:
            self.base.resolve()
            self.base.do_transaction()

    def process_installed(self, installed, dump_pkgs, opts):
        for pkg in sorted(installed):
            filtered = False
            spec = pkgspec(pkg)
            action, dn, da, de, dv, dr = dump_pkgs.get((pkg.name, pkg.arch),
                                                       [None, None, None,
                                                        None, None, None])
            dump_naevr = (dn, da, de, dv, dr)
            if pkg.pkgtup == dump_naevr:
                # package unchanged
                del dump_pkgs[(pkg.name, pkg.arch)]
            else:
                if action == 'install':
                    # already have some version
                    dump_pkgs[(pkg.name, pkg.arch)][0] = 'replace'
                    if 'replace' not in opts.filter_types:
                        filtered = True
                else:
                    if 'remove' not in opts.filter_types:
                        filtered = True
                if not filtered:
                    if opts.output:
                        print("remove    %s" % spec)
                    else:
                        self.base.package_remove(pkg)

    def process_dump(self, dump_pkgs, opts):
        for (action, n, a, e, v, r) in sorted(dump_pkgs.values()):
            filtered = False
            if opts.ignore_arch:
                arch = ''
            else:
                arch = '.' + a
            if opts.install_latest and action == 'install':
                pkg_spec = "%s%s" % (n, arch)
                if 'install' not in opts.filter_types:
                    filtered = True
            else:
                pkg_spec = pkgtup2spec(n, arch, e, v, r)
                if (action == 'replace' and
                        'replace' not in opts.filter_types):
                    filtered = True
            if not filtered:
                if opts.output:
                    print("install   %s" % pkg_spec)
                else:
                    try:
                        self.base.install(pkg_spec)
                    except dnf.exceptions.MarkingError:
                        logger.error(_("Package %s is not available"), pkg_spec)

    @staticmethod
    def read_dump_file(filename):
        if filename.endswith(".gz"):
            fobj = gzip.GzipFile(filename)
        else:
            fobj = open(filename)

        if fobj.readline() != DEBUG_VERSION:
            logger.error(_("Bad dnf debug file: %s"), filename)
            sys.exit(1)

        skip = True
        pkgs = {}
        for line in fobj:
            if skip:
                if line == '%%%%RPMDB\n':
                    skip = False
                continue

            if not line or line[0] != ' ':
                break

            pkg_spec = line.strip()
            nevra = hawkey.split_nevra(pkg_spec)
            pkgs[(nevra.name, nevra.arch)] = ['install', unicode(nevra.name),
                                              unicode(nevra.arch),
                                              unicode(nevra.epoch),
                                              unicode(nevra.version),
                                              unicode(nevra.release)]

        return pkgs

def rpm_problems(base):
    base.fill_sack(load_system_repo=True, load_available_repos=False)
    q = base.sack.query()
    allpkgs = q.installed()

    requires = set()
    conflicts = set()
    for pkg in allpkgs:
        requires.update([(req, pkg) for req in pkg.requires
                         if not str(req) == 'solvable:prereqmarker'
                         and not str(req).startswith('rpmlib(')])
        conflicts.update([(conf, pkg) for conf in pkg.conflicts])

    missing_requires = [(req, pkg) for (req, pkg) in requires
                        if not q.filter(provides=req)]
    existing_conflicts = [(conf, pkg) for (conf, pkg) in conflicts
                          if q.filter(provides=conf)]
    return (missing_requires, existing_conflicts)

def pkgspec(pkg):
    return pkgtup2spec(pkg.name, pkg.arch, pkg.epoch, pkg.version, pkg.release)

def pkgtup2spec(name, arch, epoch, version, release):
    a = '' if not arch else '.%s' % arch
    e = '' if epoch in (None, '') else '%s:' % epoch
    return "%s-%s%s-%s%s" % (name, e, version, release, a)