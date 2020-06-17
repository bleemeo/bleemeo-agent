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

import logging
import os
import re
import shlex
import socket
import threading
import time

import bleemeo_agent.jmxtrans
import bleemeo_agent.telegraf
import bleemeo_agent.util


def graphite_split_line(line):
    """ Split a "graphite" line.

        >>> # 42 is the value, 1000 is the timestamp
        >>> graphite_split_line(b'metric.name 42 1000')
        ('metric.name', 42.0, 1000.0)
    """
    line = line.decode('utf-8')

    # graphite line looks like "METRIC VALUE TIMESTAMP"
    # Usually metric, value and timestamp do not contains space (see tests case
    # for example with space).
    # Use faster method when they don't contain space
    if line.count(' ') == 2:
        (metric, value, timestamp) = line.split(' ')
    else:
        part = shlex.split(line)
        timestamp = part[-1]
        value = part[-2]
        metric = ' '.join(part[0:-2])

    timestamp = float(timestamp)
    try:
        value = float(value)
    except ValueError:
        # assume value is a string, like "20 days, 23:26"
        pass

    return (metric, value, timestamp)


def _disk_path_rename(path, mount_point, ignored_patterns):
    if mount_point is not None:
        if mount_point.endswith('/'):
            mount_point = mount_point[:-1]

        if not path.startswith(mount_point):
            # partition don't start with mount_point, so it's a parition
            # which is only inside the container. Ignore it
            return None

        path = path.replace(mount_point, '')
        if not path.startswith('/'):
            path = '/' + path

    for pattern in ignored_patterns:
        if pattern.endswith('/'):
            pattern = pattern[:-1]

        if path == pattern or path.startswith(pattern + os.sep):
            return None

    return path


class GraphiteServer(threading.Thread):

    def __init__(self, core):
        super(GraphiteServer, self).__init__()

        self.data_last_seen_at = None
        self.core = core
        self.listener_up = False
        self.telegraf_last_diagnostic = None
        self.initialization_done = threading.Event()

    @property
    def metrics_source(self):
        """ Return the current metrics source (currently only telegraf
            is supported)
        """
        return self.core.config['graphite.metrics_source']

    @property
    def jmx_enabled(self):
        """ Returns True if JMX collector is enabled
        """
        return self.core.config['jmx.enabled']

    def health_check(self):
        clock_now = bleemeo_agent.util.get_clock()
        no_data = (
            self.data_last_seen_at is None
            or clock_now - self.data_last_seen_at > 60
        )
        if no_data:
            logging.info(
                'Issue with metrics collector: no metric received from %s',
                self.metrics_source,
            )
        if self.metrics_source == 'telegraf':
            telegraf_running = bleemeo_agent.util.is_process_running(
                'telegraf',
                self.core.top_info,
            )
            if no_data and (
                    self.telegraf_last_diagnostic is None or
                    clock_now - self.telegraf_last_diagnostic > 3600):
                self.telegraf_last_diagnostic = clock_now
                try:
                    bleemeo_agent.telegraf.diagnostic(self.core)
                except Exception:  # pylint: disable=broad-except
                    logging.info(
                        "Diagnostic for Telegraf connection failed:",
                        exc_info=True,
                    )
            if (self.core.config['telegraf.statsd.enabled']
                    and no_data
                    and not telegraf_running):
                stastd_port_used = bleemeo_agent.util.is_port_used(
                    self.core.config['telegraf.statsd.address'],
                    self.core.config['telegraf.statsd.port'],
                    socket.SOCK_DGRAM
                )
                if stastd_port_used:
                    self.telegraf_last_diagnostic = None
                    logging.warning(
                        'Telegraf seems not running and StatsD port (UDP %d)'
                        ' is already used. Telegraf is configured to listen on'
                        ' StatsD which explain why it fail to start.',
                        self.core.config['telegraf.statsd.port'],
                    )
                    logging.warning(
                        'The StatsD integration is now disabled. Restart the'
                        ' agent to try re-enabling it.'
                    )
                    logging.warning(
                        'See https://docs.bleemeo.com/agent/configuration/ to'
                        ' permanently disable StatsD integration or using an'
                        ' alternate port'
                    )
                    self.core.config['telegraf.statsd.enabled'] = False
                    self.update_discovery()
                    self.core.fire_triggers(facts=True)

    def run(self):
        bind_address = self.core.config['graphite.listener.address']
        bind_port = self.core.config['graphite.listener.port']
        sock_server = socket.socket()
        sock_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock_server.bind((bind_address, bind_port))
        except socket.error as exc:
            logging.error(
                'Failed to listen on graphite port %s:%s: %s',
                bind_address, bind_port, exc
            )
            self.initialization_done.set()
            return

        sock_server.listen(5)
        sock_server.settimeout(1)
        self.listener_up = True
        self.initialization_done.set()

        clients = []
        while not self.core.is_terminating.is_set():
            try:
                (sock_client, addr) = sock_server.accept()

                client = GraphiteClient(self, sock_client, addr)
                client.start()
                clients.append(client)
            except socket.timeout:
                pass

        sock_server.close()

        for client in clients:
            client.join()

    def update_discovery(self):
        """ Update configuration after a service discovery was run
        """
        if self.metrics_source == 'telegraf':
            bleemeo_agent.telegraf.update_discovery(self.core)

        if self.jmx_enabled:
            bleemeo_agent.jmxtrans.update_discovery(self.core)

    def get_time_elapsed_since_last_data(self):
        # pylint: disable=invalid-name
        """ Returns a metric "time_elapsed_since_last_data" which
            is the number of seconds since the last metric received
            by any graphite clients.
        """
        clock_now = bleemeo_agent.util.get_clock()
        threshold = self.core.get_threshold('time_elapsed_since_last_data')
        highest_threshold = 0
        if threshold is not None:
            if threshold.get('high_critical') is not None:
                highest_threshold = threshold.get('high_critical')
            elif threshold.get('high_warning') is not None:
                highest_threshold = threshold.get('high_warning')

        if self.data_last_seen_at is not None:
            delay = clock_now - self.data_last_seen_at
        else:
            delay = clock_now - self.core.started_at

        # It only emit the metric if:
        # * either it actually had seen some data (e.g. metric is exact, not
        #   approximated base on agent start date).
        # * or no threshold is defined
        # * or the highest threshold is already crossed
        # It does this to avoid changing state of this metric after an agent
        # restart. E.g. collector is dead: status is critical; user restart
        # agent, status must NOT goes OK, then few minute later critical.
        if (self.data_last_seen_at is None
                and threshold is not None
                and delay < highest_threshold):
            return None

        return bleemeo_agent.type.DEFAULT_METRICPOINT._replace(
            label='time_elapsed_since_last_data',
            time=time.time(),
            value=delay,
        )

    def network_interface_blacklist(self, if_name):
        """ Returns True if the given interface is blacklisted
        """
        for pattern in self.core.config['network_interface_blacklist']:
            if if_name.startswith(pattern):
                return True
        return False

    def ignored_disk(self, disk):
        """ Tell if disk should be monitored. It avoid monitoring sda1 or
            dm-1
        """
        for pattern in self.core.config['disk_monitor']:
            if re.match(pattern, disk):
                return False

        return True

    def disk_path_rename(self, path):
        """ Rename (and possibly ignore) a disk partition

            In case of telegraf running in a container, it's used to show
            partition as seen by the host, instead of as seen by a container.
        """
        mount_point = self.core.config['df.host_mount_point']
        ignored_patterns = self.core.config['df.path_ignore']

        return _disk_path_rename(path, mount_point, ignored_patterns)


