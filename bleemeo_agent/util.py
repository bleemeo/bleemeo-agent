import datetime
import json
import logging
import os
import random
import shlex
import socket
import subprocess
import threading
import time

import psutil
import requests

import bleemeo_agent


# With generate_password, taken from Django project
# Use the system PRNG if possible
try:
    random = random.SystemRandom()
    using_sysrandom = True
except NotImplementedError:
    import warnings
    warnings.warn('A secure pseudo-random number generator is not available '
                  'on your system. Falling back to Mersenne Twister.')
    using_sysrandom = False


class Sleeper:
    """ Helper to manage exponential sleep time.

        You can get the next duration of sleep with self.current_duration
    """

    def __init__(self, start_duration=10, max_duration=600):
        self.start_duration = start_duration
        self.max_duration = max_duration

        # number of sleep done with minimal duration
        self.grace_count = 3
        self.current_duration = start_duration

    def get_sleep_duration(self):
        if self.grace_count > 0:
            self.grace_count -= 1
        else:
            self.current_duration = min(
                self.max_duration, self.current_duration * 2)

        return self.current_duration

    def sleep(self):
        duration = self.get_sleep_duration()
        logging.debug('Sleeping %s seconds', duration)
        time.sleep(duration)


# Taken from Django project
def generate_password(length=10,
                      allowed_chars='abcdefghjkmnpqrstuvwxyz'
                                    'ABCDEFGHJKLMNPQRSTUVWXYZ'
                                    '23456789'):
    """
    Generates a random password with the given length and given
    allowed_chars. Note that the default value of allowed_chars does not
    have "I" or "O" or letters and digits that look similar -- just to
    avoid confusion.
    """
    return ''.join(random.choice(allowed_chars) for i in range(length))


def get_facts(core):
    """ Return facts/grains/information about current machine.

        Returned facts are informations like hostname, OS type/version, etc

        It will use facter tools if available
    """
    if os.path.exists('/etc/os-release'):
        pretty_name = get_os_pretty_name()
    else:
        pretty_name = 'Unknown OS'

    if os.path.exists('/sys/devices/virtual/dmi/id/product_name'):
        with open('/sys/devices/virtual/dmi/id/product_name') as fd:
            product_name = fd.read()
    else:
        product_name = ''

    uptime_seconds = get_uptime()
    uptime_string = format_uptime(uptime_seconds)

    primary_address = get_primary_address()

    # Basic "minimal" facts
    facts = {
        'hostname': socket.gethostname(),
        'fqdn': socket.getfqdn(),
        'os_pretty_name': pretty_name,
        'uptime': uptime_string,
        'uptime_seconds': uptime_seconds,
        'primary_address': primary_address,
        'product_name': product_name,
        'agent_version': bleemeo_agent.__version__,
        'current_time': datetime.datetime.now().isoformat(),
        'account_uuid': core.bleemeo_connector.account_id,
    }

    # Update with facter facts
    try:
        facter_raw = subprocess.check_output([
            'facter', '--json'
        ]).decode('utf-8')
        facts.update(json.loads(facter_raw))
    except OSError:
        facts.setdefault('errors', []).append('facter not installed')
        logging.warning(
            'facter is not installed. Only limited facts are sents')

    return facts


def get_uptime():
    with open('/proc/uptime', 'r') as f:
        uptime_seconds = float(f.readline().split()[0])
        return uptime_seconds


def format_uptime(uptime_seconds):
    """ Format uptime to human readable format

        Output will be something like "1 hour" or "3 days, 7 hours"
    """
    uptime_days = int(uptime_seconds / (24 * 60 * 60))
    uptime_hours = int((uptime_seconds % (24 * 60 * 60)) / (60 * 60))
    uptime_minutes = int((uptime_seconds % (60 * 60)) / 60)

    if uptime_minutes > 1:
        text_minutes = 'minutes'
    else:
        text_minutes = 'minute'
    if uptime_hours > 1:
        text_hours = 'hours'
    else:
        text_hours = 'hour'
    if uptime_days > 1:
        text_days = 'days'
    else:
        text_days = 'day'

    if uptime_days == 0 and uptime_hours == 0:
        uptime_string = '%s %s' % (uptime_minutes, text_minutes)
    elif uptime_days == 0:
        uptime_string = '%s %s' % (uptime_hours, text_hours)
    else:
        uptime_string = '%s %s, %s %s' % (
            uptime_days, text_days, uptime_hours, text_hours)

    return uptime_string


def get_os_pretty_name():
    """ Return the PRETTY_NAME from os-release
    """
    with open('/etc/os-release') as fd:
        for line in fd:
            line = line.strip()
            if line.startswith('PRETTY_NAME'):
                (_, value) = line.split('=')
                # value is a quoted string (single or double quote).
                # Use shlex.split to convert to normal string (handling
                # correctly if the string contains escaped quote)
                value = shlex.split(value)[0]
                return value


