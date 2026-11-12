"""Resolve group_vars and render templates with ansible-core, as the playbook would.

Optional: tests that use this module are skipped when ansible-core is not installed
(CI runs the unit tests before it installs Ansible).
"""
import base64
import json
from pathlib import Path

try:
    import yaml
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
except ImportError:  # pragma: no cover - depends on the environment
    Templar = None

REPO = Path(__file__).resolve().parents[1]
AVAILABLE = Templar is not None
PAIR_PASSWORD = 'Rel4yPairPassw0rdForTests2026xyz'
SECRETS = {'PULSAR_JWT_SECRET_KEY': 'a' * 64, 'PULSAR_VALKEY_PASSWORD': 'b' * 64,
           'PULSAR_SRC_PROXY_SECRET': 'c' * 64, 'PULSAR_PAIR_PASSWORD': PAIR_PASSWORD}
# Facts and parameters as on an SRC Ubuntu workspace (parameters as SRC passes them: strings).
FACTS = {
    'ansible_facts': {'processor_vcpus': 4, 'memtotal_mb': 16000, 'fqdn': 'relay.example.org'},
    'ansible_processor_vcpus': 4,
    'ansible_fqdn': 'relay.example.org',
    'workspace_fqdn': 'relay.example.org',
    'inventory_hostname': 'localhost',
    # A task variable of the template tasks (from secrets.json).
    '_relay_pair_password': PAIR_PASSWORD,
    '_relay_secrets_file': {'content': base64.b64encode(json.dumps(SECRETS).encode()).decode()},
}
BYOC = {'relay_byoc': 'true', '_relay_byoc_name': 'hpc', '_relay_pair_username': 'galaxy_hpc_byoc',
        'relay_install_pulsar': 'true'}


def templar(**params):
    loader = DataLoader()
    variables = loader.load_from_file(str(REPO / 'group_vars' / 'all.yml'))
    variables.update(yaml.safe_load((REPO / 'parameters.yml').read_text()))
    variables.update(FACTS)
    variables.update(params)
    return Templar(loader=loader, variables=variables)


def resolve(name, **params):
    """Variable ``name`` as the playbook sees it."""
    return templar(**params).template('{{ %s }}' % name, convert_data=True)


def check(expression, **params):
    """A task's condition (``when``/``that``) as the playbook evaluates it."""
    return templar(**params).template('{{ (%s) | bool }}' % expression, convert_data=True)


def render(template, **params):
    """Render ``templates/<template>`` like ansible.builtin.template (trim_blocks)."""
    text = (REPO / 'templates' / template).read_text()
    env = templar(**params).copy_with_new_env(trim_blocks=True)
    return env.do_template(text, preserve_trailing_newlines=True, escape_backslashes=False)


def render_yaml(template, **params):
    return yaml.safe_load(render(template, **params))


def load_yaml(path):
    return yaml.safe_load((REPO / path).read_text())


def task_named(path, name):
    tasks = load_yaml(path)
    if isinstance(tasks, list) and tasks and 'hosts' in tasks[0]:
        tasks = tasks[0].get('pre_tasks', []) + tasks[0].get('tasks', [])
    [task] = [t for t in tasks if t.get('name') == name]
    return task
