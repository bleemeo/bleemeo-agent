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
# pylint: disable=too-many-lines

import collections
import copy
import datetime
import hashlib
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
import zlib

import paho.mqtt.client as mqtt
import requests
from six.moves import queue
from six.moves import urllib_parse

import bleemeo_agent
import bleemeo_agent.util


MQTT_QUEUE_MAX_SIZE = 2000
REQUESTS_TIMEOUT = 15.0

# API don't accept full length for some object.
API_METRIC_ITEM_LENGTH = 100
API_CONTAINER_NAME_LENGTH = 100
API_SERVICE_INSTANCE_LENGTH = 50


MetricThreshold = collections.namedtuple('MetricThreshold', (
    'low_warning',
    'low_critical',
    'high_warning',
    'high_critical',
))
Metric = collections.namedtuple('Metric', (
    'uuid',
    'label',
    'labels',
    'service_uuid',
    'container_uuid',
    'status_of',
    'thresholds',
    'unit',
    'unit_text',
    'deactivated_at',
))
MetricRegistrationReq = collections.namedtuple('MetricRegistrationReq', (
    'label',
    'labels',
    'service_label',
    'instance',
    'container_name',
    'status_of_label',
    'last_status',
    'last_problem_origins',
    'last_seen',
))
Service = collections.namedtuple('Service', (
    'uuid',
    'label',
    'instance',
    'listen_addresses',
    'exe_path',
    'stack',
    'active',
))
Container = collections.namedtuple('Container', (
    'uuid', 'name', 'docker_id', 'inspect_hash',
))
AgentConfig = collections.namedtuple('AgentConfig', (
    'uuid',
    'name',
    'docker_integration',
    'topinfo_period',
    'metrics_whitelist',
    'metric_resolution',
))
AgentFact = collections.namedtuple('AgentFact', (
    'uuid', 'key', 'value',
))


def services_to_short_key(services):
    reverse_lookup = {}
    for key, service_info in services.items():
        (service_name, instance) = key
        short_key = (service_name, instance[:API_SERVICE_INSTANCE_LENGTH])

        if short_key not in reverse_lookup:
            reverse_lookup[short_key] = key
        else:
            other_info = services[reverse_lookup[short_key]]
            if (service_info.get('active', True)
                    and not other_info.get('active', True)):
                # Prefer the active service
                reverse_lookup[short_key] = key
            elif (service_info.get('container_id', '') >
                  other_info.get('container_id', '')):
                # Completly arbitrary condition that will hopefully keep
                # a consistent result whatever the services order is.
                reverse_lookup[short_key] = key

    result = {
        key: short_key for (short_key, key) in reverse_lookup.items()
    }
    return result


def sort_docker_inspect(inspect):
    """ Sort the docker inspect to have consistent hash value

        Mounts order does not matter but is not consistent between
        call to docker inspect (at least on minikube).
    """
    if inspect.get('Mounts'):
        inspect['Mounts'].sort(
            key=lambda x: (x.get('Source', ''), x.get('Destination', '')),
        )
    return inspect


def _api_datetime_to_time(date_text):
    """ Convert a textual date to an timestamp

        >>> _api_datetime_to_time("2018-06-08T09:06:53.310377Z")
        1528448813.310377
        >>> _api_datetime_to_time(None)  # return None
        >>> _api_datetime_to_time("2018-06-08T09:06:53Z")
        1528448813.0
    """
    if not date_text:
        return None

    formats = [
        '%Y-%m-%dT%H:%M:%S.%fZ',
        '%Y-%m-%dT%H:%M:%SZ',
    ]
    date = None
    for fmt in formats:
        try:
            date = datetime.datetime.strptime(
                date_text, fmt
            ).replace(tzinfo=datetime.timezone.utc)
            break
        except ValueError:
            pass

    if date is None:
        return None
    if hasattr(date, 'timestamp'):
        return date.timestamp()
    epoc = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (date - epoc).total_seconds()


def _api_metric_to_internal(data):
    """ Convert a API metric object to internal Metric object
    """
    labels = {}
    if data.get('item', ''):
        labels['item'] = data['item']
    if 'labels' in data:
        labels.update(data['labels'])
    metric = Metric(
        data['id'],
        data['label'],
        labels,
        data['service'],
        data['container'],
        data['status_of'],
        MetricThreshold(
            data['threshold_low_warning'],
            data['threshold_low_critical'],
            data['threshold_high_warning'],
            data['threshold_high_critical'],
        ),
        data['unit'],
        data['unit_text'],
        _api_datetime_to_time(data['deactivated_at']),
    )
    return metric


class ApiError(Exception):
    def __init__(self, response):
        super(ApiError, self).__init__()
        self.response = response

    def __str__(self):
        try:
            content = self.response.content.decode('utf-8').replace(
                '\r\n', '\n').replace('\n\r', '\n').replace('\n', ' ')
        except UnicodeDecodeError:
            content = repr(self.response.content)
        if len(content) > 70:
            content = '%s...' % content[:67]

        return 'HTTP %s: %s' % (
            self.response.status_code,
            content,
        )


class AuthApiError(ApiError):
    """ Fail to authenticate on API (bad username/password) """


class BleemeoAPI:
    """ class to handle communication with Bleemeo API
    """

    def __init__(self, base_url, auth, user_agent, ssl_verify):
        self.auth = auth
        self.user_agent = user_agent
        self.base_url = base_url
        self.requests_session = requests.Session()
        self._jwt_token = None
        self.ssl_verify = ssl_verify

    def _get_jwt(self):
        url = urllib_parse.urljoin(self.base_url, 'v1/jwt-auth/')
        response = self.requests_session.post(
            url,
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'Content-type': 'application/json',
            },
            data=json.dumps({
                'username': self.auth[0],
                'password': self.auth[1],
            }),
            timeout=10,
            verify=self.ssl_verify,
        )
        if response.status_code != 200:
            if response.status_code < 500:
                err = AuthApiError(response)
            else:
                err = ApiError(response)
            logging.debug('Failed to retrieve JWT: %s', err)
            raise err
        return response.json()['token']

    def api_call(
            self, url, method='get', params=None, data=None,
            allow_redirects=True):
        # pylint: disable=too-many-arguments
        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': self.user_agent,
        }
        if data:
            headers['Content-type'] = 'application/json'

        url = urllib_parse.urljoin(self.base_url, url)

        first_call = True
        while True:
            if self._jwt_token is None:
                self._jwt_token = self._get_jwt()
            headers['Authorization'] = 'JWT %s' % self._jwt_token
            response = self.requests_session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                data=data,
                timeout=REQUESTS_TIMEOUT,
                allow_redirects=allow_redirects,
                verify=self.ssl_verify,
            )
            if response.status_code == 401 and first_call:
                # If authentication failed for the first call,
                # retry immediatly
                logging.debug('JWT token expired, retry authentication')
                self._jwt_token = None
                first_call = False
                continue
            first_call = False

            return response

    def api_iterator(self, url, params=None):
        """ Call Bleemeo API on a list endpoints and return a iterator
            that request all pages
        """
        if params is None:
            params = {}
        else:
            params = params.copy()

        if 'page_size' not in params:
            params['page_size'] = 100

        data = {'next': url}
        while data['next']:
            response = self.api_call(
                data['next'],
                params=params,
            )

            if response.status_code == 404:
                break

            if response.status_code != 200:
                raise ApiError(response)

            # After first call, params are present in URL data['next']
            params = None

            data = response.json()
            for item in data['results']:
                yield item


def convert_docker_date(input_date):
    """ Take a string representing a date using Docker inspect format and return
        None if the date is "0001-01-01T00:00:00Z"
    """
    if input_date is None:
        return None

    if input_date == '0001-01-01T00:00:00Z':
        return None
    return input_date


def get_listen_addresses(service_info):
    """ Return the listen_addresses for a service_info
    """
    try:
        address = socket.gethostbyname(service_info['address'])
    except (socket.gaierror, TypeError, KeyError):
        # gaierror => unable to resolv name
        # TypeError => service_info['address'] is None (happen when
        #              service is on a stopped container)
        # KeyError => no 'address' in service_info (happen when service
        #             is a customer defined using Nagios check).
        address = None

    netstat_ports = {}
    for port_proto, address in service_info.get('netstat_ports', {}).items():
        if port_proto == 'unix':
            continue
        port = int(port_proto.split('/')[0])
        if service_info.get('ignore_high_port') and port > 32000:
            continue
        netstat_ports[port_proto] = address

    if service_info.get('port') is not None and not netstat_ports:
        if service_info['protocol'] == socket.IPPROTO_TCP:
            netstat_ports['%s/tcp' % service_info['port']] = address
        elif service_info['protocol'] == socket.IPPROTO_UDP:
            netstat_ports['%s/udp' % service_info['port']] = address

    return set(
        '%s:%s' % (address, port_proto)
        for (port_proto, address) in netstat_ports.items()
    )


def _prioritize_metrics(metrics):
    """ Move some metrics to the head of the list
    """
    # We do this by swapping "high" priority metric with
    # another metrics.

    priority_label = set((
        'cpu_idle', 'cpu_wait', 'cpu_nice', 'cpu_user', 'cpu_system',
        'cpu_interrupt', 'cpu_softirq', 'cpu_steal',
        'mem_free', 'mem_cached', 'mem_buffered', 'mem_used',
        'io_utilization', 'io_read_bytes', 'io_write_bytes', 'io_reads',
        'io_writes', 'net_bits_recv', 'net_bits_sent', 'net_packets_recv',
        'net_packets_sent', 'net_err_in', 'net_err_out', 'disk_used_perc',
        'swap_used_perc', 'cpu_used', 'mem_used_perc',
        'agent_status',
    ))

    swap_idx = 0
    for (idx, metric) in enumerate(metrics):
        if metric.label in priority_label:
            metrics[idx], metrics[swap_idx] = metrics[swap_idx], metrics[idx]
            swap_idx += 1
    return metrics