def get_primary_address():
    """ Return the primary IP(v4) address.

        This should be the address that this server use to communicate
        on internet. It may be the private IP if the box is NATed
    """
    # Any python library doing the job ?
    # psutils could retrive IP address from interface, but we don't
    # known which is the "primary" interface.
    # For now rely on "ip" command
    try:
        output = subprocess.check_output(
            ['ip', 'route', 'get', '8.8.8.8'])
        split_output = output.split()
        for (index, word) in enumerate(split_output):
            if word == 'src':
                return split_output[index+1]
    except subprocess.CalledProcessError:
        # Either "ip" is not found... or you don't have a route to 8.8.8.8
        # (no internet ?).
        # We could try with psutil, but "ip" is present on all recent ditro
        # and you should have internet :)
        pass

    return None


def run_command_timeout(command, timeout=10):
    """ Run a command and wait at most timeout seconds

        Both stdout and stderr and captured and returned.

        Returns (return_code, output)
    """
    def _kill_proc(proc, wait_event, timeout):
        """ function used in a separate thread to kill process """
        if not wait_event.wait(timeout):
            # event is not set, so process didn't finished itself
            proc.terminate()

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError:
        # Most probably : command not found
        return (127, "Unable to run command")
    proc_finished = threading.Event()
    killer_thread = threading.Thread(
        target=_kill_proc, args=(proc, proc_finished, timeout))
    killer_thread.start()

    (output, _) = proc.communicate()
    proc_finished.set()
    killer_thread.join()

    return (proc.returncode, output)


def package_installed(package_name):
    """ Return True if given package is installed.

        Work on Debian/Ubuntu derivate
    """
    try:
        output = subprocess.check_output(
            ['dpkg-query', '--show', '--showformat=${Status}', package_name],
            stderr=subprocess.STDOUT,
        )
        installed = output.startswith(b'install')
    except subprocess.CalledProcessError:
        installed = False

    return installed


def get_processes_info():
    """ Return informations on all running process.

        Information (per process) returned are:

        * pid
        * create_time
        * name
        * cmdline
        * ppid
        * memory usage
        * cpu_percent
        * status (running, sleeping...)
    """
    result = []
    for process in psutil.process_iter():
        result.append({
            'pid': process.pid,
            'create_time': process.create_time(),
            'name': process.name(),
            'cmdline': ' '.join(process.cmdline()),
            'ppid': process.ppid(),
            'memory_rss': process.memory_info().rss,
            'cpu_percent': process.cpu_percent(),
            'status': process.status(),
        })

    return result


def _get_url(name, metric_config):
    response = None
    try:
        response = requests.get(
            metric_config['url'],
            verify=metric_config.get('ssl_check', True),
            timeout=3.0,
        )
    except requests.exceptions.ConnectionError:
        logging.warning(
            'Failed to retrive metric %s : failed to establish connection',
            name)
    except requests.exceptions.ConnectionError:
        logging.warning(
            'Failed to retrive metric %s : request timed out',
            name)
    except requests.exceptions.RequestException:
        logging.warning(
            'Failed to retrive metric %s',
            name)

    return response


def pull_raw_metric(core, name):
    """ Pull a metrics (on HTTP(s)) in "raw" format.

        "raw" format means that the URL must return one number in plain/text.

        We expect to have the following configuration key under
        section "metric.pull.$NAME.":

        * url : where to fetch the metric [mandatory]
        * tags: tags to add on your metric (a dict) [default: no tags]
        * interval : retrive the metric every interval seconds [default: 10s]
        * ssl_check : should we check that SSL certificate are valid
          [default: yes]
    """
    metric_config = core.config.get('metric.pull.%s' % name, {})
    if 'url' not in metric_config:
        logging.warning('Missing URL for metric %s. Ignoring it', name)
        return

    response = _get_url(name, metric_config)
    if response is not None:
        value = None
        try:
            value = float(response.content)
        except ValueError:
            logging.warning(
                'Failed to retrive metric %s : response it not a number',
                name)

        if value is not None:
            metric = {
                'time': time.time(),
                'measurement': name,
                'tags': metric_config.get('tags', {}),
                'fields': {
                    'value': value,
                }
            }
            core.emit_metric(metric)

    core.scheduler.enter(
        metric_config.get('interval', 10), 1, pull_raw_metric, (core, name))


def pull_json_metric(core, name):
    """ Pull a metrics (on HTTP(s)) in "json" format.

        "json" format means that the URL must return a JSON content which
        is the fields of the metric. For simple metric, it should only contains
        one entry "value", e.g.:

        >>> {"value": 42.0}

        We expect to have the following configuration key under
        section "metric.pull.$NAME.":

        * url : where to fetch the metric [mandatory]
        * tags: tags to add on your metric (a dict) [default: no tags]
        * interval : retrive the metric every interval seconds [default: 10s]
        * ssl_check : should we check that SSL certificate are valid
          [default: yes]
    """
    metric_config = core.config.get('metric.pull.%s' % name, {})
    response = None
    if 'url' not in metric_config:
        logging.warning('Missing URL for metric %s. Ignoring it', name)
        return

    response = _get_url(name, metric_config)
    if response is not None:
        fields = None
        try:
            fields = response.json()
        except ValueError:
            logging.warning(
                'Failed to retrive metric %s : response it not a json',
                name)

        if fields is not None:
            metric = {
                'time': time.time(),
                'measurement': name,
                'tags': metric_config.get('tags', {}),
                'fields': fields,
            }
            core.emit_metric(metric)

    core.scheduler.enter(
        metric_config.get('interval', 10), 1, pull_json_metric, (core, name))
