#
#  Copyright 2015-2016 Bleemeo
#
#  bleemeo.com an infrastructure monitoring solution in the Cloud
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
"""
Module to gather facts about the server (OS familly/version, CPU type, ...)
"""

import datetime
import importlib
import logging
import os
import platform
import re
import shlex
import socket
import subprocess

import psutil
import requests
import yaml

import bleemeo_agent.config
import bleemeo_agent.util

if os.name == 'nt':
    # pylint: disable=import-error
    import pythoncom
    import winreg
    import wmi


DMI_DIR = '/sys/devices/virtual/dmi/id/'


def get_file_content(file_name):
    """ Read file content. If error occur, ignore and return None
    """
    try:
        with open(file_name) as file_obj:
            return file_obj.read().strip()
    except IOError:
        return None


def get_url_content(core, url, timeout=5.0):
    """ Get URL content. If error occur or status is not 200 return None
    """
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={'User-Agent': core.http_user_agent},
        )
        if response.status_code != 200:
            return None
        return response.text
    except requests.exceptions.RequestException:
        return None


def _get_package_version_dpkg(package_name):
    try:
        stdout = subprocess.check_output(
            ['dpkg', '-l', package_name],
            stderr=open('/dev/null', 'w')
        )
    except (OSError, subprocess.CalledProcessError):
        stdout = b''

    if not stdout:
        return None

    last_line = stdout.splitlines()[-1]
    parts = last_line.split()
    # dpkg -l output should contains:
    # state, package_name, version, architechure, description
    if len(parts) < 5:
        return None

    return parts[2].decode('utf-8')


def _get_package_version_rpm(package_name):
    try:
        stdout = subprocess.check_output(
            ['rpm', '-q', package_name, '--qf', '%{EVR}'],
            stderr=open('/dev/null', 'w')
        )
    except (OSError, subprocess.CalledProcessError):
        stdout = b''

    if not stdout:
        return None

    return stdout.decode('utf-8')


def get_package_version(package_name, default=None, distribution=None):
    """ Return the package version.

        If not installed or if unable to find the version, return default.

        For Windows, distribution is ignored and it use registry.

        For non-Windows, if distribution is set to "debian" or "centos", use
        dpkg or rpm respectivly to find the installed version.

        If distribution is set to None, try both dpkg then rpm.
    """

    if os.name == 'nt':
        key_path = (
            r'Software\Microsoft\Windows\CurrentVersion\Uninstall\%s' %
            package_name
        )
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                result = winreg.QueryValueEx(key, "DisplayVersion")[0]
        except FileNotFoundError:
            return default

        return result

    result = None
    if distribution is None or distribution == 'debian':
        result = _get_package_version_dpkg(package_name)
        if result is not None and result != "<none>":
            return result

    if distribution is None or distribution == 'centos':
        result = _get_package_version_rpm(package_name)
        if result is not None and result != "<none>":
            return result

    return default


def get_agent_version(core):
    """ Returns the version of Bleemeo agent

        Use system package tools (dpkg/rpm) to query the installed version.
    """
    return get_package_version(
        'bleemeo-agent',
        bleemeo_agent.__version__,
        distribution=core.config['distribution'],
    )


def get_docker_version(core):
    """ return a couple (docker-engine-version, docker-api-version)
    """
    api_version = None
    package_version = None

    # Read of (single) attribute is atomic, no lock needed
    docker_client = core.docker_client
    if docker_client is not None:
        try:
            versions = docker_client.version()
            api_version = versions.get('ApiVersion')
            package_version = versions.get('Version')
            return (package_version, api_version)
        except requests.exceptions.RequestException:
            logging.debug('error getting docker verion', exc_info=True)

    package_version = get_package_version(
        'docker-ce',
        package_version,
        distribution=core.config['distribution'],
    )
    if package_version is None:
        package_version = get_package_version(
            'docker-ee',
            package_version,
            distribution=core.config['distribution'],
        )
    if package_version is None:
        package_version = get_package_version(
            'docker-engine',
            package_version,
            distribution=core.config['distribution'],
        )
    if package_version is None:
        package_version = get_package_version(
            'docker.io',
            package_version,
            distribution=core.config['distribution'],
        )
    if package_version is None and core.config['distribution'] == 'centos':
        package_version = get_package_version(
            'docker',
            package_version,
            distribution=core.config['distribution'],
        )

    return (package_version, api_version)


