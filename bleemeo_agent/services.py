#
#  Copyright 2018 Bleemeo
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


import re
import subprocess
import time

import bleemeo_agent.type


def gather_exim_queue_size(instance, core):
    """ Gather and send metric for queue size

        instance may be unset (empty string) which means gather metric
        for a postfix running outside any container. This is only done
        if the agent is running outside any container.

        instance may also be set to the Docker container name running
        the postfix. This require core.docker_client to be set.

        In all case, this will run "exim4 -bpc" (subprocess or docker exec)
    """
    # Read of (single) attribute is atomic, no lock needed
    docker_client = core.docker_client
    if instance and docker_client is not None:
        result = docker_client.exec_create(
            instance,
            ['exim4', '-bpc'],
        )
        output = docker_client.exec_start(result['Id'])
    elif not instance and not core.container:
        try:
            output = subprocess.check_output(
                ['exim4', '-bpc'],
                stderr=subprocess.STDOUT,
            )
        except (subprocess.CalledProcessError, IOError, OSError):
            return
    else:
        return

    if isinstance(output, bytes):
        output = output.decode('utf-8')

    labels = {}
    if instance:
        labels['item'] = instance

    try:
        count = int(output)
        core.emit_metric(
            bleemeo_agent.type.DEFAULT_METRICPOINT._replace(
                label='exim_queue_size',
                labels=labels,
                time=time.time(),
                value=float(count),
                service_label='exim',
                service_instance=instance,
            )
        )
    except ValueError:
        return


def gather_postfix_queue_size(instance, core):
    """ Gather and send metric for queue size

        instance may be unset (empty string) which means gather metric
        for a postfix running outside any container. This is only done
        if the agent is running outside any container.

        instance may also be set to the Docker container name running
        the postfix. This require core.docker_client to be set.

        In all case, this will run "postqueue -p" (subprocess or docker exec)
    """
    # Read of (single) attribute is atomic, no lock needed
    docker_client = core.docker_client
    if instance and docker_client is not None:
        result = docker_client.exec_create(
            instance,
            ['postqueue', '-p'],
        )
        output = docker_client.exec_start(result['Id'])
    elif not instance and not core.container:
        try:
            output = subprocess.check_output(
                ['postqueue', '-p'],
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError:
            return
    else:
        return

    if isinstance(output, bytes):
        output = output.decode('utf-8')

    labels = {}
    if instance:
        labels['item'] = instance

    match = re.search(r'-- \d+ Kbytes in (\d+) Request.', output)
    match_zero = re.search(r'Mail queue is empty', output)
    if match or match_zero:
        if match:
            count = int(match.group(1))
        else:
            count = 0
        core.emit_metric(
            bleemeo_agent.type.DEFAULT_METRICPOINT._replace(
                label='postfix_queue_size',
                labels=labels,
                time=time.time(),
                value=float(count),
                service_label='postfix',
                service_instance=instance,
            )
        )
