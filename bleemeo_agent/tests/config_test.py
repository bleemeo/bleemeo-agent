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

import pytest

import bleemeo_agent.config


def test_config_object():
    conf = bleemeo_agent.config.Config()
    conf['test'] = {
        'a': 'a',
        'one': 1,
        'sub-level': {
            'two': 2.0,
        },
    }

    assert conf['test.a'] == 'a'
    assert conf['test.one'] == 1
    assert conf['test.sub-level.two'] == 2.0
    with pytest.raises(KeyError):
        conf['test.does.not.exists']

    conf['test.b'] = 'B'
    assert conf['test.b'] == 'B'
    assert conf['test.one'] == 1

    conf['test.now.does.exists.value'] = 42
    assert conf['test.now.does.exists.value'] == 42

    del conf['test.now.does.exists.value']
    with pytest.raises(KeyError):
        conf['test.now.does.exists.value']


def test_merge_dict():
    assert(bleemeo_agent.config.merge_dict({}, {}) == {})
    assert (
        bleemeo_agent.config.merge_dict({'a': 1}, {'b': 2}) ==
        {'a': 1, 'b': 2}
    )

    d1 = {
        'd1': 1,
        'remplaced': 1,
        'sub_dict': {
            'd1': 1,
            'remplaced': 1,
        }
    }
    d2 = {
        'd2': 2,
        'remplaced': 2,
        'sub_dict': {
            'd2': 2,
            'remplaced': 2,
        }
    }
    want = {
        'd1': 1,
        'd2': 2,
        'remplaced': 2,
        'sub_dict': {
            'd1': 1,
            'd2': 2,
            'remplaced': 2,
        }
    }
    assert(
        bleemeo_agent.config.merge_dict(d1, d2) ==
        want
    )


def test_merge_list():
    assert(
        bleemeo_agent.config.merge_dict({'a': []}, {'a': []}) ==
        {'a': []}
    )
    assert (
        bleemeo_agent.config.merge_dict({'a': []}, {'a': [1]}) ==
        {'a': [1]}
    )

    d1 = {
        'a': [1, 2],
        'merge_create_duplicate': [1, 2, 3],
        'sub_dict': {
            'a': [1, 2],
            'b': [1, 2],
        }
    }
    d2 = {
        'a': [3, 4],
        'merge_create_duplicate': [3, 4],
        'sub_dict': {
            'a': [3, 4],
            'c': [3, 4],
        }
    }
    want = {
        'a': [1, 2, 3, 4],
        'merge_create_duplicate': [1, 2, 3, 3, 4],
        'sub_dict': {
            'a': [1, 2, 3, 4],
            'b': [1, 2],
            'c': [3, 4],
        }
    }
    assert(
        bleemeo_agent.config.merge_dict(d1, d2) ==
        want
    )


def test_config_files():
    assert bleemeo_agent.config._config_files(['/does-not-exsits']) == []
    assert bleemeo_agent.config._config_files(
        ['/etc/passwd']) == ['/etc/passwd']

    wants = [
        'bleemeo_agent/tests/configs/main.conf',
        'bleemeo_agent/tests/configs/conf.d/empty.conf',
        'bleemeo_agent/tests/configs/conf.d/first.conf',
        'bleemeo_agent/tests/configs/conf.d/second.conf',
    ]
    paths = [
        'bleemeo_agent/tests/configs/main.conf',
        'bleemeo_agent/tests/configs/conf.d'
    ]
    assert bleemeo_agent.config._config_files(paths) == wants