def _get_telegraf_version(core):
    package_version = None
    telegraf_container = core.config['telegraf.docker_name']
    if telegraf_container:
        output = bleemeo_agent.util.docker_exec(
            core.docker_client,
            telegraf_container,
            ['telegraf', '-version'],
        )
    else:
        package_version = get_package_version(
            'telegraf',
            distribution=core.config['distribution'],
        )
        if package_version is not None:
            return package_version

        telegraf = 'telegraf'
        if os.name == 'nt':
            telegraf = bleemeo_agent.util.windows_telegraf_path(telegraf)
        try:
            output = subprocess.check_output([telegraf, '-version'])
            output = output.decode('utf-8').strip()
        except (subprocess.CalledProcessError, OSError, UnicodeDecodeError):
            return None

    # output is either "Telegraf - version 1.0.0"
    # or "Telegraf v1.2.0 (git: release-1.2 b2c[...])"
    # or "Telegraf 1.8.2+bleemeo1 (git: bleemeo 7d9b8309)"
    prefix = 'Telegraf - version '
    if output.startswith(prefix):
        package_version = output[len(prefix):]

    match = re.match(r'Telegraf v([^ ]+) \(git: .*\)', output)
    if match:
        package_version = match.group(1)

    match = re.match(r'Telegraf ([^ ]+) \(git: .*\)', output)
    if match:
        package_version = match.group(1)

    return package_version


def _get_package_version(package_name):
    try:
        module = importlib.import_module(package_name)
        return getattr(module, '__version__', None)
    except ImportError:
        return None


def read_os_release(core):
    """ Read os-release file and returns its content as dict

        os-relase is a FreeDesktop standard:
        http://www.freedesktop.org/software/systemd/man/os-release.html
    """
    result = {}
    file_path = '/etc/os-release'
    if core.container is not None:
        mount_point = core.config['df.host_mount_point']
        if mount_point is not None:
            file_path = mount_point + file_path
        else:
            return result

    try:
        with open(file_path) as os_release_file:
            for line in os_release_file:
                line = line.strip()
                if line == '':
                    continue
                (key, value) = line.split('=', 1)
                # value is a quoted string (single or double quote).
                # Use shlex.split to convert to normal string (handling
                # correctly if the string contains escaped quote)
                value = shlex.split(value)[0]
                result[key] = value
    except (IOError, OSError):
        pass
    return result


def _get_aws_facts(core):
    facts = {}
    facts['aws_ami_id'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/ami-id',
    )
    # If first request fail, don't try other one, it's probably not an
    # AWS EC2.
    if facts['aws_ami_id'] is None:
        return facts

    facts['aws_instance_id'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/instance-id',
    )
    facts['aws_instance_type'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/instance-type',
    )
    facts['aws_local_hostname'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/local-hostname',
    )
    facts['aws_security_groups'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/security-groups',
    )
    facts['aws_public_ipv4'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/public-ipv4',
    )
    facts['aws_placement'] = get_url_content(
        core,
        'http://169.254.169.254/latest/meta-data/placement/availability-zone',
    )

    base_url = (
        'http://169.254.169.254/latest/meta-data/network/interfaces/macs/'
    )
    macs = get_url_content(core, base_url)
    if macs is not None:
        result = [
            get_url_content(core, base_url + x + 'vpc-id')
            for x in macs.splitlines()
        ]
        result = [x for x in result if x is not None]
        if result:
            facts['aws_vpc_id'] = ','.join(result)

        result = [
            get_url_content(core, base_url + x + 'vpc-ipv4-cidr-block')
            for x in macs.splitlines()
        ]
        result = [x for x in result if x is not None]
        if result:
            facts['aws_vpc_ipv4_cidr_block'] = ','.join(result)

    return facts


