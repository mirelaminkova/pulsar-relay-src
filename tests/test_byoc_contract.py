"""This component's BYOC mode against byoc-sync's own code (vendored in tests/byoc_contract/).

byoc-sync (sanjaysrikakulam/elixir_biohackathon_2026_project1, byoc-sync/) turns the
``byoc_pulsar`` form of a Galaxy user's preferences into the runner and TPV destination of
their Pulsar. With its relay backend, Galaxy reaches that Pulsar through a Pulsar Relay; the
account and topic prefix are galaxy_<BYOC name>_byoc unless the user enters others. Its
naming and validation rules are the contract: the names this component uses must be the
ones byoc-sync derives, the form the welcome page shows must pass byoc-sync's validation,
and the co-located Pulsar's app.yml must match the runner and destination byoc-sync writes.
"""
import filecmp
import json
import os
from pathlib import Path
import secrets
import string
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / 'scripts'))
sys.path.insert(0, str(TESTS))

import configure  # noqa: E402

try:
    import jinja2
    import yaml
except ImportError:  # pragma: no cover - depends on the environment
    yaml = jinja2 = None

if yaml is not None:
    from byoc_contract import models, naming, validate  # noqa: E402

import ansible_vars  # noqa: E402

CONTRACT = TESTS / 'byoc_contract'
needs_yaml = unittest.skipIf(yaml is None, 'needs PyYAML and Jinja2')
needs_ansible = unittest.skipUnless(ansible_vars.AVAILABLE and yaml is not None, 'needs ansible-core')

VALID_NAMES = ['abc', 'hpc', 'eu_hpc_2', 'src1a2b3c4d', 'a' * 32, 'a1_b2_c3', 'x_byoc1']
INVALID_NAMES = ['ab', 'a' * 33, '1abc', '_abc', 'Abc', 'ab-c', 'abc_', 'a__b', 'abc_byoc', 'ab c', 'ab.c', '']
VALID_PASSWORDS = ['a' * 16, 'Rel4yPairPassw0rd', 'A1' * 64, '0123456789abcdefZ', '0xZZZZZZZZZZZZZZ',
                   '0b0101010101010120']
INVALID_PASSWORDS = ['a' * 15, 'a' * 129, '1234567890123456', '0x0123456789abcdef', '0b0101010101010101',
                     'has-dash-password1', 'has.dot.password12', 'has space password1', 'pass@word12345678']


def prepare(directory):
    path = Path(directory) / 'files'
    path.mkdir()
    path.joinpath('valkey.conf').write_text((TESTS.parent / 'files' / 'valkey.conf').read_text())


def site():
    return yaml.safe_load((CONTRACT / 'eu.yml').read_text())


def byoc_names(name):
    return naming.Names.for_(name, naming.NamingConfig(**site()['naming']))


def backends():
    config = site()
    return validate.Backends(allowed=tuple(config['backends']), default=config['default_backend'])


def byoc_errors(validator, *args):
    errors = {}
    validator(*args, errors) if len(args) == 1 else validator(args[0], errors, *args[1:])
    return errors