def test_load_config():
    config, errors, warnings = bleemeo_agent.config._load_config([
        'bleemeo_agent/tests/configs/main.conf',
        'bleemeo_agent/tests/configs/conf.d',
    ])

    assert len(errors) == 0

    config_expected = {
        'main_conf_loaded': True,
        'first_conf_loaded': True,
        'second_conf_loaded': True,
        'overridden_value': 'second',
        'merged_dict': {
            'main': 1.0,
            'first': 'yes',
        },
        'merged_list': [
            'duplicated between main.conf & second.conf',
            'item from main.conf',
            'item from first.conf',
            'item from second.conf',
            'duplicated between main.conf & second.conf',
        ],
        'sub_section': {
            'nested': None,
        },
        'telegraf': {
            'statsd': {
                'enabled': None,
            },
        },
    }

    assert len(warnings) == 1
    assert isinstance(config, bleemeo_agent.config.Config)

    assert config._internal_dict == config_expected

    assert config['second_conf_loaded'] is True
    assert config['merged_dict.main'] == 1.0


def test_convert_conf_name():
    test = [
        (
            'agent.public_ip_indicator',
            ['BLEEMEO_AGENT_AGENT_PUBLIC_IP_INDICATOR']
        ),
        (
            'telegraf.docker_metrics_enabled',
            ['BLEEMEO_AGENT_TELEGRAF_DOCKER_METRICS_ENABLED']
        ),
        (
            'disk_monitor',
            ['BLEEMEO_AGENT_DISK_MONITOR']
        ),
        (
            'a.disk_monitor',
            ['BLEEMEO_AGENT_A_DISK_MONITOR']
        )
    ]
    test_compatibility = [
        (
            'bleemeo.account_id',
            ['BLEEMEO_AGENT_BLEEMEO_ACCOUNT_ID', 'BLEEMEO_AGENT_ACCOUNT']
        ),
        (
            'bleemeo.registration_key',
            [
                'BLEEMEO_AGENT_BLEEMEO_REGISTRATION_KEY',
                'BLEEMEO_AGENT_REGISTRATION_KEY'
            ]
        ),
        (
            'bleemeo.api_base',
            ['BLEEMEO_AGENT_API_BASE', 'BLEEMEO_AGENT_BLEEMEO_API_BASE']
        ),
        (
            'bleemeo.mqtt.host',
            ['BLEEMEO_AGENT_MQTT_HOST', 'BLEEMEO_AGENT_BLEEMEO_MQTT_HOST']
        ),
        (
            'bleemeo.mqtt.port',
            ['BLEEMEO_AGENT_MQTT_PORT', 'BLEEMEO_AGENT_BLEEMEO_MQTT_PORT']
        ),
        (
            'bleemeo.mqtt.ssl',
            ['BLEEMEO_AGENT_MQTT_SSL', 'BLEEMEO_AGENT_BLEEMEO_MQTT_SSL']
        ),
    ]

    for (conf, envs) in test:
        for env in envs:
            env_names, warnings = bleemeo_agent.config._convert_conf_name(
                conf, []
            )
            assert len(warnings) == 0
            assert(env in env_names)
    for (conf, envs) in test_compatibility:
        for env in envs:
            env_names, warnings = bleemeo_agent.config._convert_conf_name(
                conf, []
            )
            assert len(warnings) == 0
            assert(env in env_names)


def test_config():
    config1 = bleemeo_agent.config._load_default_config()
    config2 = bleemeo_agent.config._load_default_config()
    config1['logging.output'] = 'console2'
    assert(config1['logging.output'] == 'console2')
    assert(config2['logging.output'] == 'console')
    config1['bleemeo.enabled'] = False
    assert(not config1['bleemeo.enabled'])
    assert(config2['bleemeo.enabled'])
    config1['bleemeo.mqtt.port'] = 2018
    assert(config1['bleemeo.mqtt.port'] == 2018)
    assert(config2['bleemeo.mqtt.port'] == 8883)
    config1['bleemeo.mqtt.port'] = 8883
    config1['logging.output'] = 'console'
    config1['bleemeo.enabled'] = True
    assert(config1._internal_dict == config2._internal_dict)
    config2['tags'].append('test')
    assert(config1['tags'] == [])
    assert(config2['tags'] == ['test'])
    config2['thresholds']['test'] = 1
    assert(config2['thresholds'] == {'test': 1})
    assert(config1['thresholds'] == {})
    del config2['thresholds']['test']
    config2['tags'].remove('test')
    assert(config1._internal_dict == config2._internal_dict)