class GraphiteClient(threading.Thread):

    def __init__(self, server, client_socket, client_addr):
        super().__init__()

        self.core = server.core
        self.server = server
        self.socket = client_socket
        self.addr = client_addr

        # Decode either Telegraf or jmxtrans input.
        self.client_decoder = None

    def run(self):
        logging.debug('graphite: client connected from %s', self.addr)

        try:
            self._process_client()
        finally:
            self.socket.close()
            logging.debug('graphite: client %s disconnectd', self.addr)
            if self.client_decoder is not None:
                self.client_decoder.close()

    def _process_client(self):
        remain = b''
        self.socket.settimeout(1)
        pending_metrics = []
        pending_first_time = 0
        while not self.core.is_terminating.is_set():
            try:
                tmp = self.socket.recv(4096)
            except socket.timeout:
                if pending_metrics:
                    self._flush_metrics(pending_metrics)
                    pending_metrics = []
                continue

            if tmp == b'':
                break

            lines = (remain + tmp).split(b'\n')
            remain = b''

            if lines[-1] != b'':
                remain = lines[-1]

            # either it's '' or we moved it to remain.
            del lines[-1]

            for line in lines:
                if line == b'':
                    continue

                metric, value, timestamp = graphite_split_line(line)
                if not metric.isprintable():
                    continue

                if not pending_metrics:
                    pending_first_time = time.time()
                pending_metrics.append(
                    (timestamp, metric, value),
                )

            if pending_metrics and time.time() > pending_first_time + 3:
                self._flush_metrics(pending_metrics)
                pending_metrics = []

        if pending_metrics:
            self._flush_metrics(pending_metrics)
            pending_metrics = []

    def _flush_metrics(self, pending_metrics):
        pending_metrics.sort()
        for (timestamp, metric, value) in pending_metrics:
            self.emit_metric(metric, timestamp, value)

        if self.client_decoder is not None:
            self.client_decoder.packet_finish()

    def emit_metric(self, name, timestamp, value):
        """ Rename a metric and pass it to core

            If the metric is used to compute a derrived metric, add it to
            computed_metrics_pending.

            Nothing is emitted if metric is unknown
        """
        if self.client_decoder is None:
            if (name.startswith('telegraf.')
                    and self.server.metrics_source == 'telegraf'):
                self.client_decoder = bleemeo_agent.telegraf.Telegraf(self)
            elif name.startswith('jmxtrans.') and self.server.jmx_enabled:
                self.client_decoder = bleemeo_agent.jmxtrans.Jmxtrans(self)
            else:
                return

        self.client_decoder.emit_metric(name, timestamp, value)
