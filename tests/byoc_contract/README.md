# Vendored byoc-sync contract

Verbatim copies from [sanjaysrikakulam/elixir_biohackathon_2026_project1](https://github.com/sanjaysrikakulam/elixir_biohackathon_2026_project1)
(`byoc-sync/src/byoc_sync/`), taken on 2026-11-12 from a working tree based on commit
`8618c3f7be8477bd063e6ea6f4ca8dc698cf5146` that adds byoc-sync's **relay backend**
(`backend: relay` in the `byoc_pulsar` form). That relay backend was not committed upstream
yet; when it lands, compare with `BYOC_SYNC_REPO=... python -m unittest tests.test_byoc_contract`
and copy any changed file again.

| File here | File in the byoc-sync repository |
| --- | --- |
| `naming.py` | `naming.py` (the names derived from the BYOC name, including the relay account and topic prefix `galaxy_<name>_byoc`) |
| `validate.py` | `validate.py` (the rules for the form's `username`, `password`, `relay_*` and `nfs_path`) |
| `models.py` | `models.py` (the data types `validate.py` uses) |
| `eu.yml` | `sites/eu.yml` (usegalaxy.eu: the naming patterns and `backends: [rabbitmq, relay]`) |
| `templates/eu/runner.yml.j2` | `templates/eu/runner.yml.j2` (Galaxy's `PulsarMQJobRunner` runner, with `relay_url` for the relay backend) |
| `templates/eu/destination.yml.j2` | `templates/eu/destination.yml.j2` (Galaxy's TPV destination) |

`tests/test_byoc_contract.py` feeds this component's BYOC settings through these files: the
names it uses must be the ones byoc-sync derives, the form on the welcome page must pass
byoc-sync's validation, and Pulsar's `app.yml` must match the runner and destination
byoc-sync writes. The tests need PyYAML and Jinja2 (and ansible-core for the playbook's
variables); without them they are skipped.