class BleemeoCache:
    # pylint: disable=too-many-instance-attributes
    """ In-memory cache backed with state file for Bleemeo API
        objects

        All information stored in this cache could be lost on Agent restart
        (e.g. rebuilt from Bleemeo API)
    """
    # Version 1: initial version
    # Version 2: Added docker_id to Containers
    # Version 3: Added active to Metric
    # Version 4: Changed field "active" (boolean) to "deactivated_at" (time) on
    #            Metric
    # Version 5: Dropped blacklist from AgentConfig
    # Version 6: Added "metric_resolution" to AgentConfig
    # Version 7: Store labels instead of item
    CACHE_VERSION = 7

    def __init__(self, state, skip_load=False):
        self._state = state

        self.metrics = {}
        self.services = {}
        self.tags = []
        self.containers = {}
        self.facts = {}
        self.current_config = None
        self.next_config_at = None
        self.registration_at = None
        self.account_id = None

        self.metrics_by_labelitem = {}
        self.containers_by_name = {}
        self.services_by_labelinstance = {}
        self.facts_by_key = {}

        if not skip_load:
            cache = self._state.get("_bleemeo_cache")
            if cache is None:
                self._load_compatibility()
            self._reload()

    def copy(self):
        new = BleemeoCache(self._state, skip_load=True)
        new.metrics = self.metrics.copy()
        new.services = self.services.copy()
        new.tags = list(self.tags)
        new.containers = self.containers.copy()
        new.facts = self.facts.copy()
        new.current_config = self.current_config
        new.next_config_at = self.next_config_at
        new.registration_at = self.registration_at
        new.account_id = self.account_id
        new.update_lookup_map()
        return new

    def _reload(self):
        # pylint: disable=too-many-branches
        self._state.reload()
        cache = self._state.get("_bleemeo_cache")

        if cache['version'] > self.CACHE_VERSION:
            return

        self.metrics = {}
        self.services = {}
        self.tags = list(cache['tags'])
        self.containers = {}

        registration_at = cache.get('registration_at')
        if registration_at:
            self.registration_at = datetime.datetime.strptime(
                registration_at,
                '%Y-%m-%d %H:%M:%S.%f',
            ).replace(tzinfo=datetime.timezone.utc)

        account_id = cache.get("account_id")
        if account_id:
            self.account_id = account_id

        for metric_uuid, values in cache['metrics'].items():
            values[6] = MetricThreshold(*values[6])
            if cache['version'] < 3:
                # Add default "True" to active field.
                # It will be fixed on next full synchronization that
                # will happen quickly
                values.append(True)
            if cache['version'] < 4:
                # The active boolean changed to a deactivated_at time
                # convert active=True to deactivated_at=None
                # and active=False to deactivated_at=now.
                # It will be fixed on next full synchronization that
                # will happen quickly
                if values[9]:
                    values[9] = None
                else:
                    values[9] = time.time()
            if cache['version'] < 7:
                # Older version stored only item not all labels
                if values[2]:
                    values[2] = {'item': values[2]}
                else:
                    values[2] = {}
            self.metrics[metric_uuid] = Metric(*values)

        for service_uuid, values in cache['services'].items():
            values[3] = set(values[3])
            self.services[service_uuid] = Service(*values)

        # Can't load containers from cache version 1
        if cache['version'] > 1:
            for container_uuid, values in cache['containers'].items():
                self.containers[container_uuid] = Container(*values)

        config = cache.get('current_config')
        if config:
            config[4] = set(config[4])
            if cache['version'] < 5:
                del config[5]
            if cache['version'] < 6:
                # Version 6 introduced metric_resolution
                config.append(10)
            self.current_config = AgentConfig(*config)

        next_config_at = cache.get('next_config_at')
        if next_config_at:
            self.next_config_at = (
                datetime.datetime
                .utcfromtimestamp(next_config_at)
                .replace(tzinfo=datetime.timezone.utc)
            )

        for fact_raw in cache.get('facts', []):
            fact = AgentFact(*fact_raw)
            self.facts[fact.uuid] = fact

        self.update_lookup_map()

    def update_lookup_map(self):
        self.metrics_by_labelitem = {}
        self.containers_by_name = {}
        self.services_by_labelinstance = {}
        self.facts_by_key = {}

        for metric in self.metrics.values():
            item = metric.labels.get('item', '')
            self.metrics_by_labelitem[(metric.label, item)] = metric

        for container in self.containers.values():
            self.containers_by_name[container.name] = container

        for service in self.services.values():
            key = (service.label, service.instance)
            self.services_by_labelinstance[key] = service

        for fact in self.facts.values():
            self.facts_by_key[fact.key] = fact

    def get_core_thresholds(self):
        """ Return thresholds in a format adapted for bleemeo_agent.core
        """
        thresholds = {}
        for metric in self.metrics.values():
            item = metric.labels.get('item', '')
            thresholds[(metric.label, item)] = (
                metric.thresholds._asdict()
            )
        return thresholds

    def get_core_units(self):
        """ Return units in a format adapted for bleemeo_agent.core
        """
        units = {}
        for metric in self.metrics.values():
            item = metric.labels.get('item', '')
            units[(metric.label, item)] = (
                metric.unit, metric.unit_text
            )
        return units

    def save(self):
        cache = {
            'version': self.CACHE_VERSION,
            'metrics': self.metrics,
            'services': self.services,
            'tags': self.tags,
            'facts': list(self.facts.values()),
            'containers': self.containers,
            'current_config': self.current_config,
            'next_config_at':
                self.next_config_at.timestamp()
                if self.next_config_at else None,
            'registration_at':
                self.registration_at.strftime('%Y-%m-%d %H:%M:%S.%f')
                if self.registration_at else None,
            'account_id': self.account_id,
        }
        self._state.set('_bleemeo_cache', cache)

    def _load_compatibility(self):
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-branches
        """ Load cache information from old keys and remove thems
        """
        metrics_uuid = self._state.get_complex_dict('metrics_uuid', {})
        thresholds = self._state.get_complex_dict('thresholds', {})
        services_uuid = self._state.get_complex_dict('services_uuid', {})

        for (key, metric_uuid) in metrics_uuid.items():
            (metric_name, service_name, item) = key
            if metric_uuid is None:
                continue

            # PRODUCT-279: elasticsearch_search_time was previously not
            # associated with the service elasticsearch
            if (metric_name == 'elasticsearch_search_time'
                    and service_name is None):
                service_name = 'elasticsearch'

            if (metric_name, item) in thresholds:
                tmp = thresholds[(metric_name, item)]
                threshold = MetricThreshold(
                    tmp['low_warning'],
                    tmp['low_critical'],
                    tmp['high_warning'],
                    tmp['high_critical'],
                )
            else:
                threshold = MetricThreshold(None, None, None, None)

            service = services_uuid.get((service_name, item))

            if service_name and not service:
                continue
            if service_name:
                service_uuid = service['uuid']
            else:
                service_uuid = None

            labels = {}
            if item:
                labels['item'] = item

            self.metrics[metric_uuid] = Metric(
                metric_uuid,
                metric_name,
                labels,
                service_uuid,
                None,
                None,
                threshold,
                None,
                None,
                None,
            )
        services_uuid = self._state.get_complex_dict('services_uuid', {})
        for service_info in services_uuid.values():
            if service_info.get('uuid') is None:
                continue

            listen_addresses = set(
                service_info.get('listen_addresses', '').split(',')
            )
            if '' in listen_addresses:
                listen_addresses.remove('')
            self.services[service_info['uuid']] = Service(
                service_info['uuid'],
                service_info['label'],
                service_info.get('instance'),
                listen_addresses,
                service_info.get('exe_path', ''),
                service_info.get('stack', ''),
                service_info.get('active', True),
            )

        self.tags = list(self._state.get('tags_uuid', {}))

        self.save()
        self._reload()
        try:
            self._state.delete('metrics_uuid')
            self._state.delete('services_uuid')
            self._state.delete('thresholds')
            self._state.delete('tags_uuid')
            self._state.delete('docker_container_uuid')
        except KeyError:
            pass


