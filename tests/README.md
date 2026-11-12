# Tests and CI

| Workflow | Runs on | What it checks |
| --- | --- | --- |
| `checks.yml` | every push and pull request | pre-commit (yamllint, ruff, ansible-lint `production`, actionlint, ast-grep rule tests; pre-push stage: semgrep `p/default` + `p/secrets`, ast-grep scan) and pytest with the coverage gate |
| `test.yml` | every push and pull request | `ansible-playbook --syntax-check` with SRC's `ansible==14.6.0` and with `ansible-core` 2.18 |
| `deploy.yml` | pull requests and pushes to `main`, weekly, manual | the playbook on Ubuntu 22.04 and 24.04 runners with the co-located Pulsar, an idempotent rerun, the deployed state, `scripts/test_relay.py`, and a Galaxy job through the deployed relay to that Pulsar and back, before and after the relay's containers are recreated |

None of them need repository secrets or self-hosted runners: the deployment generates its own
credentials, and GitHub's `ubuntu-22.04`/`ubuntu-24.04` runners have Docker and passwordless
sudo. `deploy.yml` skips what a runner cannot have: SRC's nginx, SRAM and CO facts
(`relay_configure_src_proxy: false`) and CVMFS (`relay_pulsar_cvmfs: "false"`).

## Locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ansible-galaxy install -r requirements.yml
.venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push
.venv/bin/pre-commit run --all-files
.venv/bin/pre-commit run --hook-stage pre-push --all-files
.venv/bin/pytest
```

`tests/test_galaxy_e2e.py` runs a Galaxy job without Docker: this component's relay app
(`relay_src.app`, with the relay's memory backend instead of Valkey) in a uvicorn subprocess,
a Pulsar app configured as `templates/pulsar-app.yml.j2` configures the deployed one, and the
Galaxy side in `tests/e2e/galaxy_job.py`, which does what Galaxy's `PulsarMQJobRunner` does
with a `relay_url` runner. `scripts/test_relay.py` and `deploy.yml` need Docker.

Against a deployed relay with its co-located Pulsar (as root on the workspace, or with the
pair password in a copy of `secrets.json`):

```bash
python3 -m venv /tmp/galaxy && /tmp/galaxy/bin/pip install -r tests/e2e/requirements-galaxy.txt
/tmp/galaxy/bin/python -m tests.e2e.galaxy_config /opt/pulsar-relay/secrets.json PARAMS.yml http://127.0.0.1:0 > /tmp/job.json
/tmp/galaxy/bin/python tests/e2e/galaxy_job.py /tmp/job.json --bind 127.0.0.1
```

`PARAMS.yml` holds the component parameters the deployment ran with (`relay_*`; an empty
file means the defaults).

The relay limits `/auth/login` to 5 requests a minute per client address. Galaxy, Pulsar and
the playbook all sign in from `127.0.0.1` on a co-located workspace, so tests that start many
clients in a row use a relay each (`tests/test_galaxy_e2e.py`).
