"""Galaxy's relay runner and environment for the deployed relay and its co-located Pulsar.

The same runner and environment the README and the welcome page tell Galaxy administrators
to add to ``job_conf.yml`` (``PulsarMQJobRunner`` with ``relay_url``, ``relay_username``,
``relay_password``, ``relay_topic_prefix`` and ``galaxy_url``), built from what the
deployment wrote: the pair password from ``secrets.json`` and the component's parameters.

    python -m tests.e2e.galaxy_config SECRETS_JSON PARAMS_YML GALAXY_URL > galaxy_job.json

PARAMS_YML holds the component parameters the deployment ran with (``relay_*``; missing
ones take group_vars/all.yml's defaults). Prints the JSON that tests/e2e/galaxy_job.py takes.
"""

import argparse
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
RUNNER_LOAD = "galaxy.jobs.runners.pulsar:PulsarMQJobRunner"
# PulsarMQJobRunner.destination_defaults, as the welcome page's environment spells them out.
ENVIRONMENT_DEFAULTS = {
    "manager": "_default_",
    "dependency_resolution": "remote",
    "default_file_action": "remote_transfer",
}


def component_defaults():
    """group_vars/all.yml's literal defaults for the parameters the runner needs."""
    defaults = yaml.safe_load((ROOT / "group_vars" / "all.yml").read_text())
    return {
        "relay_port": defaults["relay_port"],
        "relay_pair_username": defaults["relay_pair_username"],
        "relay_topic_prefix": defaults["relay_topic_prefix"],
        "relay_pulsar_root": defaults["relay_pulsar_root"],
    }


def pulsar_root(value):
    """_relay_pulsar_root: an empty relay_pulsar_root (as SRC passes it) means /var/lib/pulsar."""
    return str(value or "").strip() or "/var/lib/pulsar"


def galaxy_job_config(secrets, params, galaxy_url):
    """Return ``{"runner": ..., "destination": ...}`` for the relay on this workspace.

    ``secrets`` is the deployment's secrets.json, ``params`` the component parameters it ran
    with; ``galaxy_url`` is where Pulsar fetches inputs and posts outputs.
    """
    settings = {**component_defaults(), **{k: v for k, v in params.items() if k.startswith("relay_")}}
    password = secrets.get("PULSAR_PAIR_PASSWORD")
    if not password:
        raise ValueError("secrets.json has no PULSAR_PAIR_PASSWORD")
    if not str(galaxy_url).startswith(("http://", "https://")):
        raise ValueError("galaxy_url must be an http(s) URL")
    runner = {
        "load": RUNNER_LOAD,
        # The co-located Pulsar's own address for the relay (templates/pulsar-app.yml.j2): CI has
        # no SRC reverse proxy in front of it.
        "relay_url": f"http://127.0.0.1:{settings['relay_port']}",
        "relay_username": settings["relay_pair_username"],
        "relay_password": password,
        "relay_topic_prefix": settings["relay_topic_prefix"],
        "galaxy_url": galaxy_url,
    }
    destination = {
        **ENVIRONMENT_DEFAULTS,
        # relay_galaxy_jobs_directory with relay_install_pulsar: Pulsar's staging directory.
        "jobs_directory": f"{pulsar_root(settings['relay_pulsar_root'])}/staging",
    }
    return {"runner": runner, "destination": destination}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("secrets_json", help="the deployment's secrets.json")
    parser.add_argument("params_yml", help="the component parameters (relay_*)")
    parser.add_argument("galaxy_url", help="Galaxy's URL for Pulsar's file transfers")
    args = parser.parse_args(argv)
    secrets = json.loads(Path(args.secrets_json).read_text())
    params = yaml.safe_load(Path(args.params_yml).read_text()) or {}
    json.dump(galaxy_job_config(secrets, params, args.galaxy_url), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover
    main()
