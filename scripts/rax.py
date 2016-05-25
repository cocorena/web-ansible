#!/usr/bin/env python

# (c) 2013, Jesse Keating <jesse.keating@rackspace.com,
#           Paul Durivage <paul.durivage@rackspace.com>,
#           Matt Martz <matt@sivel.net>
#
# This file is part of Ansible.
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import sys
import argparse
import warnings
import collections
import ConfigParser

from six import iteritems

from ansible.constants import get_config, mk_boolean

try:
    import json
except ImportError:
    import simplejson as json

try:
    import pyrax
    from pyrax.utils import slugify
except ImportError:
    print('pyrax is required for this module')
    sys.exit(1)

from time import time


NON_CALLABLES = (basestring, bool, dict, int, list, type(None))


def load_config_file():
    p = ConfigParser.ConfigParser()
    config_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               'rax.ini')
    try:
        p.read(config_file)
    except ConfigParser.Error:
        return None
    else:
        return p
p = load_config_file()


def rax_slugify(value):
    return 'rax_%s' % (re.sub('[^\w-]', '_', value).lower().lstrip('_'))


def to_dict(obj):
    instance = {}
    for key in dir(obj):
        value = getattr(obj, key)
        if isinstance(value, NON_CALLABLES) and not key.startswith('_'):
            key = rax_slugify(key)
            instance[key] = value

    return instance


def host(regions, hostname):
    hostvars = {}

    for region in regions:
        # Connect to the region
        cs = pyrax.connect_to_cloudservers(region=region)
        for server in cs.servers.list():
            if server.name == hostname:
                for key, value in to_dict(server).items():
                    hostvars[key] = value

                # And finally, add an IP address
                hostvars['ansible_ssh_host'] = server.accessIPv4
    print(json.dumps(hostvars, sort_keys=True, indent=4))


def _list_into_cache(regions):
    groups = collections.defaultdict(list)
    hostvars = collections.defaultdict(dict)
    images = {}
    cbs_attachments = collections.defaultdict(dict)

    prefix = get_config(p, 'rax', 'meta_prefix', 'RAX_META_PREFIX', 'meta')

    networks = get_config(p, 'rax', 'access_network', 'RAX_ACCESS_NETWORK',
                          'public', islist=True)
    try:
        ip_versions = map(int, get_config(p, 'rax', 'access_ip_version',
                                          'RAX_ACCESS_IP_VERSION', 4,
                                          islist=True))
    except:
        ip_versions = [4]
    else:
        ip_versions = [v for v in ip_versions if v in [4, 6]]
        if not ip_versions:
            ip_versions = [4]

    # Go through all the regions looking for servers
    for region in regions:
        # Connect to the region
        cs = pyrax.connect_to_cloudservers(region=region)
        if cs is None:
            warnings.warn(
                'Connecting to Rackspace region "%s" has caused Pyrax to '
                'return None. Is this a valid region?' % region,
                RuntimeWarning)
            continue
        for server in cs.servers.list():
            # Create a group on region
            groups[region].append(server.name)

            # Check if group metadata key in servers' metadata
            group = server.metadata.get('group')
            if group:
                groups[group].append(server.name)

            for extra_group in server.metadata.get('groups', '').split(','):
                if extra_group:
                    groups[extra_group].append(server.name)

            # Add host metadata
            for key, value in to_dict(server).items():
                hostvars[server.name][key] = value

            hostvars[server.name]['rax_region'] = region

            for key, value in iteritems(server.metadata):
                groups['%s_%s_%s' % (prefix, key, value)].append(server.name)

            groups['instance-%s' % server.id].append(server.name)
            groups['flavor-%s' % server.flavor['id']].append(server.name)

            # Handle boot from volume
            if not server.image:
                if not cbs_attachments[region]:
                    cbs = pyrax.connect_to_cloud_blockstorage(region)
                    for vol in cbs.list():
                        if mk_boolean(vol.bootable):
                            for attachment in vol.attachments:
                                metadata = vol.volume_image_metadata
                                server_id = attachment['server_id']
                                cbs_attachments[region][server_id] = {
                                    'id': metadata['image_id'],
                                    'name': slugify(metadata['image_name'])
                                }
                image = cbs_attachments[region].get(server.id)
                if image:
                    server.image = {'id': image['id']}
                    hostvars[server.name]['rax_image'] = server.image
                    hostvars[server.name]['rax_boot_source'] = 'volume'
                    images[image['id']] = image['name']
            else:
                hostvars[server.name]['rax_boot_source'] = 'local'

            try:
                imagegroup = 'image-%s' % images[server.image['id']]
                groups[imagegroup].append(server.name)
                groups['image-%s' % server.image['id']].append(server.name)
            except KeyError:
                try:
                    image = cs.images.get(server.image['id'])
                except cs.exceptions.NotFound:
                    groups['image-%s' % server.image['id']].append(server.name)
                else:
                    images[image.id] = image.human_id
                    groups['image-%s' % image.human_id].append(server.name)
                    groups['image-%s' % server.image['id']].append(server.name)

            # And finally, add an IP address
            ansible_ssh_host = None
            # use accessIPv[46] instead of looping address for 'public'
            for network_name in networks:
                if ansible_ssh_host:
                    break
                if network_name == 'public':
                    for version_name in ip_versions:
                        if ansible_ssh_host:
                            break
                        if version_name == 6 and server.accessIPv6:
                            ansible_ssh_host = server.accessIPv6
                        elif server.accessIPv4:
                            ansible_ssh_host = server.accessIPv4
                if not ansible_ssh_host:
                    addresses = server.addresses.get(network_name, [])
                    for address in addresses:
                        for version_name in ip_versions:
                            if ansible_ssh_host:
                                break
                            if address.get('version') == version_name:
                                ansible_ssh_host = address.get('addr')
                                break
            if ansible_ssh_host:
                hostvars[server.name]['ansible_ssh_host'] = ansible_ssh_host

    if hostvars:
        groups['_meta'] = {'hostvars': hostvars}

    with open(get_cache_file_path(regions), 'w') as cache_file:
        json.dump(groups, cache_file)


