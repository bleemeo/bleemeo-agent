import json
import logging
import os
import socket
import subprocess
import threading

COLLECTD_CONFIG_FRAGMENTS = """
# Generated by bleemeo-agent. Do NOT MODIFY, change will be overriten
# by next run of agent.
LoadPlugin write_graphite
<Plugin write_graphite>
  <Node "bleemeo">
     # default host and port is localhost:2003
     # default protocol is ... udp !
     Protocol "tcp"
  </Node>
</Plugin>
"""


class Collectd(threading.Thread):

    def __init__(self, agent):
        super(Collectd, self).__init__()

        self.agent = agent
        self.config_fragments = [COLLECTD_CONFIG_FRAGMENTS]
        self.collectd_config = '/etc/collectd/collectd.conf.d/bleemeo.conf'

    def run(self):
        self.write_config()

        sock_server = socket.socket()
        sock_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock_server.bind(('127.0.0.1', 2003))
        sock_server.listen(5)
        sock_server.settimeout(1)

        clients = []
        while not self.agent.is_terminating.is_set():
            try:
                (sock_client, addr) = sock_server.accept()
                t = threading.Thread(
                    target=self.process_client,
                    args=(sock_client, addr))
                t.start()
                clients.append(t)
            except socket.timeout:
                pass

        sock_server.close()
        [x.join() for x in clients]

    def process_client(self, sock_client, addr):
        logging.debug('collectd: client connectd from %s', addr)

        try:
            self.process_client_inner(sock_client, addr)
        finally:
            sock_client.close()
            logging.debug('collectd: client %s disconnectd', addr)

    def process_client_inner(self, sock_client, addr):
        remain = ''
        sock_client.settimeout(1)
        while not self.agent.is_terminating.is_set():
            try:
                tmp = sock_client.recv(4096)
            except socket.timeout:
                continue

            if tmp == '':
                break

            lines = (remain + tmp).split('\n')
            remain = ''

            if lines[-1] != '':
                remain = lines[-1]

            # either it's '' or we moved it to remain.
            del lines[-1]

            points = []
            for line in lines:
                # inspired from graphite project : lib/carbon/protocols.py
                metric, value, timestamp = line.split()
                (timestamp, value) = (float(timestamp), float(value))

                metric = metric.decode('utf-8')
                # the first component is the hostname
                metric = metric.split('.', 1)[1]

                for extension in self.agent.plugins_v1_mgr:
                    result = extension.obj.canonical_metric_name(metric)
                    if result:
                        metric = result
                        break  # first who rename win

                points.append((metric, value, timestamp))

            self.agent.mqtt_connector.publish(
                'agent/points/POST',
                json.dumps(points))

    def add_config(self, fragments):
        if fragments:
            self.config_fragments.append(fragments)

    def write_config(self):
        config_content = '\n'.join(self.config_fragments)
        if os.path.exists(self.collectd_config):
            with open(self.collectd_config) as fd:
                current_content = fd.read()

            if config_content == current_content:
                logging.debug('collectd already configured')
                return

        with open(self.collectd_config, 'w') as fd:
            fd.write(config_content)

        try:
            output = subprocess.check_output(
                ['sudo', '--non-interactive',
                    'service', 'collectd', 'restart'],
                stderr=subprocess.STDOUT,
            )
            return_code = 0
        except subprocess.CalledProcessError as e:
            output = e.output
            return_code = e.returncode

        if return_code != 0:
            logging.info(
                'Failed to restart collectd after reconfiguration : %s',
                output)
        else:
            logging.debug('collectd reconfigured and restarted : %s', output)