def render_byoc_template(template, **context):
    """Render a byoc-sync template as its Renderer does ([[ ]] delimiters, its filters)."""
    env = jinja2.Environment(  # noqa: S701 - YAML, not HTML, as in byoc-sync
        loader=jinja2.FileSystemLoader(str(CONTRACT / 'templates' / 'eu')),
        variable_start_string='[[', variable_end_string=']]',
        block_start_string='[%', block_end_string='%]',
        comment_start_string='[#', comment_end_string='#]',
        undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters['q'] = lambda value: json.dumps(str(value))
    env.filters['pystr'] = lambda value: json.dumps(str(value))
    return env.get_template(template).render(**context)


def form_request(form, password):
    """byoc-sync's validation and planner defaults for the form as the welcome page shows it."""
    record = models.UserRecord(
        galaxy_id='1', email='alice@example.org', username='alice', deleted=False, purged=False,
        active=True, source='test',
        prefs={f'byoc_pulsar|{key}': str(value) for key, value in form.items()} | {'byoc_pulsar|password': password})
    request = validate.validate(record, 'byoc_pulsar', backends())
    assert isinstance(request, models.ByocRequest), request
    names = byoc_names(request.name)
    # byoc-sync's planner (_Planner._validate) fills in the account and the topic prefix.
    return models.ByocRequest(**{**request.__dict__,
                                 'relay_username': request.relay_username or names.relay_user,
                                 'relay_topic_prefix': request.relay_topic_prefix or names.relay_topic_prefix})


# Names


class NamingTest(unittest.TestCase):
    @needs_yaml
    def test_pair_account_is_byoc_syncs_relay_user_and_topic_prefix(self):
        for name in VALID_NAMES:
            with self.subTest(name=name):
                theirs = byoc_names(name)
                self.assertEqual(configure.byoc_pair_username(name), theirs.relay_user)
                self.assertEqual(configure.byoc_pair_username(name), theirs.relay_topic_prefix)
                config = naming.NamingConfig(**site()['naming'])
                self.assertEqual(config.name_from('relay_user', configure.byoc_pair_username(name)), name)
                self.assertRegex(theirs.relay_user, '^' + configure.PAIR_USERNAME_PATTERN + '$')

    @needs_ansible
    def test_topic_prefix_and_account_in_byoc_mode(self):
        self.assertEqual(ansible_vars.resolve('_relay_topic_prefix', **ansible_vars.BYOC), 'galaxy_hpc_byoc')
        self.assertEqual(ansible_vars.resolve('_relay_topic_prefix'), 'src')
        self.assertEqual(ansible_vars.resolve('_relay_capabilities_topic', **ansible_vars.BYOC),
                         'galaxy_hpc_byoc_pulsar_capabilities')
        self.assertEqual(ansible_vars.resolve('_relay_job_setup_topic', **ansible_vars.BYOC),
                         'galaxy_hpc_byoc_job_setup')
        galaxy = ansible_vars.resolve('_relay_byoc_galaxy', **ansible_vars.BYOC)
        theirs = byoc_names('hpc')
        self.assertEqual(galaxy, {'runner_id': theirs.runner_id, 'destination_id': theirs.destination_id,
                                  'relay_vault_var': theirs.relay_vault_var, 'tag': theirs.tag})


# Validation: valid here <=> byoc-sync accepts the value


class ValidationTest(unittest.TestCase):
    @needs_yaml
    def test_name_rules_match_byoc_syncs(self):
        for name in VALID_NAMES + INVALID_NAMES:
            with self.subTest(name=name):
                self.assertEqual(configure.is_valid_byoc_name(name), validate.is_valid_name(name))
                self.assertEqual(configure.is_valid_byoc_name(name), not byoc_errors(validate._name, name))

    @needs_ansible
    def test_playbook_name_check_matches_byoc_syncs(self):
        that = ansible_vars.task_named('playbook.yml', 'Validate the Galaxy BYOC mode parameters')['ansible.builtin.assert']['that']
        for name in VALID_NAMES + INVALID_NAMES:
            with self.subTest(name=name):
                ours = ansible_vars.check(that[0], relay_byoc_name=name)
                self.assertEqual(ours, validate.is_valid_name(name) or name == '')

    @needs_yaml
    def test_byoc_passwords_pass_every_byoc_sync_and_read_back_from_yaml(self):
        generated = [configure.generate_byoc_password() for _ in range(200)]
        for password in VALID_PASSWORDS + generated:
            with self.subTest(password=password):
                self.assertTrue(configure.is_valid_byoc_password(password))
                for backend in (models.RELAY, models.RABBITMQ):
                    self.assertEqual(byoc_errors(validate._password, password, backend), {})
                # Galaxy's job_conf.yml templates print runner parameters unquoted.
                self.assertEqual(yaml.safe_load(f'k: {password}'), {'k': password})
                self.assertRegex(password, '^' + configure.PAIR_PASSWORD_PATTERN + '$')

    @needs_yaml
    def test_refused_byoc_passwords(self):
        for password in INVALID_PASSWORDS:
            with self.subTest(password=password):
                self.assertFalse(configure.is_valid_byoc_password(password))

    @needs_yaml
    def test_yaml_safety_rule_refuses_every_yaml_number(self):
        # The rule names YAML's integer forms (it also refuses some digit strings YAML would keep).
        alphabets = ['0123456789', '01', '0123456789abcdefABCDEF', string.ascii_letters + string.digits]
        for alphabet in alphabets:
            for prefix in ['', '0x', '0b', '0']:
                value = prefix + ''.join(secrets.choice(alphabet) for _ in range(20))
                with self.subTest(value=value):
                    safe = yaml.safe_load(f'k: {value}') == {'k': value}
                    if not safe:
                        self.assertFalse(configure.is_valid_byoc_password(value))

    @needs_ansible
    def test_playbook_password_check_matches_configure(self):
        that = ansible_vars.task_named('playbook.yml', 'Validate the Galaxy BYOC mode parameters')['ansible.builtin.assert']['that']
        for password in VALID_PASSWORDS + INVALID_PASSWORDS:
            with self.subTest(password=password):
                self.assertEqual(ansible_vars.check(that[1], relay_pair_password=password),
                                 configure.is_valid_byoc_password(password))

    @needs_ansible
    def test_playbook_relay_url_check_accepts_only_what_byoc_sync_accepts(self):
        that = ansible_vars.task_named('playbook.yml', 'Validate the Galaxy BYOC mode parameters')['ansible.builtin.assert']['that']
        for url, ours in [('https://relay.example.org', True), ('https://relay.example.org/', True),
                          ('https://a-b.src.surf-hosted.nl:8443', True), ('http://relay.example.org', False),
                          ('https://relay', False), ('https://relay.example.org/path', False),
                          ('https://user@relay.example.org', False), ('https://10.0.0.1', False)]:
            with self.subTest(url=url):
                self.assertEqual(ansible_vars.check(that[2], relay_public_url=url), ours)
                if ours:
                    self.assertIsNone(validate.relay_url_problem(url.rstrip('/')))


# The form on the welcome page, and Galaxy's runner and destination from byoc-sync's templates


@needs_ansible
class FormAndRunnerTest(unittest.TestCase):
    def setUp(self):
        self.form = ansible_vars.resolve('_relay_byoc_form', **ansible_vars.BYOC)
        self.app = ansible_vars.render_yaml('pulsar-app.yml.j2', **ansible_vars.BYOC)
        self.request = form_request(self.form, ansible_vars.PAIR_PASSWORD)
        self.names = byoc_names(self.request.name)
        context = dict(req=self.request, names=self.names, site=site()['site'], email='alice@example.org')
        [self.runner] = yaml.safe_load(render_byoc_template('runner.yml.j2', **context))
        self.destination = yaml.safe_load(render_byoc_template('destination.yml.j2', **context))[
            self.names.destination_id]

    def test_the_form_passes_byoc_syncs_validation_as_a_relay_endpoint(self):
        self.assertEqual(self.form['backend'], 'relay')
        self.assertEqual(self.request.backend, models.RELAY)
        self.assertEqual(self.request.name, 'hpc')
        self.assertEqual(self.request.password, ansible_vars.PAIR_PASSWORD)
        self.assertEqual(self.request.relay_url, 'https://relay.example.org')
        self.assertEqual((self.request.max_cores, self.request.max_mem), (4, 14))
        self.assertEqual(self.request.nfs_path, '/var/lib/pulsar')

    def test_runner_reaches_this_relay_with_pulsars_account_and_topics(self):
        params = self.runner['params']
        self.assertEqual(self.runner['id'], self.names.runner_id)
        self.assertEqual(self.runner['load'], 'galaxy.jobs.runners.pulsar:PulsarMQJobRunner')
        self.assertEqual(params['relay_url'], ansible_vars.resolve('relay_public_url'))
        self.assertEqual(params['relay_username'], self.app['message_queue_username'])
        self.assertEqual(params['relay_topic_prefix'], self.app['relay_topic_prefix'])
        self.assertEqual(params['relay_password'], '{{ %s }}' % self.names.relay_vault_var)
        self.assertEqual(self.app['message_queue_password'], ansible_vars.PAIR_PASSWORD)
        # Pulsar reaches the relay over loopback, which is the same relay.
        self.assertEqual(self.app['message_queue_url'], 'http://127.0.0.1:8080')
        # Every runner parameter is one Galaxy 26.0+ accepts for the Pulsar runners.
        self.assertLessEqual(set(params), {'relay_url', 'relay_username', 'relay_password', 'relay_topic_prefix',
                                           'galaxy_url'})
        # The relay runner names no manager, so Pulsar must serve the default one.
        self.assertNotIn('manager', params)
        self.assertEqual(set(self.app['managers']), {'_default_'})

    def test_destination_directories_are_the_co_located_pulsars(self):
        self.assertEqual(self.destination['runner'], self.names.runner_id)
        self.assertEqual(self.destination['params']['jobs_directory'], self.app['staging_directory'])
        self.assertEqual(self.destination['params']['persistence_directory'], self.app['persistence_directory'])
        self.assertEqual(self.destination['env']['SINGULARITY_CACHEDIR'],
                         ansible_vars.resolve('relay_pulsar_container_cache_dir'))
        self.assertEqual(self.destination['params']['jobs_directory'],
                         ansible_vars.resolve('relay_galaxy_jobs_directory', **ansible_vars.BYOC))
        self.assertEqual(self.destination['max_accepted_cores'], 4)
        self.assertEqual(self.destination['max_accepted_mem'], 14)

    def test_without_co_located_pulsar_the_form_leaves_limits_to_the_user(self):
        form = ansible_vars.resolve('_relay_byoc_form', **{**ansible_vars.BYOC, 'relay_install_pulsar': 'false'})
        self.assertEqual(set(form), {'backend', 'username', 'relay_url', 'relay_username', 'relay_topic_prefix'})

    def test_non_byoc_app_yml_is_unchanged(self):
        app = ansible_vars.render_yaml('pulsar-app.yml.j2', relay_install_pulsar='true')
        self.assertEqual(app['message_queue_username'], 'galaxy-pulsar')
        self.assertEqual(app['relay_topic_prefix'], 'src')
        self.assertEqual(app['staging_directory'], '/var/lib/pulsar/staging')


# The connection state (tasks/connection.yml)


@needs_ansible
class ConnectionStateTest(unittest.TestCase):
    def state(self, login_status, topics, install_pulsar):
        task = ansible_vars.task_named('tasks/connection.yml', 'Record the relay connection state')
        facts = task['ansible.builtin.set_fact']['_relay_byoc_state']
        params = {**ansible_vars.BYOC, 'relay_install_pulsar': install_pulsar,
                  '_relay_pair_login': {'status': login_status},
                  '_relay_topics': {'json': [{'topic_name': name} for name in topics]} if topics is not None
                  else {'skipped': True}}
        templar = ansible_vars.templar(**params)
        templar.available_variables = {**templar.available_variables,
                                       '_relay_topic_names': templar.template(task['vars']['_relay_topic_names'])}
        return {key: templar.template(value, convert_data=True) for key, value in facts.items()
                if key != 'checked_at'}

    def test_states(self):
        capabilities, setup = 'galaxy_hpc_byoc_pulsar_capabilities', 'galaxy_hpc_byoc_job_setup'
        for status, topics, install, state, galaxy_seen in [
            (200, [capabilities], 'true', 'connected', False),
            (200, [capabilities, setup], 'false', 'connected', True),
            (200, [], 'false', 'waiting', False),
            (200, ['src_pulsar_capabilities'], 'true', 'error', False),
            (401, None, 'false', 'error', False),
        ]:
            with self.subTest(status=status, topics=topics, install=install):
                result = self.state(status, topics, install)
                self.assertEqual(result['state'], state)
                self.assertEqual(result['galaxy_seen'], galaxy_seen)
                self.assertIn('galaxy_hpc_byoc' if state != 'error' or install == 'false' else 'Pulsar',
                              result['message'])


# Credentials and the pair account in BYOC mode


class ConfigureByocTest(unittest.TestCase):
    def pair_state(self, directory):
        return json.loads((Path(directory) / 'pair.json').read_text())

    def relay(self, directory):
        return json.loads((Path(directory) / '.secrets' / 'relay.json').read_text())

    def test_byoc_name_is_generated_kept_and_names_the_pair_account(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            self.assertTrue(configure.configure(directory, 'https://relay.example.org', byoc=True))
            state = self.pair_state(directory)
            name = state['byoc_name']
            self.assertRegex(name, r'^src[0-9a-f]{8}$')
            self.assertTrue(configure.is_valid_byoc_name(name))
            self.assertEqual(state['pair_username'], f'galaxy_{name}_byoc')
            self.assertEqual(self.relay(directory)['pair_username'], f'galaxy_{name}_byoc')
            password = self.relay(directory)['pair_password']
            self.assertTrue(configure.is_valid_byoc_password(password))
            self.assertEqual(os.stat(Path(directory) / 'pair.json').st_mode & 0o777, 0o600)
            # A rerun keeps everything and changes nothing.
            self.assertFalse(configure.configure(directory, 'https://relay.example.org', byoc=True))
            self.assertEqual(self.pair_state(directory)['byoc_name'], name)
            # relay_pair_username is ignored in BYOC mode.
            self.assertFalse(configure.configure(directory, 'https://relay.example.org', pair_username='other-pair',
                                                 byoc=True))
            # A given name wins and retires the earlier account.
            configure.configure(directory, 'https://relay.example.org', byoc=True, byoc_name='hpc')
            self.assertEqual(self.relay(directory)['pair_username'], 'galaxy_hpc_byoc')
            self.assertEqual(self.relay(directory)['retired_pair_usernames'], [f'galaxy_{name}_byoc'])
            self.assertEqual(self.relay(directory)['pair_password'], password)

    def test_switching_to_byoc_retires_the_old_pair_and_back_reactivates_it(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            configure.configure(directory, 'https://relay.example.org')
            self.assertEqual(self.relay(directory)['retired_pair_usernames'], [])
            hex_password = self.relay(directory)['pair_password']
            configure.configure(directory, 'https://relay.example.org', byoc=True, byoc_name='hpc')
            relay = self.relay(directory)
            self.assertEqual(relay['pair_username'], 'galaxy_hpc_byoc')
            self.assertEqual(relay['retired_pair_usernames'], ['galaxy-pulsar'])
            # A generated (hexadecimal) password is valid in BYOC mode and is kept.
            self.assertEqual(relay['pair_password'], hex_password)
            configure.configure(directory, 'https://relay.example.org')
            relay = self.relay(directory)
            self.assertEqual(relay['pair_username'], 'galaxy-pulsar')
            self.assertEqual(relay['retired_pair_usernames'], ['galaxy_hpc_byoc'])
            # BYOC mode later comes back with the same name.
            configure.configure(directory, 'https://relay.example.org', byoc=True)
            self.assertEqual(self.relay(directory)['pair_username'], 'galaxy_hpc_byoc')
            self.assertEqual(self.relay(directory)['retired_pair_usernames'], ['galaxy-pulsar'])

    def test_upgrade_from_a_deployment_without_pair_record(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            configure.configure(directory, 'https://relay.example.org', pair_username='old-pair')
            (Path(directory) / 'pair.json').unlink()
            configure.configure(directory, 'https://relay.example.org', byoc=True, byoc_name='hpc')
            self.assertEqual(self.relay(directory)['retired_pair_usernames'], ['old-pair'])

    def test_a_chosen_password_with_symbols_is_replaced_in_byoc_mode_only_when_not_given(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            configure.configure(directory, 'https://relay.example.org', pair_password='Chosen-pair.pass_2026~ok')
            configure.configure(directory, 'https://relay.example.org', byoc=True, byoc_name='hpc')
            password = self.relay(directory)['pair_password']
            self.assertTrue(configure.is_valid_byoc_password(password))
            self.assertEqual(json.loads((Path(directory) / 'secrets.json').read_text())['PULSAR_PAIR_PASSWORD'],
                             password)
            with self.assertRaises(ValueError):
                configure.configure(directory, 'https://relay.example.org', byoc=True,
                                    pair_password='Chosen-pair.pass_2026~ok')
            configure.configure(directory, 'https://relay.example.org', byoc=True, pair_password='Chosen2026pairPass')
            self.assertEqual(self.relay(directory)['pair_password'], 'Chosen2026pairPass')

    def test_rejects_invalid_byoc_names_and_corrupt_pair_records(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            for name in INVALID_NAMES[:-1]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    configure.configure(directory, 'https://relay.example.org', byoc=True, byoc_name=name)
            self.assertFalse((Path(directory) / 'secrets.json').exists())
            configure.configure(directory, 'https://relay.example.org', byoc=True)
            for record in ['[]', '{"pair_username": "x"}',
                           json.dumps({'byoc': True, 'byoc_name': 'Bad Name', 'pair_username': 'galaxy-pulsar',
                                       'retired_pair_usernames': []}),
                           json.dumps({'byoc': True, 'byoc_name': None, 'pair_username': 'galaxy-pulsar',
                                       'retired_pair_usernames': ['sram:admin']})]:
                (Path(directory) / 'pair.json').write_text(record)
                with self.subTest(record=record), self.assertRaises(ValueError):
                    configure.configure(directory, 'https://relay.example.org', byoc=True)


# The vendored files are byoc-sync's


VENDORED_FILES = {
    'naming.py': 'naming.py',
    'validate.py': 'validate.py',
    'models.py': 'models.py',
    'eu.yml': 'sites/eu.yml',
    'templates/eu/runner.yml.j2': 'templates/eu/runner.yml.j2',
    'templates/eu/destination.yml.j2': 'templates/eu/destination.yml.j2',
}


@unittest.skipUnless(os.environ.get('BYOC_SYNC_REPO'), 'set BYOC_SYNC_REPO to a checkout of the byoc-sync repository')
class VendoredFilesTest(unittest.TestCase):
    def test_vendored_files_match_the_byoc_sync_repository(self):
        checkout = Path(os.environ['BYOC_SYNC_REPO']) / 'byoc-sync' / 'src' / 'byoc_sync'
        for ours, theirs in VENDORED_FILES.items():
            with self.subTest(file=theirs):
                self.assertTrue(filecmp.cmp(CONTRACT / ours, checkout / theirs, shallow=False),
                                f'{theirs} changed in the byoc-sync repository; review it and copy it to '
                                'tests/byoc_contract/')


if __name__ == '__main__':
    unittest.main()