def get_cache_file_path(regions):
    regions_str = '.'.join([reg.strip().lower() for reg in regions])
    ansible_tmp_path = os.path.join(os.path.expanduser("~"), '.ansible', 'tmp')
    if not os.path.exists(ansible_tmp_path):
        os.makedirs(ansible_tmp_path)
    return os.path.join(ansible_tmp_path,
                        'ansible-rax-%s-%s.cache' % (
                            pyrax.identity.username, regions_str))


def _list(regions, refresh_cache=True):
    cache_max_age = int(get_config(p, 'rax', 'cache_max_age',
                                   'RAX_CACHE_MAX_AGE', 600))

    if (not os.path.exists(get_cache_file_path(regions)) or
        refresh_cache or
        (time() - os.stat(get_cache_file_path(regions))[-1]) > cache_max_age):
        # Cache file doesn't exist or older than 10m or refresh cache requested
        _list_into_cache(regions)

    with open(get_cache_file_path(regions), 'r') as cache_file:
        groups = json.load(cache_file)
        print(json.dumps(groups, sort_keys=True, indent=4))


def parse_args():
    parser = argparse.ArgumentParser(description='Ansible Rackspace Cloud '
                                                 'inventory module')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--list', action='store_true',
                       help='List active servers')
    group.add_argument('--host', help='List details about the specific host')
    parser.add_argument('--refresh-cache', action='store_true', default=False,
                        help=('Force refresh of cache, making API requests to'
                              'RackSpace (default: False - use cache files)'))
    return parser.parse_args()


def setup():
    default_creds_file = os.path.expanduser('~/.rackspace_cloud_credentials')

    env = get_config(p, 'rax', 'environment', 'RAX_ENV', None)
    if env:
        pyrax.set_environment(env)

    keyring_username = pyrax.get_setting('keyring_username')

    # Attempt to grab credentials from environment first
    creds_file = get_config(p, 'rax', 'creds_file',
                            'RAX_CREDS_FILE', None)
    if creds_file is not None:
        creds_file = os.path.expanduser(creds_file)
    else:
        # But if that fails, use the default location of
        # ~/.rackspace_cloud_credentials
        if os.path.isfile(default_creds_file):
            creds_file = default_creds_file
        elif not keyring_username:
            sys.stderr.write('No value in environment variable %s and/or no '
                             'credentials file at %s\n'
                             % ('RAX_CREDS_FILE', default_creds_file))
            sys.exit(1)

    identity_type = pyrax.get_setting('identity_type')
    pyrax.set_setting('identity_type', identity_type or 'rackspace')

    region = pyrax.get_setting('region')

    try:
        if keyring_username:
            pyrax.keyring_auth(keyring_username, region=region)
        else:
            pyrax.set_credential_file(creds_file, region=region)
    except Exception as e:
        sys.stderr.write("%s: %s\n" % (e, e.message))
        sys.exit(1)

    regions = []
    if region:
        regions.append(region)
    else:
        region_list = get_config(p, 'rax', 'regions', 'RAX_REGION', 'all',
                                 islist=True)
        for region in region_list:
            region = region.strip().upper()
            if region == 'ALL':
                regions = pyrax.regions
                break
            elif region not in pyrax.regions:
                sys.stderr.write('Unsupported region %s' % region)
                sys.exit(1)
            elif region not in regions:
                regions.append(region)

    return regions


def main():
    args = parse_args()
    regions = setup()
    if args.list:
        _list(regions, refresh_cache=args.refresh_cache)
    elif args.host:
        host(regions, args.host)
    sys.exit(0)


if __name__ == '__main__':
    main()