class BleemeoConnector(threading.Thread):
    # pylint: disable=too-many-instance-attributes
    # pylint: disable=too-many-public-methods

    def __init__(self, core):
        super(BleemeoConnector, self).__init__()
        self.core = core

        self._metric_queue = queue.Queue(10000)
        self._unregistered_metric_queue = queue.Queue()
        self.connected = False
        self._last_disconnects = []
        self._successive_mqtt_errors = 0
        self._mqtt_reconnect_at = 0
        self._duplicate_disable_until = 0
        self._mqtt_queue_size = 0
        self._mqtt_thread = None
        self._last_diagnostic = None

        self.trigger_full_sync = False
        self.trigger_fact_sync = 0
        self._update_metrics = set()
        self._update_metrics_lock = threading.Lock()
        self._sync_loop_event = threading.Event()
        self.last_containers_removed = bleemeo_agent.util.get_clock()
        self.last_config_will_change_msg = bleemeo_agent.util.get_clock()
        self._bleemeo_cache = None
        self._account_mismatch_notify_at = None

        self.mqtt_client = mqtt.Client()

        self._api_support_labels = True
        self._current_metrics = {}
        self._current_metrics_lock = threading.Lock()
        # Make sure this metrics exists and try to be registered
        self._current_metrics[('agent_status', '')] = MetricRegistrationReq(
            'agent_status',
            {},
            None,
            '',
            '',
            None,
            bleemeo_agent.type.STATUS_OK,
            '',
            bleemeo_agent.util.get_clock(),
        )

    def on_connect(self, _client, _userdata, _flags, result_code):
        if result_code == 0 and not self.core.is_terminating.is_set():
            self.connected = True
            msg = {
                'public_ip': self.core.last_facts.get('public_ip'),
            }
            self.publish(
                'v1/agent/%s/connect' % self.agent_uuid,
                json.dumps(msg),
            )
            # FIXME: PRODUCT-137: to be removed when upstream bug is fixed
            # pylint: disable=protected-access
            if (self.mqtt_client._ssl is not None
                    and not isinstance(self.mqtt_client._ssl, bool)):
                # pylint: disable=no-member
                self.mqtt_client._ssl.setblocking(0)

            self.mqtt_client.subscribe(
                'v1/agent/%s/notification' % self.agent_uuid
            )
            self._successive_mqtt_errors = 0
            logging.info('MQTT connection established')
            self.core.fire_triggers(facts=True)

    def on_disconnect(self, _client, _userdata, result_code):
        if self.connected:
            logging.info('MQTT connection lost')
        self._last_disconnects.append(bleemeo_agent.util.get_clock())
        self._last_disconnects = self._last_disconnects[-15:]
        self._successive_mqtt_errors += 1
        self.connected = False

        clock_now = bleemeo_agent.util.get_clock()
        if (self._successive_mqtt_errors > 3
                and not self._mqtt_reconnect_at):
            if result_code == 1:
                # code 1 is a generic code. It could be timeout,
                # connection refused, bad protocol, etc
                # The most likely error that would trigger successive errors is
                # being unable to connect due to connection refused/dropped.
                reason = 'unable to connect'
            elif result_code == 0:
                # code 0 is succesful disconnect, e.g. after call to disconnect
                # This case should never cause successive errors
                reason = 'unknown error'
            else:
                reason = (
                    'connection refused. Was this server deleted '
                    'on Bleemeo Cloud platform ?'
                )
            delay = random.randint(
                min(300, 20 * self._successive_mqtt_errors),
                min(900, 60 * self._successive_mqtt_errors)
            )
            logging.info(
                'Unable to connect to MQTT: %s.'
                ' Disable MQTT for %d seconds',
                reason,
                delay,
            )
            self._mqtt_reconnect_at = clock_now + delay
        elif (len(self._last_disconnects) >= 6
              and self._last_disconnects[-6] > clock_now - 60
              and not self._mqtt_reconnect_at):
            delay = 60 + random.randint(-15, 15)
            logging.info(
                'Too many attempt to connect to MQTT on last minute.'
                ' Disabling MQTT for %d seconds',
                delay
            )
            self._mqtt_reconnect_at = clock_now + delay
            self.trigger_fact_sync = clock_now
        elif (len(self._last_disconnects) >= 15
              and self._last_disconnects[-15] > clock_now - 600
              and not self._mqtt_reconnect_at):
            delay = 300 + random.randint(-60, 60)
            logging.info(
                'Too many attempt to connect to MQTT on last 10 minutes.'
                ' Disabling MQTT for %d seconds',
                delay,
            )
            self._mqtt_reconnect_at = clock_now + delay
            self.trigger_fact_sync = clock_now

    def on_message(self, _client, _userdata, msg):
        notify_topic = 'v1/agent/%s/notification' % self.agent_uuid
        if msg.topic == notify_topic and len(msg.payload) < 1024 * 64:
            try:
                body = json.loads(msg.payload.decode('utf-8'))
            except Exception as exc:  # pylint: disable=broad-except
                logging.info('Failed to decode message for Bleemeo: %s', exc)
                return

            if 'message_type' not in body:
                return
            if body['message_type'] == 'threshold-update':
                logging.debug('Got "threshold-update" message from Bleemeo')
                if 'metric_uuid' in body:
                    with self._update_metrics_lock:
                        self._update_metrics.add(body['metric_uuid'])
                else:
                    self.trigger_full_sync = True
            if body['message_type'] == 'config-changed':
                logging.debug('Got "config-changed" message from Bleemeo')
                self.trigger_full_sync = True
            if body['message_type'] == 'config-will-change':
                logging.debug('Got "config-will-change" message from Bleemeo')
                self.last_config_will_change_msg = (
                    bleemeo_agent.util.get_clock()
                )

    def on_publish(self, _client, _userdata, _mid):
        self._mqtt_queue_size -= 1
        self.core.update_last_report()

    def check_config_requirement(self):
        sleep_delay = 10
        while (self.core.config['bleemeo.account_id'] is None
               or self.core.config['bleemeo.registration_key'] is None):
            logging.warning(
                'bleemeo.account_id and/or '
                'bleemeo.registration_key is undefine. '
                'Please see https://docs.bleemeo.com/how-to-configure-agent')
            self.core.is_terminating.wait(sleep_delay)
            if self.core.is_terminating.is_set():
                raise StopIteration
            self.core.reload_config()
            sleep_delay = min(sleep_delay * 2, 600)

        if self.core.state.get('password') is None:
            self.core.state.set(
                'password', bleemeo_agent.util.generate_password())

    def init(self):
        if self.core.sentry_client and self.agent_uuid:
            self.core.sentry_client.site = self.agent_uuid

        try:
            self._bleemeo_cache = BleemeoCache(self.core.state)
        except Exception:  # pylint: disable=broad-except
            logging.warning(
                'Error while loading the cache. Starting with empty cache',
                exc_info=True,
            )
            self._bleemeo_cache = BleemeoCache(self.core.state, skip_load=True)

        if self._bleemeo_cache.current_config:
            self.core.configure_resolution(
                self._bleemeo_cache.current_config.topinfo_period,
                self._bleemeo_cache.current_config.metric_resolution,
            )

    def run(self):
        try:
            self.check_config_requirement()
        except StopIteration:
            return

        sync_thread = threading.Thread(target=self._bleemeo_synchronizer)
        sync_thread.daemon = True
        sync_thread.start()

        while not self._ready_for_mqtt():
            self.core.is_terminating.wait(1)
            if self.core.is_terminating.is_set():
                return

        self._mqtt_setup()

        while not self.core.is_terminating.is_set():
            self._loop()
            self._mqtt_check()

        if self.connected and self.upgrade_in_progress:
            self.publish(
                'v1/agent/%s/disconnect' % self.agent_uuid,
                json.dumps({'disconnect-cause': 'Upgrade'}),
                force=True
            )
        elif self.connected:
            self.publish(
                'v1/agent/%s/disconnect' % self.agent_uuid,
                json.dumps({'disconnect-cause': 'Clean shutdown'}),
                force=True
            )

        self._mqtt_stop(wait_delay=5)
        self._sync_loop_event.set()  # unblock sync_loop thread
        sync_thread.join(5)

    def stop(self):
        """ Stop and wait to completion of self
        """
        # Break _loop() immediatly
        self._metric_queue.put(None)
        self.join()

    def _ready_for_mqtt(self):
        """ Check for requirement needed before MQTT connection

            * agent must be registered
            * it need initial facts
            * "agent_status" metrics must be registered
        """
        agent_status = self._bleemeo_cache.metrics_by_labelitem.get(
            ('agent_status', '')
        )
        return (
            self.agent_uuid is not None and
            self.core.last_facts and
            agent_status is not None
        )

    def health_check(self):
        """ Check the Bleemeo connector works correctly. Log any issue found
        """
        clock_now = bleemeo_agent.util.get_clock()
        need_diag = False

        if self.agent_uuid is None:
            logging.info('Agent not yet registered')
            need_diag = True

        if not self.connected and self._mqtt_reconnect_at > clock_now:
            logging.info(
                'Bleemeo connection (MQTT) is disabled for %d seconds',
                self._mqtt_reconnect_at - clock_now,
            )
        elif not self.connected:
            logging.info(
                'Bleemeo connection (MQTT) is currently not established'
            )
            need_diag = True

        if self._mqtt_queue_size >= MQTT_QUEUE_MAX_SIZE:
            logging.warning(
                'Sending queue to Bleemeo Cloud is full. '
                'New messages are dropped'
            )
        elif self._mqtt_queue_size > 10:
            logging.info(
                '%s messages waiting to be sent to Bleemeo Cloud',
                self._mqtt_queue_size,
            )

        if self._unregistered_metric_queue.qsize() > 10:
            logging.info(
                '%s metric points blocked due to metric not yet registered',
                self._unregistered_metric_queue.qsize(),
            )

        if need_diag and (
                self._last_diagnostic is None or
                clock_now - self._last_diagnostic > 3600):
            self._last_diagnostic = clock_now
            try:
                self._diagnostic()
            except Exception:  # pylint: disable=broad-except
                logging.info(
                    "Diagnostic for Bleemeo connector connection failed:",
                    exc_info=True,
                )

    def _diagnostic(self):
        logging.info("Diagnostic for Bleemeo Cloud platform connection:")
        logging.info("The Bleemeo account UUID is %s", self.account_id)
        if self.agent_uuid is None:
            logging.info("This agnet is not yet registered")
        else:
            logging.info(
                "This agent is registered with UUID = %s", self.agent_uuid,
            )
        try:
            mqtt_ip = socket.gethostbyname(self.core.config['bleemeo.mqtt.host'])
            logging.info(
                "MQTT server (%s) resolve to IP %s",
                self.core.config['bleemeo.mqtt.host'],
                mqtt_ip,
            )
        except socket.error as exc:
            logging.info(
                "Unable to resolve DNS name for %s: %s",
                self.core.config['bleemeo.mqtt.host'],
                exc
            )
        try:
            tcp_to_mqtt = socket.create_connection(
                (
                    self.core.config['bleemeo.mqtt.host'],
                    self.core.config['bleemeo.mqtt.port'],
                ),
                timeout=5
            )
        except socket.error as exc:
            logging.info(
                "Unable to open an TCP connection to %s:%d: %s."
                " Is you firewall blocking connection ?",
                self.core.config['bleemeo.mqtt.host'],
                self.core.config['bleemeo.mqtt.port'],
                exc,
            )
            tcp_to_mqtt = None

        try:
            if tcp_to_mqtt is not None:
                tls_context = ssl.create_default_context()
                ssl_sock = tls_context.wrap_socket(
                    tcp_to_mqtt,
                    server_hostname=self.core.config['bleemeo.mqtt.host'],
                    do_handshake_on_connect=False,
                )

                ssl_sock.settimeout(5)
                ssl_sock.do_handshake()
                logging.info("SSL connection to MQTT can be established")
        except ssl.CertificateError as exc:
            logging.info("Unable to open SSL connection to MQTT: %s", exc)
        except socket.error as exc:
            logging.info("Unable to open SSL connection to MQTT: %s", exc)

        if tcp_to_mqtt is not None:
            tcp_to_mqtt.close()

        try:
            requests.get(self.bleemeo_base_url, timeout=5)
        except Exception as exc:
            logging.info("Unable to do HTTP request to Bleemeo: %s", exc)


    def _mqtt_setup(self):
        self.mqtt_client.will_set(
            'v1/agent/%s/disconnect' % self.agent_uuid,
            json.dumps({'disconnect-cause': 'disconnect-will'}),
            1,
        )
        if hasattr(ssl, 'PROTOCOL_TLSv1_2'):
            # Need Python 3.4+ or 2.7.9+
            tls_version = ssl.PROTOCOL_TLSv1_2
        else:
            tls_version = ssl.PROTOCOL_TLSv1

        if self.core.config['bleemeo.mqtt.ssl']:
            cafile = self.core.config['bleemeo.mqtt.cafile']
            if cafile is not None and '$INSTDIR' in cafile and os.name == 'nt':
                # Under Windows, $INSTDIR is remplaced by installation
                # directory
                cafile = cafile.replace(
                    '$INSTDIR',
                    bleemeo_agent.util.windows_instdir()
                )
            if self.core.config['bleemeo.mqtt.ssl_insecure']:
                self.mqtt_client.tls_set(
                    cafile,
                    tls_version=tls_version,
                    cert_reqs=ssl.CERT_NONE,
                )
            else:
                self.mqtt_client.tls_set(
                    cafile,
                    tls_version=tls_version,
                )
            self.mqtt_client.tls_insecure_set(
                self.core.config['bleemeo.mqtt.ssl_insecure']
            )

        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_disconnect = self.on_disconnect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.on_publish = self.on_publish

        self.mqtt_client.username_pw_set(
            self.agent_username,
            self.agent_password,
        )
        self._mqtt_start()

    def _mqtt_start(self):
        assert self._mqtt_thread is None

        mqtt_host = self.core.config['bleemeo.mqtt.host']
        mqtt_port = self.core.config['bleemeo.mqtt.port']

        try:
            logging.debug('Connecting to MQTT broker at %s', mqtt_host)
            self.mqtt_client.connect_async(
                mqtt_host,
                mqtt_port,
                60,
            )
        except socket.error:
            pass

        self._mqtt_thread = threading.Thread(
            target=self.mqtt_client.loop_forever,
            kwargs={'retry_first_connection': True},
        )
        self._mqtt_thread.daemon = True
        self._mqtt_thread.start()

    def _mqtt_stop(self, wait_delay=5):
        if wait_delay:
            deadline = bleemeo_agent.util.get_clock() + wait_delay
            while (self._mqtt_queue_size > 0
                   and bleemeo_agent.util.get_clock() < deadline):
                time.sleep(0.1)
        else:
            deadline = 0

        self.mqtt_client.disconnect()
        if self._mqtt_thread is not None:
            remaining = max(0, deadline - bleemeo_agent.util.get_clock())
            self._mqtt_thread.join(min(3, remaining))
        self._mqtt_thread = None

    def _mqtt_check(self):
        """ Check MQTT connection status

            * Temporary disconnect if requested to.
            * Reconnect when temporary disconnection expired
            * Stop if abnormally disconnected
        """
        if (self._mqtt_thread is not None
                and not self._mqtt_thread.is_alive()):
            logging.error('Thread MQTT connector crashed. Stopping agent')
            self.core.is_terminating.set()
            return

        clock_now = bleemeo_agent.util.get_clock()
        if (self._mqtt_thread is not None
                and self._mqtt_reconnect_at > clock_now):
            self._mqtt_stop(wait_delay=0)
        elif (self._mqtt_thread is None
              and self._mqtt_reconnect_at < clock_now):
            logging.info('Re-enabling MQTT connection')
            self._mqtt_reconnect_at = 0
            self._mqtt_start()

    def _loop(self):
        # pylint: disable=too-many-branches
        """ Call as long as agent is running. It's the "main" method for
            Bleemeo connector thread.
        """
        metrics = []
        timeout = 6
        deadline = None

        try:
            while True:
                metric_point = self._metric_queue.get(timeout=timeout)
                if metric_point is None:
                    break
                if deadline is None:
                    deadline = time.time() + 6
                timeout = max(0, min(deadline - time.time(), 6))
                if self._duplicate_disable_until:
                    continue
                item = metric_point.labels.get('item', '')
                short_item = item[:API_METRIC_ITEM_LENGTH]
                if metric_point.service_instance:
                    short_item = short_item[:API_SERVICE_INSTANCE_LENGTH]
                key = (
                    metric_point.label,
                    short_item,
                )
                metric = self._bleemeo_cache.metrics_by_labelitem.get(key)

                if metric is None:
                    if time.time() - metric_point.time > 7200:
                        continue
                    elif key not in self._current_metrics:
                        continue
                    else:
                        if self._unregistered_metric_queue.qsize() > 100000:
                            self._unregistered_metric_queue_cleanup()
                        self._unregistered_metric_queue.put(metric_point)
                    continue

                bleemeo_metric = {
                    'uuid': metric.uuid,
                    'measurement': metric.label,
                    'time': metric_point.time,
                    'value': metric_point.value,
                }
                if 'item' in metric.labels:
                    bleemeo_metric['item'] = metric.labels['item']
                if metric_point.status_code is not None:
                    bleemeo_metric['status'] = bleemeo_agent.type.STATUS_NAME[
                        metric_point.status_code
                    ]
                    if metric_point.service_label:
                        # If the service received a kill signal, give 5 minutes
                        # of grace time after that kill signal.
                        service_key = (
                            metric_point.service_label,
                            metric_point.service_instance,
                        )
                        last_kill_at = self.core.services.get(
                            service_key, {}
                        ).get(
                            'last_kill_at', 0,
                        )
                        clock_now = bleemeo_agent.util.get_clock()
                        grace_period = last_kill_at + 300 - clock_now
                        # Ignore grace period shorter than 1 minute. Without
                        # explicit grace period, a default of 1 minute will
                        # be used.
                        if grace_period > 60:
                            bleemeo_metric['event_grace_period'] = grace_period
                if metric_point.problem_origin:
                    bleemeo_metric['check_output'] = (
                        metric_point.problem_origin
                    )
                metrics.append(bleemeo_metric)
                if len(metrics) > 2000:
                    break
        except queue.Empty:
            pass

        if metrics:
            self.publish(
                'v1/agent/%s/data' % self.agent_uuid,
                json.dumps(metrics)
            )

    def publish_top_info(self, top_info):
        if self.agent_uuid is None:
            return

        if not self.connected:
            return

        self.publish(
            'v1/agent/%s/top_info' % self.agent_uuid,
            bytearray(zlib.compress(json.dumps(top_info).encode('utf8')))
        )

    def publish(self, topic, message, force=False):
        if self._mqtt_queue_size > MQTT_QUEUE_MAX_SIZE and not force:
            return

        self._mqtt_queue_size += 1
        self.mqtt_client.publish(
            topic,
            message,
            1)

    def register(self):
        """ Register the agent to Bleemeo SaaS service

            Return an error message or None if no error. Absence of error does
            not necessary means registration was done. It may have been delayed
            due to information not yet available.
        """
        base_url = self.bleemeo_base_url
        registration_url = urllib_parse.urljoin(base_url, '/v1/agent/')

        fqdn = self.core.last_facts.get('fqdn')
        if not fqdn:
            logging.debug('Register delayed, fact fqdn not available')
            return None
        name = self.core.config['bleemeo.initial_agent_name']
        if not name:
            name = fqdn

        registration_key = self.core.config['bleemeo.registration_key']
        payload = {
            'account': self.core.config['bleemeo.account_id'],
            'initial_password': self.core.state.get('password'),
            'display_name': name,
            'fqdn': fqdn,
        }

        content = None
        try:
            response = requests.post(
                registration_url,
                data=json.dumps(payload),
                auth=(
                    '%s@bleemeo.com' % self.core.config['bleemeo.account_id'],
                    registration_key
                ),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-type': 'application/json',
                    'User-Agent': self.core.http_user_agent,
                },
                timeout=REQUESTS_TIMEOUT,
                verify=not self.core.config['bleemeo.api_ssl_insecure'],
            )
            content = response.json()
        except requests.exceptions.RequestException:
            response = None
        except ValueError:
            pass  # unable to decode JSON

        if (response is not None
                and response.status_code == 201
                and content is not None
                and 'id' in content):
            self.core.state.set('agent_uuid', content['id'])
            logging.debug('Regisration successfull')
        elif content is not None:
            if 'Invalid username/password' in str(content):
                return (
                    'Wrong credential for registration. '
                    'Configuration contains account_id=%s and '
                    'registration_key starts with %s' % (
                        self.core.config['bleemeo.account_id'],
                        registration_key[:8],
                    )
                )
            return 'Registration failed: %s' % content
        elif response is not None:
            return 'Registration failed: %s' % content[:100]
        else:
            return 'Registration failed: unable to connect to API'

        if self.core.sentry_client and self.agent_uuid:
            self.core.sentry_client.site = self.agent_uuid

        return None

    def _bleemeo_synchronizer(self):
        if self._ready_for_mqtt():
            # Not a new agent. Most thing must be already synced.
            # Give a small jitter to avoid lots of agent to sync
            # at the same time.
            time.sleep(random.randint(5, 30))
        while not self.core.is_terminating.is_set():
            try:
                self._sync_loop()
            except Exception:  # pylint: disable=broad-except
                logging.warning(
                    'Bleemeo synchronization loop crashed.'
                    ' Restarting it in 60 seconds',
                    exc_info=True,
                )
            finally:
                self.core.is_terminating.wait(60)

    def _sync_loop(self):
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-statements
        """ Synchronize object between local state and Bleemeo SaaS
        """
        next_full_sync = 0
        last_sync = 0
        bleemeo_cache = self._bleemeo_cache.copy()

        bleemeo_api = None

        last_metrics_count = 0
        successive_errors = 0
        last_duplicated_events = []
        error_delay = 5
        last_error = None
        first_loop = True
        interrupted = False

        while not self.core.is_terminating.is_set():
            duplicated_checked = False

            if last_error is not None:
                successive_errors += 1
                if isinstance(last_error, AuthApiError):
                    if 'fqdn' in bleemeo_cache.facts_by_key:
                        with_fqdn = (
                            ' with fqdn %s' %
                            bleemeo_cache.facts_by_key['fqdn'].value
                        )
                    else:
                        with_fqdn = ''
                    logging.info(
                        'Synchronize with Bleemeo Cloud platform failed: '
                        'Unable to log in with credentials from state.json. '
                        'Using agent ID %s%s. '
                        'Was this server deleted on Bleemeo Cloud platform ?',
                        self.agent_uuid,
                        with_fqdn,
                    )
                    delay = random.randint(
                        min(300, successive_errors * 10),
                        min(900, successive_errors * 30),
                    )
                else:
                    logging.info(
                        'Synchronize with Bleemeo Cloud platform failed: %s',
                        last_error,
                    )
                    delay = random.randint(
                        min(150, successive_errors * 5),
                        min(300, successive_errors * 10),
                    )
                error_delay = min(5 + successive_errors, 45)
            elif first_loop:
                delay = 0
            elif not self._duplicate_disable_until:
                successive_errors = 0
                delay = 15

            last_error = None
            first_loop = False

            clock_now = bleemeo_agent.util.get_clock()
            if self._duplicate_disable_until > clock_now:
                logging.info(
                    'Bleemeo connector is disabled for %d seconds',
                    self._duplicate_disable_until - clock_now,
                )
                self.core.is_terminating.wait(min(
                    60, self._duplicate_disable_until - clock_now,
                ))
                continue
            elif self._duplicate_disable_until:
                logging.info('Re-enabling Bleemeo connector')
                self._duplicate_disable_until = 0
                next_full_sync = 0

            clock_now = bleemeo_agent.util.get_clock()
            deadline = clock_now + delay
            while clock_now < deadline:
                remain_delay = deadline - clock_now
                if remain_delay > 60:
                    logging.info(
                        'Synchronize with Bleemeo Cloud platform still have to'
                        ' wait %d seconds due to last error',
                        remain_delay,
                    )
                if interrupted:
                    self.core.is_terminating.wait(min(60, remain_delay))
                else:
                    interrupted = self._sync_loop_event.wait(
                        min(60, remain_delay)
                    )
                clock_now = bleemeo_agent.util.get_clock()

            if interrupted:
                # Wait a tiny bit, so other metrics in the same batch could
                # arrive
                self.core.is_terminating.wait(1)
            interrupted = False
            self._sync_loop_event.clear()
            if self.core.is_terminating.is_set():
                break

            if self.agent_uuid is None:
                last_error = self.register()

            if self.agent_uuid is None:
                continue

            if bleemeo_api is None:
                bleemeo_api = BleemeoAPI(
                    self.bleemeo_base_url,
                    (self.agent_username, self.agent_password),
                    self.core.http_user_agent,
                    not self.core.config['bleemeo.api_ssl_insecure'],
                )

            if (bleemeo_cache.next_config_at is not None and
                    bleemeo_cache.next_config_at.timestamp() < time.time()):
                self.trigger_full_sync = True

            if self.trigger_full_sync:
                next_full_sync = 0
                time.sleep(random.randint(5, 15))
                self.trigger_full_sync = False

            with self._current_metrics_lock:
                metrics_count = len(self._current_metrics)

            sync_run = False
            metrics_sync = False
            clock_now = bleemeo_agent.util.get_clock()

            if (next_full_sync < clock_now
                    or last_sync <= self.last_config_will_change_msg):
                try:
                    if not duplicated_checked:
                        self._sync_check_duplicated(
                            bleemeo_cache, bleemeo_api, last_duplicated_events,
                        )
                        duplicated_checked = True
                    if self._duplicate_disable_until:
                        continue
                    self._sync_agent(bleemeo_cache, bleemeo_api)
                    sync_run = True
                except (ApiError, requests.exceptions.RequestException) as exc:
                    logging.debug('Unable to synchronize agent. %s', exc)
                    self.core.is_terminating.wait(error_delay)
                    last_error = exc

            if (next_full_sync < clock_now or
                    last_sync < self.core.last_facts_update or
                    last_sync < self.trigger_fact_sync):
                try:
                    if not duplicated_checked:
                        self._sync_check_duplicated(
                            bleemeo_cache, bleemeo_api, last_duplicated_events,
                        )
                        duplicated_checked = True
                    if self._duplicate_disable_until:
                        continue
                    self._sync_facts(bleemeo_cache, bleemeo_api)
                    sync_run = True
                except (ApiError, requests.exceptions.RequestException) as exc:
                    logging.debug('Unable to synchronize facts. %s', exc)
                    self.core.is_terminating.wait(error_delay)
                    last_error = exc

            if (next_full_sync <= clock_now or
                    last_sync <= self.last_containers_removed or
                    last_sync <= self.core.last_discovery_update):
                try:
                    if not duplicated_checked:
                        self._sync_check_duplicated(
                            bleemeo_cache, bleemeo_api, last_duplicated_events
                        )
                        duplicated_checked = True
                    if self._duplicate_disable_until:
                        continue
                    full = (
                        next_full_sync <= clock_now or
                        last_sync <= self.last_containers_removed or
                        # After 3 successive_errors force a full sync.
                        successive_errors == 3
                    )
                    self._sync_services(bleemeo_cache, bleemeo_api, full)
                    # Metrics registration may need services to be synced.
                    # For a pass of metric registrations
                    metrics_sync = True
                    sync_run = True
                except (ApiError, requests.exceptions.RequestException) as exc:
                    logging.debug('Unable to synchronize services. %s', exc)
                    self.core.is_terminating.wait(error_delay)
                    last_error = exc

            if (next_full_sync <= clock_now or
                    last_sync <= self.core.last_discovery_update):
                try:
                    if not duplicated_checked:
                        self._sync_check_duplicated(
                            bleemeo_cache, bleemeo_api, last_duplicated_events,
                        )
                        duplicated_checked = True
                    if self._duplicate_disable_until:
                        continue
                    full = (
                        next_full_sync <= clock_now or
                        # After 3 successive_errors force a full sync.
                        successive_errors == 3
                    )
                    self._sync_containers(bleemeo_cache, bleemeo_api, full)
                    # Metrics registration may need containers to be synced.
                    # For a pass of metric registrations
                    metrics_sync = True
                    sync_run = True
                except (ApiError, requests.exceptions.RequestException) as exc:
                    logging.debug('Unable to synchronize containers. %s', exc)
                    self.core.is_terminating.wait(error_delay)
                    last_error = exc

            with self._update_metrics_lock:
                update_metrics = self._update_metrics
                self._update_metrics = set()

            if (metrics_sync or
                    update_metrics or
                    next_full_sync <= clock_now or
                    last_sync <= self.core.last_discovery_update or
                    last_metrics_count != metrics_count):
                try:
                    if not duplicated_checked:
                        self._sync_check_duplicated(
                            bleemeo_cache, bleemeo_api, last_duplicated_events,
                        )
                        duplicated_checked = True
                    if self._duplicate_disable_until:
                        continue
                    with self._current_metrics_lock:
                        self._current_metrics = {
                            key: value
                            for (key, value) in self._current_metrics.items()
                            if self.sent_metric(
                                value.label,
                                value.service_label and value.label == (
                                    value.service_label + '_status'
                                ),
                                bleemeo_cache,
                            )
                        }
                    full = (
                        next_full_sync <= clock_now or
                        # After 3 successive_errors force a full sync.
                        successive_errors == 3
                    )
                    sync_success = self._sync_metrics(
                        bleemeo_cache, bleemeo_api, update_metrics, full,
                    )
                    if sync_success:
                        last_metrics_count = metrics_count
                    sync_run = True
                except (ApiError, requests.exceptions.RequestException) as exc:
                    logging.debug('Unable to synchronize metrics. %s', exc)
                    self.core.is_terminating.wait(error_delay)
                    last_error = exc

            if next_full_sync < clock_now and last_error is None:
                next_full_sync = (
                    clock_now +
                    random.randint(3500, 3700)
                )
                bleemeo_cache.save()
                logging.debug(
                    'Next full sync in %d seconds',
                    next_full_sync - clock_now,
                )

            if sync_run and last_error is None:
                last_sync = clock_now

            self._bleemeo_cache = bleemeo_cache.copy()

            if sync_run:
                self._unregistered_metric_queue_cleanup(
                    push_to_metric_queue=True
                )

    @property
    def registration_at(self):
        return self._bleemeo_cache.registration_at

    def _sync_agent(self, bleemeo_cache, bleemeo_api):
        # pylint: disable=too-many-branches
        logging.debug('Synchronize agent')
        tags = set(self.core.config['tags'])

        response = bleemeo_api.api_call(
            'v1/agent/%s/' % self.agent_uuid,
            'patch',
            params={
                'fields': 'tags,current_config,next_config_at'
                          ',created_at,account'
            },
            data=json.dumps({'tags': [
                {'name': x} for x in tags if x and len(x) <= 100
            ]}),
        )
        if response.status_code >= 400:
            raise ApiError(response)

        data = response.json()

        if data['created_at']:
            bleemeo_cache.registration_at = datetime.datetime.strptime(
                data['created_at'],
                '%Y-%m-%dT%H:%M:%S.%fZ',
            ).replace(tzinfo=datetime.timezone.utc)

        if data['account']:
            bleemeo_cache.account_id = data['account']
            if (data['account'] != self.core.config['bleemeo.account_id'] and
                    self._account_mismatch_notify_at is None):
                self._account_mismatch_notify_at = (
                    bleemeo_agent.util.get_clock()
                )
                logging.warning(
                    'Account ID in configuration file ("%s") mismatch'
                    ' the current account ID ("%s"). The Account ID from'
                    ' configuration file will be ignored.',
                    self.core.config['bleemeo.account_id'],
                    data['account'],
                )

        bleemeo_cache.tags = []
        for tag in data['tags']:
            if not tag['is_automatic']:
                bleemeo_cache.tags.append(tag['name'])

        if data.get('next_config_at'):
            bleemeo_cache.next_config_at = datetime.datetime.strptime(
                data['next_config_at'],
                '%Y-%m-%dT%H:%M:%SZ',
            ).replace(tzinfo=datetime.timezone.utc)
        else:
            bleemeo_cache.next_config_at = None

        config_uuid = data.get('current_config')
        if config_uuid is None:
            bleemeo_cache.current_config = None
            return

        response = bleemeo_api.api_call(
            '/v1/accountconfig/%s/' % config_uuid,
            allow_redirects=False
        )
        if response.status_code == 302:
            response = bleemeo_api.api_call(
                '/v1/config/%s/' % config_uuid,
            )
        if response.status_code >= 400:
            raise ApiError(response)

        data = response.json()
        if data.get('metrics_agent_whitelist'):
            whitelist = set(data['metrics_agent_whitelist'].split(','))
        elif data.get('metrics_whitelist'):
            whitelist = set(data['metrics_whitelist'].split(','))
        else:
            whitelist = set()

        whitelist = set(x.strip() for x in whitelist)

        try:
            metric_resolution = int(data.get('metrics_agent_resolution', '10'))
        except ValueError:
            metric_resolution = 10

        config = AgentConfig(
            data['id'],
            data.get('name', 'no-name'),
            data.get('docker_integration', True),
            data.get(
                'live_process_resolution', data.get('topinfo_period', 10)
            ),
            whitelist,
            metric_resolution,
        )
        if bleemeo_cache.current_config == config:
            return
        bleemeo_cache.current_config = config

        self.core.configure_resolution(
            config.topinfo_period, config.metric_resolution
        )
        self.core.fire_triggers(updates_count=True)
        logging.info('Changed to configuration %s', config.name)

    def _sync_metrics(self, bleemeo_cache, bleemeo_api, update_metrics, full):
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-statements
        """ Synchronize metrics with Bleemeo SaaS
        """
        logging.debug('Synchronize metrics (full=%s)', full)
        clock_now = bleemeo_agent.util.get_clock()
        metric_url = 'v1/metric/'
        sync_success = True

        with self._current_metrics_lock:
            current_metrics = list(self._current_metrics.values())
        # If one metric fail to register, it may block other metric that would
        # register correctly. To reduce this risk, randomize the list, so on
        # next run, the metric that failed to register may no longer block
        # other.
        random.shuffle(current_metrics)
        current_metrics = _prioritize_metrics(current_metrics)

        pending_registrations = []
        for reg_req in current_metrics:
            item = reg_req.labels.get('item', '')
            short_item = item[:API_METRIC_ITEM_LENGTH]
            if reg_req.service_label:
                short_item = short_item[:API_SERVICE_INSTANCE_LENGTH]
            key = (reg_req.label, short_item)
            metric = bleemeo_cache.metrics_by_labelitem.get(key)
            if metric is None:
                pending_registrations.append(key)

        active_metric_count = 0
        for metric in bleemeo_cache.metrics.values():
            if not metric.deactivated_at:
                active_metric_count += 1

        if len(update_metrics) > 0.03 * active_metric_count:
            # If more than 3% of known active metrics needs update, do a full
            # update. 3% is arbitrary choose, based on assumption request for
            # one page of (100) metrics is cheaper than 3 request for
            # one metric.
            full = True

        inactive_full = False
        if len(pending_registrations) > 0.03 * len(bleemeo_cache.metrics):
            # If the number of registration exceed 3% of all known metrics,
            # do a full update of inactive metrics.
            inactive_full = True

        if full:
            # retry labels update
            self._api_support_labels = True

        # Step 1: refresh cache from API
        if full and inactive_full:
            api_metrics = bleemeo_api.api_iterator(
                metric_url,
                params={
                    'agent': self.agent_uuid,
                    'fields':
                        'id,item,label,labels,unit,unit_text,deactivated_at'
                        ',threshold_low_warning,threshold_low_critical'
                        ',threshold_high_warning,threshold_high_critical'
                        ',service,container,status_of',
                },
            )

            old_metrics = bleemeo_cache.metrics
            bleemeo_cache.metrics = {}
        elif full:
            api_metrics = bleemeo_api.api_iterator(
                metric_url,
                params={
                    'agent': self.agent_uuid,
                    'active': 'True',
                    'fields':
                        'id,item,label,labels,unit,unit_text,deactivated_at'
                        ',threshold_low_warning,threshold_low_critical'
                        ',threshold_high_warning,threshold_high_critical'
                        ',service,container,status_of',
                },
            )

            old_metrics = bleemeo_cache.metrics
            # We will only refetch active metrics, so keep inactive ones in the
            # cache
            bleemeo_cache.metrics = {
                metric_uuid: metric
                for (metric_uuid, metric) in bleemeo_cache.metrics.items()
                if metric.deactivated_at
            }
        elif update_metrics:
            old_metrics = bleemeo_cache.metrics.copy()

            api_metrics = []
            for metric_uuid in update_metrics:
                response = bleemeo_api.api_call(
                    metric_url + metric_uuid + '/',
                    params={
                        'fields':
                            'id,item,label,labels,unit,unit_text'
                            ',deactivated_at'
                            ',threshold_low_warning,threshold_low_critical'
                            ',threshold_high_warning,threshold_high_critical'
                            ',service,container,status_of',
                    },
                )
                if response.status_code == 404:
                    if metric_uuid in bleemeo_cache.metrics:
                        del bleemeo_cache.metrics[metric_uuid]
                    continue
                if response.status_code != 200:
                    raise ApiError(response)

                api_metrics.append(response.json())
        else:
            api_metrics = []
            old_metrics = bleemeo_cache.metrics.copy()

        if api_metrics:
            for data in api_metrics:
                metric = _api_metric_to_internal(data)
                bleemeo_cache.metrics[metric.uuid] = metric
            bleemeo_cache.update_lookup_map()

        if not inactive_full:
            for key in pending_registrations:
                (label, item) = key
                if key in bleemeo_cache.metrics_by_labelitem:
                    continue
                api_metrics = bleemeo_api.api_iterator(
                    metric_url,
                    params={
                        'agent': self.agent_uuid,
                        'label': label,
                        'item': item,
                        'fields':
                            'id,item,label,labels'
                            ',unit,unit_text,deactivated_at'
                            ',threshold_low_warning,threshold_low_critical'
                            ',threshold_high_warning,threshold_high_critical'
                            ',service,container,status_of',
                    },
                )
                for data in api_metrics:
                    metric = _api_metric_to_internal(data)
                    bleemeo_cache.metrics[metric.uuid] = metric
            bleemeo_cache.update_lookup_map()

        # Step 2: delete local object that are deleted from API
        deleted_metrics = []
        for metric_uuid in set(old_metrics) - set(bleemeo_cache.metrics):
            metric = old_metrics[metric_uuid]
            item = metric.labels.get('item', '')
            deleted_metrics.append((metric.label, item))

        if deleted_metrics:
            self.core.purge_metrics(deleted_metrics)

        # Step 3: register/update object present in local but not in API
        registration_error = 0
        last_error = None

        metric_last_seen = {}
        service_short_lookup = services_to_short_key(self.core.services)
        metrics_req_count = len(current_metrics)
        count = 0
        reg_count_before_update = 30
        while current_metrics:
            reg_req = current_metrics.pop(0)
            count += 1
            item = reg_req.labels.get('item', '')
            short_item = item[:API_METRIC_ITEM_LENGTH]
            if reg_req.service_label:
                short_item = short_item[:API_SERVICE_INSTANCE_LENGTH]
            key = (reg_req.label, short_item)

            if key in deleted_metrics:
                continue
            metric = bleemeo_cache.metrics_by_labelitem.get(key)

            if metric:
                last_seen_time = time.time() - (clock_now - reg_req.last_seen)
                if (metric.deactivated_at
                        and last_seen_time > metric.deactivated_at + 60
                        and reg_req.last_seen > clock_now - 600):
                    try:
                        metric = self._reactivate_metric(bleemeo_api, metric)
                    except ApiError as error:
                        if error.response.status_code != 404:
                            raise
                        # We need to re-run the _sync_metric to register the
                        # metric. Calling _register_metric now may be wrong
                        # if the metric is registered on API with another UUID
                        sync_success = False
                        logging.debug(
                            'Metric %s: %s (%s) no longer exist on API',
                            metric.uuid,
                            metric.label,
                            item,
                        )
                        del bleemeo_cache.metrics[metric.uuid]
                        continue
                # Only update labels if we have label not present on API.
                # This is needed for the transition from (label, item) to
                # (label, labels). Once done, labels will be set at
                # registration time and never change.
                api_label_keys = set(metric.labels.keys())
                new_label_keys = set(reg_req.labels.keys()) - api_label_keys
                if new_label_keys and self._api_support_labels:
                    try:
                        metric = self._metric_update_labels(
                            bleemeo_api, metric, reg_req.labels
                        )
                    except ApiError as error:
                        if error.response.status_code != 404:
                            raise
                        # We need to re-run the _sync_metric to register the
                        # metric. Calling _register_metric now may be wrong
                        # if the metric is registered on API with another UUID
                        sync_success = False
                        logging.debug(
                            'Metric %s: %s (%s) no longer exist on API',
                            metric.uuid,
                            metric.label,
                            item,
                        )
                        del bleemeo_cache.metrics[metric.uuid]
                        continue
            else:
                if reg_req.status_of_label:
                    status_of_key = (reg_req.status_of_label, short_item)
                    if status_of_key not in bleemeo_cache.metrics_by_labelitem:
                        if count >= metrics_req_count:
                            logging.debug(
                                'Metric %s need the metric %s (for status_of)',
                                reg_req.label,
                                reg_req.status_of_label,
                            )
                        else:
                            current_metrics.append(reg_req)
                        continue
                try:
                    metric = self._register_metric(
                        bleemeo_cache,
                        bleemeo_api,
                        reg_req,
                        short_item,
                        service_short_lookup,
                    )
                except ApiError as error:
                    metric = None
                    if error.response.status_code >= 500:
                        raise
                    registration_error += 1
                    last_error = error
                    time.sleep(min(registration_error * 0.5, 5))
                    if registration_error > 10:
                        raise last_error
                reg_count_before_update -= 1
            if not metric:
                continue

            bleemeo_cache.metrics[metric.uuid] = metric
            bleemeo_cache.metrics_by_labelitem[(reg_req.label, short_item)] = (
                metric
            )
            metric_last_seen[metric.uuid] = reg_req.last_seen
            if reg_count_before_update == 0:
                bleemeo_cache.update_lookup_map()
                self._bleemeo_cache = bleemeo_cache
                reg_count_before_update = 60
                self._unregistered_metric_queue_cleanup(
                    push_to_metric_queue=True
                )

        bleemeo_cache.update_lookup_map()

        # Step 4: delete object present in API by not in local
        # Only metric $SERVICE_NAME_status from service with ignore_check=True
        # are deleted
        service_short_lookup = services_to_short_key(self.core.services)
        for (key, service_info) in self.core.services.items():
            if not service_info.get('ignore_check', False):
                continue
            if key not in service_short_lookup:
                continue
            short_key = service_short_lookup[key]
            (service_name, instance) = short_key
            metric = bleemeo_cache.metrics_by_labelitem.get(
                ('%s_status' % service_name, instance)
            )
            if metric is None:
                continue

            response = bleemeo_api.api_call(
                metric_url + '%s/' % metric.uuid,
                'delete',
            )
            if response.status_code == 403:
                logging.debug(
                    "Metric deletion failed for %s. Skip metrics deletion",
                    key,
                )
                break
            elif response.status_code not in (204, 404):
                raise ApiError(response)
            if 'item' in metric.labels:
                logging.debug(
                    'Metric %s (%s) deleted',
                    metric.label,
                    metric.labels['item'],
                )
            else:
                logging.debug(
                    'Metric %s deleted', metric.label,
                )

        # Extra step: mark inactive metric (not seen for last hour + 10min)
        # But only if agent is running for at least 1 hours & 10 min
        if self.core.started_at < clock_now - 4200:
            for metric in bleemeo_cache.metrics.values():
                if metric.label == 'agent_sent_message':
                    # This metric is managed by Bleemeo Cloud platform
                    continue
                if metric.label == 'agent_status':
                    # This metric should always stay active
                    continue
                last_seen = metric_last_seen.get(metric.uuid)
                if ((last_seen is None or last_seen < clock_now - 4200)
                        and not metric.deactivated_at):
                    logging.debug(
                        'Mark inactive the metric %s: %s (%s)',
                        metric.uuid,
                        metric.label,
                        metric.labels.get('item', ''),
                    )
                    response = bleemeo_api.api_call(
                        urllib_parse.urljoin(metric_url, '%s/' % metric.uuid),
                        'patch',
                        params={
                            'fields': 'active',
                        },
                        data=json.dumps({
                            'active': False,
                        }),
                    )
                    if response.status_code != 200:
                        raise ApiError(response)
                    bleemeo_cache.metrics[metric.uuid] = (
                        metric._replace(deactivated_at=time.time())
                    )
            bleemeo_cache.update_lookup_map()

        self.core.update_thresholds(bleemeo_cache.get_core_thresholds())
        self.core.metrics_unit = bleemeo_cache.get_core_units()

        # During full sync, also drop metric not seen for last hour + 10min
        # or deleted by API.
        with self._current_metrics_lock:
            cutoff = bleemeo_agent.util.get_clock() - 4200
            result = {}
            for (key, value) in self._current_metrics.items():
                if value.last_seen < cutoff:
                    continue
                item = value.labels.get('item', '')
                short_item = item[:API_METRIC_ITEM_LENGTH]
                if value.service_label:
                    short_item = short_item[:API_SERVICE_INSTANCE_LENGTH]
                if (value.label, short_item) not in deleted_metrics:
                    result[key] = value
            self._current_metrics = result

        # Cleanup deactivated metrics that are deactivated for too long time.
        # They may still exists on API but it's not an issue. Before
        # registration we always make sure that have fresh list of inactive
        # metrics.
        # TODO: should be delete them from API ?
        cutoff = (
            time.time() - 86400 * 200
        )
        bleemeo_cache.metrics = {
            metric_uuid: metric
            for (metric_uuid, metric) in bleemeo_cache.metrics.items()
            if not metric.deactivated_at or metric.deactivated_at > cutoff
        }
        bleemeo_cache.update_lookup_map()

        if last_error is not None:
            raise last_error  # pylint: disable=raising-bad-type
        return sync_success

    def _reactivate_metric(self, bleemeo_api, metric):
        # pylint: disable=no-self-use
        metric_url = 'v1/metric/'
        logging.debug(
            'Mark active the metric %s: %s (%s)',
            metric.uuid,
            metric.label,
            metric.labels.get('item', ''),
        )
        response = bleemeo_api.api_call(
            urllib_parse.urljoin(metric_url, '%s/' % metric.uuid),
            'patch',
            params={
                'fields': 'active',
            },
            data=json.dumps({
                'active': True,
            }),
        )
        if response.status_code != 200:
            raise ApiError(response)
        return metric._replace(deactivated_at=None)

    def _metric_update_labels(self, bleemeo_api, metric, labels):
        # pylint: disable=no-self-use
        new_labels = metric.labels.copy()
        new_labels.update(labels)
        metric_url = 'v1/metric/'
        logging.debug(
            'Update metric label of metric %s: %s (%r -> %r)',
            metric.uuid,
            metric.label,
            metric.labels,
            new_labels,
        )
        response = bleemeo_api.api_call(
            urllib_parse.urljoin(metric_url, '%s/' % metric.uuid),
            'patch',
            params={
                'fields': 'labels,item',
            },
            data=json.dumps({
                'labels': new_labels,
            }),
        )
        if response.status_code != 200:
            raise ApiError(response)
        data = response.json()
        if not data.get('labels'):
            logging.debug(
                'API does not yet support labels. Skipping updates',
            )
            self._api_support_labels = False
            api_labels = {}
            if data.get('item', ''):
                api_labels['item'] = data['item']
        else:
            api_labels = data['labels']
        return metric._replace(labels=api_labels)

    def _register_metric(
            self, bleemeo_cache, bleemeo_api, reg_req, short_item,
            service_short_lookup):
        # pylint: disable=too-many-arguments
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-return-statements
        # pylint: disable=too-many-branches
        metric_url = 'v1/metric/'
        payload = {
            'agent': self.agent_uuid,
            'label': reg_req.label,
            'labels': reg_req.labels,
        }
        if reg_req.status_of_label:
            status_of_key = (reg_req.status_of_label, short_item)
            if status_of_key not in bleemeo_cache.metrics_by_labelitem:
                return (None, None)
            payload['status_of'] = (
                bleemeo_cache.metrics_by_labelitem[status_of_key].uuid
            )
        if reg_req.container_name:
            container = bleemeo_cache.containers_by_name.get(
                reg_req.container_name,
            )
            if container is None:
                # Container not yet registered
                return None
            payload['container'] = container.uuid
        if reg_req.service_label:
            key = (reg_req.service_label, reg_req.instance)
            if key not in service_short_lookup:
                return None
            short_key = service_short_lookup[key]
            service = bleemeo_cache.services_by_labelinstance.get(
                short_key
            )
            if service is None:
                return None
            payload['service'] = service.uuid

        if short_item:
            payload['item'] = short_item

        if reg_req.last_status is not None:
            payload['last_status'] = reg_req.last_status
            payload['last_status_changed_at'] = (
                datetime.datetime.utcnow()
                .replace(tzinfo=datetime.timezone.utc)
                .isoformat()
            )
            payload['problem_origins'] = [reg_req.last_problem_origins]

        response = bleemeo_api.api_call(
            metric_url,
            method='post',
            data=json.dumps(payload),
            params={
                'fields': 'id,label,labels,item,service,container'
                          ',deactivated_at,'
                          'threshold_low_warning,threshold_low_critical,'
                          'threshold_high_warning,threshold_high_critical,'
                          'unit,unit_text,agent,status_of,service,'
                          'last_status,last_status_changed_at,'
                          'problem_origins',
            },
        )
        if 400 <= response.status_code < 500:
            logging.debug(
                'Metric registration failed for %s. '
                'Server reported a client error: %s',
                reg_req.label,
                response.content,
            )
            raise ApiError(response)
        if response.status_code != 201:
            raise ApiError(response)
        data = response.json()

        metric = _api_metric_to_internal(data)
        if 'item' in metric.labels:
            logging.debug(
                'Metric %s (item %s) registered with uuid %s',
                metric.label,
                metric.labels['item'],
                metric.uuid,
            )
        else:
            logging.debug(
                'Metric %s registered with uuid %s',
                metric.label,
                metric.uuid,
            )
        return metric

    def _sync_services(self, bleemeo_cache, bleemeo_api, full=True):
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-statements
        """ Synchronize services with Bleemeo SaaS
        """
        logging.debug('Synchronize services (full=%s)', full)
        service_url = 'v1/service/'

        # Step 1: refresh cache from API
        if full:
            api_services = bleemeo_api.api_iterator(
                service_url,
                params={
                    'agent': self.agent_uuid,
                    'fields': 'id,label,instance,listen_addresses,exe_path,'
                              'stack,active',
                },
            )

            old_services = bleemeo_cache.services
            new_services = {}
            for data in api_services:
                listen_addresses = set(data['listen_addresses'].split(','))
                if '' in listen_addresses:
                    listen_addresses.remove('')
                service = Service(
                    data['id'],
                    data['label'],
                    data['instance'],
                    listen_addresses,
                    data['exe_path'],
                    data['stack'],
                    data['active'],
                )
                new_services[service.uuid] = service
            bleemeo_cache.services = new_services
            bleemeo_cache.update_lookup_map()
        else:
            old_services = bleemeo_cache.services

        # Step 2: delete local object that are deleted from API
        deleted_services = []
        for service_uuid in set(old_services) - set(bleemeo_cache.services):
            service = old_services[service_uuid]
            deleted_services.append((service.label, service.instance))

        if deleted_services:
            logging.debug(
                'API deleted the following services: %s',
                deleted_services
            )
            self.core.update_discovery(deleted_services=deleted_services)

        # Step 3: register/update object present in local but not in API
        service_short_lookup = services_to_short_key(self.core.services)
        for key, service_info in self.core.services.items():
            if key not in service_short_lookup:
                continue
            short_key = service_short_lookup[key]
            (service_name, instance) = short_key
            listen_addresses = get_listen_addresses(service_info)

            service = bleemeo_cache.services_by_labelinstance.get(short_key)
            if (service is not None and
                    service.listen_addresses == listen_addresses and
                    service.exe_path == service_info.get('exe_path', '') and
                    service.stack == service_info.get('stack', '') and
                    service.active == service_info.get('active', True)):
                continue

            payload = {
                'listen_addresses': ','.join(listen_addresses),
                'label': service_name,
                'exe_path': service_info.get('exe_path', ''),
                'stack': service_info.get('stack', ''),
                'active': service_info.get('active', True),
            }
            if instance is not None:
                payload['instance'] = instance

            if service is not None:
                method = 'put'
                action_text = 'updated'
                url = service_url + str(service.uuid) + '/'
                expected_code = 200
                active_changed = (service.active != payload['active'])
            else:
                method = 'post'
                action_text = 'registrered'
                url = service_url
                expected_code = 201
                active_changed = False

            payload.update({
                'account': self.account_id,
                'agent': self.agent_uuid,
            })

            response = bleemeo_api.api_call(
                url,
                method,
                data=json.dumps(payload),
                params={
                    'fields': 'id,listen_addresses,label,exe_path,stack'
                              ',active,instance,account,agent'
                },
            )
            if response.status_code != expected_code:
                raise ApiError(response)
            data = response.json()
            listen_addresses = set(data['listen_addresses'].split(','))
            if '' in listen_addresses:
                listen_addresses.remove('')

            service = Service(
                data['id'],
                data['label'],
                data['instance'],
                listen_addresses,
                data['exe_path'],
                data['stack'],
                data['active'],
            )
            bleemeo_cache.services[service.uuid] = service

            if service.instance:
                logging.debug(
                    'Service %s on %s %s with uuid %s',
                    service.label,
                    service.instance,
                    action_text,
                    service.uuid,
                )
            else:
                logging.debug(
                    'Service %s %s with uuid %s',
                    service.label,
                    action_text,
                    service.uuid,
                )
            if active_changed:
                # API will update all associated metrics and update their
                # active status. Apply the same rule on local cache
                if service.active:
                    deactivated_at = None
                else:
                    deactivated_at = time.time()

                for (metric_key, metric) in bleemeo_cache.metrics.items():
                    if metric.service_uuid == service.uuid:
                        bleemeo_cache.metrics[metric_key] = metric._replace(
                            deactivated_at=deactivated_at,
                        )
        bleemeo_cache.update_lookup_map()

        # Step 4: delete object present in API by not in local
        try:
            local_uuids = set(
                bleemeo_cache.services_by_labelinstance[
                    service_short_lookup[key]
                ].uuid
                for key in self.core.services if key in service_short_lookup
            )
        except KeyError:
            logging.info(
                'Some services are not registered, skipping deleting phase',
            )
            return
        deleted_services_from_state = set(bleemeo_cache.services) - local_uuids
        for service_uuid in deleted_services_from_state:
            service = bleemeo_cache.services[service_uuid]
            response = bleemeo_api.api_call(
                service_url + '%s/' % service_uuid,
                'delete',
            )
            if response.status_code not in (204, 404):
                logging.debug(
                    'Service deletion failed. Server response = %s',
                    response.content
                )
                continue
            del bleemeo_cache.services[service_uuid]
            key = (service.label, service.instance)
            if service.instance:
                logging.debug(
                    'Service %s on %s deleted',
                    service.label,
                    service.instance,
                )
            else:
                logging.debug(
                    'Service %s deleted',
                    service.label,
                )
        bleemeo_cache.update_lookup_map()

    def _sync_containers(self, bleemeo_cache, bleemeo_api, full=True):
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-locals
        # pylint: disable=too-many-statements
        logging.debug('Synchronize containers (full=%s)', full)
        container_url = 'v1/container/'

        # Step 1: refresh cache from API
        if full:
            api_containers = bleemeo_api.api_iterator(
                container_url,
                params={
                    'agent': self.agent_uuid,
                    'fields': 'id,name,docker_id,docker_inspect'
                },
            )

            new_containers = {}
            for data in api_containers:
                docker_inspect = json.loads(data['docker_inspect'])
                docker_inspect = sort_docker_inspect(docker_inspect)
                name = docker_inspect['Name'].lstrip('/')
                inspect_hash = hashlib.sha1(
                    json.dumps(docker_inspect, sort_keys=True).encode('utf-8')
                ).hexdigest()
                container = Container(
                    data['id'],
                    name,
                    data['docker_id'],
                    inspect_hash,
                )
                new_containers[container.uuid] = container
            bleemeo_cache.containers = new_containers
            bleemeo_cache.update_lookup_map()

        # Step 2: delete local object that are deleted from API
        # Not done for containers. API never delete a container

        local_containers = self.core.docker_containers
        if (bleemeo_cache.current_config is not None
                and not bleemeo_cache.current_config.docker_integration):
            local_containers = {}

        # Step 3: register/update object present in local but not in API
        for docker_id, inspect in local_containers.items():
            name = inspect['Name'].lstrip('/')
            inspect = sort_docker_inspect(copy.deepcopy(inspect))
            new_hash = hashlib.sha1(
                json.dumps(inspect, sort_keys=True).encode('utf-8')
            ).hexdigest()
            container = bleemeo_cache.containers_by_name.get(name)

            if container is not None and container.inspect_hash == new_hash:
                continue

            if container is None:
                method = 'post'
                action_text = 'registered'
                url = container_url
            else:
                method = 'put'
                action_text = 'updated'
                url = container_url + container.uuid + '/'

            cmd = inspect.get('Config', {}).get('Cmd', [])
            if cmd is None:
                cmd = []

            payload = {
                'host': self.agent_uuid,
                'name': name[:API_CONTAINER_NAME_LENGTH],
                'command': ' '.join(cmd),
                'docker_status': inspect.get('State', {}).get('Status', ''),
                'docker_created_at': convert_docker_date(
                    inspect.get('Created')
                ),
                'docker_started_at': convert_docker_date(
                    inspect.get('State', {}).get('StartedAt')
                ),
                'docker_finished_at': convert_docker_date(
                    inspect.get('State', {}).get('FinishedAt')
                ),
                'docker_api_version': self.core.last_facts.get(
                    'docker_api_version', ''
                ),
                'docker_id': docker_id,
                'docker_image_id': inspect.get('Image', ''),
                'docker_image_name':
                    inspect.get('Config', '').get('Image', ''),
                'docker_inspect': json.dumps(inspect),
            }

            response = bleemeo_api.api_call(
                url,
                method,
                data=json.dumps(payload),
                params={'fields': ','.join(['id'] + list(payload.keys()))},
            )

            if response.status_code not in (200, 201):
                raise ApiError(response)
            obj_uuid = response.json()['id']
            container = Container(
                obj_uuid,
                name,
                docker_id,
                new_hash,
            )
            bleemeo_cache.containers[obj_uuid] = container
            logging.debug('Container %s %s', container.name, action_text)
        bleemeo_cache.update_lookup_map()

        # Step 4: delete object present in API by not in local
        try:
            local_uuids = set(
                bleemeo_cache.containers_by_name[v['Name'].lstrip('/')].uuid
                for v in local_containers.values()
            )
        except KeyError:
            logging.info(
                'Some containers are not registered, skipping deleting phase',
            )
            return
        deleted_containers_from_state = (
            set(bleemeo_cache.containers) - local_uuids
        )
        for container_uuid in deleted_containers_from_state:
            container = bleemeo_cache.containers[container_uuid]
            url = container_url + container_uuid + '/'
            response = bleemeo_api.api_call(
                url,
                'delete',
            )
            if response.status_code not in (204, 404):
                logging.debug(
                    'Container deletion failed. Server response = %s',
                    response.content,
                )
                continue
            self.last_containers_removed = bleemeo_agent.util.get_clock()
            del bleemeo_cache.containers[container_uuid]
            logging.debug('Container %s deleted', container.name)

        if deleted_containers_from_state:
            deleted_metrics = []
            new_metrics = {}
            for (metric_uuid, metric) in bleemeo_cache.metrics.items():
                if metric.container_uuid in deleted_containers_from_state:
                    deleted_metrics.append(
                        (metric.label, metric.labels.get('item', ''))
                    )
                else:
                    new_metrics[metric_uuid] = metric
            bleemeo_cache.metrics = new_metrics
            if deleted_metrics:
                self.core.purge_metrics(deleted_metrics)
        bleemeo_cache.update_lookup_map()

        with self._current_metrics_lock:
            self._current_metrics = {
                key: value
                for (key, value) in self._current_metrics.items()
                if not value.container_name
                or value.container_name in bleemeo_cache.containers_by_name
            }

    def sent_metric(self, metric_name, is_service_status, bleemeo_cache=None):
        """ Return True if the metric should be sent to Bleemeo Cloud platform
        """
        if bleemeo_cache is None:
            bleemeo_cache = self._bleemeo_cache
        if bleemeo_cache.current_config is None:
            return True

        whitelist = bleemeo_cache.current_config.metrics_whitelist
        if not whitelist:
            # Always sent metrics if whitelist is empty
            return True

        if is_service_status:
            return True

        if metric_name in whitelist:
            return True

        return False

    def _sync_update_facts(self, bleemeo_cache, bleemeo_api):
        # pylint: disable=too-many-locals
        logging.debug('Update facts')
        fact_url = 'v1/agentfact/'

        if self.core.state.get('facts_uuid') is not None:
            # facts_uuid were used in older version of Agent
            self.core.state.delete('facts_uuid')

        # Step 1: refresh cache from API
        api_facts = bleemeo_api.api_iterator(
            fact_url,
            params={'agent': self.agent_uuid, 'page_size': 100},
        )

        facts = {}

        for data in api_facts:
            fact = AgentFact(
                data['id'],
                data['key'],
                data['value'],
            )
            facts[fact.uuid] = fact
        bleemeo_cache.facts = facts
        bleemeo_cache.update_lookup_map()

    def _sync_facts(self, bleemeo_cache, bleemeo_api):
        # pylint: disable=too-many-locals
        logging.debug('Synchronize facts')
        fact_url = 'v1/agentfact/'

        # Step 1: refresh cache from API
        # This step is already done by _sync_update_facts

        # Step 2: delete local object that are deleted from API
        # Not done with facts. API never delete facts.

        if bleemeo_cache.current_config is not None:
            docker_integration = (
                bleemeo_cache.current_config.docker_integration
            )
        else:
            docker_integration = True
        facts = {
            fact_name: value
            for (fact_name, value) in self.core.last_facts.items()
            if docker_integration or not fact_name.startswith('docker_')
        }

        # Step 3: register/update object present in local but not in API
        for fact_name, value in facts.items():
            fact = bleemeo_cache.facts_by_key.get(fact_name)

            if fact is not None and fact.value == str(value):
                continue

            # Agent is not allowed to update fact. Always
            # do a create and it will be removed later.

            payload = {
                'agent': self.agent_uuid,
                'key': fact_name,
                'value': str(value),
            }
            response = bleemeo_api.api_call(
                fact_url,
                'post',
                data=json.dumps(payload),
            )
            if response.status_code == 201:
                logging.debug(
                    'Send fact %s, stored with uuid %s',
                    fact_name,
                    response.json()['id'],
                )
            else:
                raise ApiError(response)

            data = response.json()
            fact = AgentFact(
                data['id'],
                data['key'],
                data['value'],
            )
            bleemeo_cache.facts[fact.uuid] = fact
            bleemeo_cache.facts_by_key[fact.key] = fact

        # Step 4: delete object present in API by not in local
        try:
            local_uuids = set(
                bleemeo_cache.facts_by_key[key].uuid
                for key in facts
            )
        except KeyError:
            logging.info(
                'Some facts are not registered, skipping delete phase',
            )
            return
        deleted_facts_from_state = set(bleemeo_cache.facts) - local_uuids
        for fact_uuid in deleted_facts_from_state:
            fact = bleemeo_cache.facts[fact_uuid]
            response = bleemeo_api.api_call(
                urllib_parse.urljoin(fact_url, '%s/' % fact_uuid),
                'delete',
            )
            if response.status_code != 204:
                raise ApiError(response)
            del bleemeo_cache.facts[fact_uuid]
            if (fact.key in bleemeo_cache.facts_by_key and
                    bleemeo_cache.facts_by_key[fact.key].uuid == fact_uuid):
                del bleemeo_cache.facts_by_key[fact.key]
            logging.debug(
                'Fact %s deleted (uuid=%s)',
                fact.key,
                fact.uuid,
            )

    def _unregistered_metric_queue_cleanup(self, push_to_metric_queue=False):
        """ Process metrics that are waiting in _unregistered_metric_queue

            * Any now regirested metrics are moved back to _metric_queue
            * Metrics points too old are droped
        """
        try:
            repush_metric_points = []
            for _ in range(self._unregistered_metric_queue.qsize()):
                metric_point = self._unregistered_metric_queue.get(timeout=0.3)
                item = metric_point.labels.get('item', '')
                short_item = item[:API_METRIC_ITEM_LENGTH]
                if metric_point.service_instance:
                    short_item = short_item[:API_SERVICE_INSTANCE_LENGTH]
                key = (metric_point.label, short_item)
                metric = self._bleemeo_cache.metrics_by_labelitem.get(key)

                if metric is not None and push_to_metric_queue:
                    self._metric_queue.put(metric_point)
                elif (key in self._current_metrics
                      and time.time() - metric_point.time < 7200):
                    repush_metric_points.append(metric_point)
        except queue.Empty:
            pass

        if len(repush_metric_points) > 90000:
            repush_metric_points.sort(key=lambda x: x.time)
            repush_metric_points = repush_metric_points[-90000:]
        for point in repush_metric_points:
            self._unregistered_metric_queue.put(point)

    def _sync_check_duplicated(
            self, bleemeo_cache, bleemeo_api, last_duplicated_events):
        """ Look at some facts and verify that they didn't changed.

            If the fact changed, I probably means another agent is using
            the same state.json.
        """
        old_facts_by_key = bleemeo_cache.facts_by_key
        self._sync_update_facts(bleemeo_cache, bleemeo_api)

        # If old_facts and facts retrieved just now
        # does not match, it means another agent modified them.
        # If this happen, (temporary) stop all connection with
        # bleemeo and warm the user.
        # We only check some key facts.
        for name in ['fqdn', 'primary_address', 'primary_mac_address']:
            if (name not in old_facts_by_key
                    or name not in bleemeo_cache.facts_by_key):
                continue
            old_value = old_facts_by_key[name].value
            current_value = bleemeo_cache.facts_by_key[name].value
            if old_value == current_value:
                continue

            clock_now = bleemeo_agent.util.get_clock()
            last_duplicated_events.append(clock_now)
            last_duplicated_events = last_duplicated_events[-15:]
            logging.info(
                'Detected duplicated state.json. '
                'Another agent changed "%s" from "%s" to "%s"',
                name,
                old_value,
                current_value,
            )
            logging.info(
                'The following links may be relevant to solve the issue: '
                'https://docs.bleemeo.com/agent/migrate-agent-new-server/'
                ' and https://docs.bleemeo.com/agent/install-cloudimage-creation/'  # noqa
            )
            if (len(last_duplicated_events) >= 3 and
                    last_duplicated_events[-3] > clock_now - 3600):
                delay = 900 + random.randint(-60, 60)
            else:
                delay = 300 + random.randint(-60, 60)
            self._duplicate_disable_until = clock_now + delay
            self._mqtt_reconnect_at = clock_now + delay
            break

        if self._duplicate_disable_until:
            self._bleemeo_cache = bleemeo_cache.copy()
            # Save cache so if agent is restarted in won't
            # complain of the same change on facts by another
            # agent.
            bleemeo_cache.save()

    def emit_metric(self, metric_point):
        if self._duplicate_disable_until:
            return
        metric_name = metric_point.label
        is_service_status = (
            metric_point.service_label
            and metric_point.label == metric_point.service_label + '_status'
        )
        if not self.sent_metric(metric_name, is_service_status):
            return

        if (self._bleemeo_cache.current_config is not None
                and not self._bleemeo_cache.current_config.docker_integration
                and metric_point.container_name != ''):
            return

        self._metric_queue.put(metric_point)
        metric_name = metric_point.label
        service = metric_point.service_label
        item = metric_point.labels.get('item', '')

        with self._current_metrics_lock:
            if (metric_name, item) not in self._current_metrics:
                self._sync_loop_event.set()
            self._current_metrics[(metric_name, item)] = MetricRegistrationReq(
                metric_name,
                metric_point.labels,
                service,
                metric_point.service_instance,
                metric_point.container_name,
                metric_point.status_of,
                metric_point.status_code,
                metric_point.problem_origin,
                bleemeo_agent.util.get_clock(),
            )

    @property
    def account_id(self):
        return self._bleemeo_cache.account_id

    @property
    def agent_uuid(self):
        return self.core.state.get('agent_uuid')

    @property
    def agent_username(self):
        return '%s@bleemeo.com' % self.agent_uuid

    @property
    def agent_password(self):
        return self.core.state.get('password')

    @property
    def bleemeo_base_url(self):
        return self.core.config['bleemeo.api_base']

    @property
    def upgrade_in_progress(self):
        upgrade_file = self.core.config['agent.upgrade_file']
        return os.path.exists(upgrade_file)
