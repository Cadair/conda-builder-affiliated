from __future__ import (division, print_function, absolute_import,
                        unicode_literals)

from argparse import ArgumentParser
import os
import re
import hashlib
import tarfile
import zipfile

import yaml

from binstar_client.utils import get_binstar
from binstar_client.errors import NotFound
from conda import config

from astropy.extern import six
from astropy.extern.six.moves import xmlrpc_client as xmlrpclib

from jinja2 import Environment, FileSystemLoader
from jinja2.exceptions import TemplateNotFound

from generate_initial_versions import get_pypi_info

BINSTAR_CHANNEL = 'astropy'
PYPI_XMLRPC = 'https://pypi.python.org/pypi'
BDIST_CONDA_FOLDER = 'bdist_conda'
TEMPLATE_FOLDER = 'recipe_templates'
RECIPE_FOLDER = 'recipes'
BUILD_ORDER = 'build_order.txt'
ALL_PLATFORMS = ['osx-64', 'linux-64', 'linux-32', 'win-32', 'win-64']

class Package(object):
    """
    A package to be built for conda.

    Parameters
    ----------

    pypi_name : str
        Name of the package on PyPI. Note that PyPI is not case sensitive.

    version: str, optional
        Version number of the package. ``None``, the default, implies the most
        recent version visible on PyPI should be used.
    """

    # The class should only need one client for communicating with PyPI
    client = xmlrpclib.ServerProxy(PYPI_XMLRPC)

    def __init__(self, pypi_name, version=None):
        self._pypi_name = pypi_name
        self.required_version = version
        self._build = False
        self._url = None
        self._md5 = None
        self._build_platforms = None
        self._extra_meta = None
        self._build_pythons = None

    @property
    def pypi_name(self):
        """
        Name of the package on PyPI.
        """
        return self._pypi_name

    @property
    def conda_name(self):
        """
        Name of the package on binstar (conda), which requires lowercase
        names.
        """
        return self.pypi_name.lower()

    @property
    def required_version(self):
        """
        Version number of the package.
        """
        return self._required_version

    @required_version.setter
    def required_version(self, value):
        self._required_version = value.strip()

    @property
    def build(self):
        """
        bool:
            ``True`` if this package needs to be built.
        """
        return self._build

    @build.setter
    def build(self, value):
        # TODO: Make sure this is a bool
        self._build = value

    @property
    def is_dev(self):
        return not (re.search('[a-zA-Z]', self.required_version) is None)

    @property
    def url(self):
        if not self._url:
            self._retrieve_package_metadata()

        return self._url

    @property
    def md5(self):
        if not self._md5:
            self._retrieve_package_metadata()

        return self._md5

    @property
    def filename(self):
        return self.url.split('/')[-1]

    @property
    def build_platforms(self):
        """
        Return list of platforms on which this package can be built.

        Defaults to the value of ``ALL_PLATFORMS``.

        Checks for build information by looking at recipe *templates*, which
        is probably not really the way to go...might be more generalizable if
        it looked at recipes instead.
        """
        # Lazy memoization...
        if self._build_platforms:
            return self._build_platforms

        platform_info = self.extra_meta

        try:
            platforms = platform_info['extra']['platforms']
        except KeyError:
            platforms = ALL_PLATFORMS

        self._build_platforms = platforms
        return self._build_platforms

    @property
    def build_pythons(self):
        if self._build_pythons:
            return self._build_pythons
        try:
            pythons = self.extra_meta['extra']['pythons']
        except KeyError:
            pythons = ["27", "34"]

        # Make sure version is always a string so it can be compared
        # to CONDA_PY later.
        self._build_pythons = [str(p) for p in pythons]
        return self._build_pythons

    @property
    def extra_meta(self):
        """
        The 'extra' metadata, for now read in from meta.yaml.
        """
        if self._extra_meta is not None:
            return self._extra_meta

        template_dir = os.path.join(TEMPLATE_FOLDER, self.conda_name)

        try:
            meta = render_template(self, 'meta.yaml')
        except TemplateNotFound as e:
            # No recipe, make an empty meta for now.
            meta = ''

        platform_info = yaml.safe_load(meta) if meta else {}
        self._extra_meta = platform_info

        return self._extra_meta

    def _retrieve_package_metadata(self):
        """
        Get URL and md5 checksum from PyPI for either the specified version
        or the most recent version.
        """
        if not self.required_version:
            version = get_pypi_info(self.pypi_name)
        else:
            version = self.required_version

        urls = self.client.release_urls(self.pypi_name, version)
        try:
            # Many packages now have wheels, need to iterate over download
            # URLs to get the source distribution.
            for a_url in urls:
                if a_url['packagetype'] == 'sdist':
                    url = a_url['url']
                    md5sum = a_url['md5_digest']
                    break
            else:
                # No source distribution, so raise an index error
                raise IndexError
        except IndexError:
            # Apparently a pypi release isn't required to have any source?
            # If it doesn't, then return None
            print('No source found for {}: {}'.format(self.pypi_name,
                  self.required_version))
            url = None
            md5sum = None
        self._url = url
        self._md5 = md5sum

    def download(self, directory, checksum=True):
        """
        Download package and store in directory.

        Parameters
        ----------
        directory : str
            Directory in which to store the downloaded package.

        checksum: bool, optional
            If ``True``, check the MD5 checksum of the download.
        """
        loader = six.moves.urllib.request.URLopener()
        destination = os.path.join(directory, self.filename)
        print(destination)
        loader.retrieve(self.url, destination)
        if checksum:
            with open(destination, 'rb') as f:
                # Not worried about the packages being too big for memory.
                contents = f.read()
            md5_downloaded = hashlib.md5(contents).hexdigest()
            if md5_downloaded != self.md5:
                raise ValueError('checksum mismatch '
                                 'in {}'.format(self.filename))