def get_primary_addresses(ip_output=None):
    # pylint: disable=too-many-locals
    """ Return the primery IPv4 and MAC addresses

        This should be the IP address that this server use to communicate
        on internet. It may be the private IP if the box is NATed. The MAC
        address the is one associated with the interface holding the
        primary IP address.
    """
    # psutils could retrive IP address from interface, but we don't
    # known which is the "primary" interface.
    # psutils could retrive MAC address but require version 3.0 which is
    # not present on all system.
    # For now rely on "ip" command.

    if ip_output is None:
        try:
            output1 = subprocess.check_output(
                ['ip', 'route', 'get', '8.8.8.8']
            )
            output2 = subprocess.check_output(
                ['ip', 'address', 'show']
            )
            ip_output = output1.decode('utf-8') + output2.decode('utf-8')
        except (subprocess.CalledProcessError, OSError):
            return (None, None)

    lines = ip_output.splitlines()
    if not lines:
        return (None, None)
    ip_route, lines = lines[0], lines[1:]

    ip_address = None
    mac_address = None

    split_output = ip_route.split()
    for (index, word) in enumerate(split_output):
        if word == 'src':
            ip_address = split_output[index+1]

    re_new_interface = re.compile(r'^\d+: .*$')
    re_ether_address = re.compile(
        r'^\s+link/ether ([0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}) .*'
    )
    re_inet_address = re.compile(r'\s+inet (\d+(\.\d+){3})/\d+ .*')
    current_mac_address = None
    for line in lines:
        if re_new_interface.match(line):
            current_mac_address = None

        match = re_inet_address.match(line)
        if match and match.group(1) == ip_address:
            mac_address = current_mac_address
            break

        match = re_ether_address.match(line)
        if match:
            current_mac_address = match.group(1)

    return (ip_address, mac_address)


def get_public_ip(core):
    """ Return public IP used by this agent
    """
    url = core.config['agent.public_ip_indicator']
    return get_url_content(core, url)


def get_virtual_type(facts):
    """ Return what virtualization is used. "physical" if it's bare-metal.
    """
    result = 'physical'
    vendor_name = facts.get('system_vendor')
    bios_vendor = facts.get('bios_vendor')

    if vendor_name is None:
        # OpenVZ don't have DMI sys_vendor file, is it OpenVZ ?
        if os.path.exists('/proc/user_beancounters'):
            return 'openvz'
        return result

    if ('qemu' in vendor_name.lower()
            or 'bochs' in vendor_name.lower()
            or 'digitalocean' in vendor_name.lower()):
        result = 'kvm'
    elif 'xen' in vendor_name.lower():
        result = 'xen'
    elif 'innotek' in vendor_name.lower():
        result = 'virtualbox'
    elif 'microsoft' in vendor_name.lower():
        result = 'hyper-v'
    elif 'google' in vendor_name.lower():
        result = 'gce'
    elif 'vmware' in vendor_name.lower():
        result = 'vmware'
    elif 'openstack' in vendor_name.lower():
        # At least OvH seem to use this for its Cloud platform.
        if bios_vendor is not None and 'bochs' in bios_vendor.lower():
            result = 'kvm'
        elif 'vmware' in facts.get('serial_number', '').lower():
            # VMware use serial_number like "VMware-42 1d 8c ..."
            result = 'vmware'
        else:
            # unknown hypervisor at this point
            result = 'openstack'

    return result


def _system_has_swap():
    return psutil.swap_memory().total > 0


def strip_or_none(value):
    """ Call value.strip() after ensuring that value is a string
    """
    if isinstance(value, str):
        return value.strip()

    return None


def get_facts_root():
    """ Gather facts that need root privilege and write them in yaml file
    """

    config, errors, warnings = bleemeo_agent.config.load_config_with_default()
    if errors:
        logging.error(
            'Error while loading configuration: %s', '\n'.join(errors)
        )
        return
    if warnings:
        logging.warning(
            'Warning while loading configuration: %s', '\n'.join(warnings)
        )

    facts_file = config['agent.facts_file']

    facts = {
        'serial_number': get_file_content(
            os.path.join(DMI_DIR, 'product_serial')
        ),
    }
    facts = strip_empty(facts)

    with open(facts_file, 'w') as file_obj:
        yaml.safe_dump(facts, file_obj, default_flow_style=False)


