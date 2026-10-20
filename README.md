# Pulsar Relay on SURF Research Cloud

An SRC Ansible component that installs [Galaxy Project's Pulsar Relay](https://github.com/galaxyproject/pulsar-relay) with Docker Compose, a persistent Valkey store, and the workspace's HTTPS reverse proxy. Supports Ubuntu 22.04 and 24.04.

Galaxy and Pulsar each connect to the relay to exchange job messages. **Files still transfer directly between Pulsar and Galaxy**; Pulsar must be able to reach Galaxy's HTTPS endpoint. This component installs the relay service; a separate Pulsar worker runs the jobs.

As in the [SRC Pulsar component](https://github.com/ErasmusMC-Bioinformatics/src-component-pulsar), the workspace root (`https://WORKSPACE_HOST/`) is an SRAM-protected **welcome page** with copy-and-paste connection details: the relay URL, a built-in Galaxy/Pulsar pair account created at deployment, the topic prefix, and ready-made `job_conf.yml` and `app.yml` snippets. The relay serves nothing at exactly `/`, so every other path still reaches the relay API.

The relay is built from upstream commit `048689a2b55192fda43fb4422ba99630051f2e09`. Valkey uses the upstream deployment's `9.0-trixie` image tag. Base image tags and Python dependency ranges can change; the complete dependency set is not locked. Review upgrades and run the live test before changing the relay revision in `Dockerfile`.

## SRC catalog setup

1. Create an Ansible component using this repository and `playbook.yml` as the script. Install the roles and collections in `requirements.yml` through SRC's Ansible dependency mechanism. Use the same mechanism as the existing [Pulsar component](https://github.com/ErasmusMC-Bioinformatics/src-component-pulsar).
2. Add the component to a catalog item using Ubuntu 22.04 or 24.04. Include **SRC-Nginx before the relay component**, with a public workspace hostname and a valid HTTPS certificate. The playbook checks for the nginx authentication include installed by SRC-Nginx.
3. Provide these component parameters as needed:

   | Parameter | Default | Purpose |
   | --- | --- | --- |
   | `relay_public_url` | Workspace FQDN, falling back to `ansible_fqdn`, with HTTPS | Actual public workspace origin; override if hostname autodetection is wrong |
   | `relay_port` | `8080` | Local relay port behind nginx |
   | `relay_admin_co_group` | `src_co_admin` | SRC-managed CO administrator group, matching the Galaxy component default |
   | `relay_pair_username` | `galaxy-pulsar` | Built-in non-admin relay account shared by one Galaxy/Pulsar pair |
   | `relay_pair_password` | empty: generated | Optional password for that account, 16-128 of `A-Z a-z 0-9 . _ ~ + = @ % -`; leave empty to generate one on the workspace |
   | `relay_topic_prefix` | `src` | Topic prefix shown on the welcome page; Galaxy and Pulsar must both use it |
   | `relay_install_pulsar` | `false` | `true` also installs Pulsar on this workspace, already connected to the relay (see below) |

4. Create the workspace. Start with at least 2 vCPUs and 4 GB RAM; size for message volume. Keep it running while Galaxy and Pulsar need to communicate and choose an appropriate expiration date.
5. Open `https://WORKSPACE_HOST/` (SRAM sign-in) for the welcome page with the Galaxy and Pulsar configuration, or `https://WORKSPACE_HOST/ready` to check readiness. CO administrators sign in through SRAM at `https://WORKSPACE_HOST/admin/docs`.

The playbook installs Docker Engine and Compose v2, copies the deployment into `/opt/pulsar-relay`, generates credentials, starts the containers, templates the welcome page into `/srv/static/index.html` (readable by nginx only), and configures `uusrc.general.nginx_reverse_proxy`. It expects a dedicated workspace: apart from the exact-match welcome location, the relay owns nginx's `/` location. Both long polling and WebSocket upgrades are supported by that SRC role.

Browser administration uses **SRAM authentication and CO group membership**. The `/admin/` location requires SRC authentication and passes the verified username with a private proxy authentication key. The relay accepts management requests only when that username is in the configured CO administrator group. Client-supplied identity headers are cleared on the public machine API. Galaxy and Pulsar use the relay's bearer authentication outside `/admin/`. Only nginx's HTTPS endpoint should be reachable externally: port 8080 is bound to `127.0.0.1`, and Valkey has no published port.

## Local development

Requires Python 3.10+, Docker Engine, Compose v2 with `up --wait`, permission to use the Docker daemon, and outbound access to GitHub, PyPI and container registries. These commands work without Ansible or SRC:

To install Docker directly on an Ubuntu host without running the playbook, use the standalone installer. It follows [Docker's official apt repository instructions](https://docs.docker.com/engine/install/ubuntu/), starts the Docker service, and verifies Engine and Compose with a `hello-world` container:

```bash
sudo bash scripts/install_docker.sh
sudo python3 scripts/test_relay.py
```

Run these from a normal host terminal with working sudo and outbound network access. The live test uses sudo to access Docker; no Docker group membership change is required. To start the relay manually with an account that already has Docker access:

```bash
python3 scripts/configure.py --public-url http://127.0.0.1:8080
docker compose up --build --wait --wait-timeout 180
curl --fail http://127.0.0.1:8080/ready
```

For a manual installation on an SRC workspace, install Ansible dependencies and run:

```bash
ansible-galaxy install -r requirements.yml
ansible-playbook -i localhost, -c local playbook.yml --become \
  -e relay_public_url=https://YOUR-WORKSPACE-HOST
```

For a non-SRC host, `-e relay_configure_src_proxy=false` skips the SRC nginx prerequisite and proxy role. Configure HTTPS separately before remote use. The playbook defaults to `localhost`; set the `variable_host` environment variable when using a remote Ansible inventory.

## Credentials and client setup

Credentials are generated once in `secrets.json` (mode `0600`) and preserved across reruns. Services receive separate read-only configuration files from `.secrets/`, whose host directory is owner-only (`0700`). Individual mounted files are readable by the unprivileged service user inside its container. The `.env` file contains only non-secret settings. These secret files are excluded from Git and the Docker build context.

Retrieve only the credential needed through your approved secret-management process on the workspace. Avoid dumping the secrets file into terminal recordings, chat, screenshots or logs. Docker administrators and host root can still read container-mounted secrets; Compose file mounts do not encrypt them at rest.

### Built-in pair account and the welcome page

`scripts/configure.py` generates `PULSAR_PAIR_PASSWORD` next to the other secrets and records `relay_pair_username` in the relay's configuration file. On every start the relay (`relay_src/pair.py`) creates that account with `read` and `write` permissions, or re-hashes its password after a rotation; it never receives `admin`. The welcome page at `https://WORKSPACE_HOST/` renders the account, the relay URL and `relay_topic_prefix` into the Galaxy and Pulsar snippets below, so connecting the default pair needs no manual registration. The page is served only after SRAM sign-in and is regenerated whenever the component reruns.

### Administration and additional pairs

Open `/admin/docs` and sign in with SRAM. As in [your Galaxy 26.0 component](https://github.com/mirelaminkova/src-component-galaxy/blob/26.0/tasks/set_config.yml), administrator membership comes from `fact_co_groups[relay_admin_co_group]`. There is no shared human administrator username or password. The relay creates a passwordless `sram:USERNAME` identity when an authorized administrator visits. Management authorization is checked on every request; it is not stored as a permanent administrator grant.

For a second Galaxy/Pulsar pair, use `POST /auth/register` on the SRAM-protected Swagger page to create another **non-admin** account. Requests on this page use your SRAM session automatically; the Swagger password authorization dialog is for machine accounts and is not needed for CO administration:

```json
{
  "username": "second-pair",
  "password": "REPLACE-WITH-A-LONG-RANDOM-PASSWORD",
  "permissions": ["read", "write"]
}
```

**Use the same relay identity for both ends of a pair.** The pinned relay namespaces topics by user; two different user accounts cannot exchange messages merely by choosing the same topic prefix. Give separate pairs separate accounts. Topic prefixes must also match at both ends.

### Galaxy and Pulsar configuration

The password-based examples below follow Galaxy 26.0's Pulsar runner (`pulsar-galaxy-lib` 0.15.14), which takes `relay_url`, `relay_username`, `relay_password` and `relay_topic_prefix` on the runner. Older Galaxy releases reject `relay_url`; Pulsar 0.15.13 or newer is needed for `relay_topic_prefix`. Current clients also support refresh credentials and OIDC device login, which requires separately configuring an OIDC provider on the relay.

The SRC Galaxy component's **"Connect existing Pulsar"** parameter configures the HTTP (REST) runner only. A Galaxy component with relay support takes a separate one-line parameter, `src_galaxy_pulsar_relay_configs`, which the welcome page prints ready to paste; otherwise edit Galaxy's job configuration as below. The welcome page shows these snippets with the real values filled in.

Pulsar's `app.yml` (`/mnt/pulsar/config/app.yml` on an SRC Pulsar workspace; an `https://` `message_queue_url` switches Pulsar to relay mode, the REST endpoint can stay):

```yaml
message_queue_url: https://YOUR-RELAY-HOST
message_queue_username: galaxy-pulsar
message_queue_password: YOUR-PAIR-PASSWORD
relay_topic_prefix: src
```

Galaxy's `job_conf.yml` (`/srv/galaxy/config/job_conf.yml` on an SRC Galaxy workspace; merge into the existing configuration):

```yaml
runners:
  pulsar_relay:
    load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner
    relay_url: https://YOUR-RELAY-HOST
    relay_username: galaxy-pulsar
    relay_password: YOUR-PAIR-PASSWORD
    relay_topic_prefix: src
    galaxy_url: https://YOUR-GALAXY-HOST

execution:
  environments:
    src_pulsar:
      runner: pulsar_relay
      jobs_directory: /mnt/pulsar/files/staging
      manager: _default_
      dependency_resolution: remote
      default_file_action: remote_transfer

tools:
  - id: YOUR-TOOL-ID
    environment: src_pulsar
```

`galaxy_url` on the runner is **Galaxy's URL**, used by Pulsar for direct file transfers through Galaxy's `/api/` endpoints, so keep the Galaxy component's *API exposed* parameter enabled. Galaxy 26.0's relay runner ignores a `url` on the environment; without `galaxy_url` it falls back to Galaxy's `galaxy_infrastructure_url`. This matches usegalaxy.eu's relay runner for the NEMO endpoint. Set `jobs_directory` to the staging directory of your Pulsar worker: the SRC Pulsar component uses `<Pulsar Root>/files/staging`, which is `/mnt/pulsar/files/staging` without attached storage. Preserve your current default destination and existing runners/tool mappings. Protect files containing passwords and restart Galaxy (`systemctl restart 'galaxy-*'`) and Pulsar (`systemctl restart pulsar`) after configuration changes. Existing REST-based Pulsar workers need their communication configuration changed to relay mode; creating the relay workspace alone does not connect them.

## Pulsar on the relay workspace

With `relay_install_pulsar: true` the component also installs Pulsar on the same workspace, modelled on usegalaxy.eu's [NEMO login-node role](https://github.com/usegalaxy-eu/pulsar-nemo-login-role): `pulsar-app` 0.15.15 and `pulsar-relay-client` 0.2.1 in `/opt/pulsar/venv`, started webless with `pulsar-main` by a `pulsar` systemd unit (instead of NEMO's supervisord). It connects to the relay over `http://127.0.0.1:8080` with the built-in pair account and topic prefix, so its `app.yml` (`/opt/pulsar/config/app.yml`, mode `0640`) is written from the generated credentials and nothing is copied by hand. The playbook waits until Pulsar's capability snapshot topic (`<prefix>_pulsar_capabilities`) appears on the relay, which proves Pulsar connected.

Jobs run on the workspace itself with the `queued_python` manager (no Slurm); `relay_pulsar_managers` can be overridden. `relay_pulsar_docker` (default `true`) adds the `pulsar` user to the `docker` group so Galaxy destinations can set `docker_enabled: true`; that group is root-equivalent on the host. Staging lives in `/var/lib/pulsar/staging`; set `relay_pulsar_root` to attached storage for larger data. The welcome page then shows `jobs_directory: /var/lib/pulsar/staging` for Galaxy.

This combined mode suits SRC, where workspaces belong to a collaboration and expire: the relay and its Pulsar live and disappear together, and only Galaxy needs the welcome page values. Keep a standalone relay for several Pulsar workspaces, or use the Galaxy provider's relay (usegalaxy.eu runs one at `stargate.galaxyproject.eu`) when the Galaxy is not on SRC.

## Testing

```bash
python3 -m unittest discover -s tests -v
ansible-playbook -i localhost, playbook.yml --syntax-check
python3 scripts/test_relay.py
```

The first command checks credential retention/migration, private file permissions, input validation, the built-in pair account bootstrap, proxy identity verification, CO membership revocation, stale-policy rejection, and exclusion of secrets from Compose metadata. The Ansible check requires the installed dependencies from `requirements.yml`.

The live test builds the actual relay and starts Valkey in a randomly named disposable Compose project on a free localhost port. It checks readiness, rejection of anonymous access, the built-in pair account's login and lack of administrative rights, trusted SRAM proxy identities, forged-header rejection, administrator revocation, non-admin machine permissions, publishing, long-poll delivery, and survival of users/topics/messages when both containers are recreated. It removes only its own test containers and volume. Use `--keep` to retain the test project for inspection. It does not test Galaxy job execution or SRC's public TLS/SRAM infrastructure; validate those on an SRC workspace with a real Galaxy/Pulsar pair.

The GitHub Actions workflow runs these checks when this repository is eventually published. The container deployment still requires live verification in a Docker-enabled environment.

## CO administrator synchronization

The initial membership list follows Galaxy's `uusrc.general.fact_regular_users` and `fact_co_groups` pattern. A host systemd timer (`pulsar-relay-admin-sync.timer`) then refreshes it every 60 seconds from `getent group`, accepting only groups listed in `/etc/rsc/managedgroups`. This uses the workspace's SRC-synchronized SRAM group data, not a separate manually maintained administrator list.

Changes take effect after SRC updates the workspace group and the next timer refresh runs. If the refresh stops or the policy is unreadable, administrative access fails closed after the policy's 180-second validity window. Machine traffic continues independently. Use `systemctl status pulsar-relay-admin-sync.timer` to check the refresh service. No application can see a CO change before SRC has synchronized it onto the workspace.

The local `relay_src` adapter adds the trusted-proxy integration absent from the pinned upstream relay. It leaves upstream JWT/storage/host/origin safety checks in place while replacing the local-bootstrap-password requirement with SRAM administration. Stored `admin` grants, including any old bootstrap account, cannot authorize administration through the machine API. Upgrading the previous setup preserves storage/JWT secrets and removes its bootstrap password from generated configuration; existing database accounts are not deleted.

For a development run without SRC, the management endpoint remains unavailable unless the isolated test supplies a mock CO policy and the private proxy key. Do not expose this mock setup publicly. The automated live test simulates the proxy boundary; real SRAM sign-in, nginx prefix routing and group synchronization still need verification on SRC.

## Operations and persistence

```bash
cd /opt/pulsar-relay
sudo docker compose -p pulsar-relay ps
sudo docker compose -p pulsar-relay logs --tail 100 relay
sudo docker compose -p pulsar-relay restart relay
```

Valkey stores messages, users, topics and authentication state in the named volume `pulsar-relay_valkey-data`. AOF (`everysec`) and RDB snapshots are enabled, and `noeviction` prevents memory pressure from deleting authentication state. At the configured 512 MB store limit, writes fail rather than evict data. Adjust `files/valkey.conf` and workspace sizing if needed. Message history is capped at 100,000 entries per topic; time-based expiry is disabled to preserve the latest state of idle topics.

Container recreation and workspace reboots retain the named volume. **Workspace deletion or expiration can delete it**; a Docker volume is not an off-workspace backup. Retain the private secrets and take backups off the workspace before destroying it. Stop both containers before taking a consistent full-volume backup. For RDB-only backup/restore procedures, see [upstream persistence instructions](https://github.com/galaxyproject/pulsar-relay/blob/main/docs/CONFIGURATION.md#persistence-and-backups); with AOF enabled, copying `dump.rdb` alone does not restore the database.

Do not run `docker compose down --volumes` on the production project. For credential rotation, set a new `relay_pair_password` parameter and rerun the component, or edit the corresponding 64-character hexadecimal secret in `secrets.json`, rerun `scripts/configure.py` with the same public URL/port/pair username, then run `docker compose -p pulsar-relay up -d --force-recreate --wait` so the service processes load the new mounted contents. JWT rotation invalidates existing tokens. A rotated `PULSAR_PAIR_PASSWORD` is applied to the built-in account at the next relay start; rerun the component to refresh the welcome page, and update Galaxy and Pulsar. Coordinate password rotation with clients. Keep secrets out of terminal captures and support logs.
