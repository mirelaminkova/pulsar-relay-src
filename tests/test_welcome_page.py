"""Render the SRAM-protected welcome page (templates/welcome.html.j2) with sample values."""
from pathlib import Path
import re
import unittest

try:
    import jinja2
except ImportError:  # Jinja2 comes with Ansible; CI installs it after the unit tests.
    jinja2 = None

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
PASSWORD = "pair-Secret.2026~x"

# Values as the playbook resolves them (group_vars/all.yml with its defaults).
SAMPLE = {
    "workspace_fqdn": "relay.example.src.surf-hosted.nl",
    "ansible_fqdn": "relay",
    "ansible_hostname": "relay_ws.1",
    "inventory_hostname": "relay",
    "ansible_distribution": "Ubuntu",
    "ansible_distribution_version": "24.04",
    "relay_public_url": "https://relay.example.src.surf-hosted.nl",
    "relay_pair_username": "galaxy-pulsar",
    "_relay_pair_password": PASSWORD,
    "relay_topic_prefix": "src",
    "relay_admin_co_group": "src_co_admin",
    "fact_co_groups": {"src_co_admin": ["alice", "bob"]},
    "relay_install_dir": "/opt/pulsar-relay",
    "relay_port": 8080,
    "relay_install_pulsar": "false",
    "relay_pulsar_version": "0.15.15",
    "relay_pulsar_relay_client_version": "0.2.1",
    "relay_pulsar_docker": True,
    "relay_pulsar_apptainer": True,
    "_relay_pulsar_cvmfs": False,
    "relay_pulsar_staging_dir": "/var/lib/pulsar/staging",
    "relay_pulsar_config_dir": "/opt/pulsar/config",
    "relay_galaxy_jobs_directory": "/mnt/pulsar/files/staging",
}

BYOC_FORM = {
    "username": "mylab_byoc",
    "max_accepted_cores": 8,
    "max_accepted_mem": 31,
    "min_accepted_gpus": 0,
    "max_accepted_gpus": 0,
    "nfs_path": "/var/lib/pulsar/staging",
}


def to_bool(value):
    """Ansible's bool filter, enough for the values used here."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "on", "1", "true", "y", "t")


def render(**overrides):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
    )
    env.filters["bool"] = to_bool
    return env.get_template("welcome.html.j2").render(**{**SAMPLE, **overrides})


@unittest.skipUnless(jinja2, "Jinja2 is not installed")
class WelcomePageTest(unittest.TestCase):
    def test_plain_page_shows_the_connection_values_with_copy_buttons(self):
        page = render()
        for ident, value in [
            ("relay-url", SAMPLE["relay_public_url"]),
            ("relay-username", "galaxy-pulsar"),
            ("relay-password", PASSWORD),
            ("relay-topic-prefix", "src"),
            ("relay-jobs-directory", "/mnt/pulsar/files/staging"),
        ]:
            self.assertIn(f'<code id="{ident}">{value}</code>', page)
            self.assertIn(f"copyValue('{ident}', this)", page)
        self.assertIn("relay_password: " + PASSWORD, page)
        self.assertIn("message_queue_password: " + PASSWORD, page)
        self.assertIn("remote_relay-ws-1:", page)
        # No BYOC form or connection state without BYOC mode.
        self.assertNotIn("byoc_pulsar", page)
        self.assertNotIn('class="state ', page)

    def test_every_copy_button_has_its_target(self):
        for page in (render(), render(_relay_byoc_form=BYOC_FORM), render(relay_install_pulsar="true")):
            targets = re.findall(r"copyValue\('([^']+)', this\)", page)
            self.assertTrue(targets)
            for target in targets:
                self.assertIn(f'id="{target}"', page)

    def test_byoc_form_in_the_forms_order_with_the_pair_password(self):
        page = render(_relay_byoc_form=BYOC_FORM, _relay_byoc_name="mylab")
        fields = re.findall(r'<code id="byoc-([a-z_]+)">', page)
        self.assertEqual(fields, ["username", "password", "max_accepted_cores", "max_accepted_mem",
                                  "min_accepted_gpus", "max_accepted_gpus", "nfs_path"])
        self.assertIn(f'<code id="byoc-password">{PASSWORD}</code>', page)
        self.assertIn("<code>mylab</code>", page)
        self.assertIn('<span class="state state-unknown">Not checked yet</span>', page)

    def test_byoc_connection_states(self):
        cases = {
            "connected": "state-connected",
            "waiting": "state-waiting",
            "error": "state-error",
            "bogus": "state-unknown",
        }
        for state, css in cases.items():
            page = render(_relay_byoc_form=BYOC_FORM,
                          _relay_byoc_state={"state": state, "message": "no <route>"})
            self.assertIn(f'<span class="state {css}">', page)
        page = render(_relay_byoc_form=BYOC_FORM, _relay_byoc_state={"state": "error", "message": "no <route>"})
        self.assertIn("no &lt;route&gt;", page)
        page = render(_relay_byoc_form=BYOC_FORM, _relay_byoc_state="garbage")
        self.assertIn('<span class="state state-unknown">', page)

    def test_byoc_flag_without_a_form_still_renders(self):
        page = render(relay_byoc="true")
        self.assertIn(f'<code id="byoc-password">{PASSWORD}</code>', page)
        self.assertIn('<span class="state state-unknown">', page)

    def test_co_located_pulsar(self):
        page = render(relay_install_pulsar="true", _relay_pulsar_cvmfs=True,
                      relay_galaxy_jobs_directory="/var/lib/pulsar/staging")
        self.assertIn("Pulsar on this workspace", page)
        self.assertIn("singularity_enabled: true", page)
        self.assertIn("cached_mulled_singularity", page)
        self.assertIn('<li class="tag">CVMFS</li>', page)
        self.assertNotIn("message_queue_password", page)


if __name__ == "__main__":
    unittest.main()