def get_facts(core):
    # pylint: disable=too-many-locals
    # pylint: disable=too-many-branches
    # pylint: disable=too-many-statements
    """ Return facts/grains/information about current machine.

        Returned facts are informations like hostname, OS type/version, etc
    """
    # Load facts that need root privilege from facts_file
    facts_file = core.config['agent.facts_file']
    try:
        with open(facts_file) as file_obj:
            facts = yaml.safe_load(file_obj)
    except IOError:
        facts = {}

    if os.name != 'nt':
        os_information = read_os_release(core)
        facts.update({
            'os_family': os_information.get('ID_LIKE', None),
            'os_name': os_information.get('NAME', None),
            'os_pretty_name': os_information.get('PRETTY_NAME', None),
            'os_version': os_information.get('VERSION_ID', None),
            'os_version_long': os_information.get('VERSION', None),
        })
        if core.container is None:
            try:
                os_codename = subprocess.check_output(
                    ['lsb_release', '--codename', '--short']
                ).decode('utf8').strip()
            except OSError:
                os_codename = None
            facts.update({
                'os_codename': os_codename,
            })
    else:
        facts.update({
            'os_version': platform.win32_ver()[0],
        })

    (primary_address, primary_mac_address) = get_primary_addresses()
    architecture = platform.machine()
    hostname = socket.gethostname()

    try:
        # This is what hostname (3.20) does:
        # * use getaddrinfo on hostname with AI_CANONNAME
        # * take ai_canonname from first result
        result = socket.getaddrinfo(
            hostname, port=0, type=socket.SOCK_DGRAM, flags=socket.AI_CANONNAME
        )
        fqdn = result[0][3]
    except (IndexError, socket.gaierror):
        fqdn = hostname

    if (fqdn in ('', 'localhost', 'localhost.local', 'localhost.localdomain')
            and hostname != 'localhost'):
        fqdn = hostname

    if '.' in fqdn:
        domain = fqdn.split('.', 1)[1]
    else:
        domain = None

    if os.name != 'nt':
        kernel = subprocess.check_output(
            ['uname', '--kernel-name']
        ).decode('utf8').strip()
        kernel_release = subprocess.check_output(
            ['uname', '--kernel-release']
        ).decode('utf8').strip()
        kernel_version = kernel_release.split('-')[0]
        kernel_major_version = '.'.join(kernel_release.split('.')[0:2])
        facts.update({
            'kernel': kernel,
            'kernel_major_version': kernel_major_version,
            'kernel_release': kernel_release,
            'kernel_version': kernel_version,
        })
    else:
        facts.update({
            'kernel': 'Windows',
        })

    if os.name != 'nt':
        facts.update({
            'bios_released_at': get_file_content(
                os.path.join(DMI_DIR, 'bios_date')
            ),
            'bios_vendor': get_file_content(
                os.path.join(DMI_DIR, 'bios_vendor')
            ),
            'bios_version': get_file_content(
                os.path.join(DMI_DIR, 'bios_version')
            ),
            'product_name': get_file_content(
                os.path.join(DMI_DIR, 'product_name')
            ),
            'system_vendor': get_file_content(
                os.path.join(DMI_DIR, 'sys_vendor')
            ),
        })
    else:
        # To use WMI, each thread must call pythoncom.CoInitializeEx() at
        # least once.
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)

        wmi_connection = wmi.WMI()
        result = wmi_connection.Win32_ComputerSystem()
        if result:
            system_info = result[0]
            facts.update({
                'product_name': strip_or_none(system_info.Model),
                'system_vendor': strip_or_none(system_info.Manufacturer),
            })

        result = wmi_connection.Win32_SystemBIOS()
        if result:
            bios_info = result[0].PartComponent
            facts.update({
                'bios_released_at': strip_or_none(bios_info.ReleaseDate),
                'bios_vendor': strip_or_none(bios_info.Manufacturer),
                'bios_version': strip_or_none(bios_info.Version),
                'serial_number': strip_or_none(bios_info.SerialNumber),
            })

    virtual = get_virtual_type(facts)

    if (facts.get('bios_version') is not None
            and 'amazon' in facts.get('bios_version').lower()):
        facts.update(_get_aws_facts(core))

    (docker_version, docker_api_version) = get_docker_version(core)

    if core.graphite_server:
        facts.update({
            'metrics_source': core.graphite_server.metrics_source,
        })

    facts.update({
        'agent_version': get_agent_version(core),
        'architecture': architecture,
        'fact_updated_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'docker_api_version': docker_api_version,
        'docker_version': docker_version,
        'domain': domain,
        'public_ip': get_public_ip(core),
        'fqdn': fqdn,
        'installation_format': core.config['agent.installation_format'],
        'hostname': hostname,
        'primary_address': primary_address,
        'primary_mac_address': primary_mac_address,
        'paho_mqtt_version': _get_package_version('paho.mqtt'),
        'docker_py_version': _get_package_version('docker'),
        'requests_version': _get_package_version('requests'),
        'psutil_version': _get_package_version('psutil'),
        'statsd_enabled': str(core.config['telegraf.statsd.enabled']),
        'swap_present': _system_has_swap(),
        'telegraf_version': _get_telegraf_version(core),
        'timezone': get_file_content('/etc/timezone'),
        'virtual': virtual,
    })

    facts = strip_empty(facts)

    return facts


def strip_empty(facts):
    """ Remove facts with "None" as value or empty string
    """
    return {
        key: value for (key, value) in facts.items()
        if value is not None and value != ''
    }