def get_package_versions(requirements_path):
    """
    Read and parse list of packages.

    Parameters
    ----------

    requirements_path : str
        Path to ``requirements.txt``

    Returns
    -------

    list
        List of ``Package`` objects, one for each in the requirements file.
    """
    with open(requirements_path, 'rt') as f:
        # The requirements file is small, read it all in.
        package_list = f.readlines()

    packages = []
    for p in package_list:
        if p.startswith('#'):
            continue
        name, version = p.split('==')
        packages.append(Package(name, version=version))

    return packages


def _conda_python_build_string():
    """
    Construct the part of the conda build string that contains the python
    version.
    """
    try:
        conda_python_version = os.environ['CONDA_PY']
    except KeyError:
        raise RuntimeError('The environment variable CONDA_PY needs to be '
                           'set before running this script.')
    # Remove the period if it is in the python version.
    conda_python_version = ''.join(conda_python_version.split('.'))
    return 'py' + conda_python_version


def construct_build_list(packages, conda_channel=None):
    channel = conda_channel or BINSTAR_CHANNEL
    conda_py = _conda_python_build_string()

    for package in packages:
        print('Checking status of {}...'.format(package.conda_name), end="")
        binstar = get_binstar()

        # Decide whether the package needs to be built by checking to see if
        # it exists on binstar.
        try:
            binstar_info = binstar.release(channel,
                                           package.conda_name,
                                           package.required_version)
        except NotFound:
            # No builds for this version on any platform, so need to build.
            package.build = True

        if not package.build:
            # We have binstar_info, need to check whether we have this
            # platform and python version.
            for d in binstar_info['distributions']:
                if (d['attrs']['subdir'] == config.subdir
                    and conda_py in d['attrs']['build']):

                    break
            else:
                package.build = True

        unsupported_platform = config.subdir not in package.build_platforms
        unsupported_python = conda_py[2:] not in package.build_pythons

        if not package.build:
            build_message = "do not build"
        elif package.is_dev:
            build_message = 'skip because package version is dev'
        elif unsupported_platform:
            build_message = 'build not supported on this platform'
        elif unsupported_python:
            build_message = 'build not supported on this python version'
        elif not package.url:
            build_message = 'no source found on PyPI'
        else:
            build_message = 'BUILD'

        package.build = (package.build and not package.is_dev
                         and package.url and not unsupported_platform
                         and not unsupported_python)

        print(build_message)

    return [p for p in packages if p.build]


def write_build_order(build_bdist):
    """
    Write list of directories to be built in the order they appear in
    requirements file.
    """
    names = [p.conda_name for p in build_bdist]
    with open(BUILD_ORDER, 'wt') as f:
        f.writelines('\n'.join(names))


def render_template(package, template):
    """
    Render recipe components from jinja2 templates.

    Parameters
    ----------

    package : Package
        :class:`Package` object for which template will be rendered.
    template : str
        Name of template file, path relative to ``TEMPLATE_FOLDER``.
    """
    full_template_path = os.path.abspath(TEMPLATE_FOLDER)
    jinja_env = Environment(loader=FileSystemLoader(full_template_path))
    tpl = jinja_env.get_template('/'.join([package.conda_name, template]))
    rendered = tpl.render(version=package.required_version, md5=package.md5)
    return rendered


def main(args):
    packages = get_package_versions(args.requirements)

    to_build = construct_build_list(packages, conda_channel='astropy')

    needs_recipe = os.listdir(TEMPLATE_FOLDER)

    build_recipe = [p for p in to_build if p.conda_name in needs_recipe]
    build_bdist = [p for p in to_build if p.conda_name not in needs_recipe]

    if build_bdist:
        os.mkdir(BDIST_CONDA_FOLDER)

    if build_recipe:
        os.mkdir(RECIPE_FOLDER)

    # Write recipes from templates.
    for p in build_recipe:
        print('Writing recipe for {}.'.format(p.conda_name))
        recipe_path = os.path.join(RECIPE_FOLDER, p.conda_name)
        template_path = os.path.join(TEMPLATE_FOLDER, p.conda_name)
        os.mkdir(recipe_path)
        templates = [d for d in os.listdir(template_path) if not d.startswith('.')]
        for template in templates:
            rendered = render_template(p, template)
            with open(os.path.join(recipe_path, template), 'wt') as f:
                f.write(rendered)

    write_build_order(build_bdist)

    # Download and unpack source for all bdist_conda builds.
    for p in build_bdist:
        p.download(BDIST_CONDA_FOLDER)
        source_archive = os.path.join(BDIST_CONDA_FOLDER, p.filename)
        source_destination = os.path.join(BDIST_CONDA_FOLDER,
                                          p.conda_name)
        try:
            with tarfile.open(source_archive) as archive:
                archive.extractall(BDIST_CONDA_FOLDER)
            extracted_name = source_archive.strip('.tar.gz')
        except tarfile.ReadError:
            with zipfile.ZipFile(source_archive) as archive:
                archive.extractall(BDIST_CONDA_FOLDER)
            extracted_name = source_archive.strip('.zip')
        os.rename(extracted_name, source_destination)
        os.remove(source_archive)


if __name__ == '__main__':
    parser = ArgumentParser('command line tool for building packages.')
    parser.add_argument('requirements',
                        help='Full path to requirements.txt')
    args = parser.parse_args()
    main(args)
