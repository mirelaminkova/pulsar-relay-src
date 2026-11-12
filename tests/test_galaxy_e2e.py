"""The end-to-end test's Galaxy side (tests/e2e/), here against a relay and a Pulsar on this host.

CI's deploy job runs tests/e2e/galaxy_job.py against the deployed relay (Docker Compose,
Valkey) and the co-located Pulsar the playbook installs. Here the same client talks to this
component's relay app (relay_src.app, with the relay's memory backend instead of Valkey) in
a uvicorn subprocess and to a Pulsar app in this process, configured as
templates/pulsar-app.yml.j2 configures the deployed one, so the setup message, the status
updates through the relay, the staging over HTTP and the un-staging are tested without Docker.
"""

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from pulsar.core import PulsarApp
import pytest
import yaml

from configure import configure
from sync_admins import write_policy
from tests.e2e import galaxy_config, galaxy_job

ROOT = Path(__file__).resolve().parents[1]
SECRETS = {"PULSAR_PAIR_PASSWORD": "Pair-password-for-tests-1234"}


def test_runner_and_destination_are_the_readmes():
    config = galaxy_config.galaxy_job_config(SECRETS, {}, "https://galaxy.example.org")

    assert config == {
        "runner": {
            "load": "galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
            "relay_url": "http://127.0.0.1:8080",
            "relay_username": "galaxy-pulsar",
            "relay_password": SECRETS["PULSAR_PAIR_PASSWORD"],
            "relay_topic_prefix": "src",
            "galaxy_url": "https://galaxy.example.org",
        },
        "destination": {
            "manager": "_default_",
            "dependency_resolution": "remote",
            "default_file_action": "remote_transfer",
            # relay_galaxy_jobs_directory with the co-located Pulsar.
            "jobs_directory": "/var/lib/pulsar/staging",
        },
    }


def test_component_parameters_override_the_defaults():
    params = {
        "relay_port": "9090",
        "relay_pair_username": "second.pair",
        "relay_topic_prefix": "lab",
        "relay_pulsar_root": " /data/storage/pulsar ",
        "unrelated": "ignored",
    }

    config = galaxy_config.galaxy_job_config(SECRETS, params, "http://g.test")

    assert config["runner"]["relay_url"] == "http://127.0.0.1:9090"
    assert config["runner"]["relay_username"] == "second.pair"
    assert config["runner"]["relay_topic_prefix"] == "lab"
    assert config["destination"]["jobs_directory"] == "/data/storage/pulsar/staging"


@pytest.mark.parametrize(
    ("secrets", "url", "message"),
    [
        pytest.param({}, "http://g.test", "PULSAR_PAIR_PASSWORD", id="no-password"),
        pytest.param(SECRETS, "g.test", "http", id="not-a-url"),
    ],
)
def test_invalid_input_is_refused(secrets, url, message):
    with pytest.raises(ValueError, match=message):
        galaxy_config.galaxy_job_config(secrets, {}, url)


def test_config_command_line(capsys, tmp_path):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps(SECRETS))
    params = tmp_path / "params.yml"
    params.write_text("relay_topic_prefix: ci\n")

    galaxy_config.main([str(secrets), str(params), "http://galaxy.test:8080"])

    config = json.loads(capsys.readouterr().out)
    assert config["runner"]["galaxy_url"] == "http://galaxy.test:8080"
    assert config["runner"]["relay_topic_prefix"] == "ci"


def test_client_manager_gets_galaxys_relay_parameters():
    runner = {"manager": "gpu", "relay_url": "http://r", "galaxy_url": "http://g", "load": "x"}
    runner |= {"relay_username": "u", "relay_password": "p", "relay_topic_prefix": "src"}

    kwargs = galaxy_job.client_manager_kwargs(runner)

    assert kwargs == {
        "manager": "gpu",
        "cache": None,
        "transport": None,
        "persistence_directory": None,
        "relay_url": "http://r",
        "relay_username": "u",
        "relay_password": "p",
        "relay_topic_prefix": "src",
    }


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_ready(url, process, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("the relay exited")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310  # local http
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("the relay did not become ready")


@pytest.fixture
def relay(tmp_path_factory):
    """This component's relay app with the credentials scripts/configure.py generates.

    One per test: the relay limits logins per client address, and every job logs in from here.
    """
    work = tmp_path_factory.mktemp("relay")
    (work / "files").mkdir()
    shutil.copyfile(ROOT / "files" / "valkey.conf", work / "files" / "valkey.conf")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    configure(work, base, port)
    write_policy(work / ".policy", [])
    # compose.yml mounts relay.json as relay_config.yaml: the relay reads it by its suffix.
    config = work / "relay_config.yaml"
    shutil.copyfile(work / ".secrets" / "relay.json", config)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        # compose.yml's environment, with the memory backend instead of Valkey.
        "PULSAR_STORAGE_BACKEND": "memory",
        "PULSAR_CONFIG_FILE": str(config),
        "SRC_ADMIN_POLICY_FILE": str(work / ".policy" / "admins.json"),
        "PULSAR_TRUSTED_HOSTS": json.dumps(["127.0.0.1", "localhost"]),
        "PULSAR_ALLOWED_ORIGINS": json.dumps([base]),
        "PULSAR_LOG_LEVEL": "WARNING",
    }
    # Pulsar's and Galaxy's long polls would hold a graceful shutdown for their 30 s.
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "relay_src.app:app",
        "--host",
        "127.0.0.1",
        "--timeout-graceful-shutdown",
        "1",
    ]
    process = subprocess.Popen([*command, "--port", str(port)], env=env, cwd=work)  # noqa: S603
    try:
        wait_ready(f"{base}/ready", process)
        secrets = json.loads((work / "secrets.json").read_text())
        yield {"port": port, "secrets": secrets}
    finally:
        process.terminate()
        process.wait(timeout=30)


@pytest.fixture
def pulsar(relay, tmp_path, monkeypatch):
    """A Pulsar app bound to the relay with the pair account, as app.yml configures it."""
    monkeypatch.chdir(tmp_path)
    for name in ("staging", "persisted"):
        (tmp_path / name).mkdir()
    prefix = "src"
    conf = {
        "message_queue_url": f"http://127.0.0.1:{relay['port']}",
        "message_queue_username": "galaxy-pulsar",
        "message_queue_password": relay["secrets"]["PULSAR_PAIR_PASSWORD"],
        "relay_topic_prefix": prefix,
        "staging_directory": str(tmp_path / "staging"),
        "persistence_directory": str(tmp_path / "persisted"),
        "managers": {"_default_": {"type": "queued_python", "num_concurrent_jobs": 1}},
    }
    app = PulsarApp(**conf)
    yield {"prefix": prefix, "staging": tmp_path / "staging"}
    app.shutdown(timeout=5)


def config_for(relay, pulsar, tmp_path):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps(relay["secrets"]))
    params = {
        "relay_port": relay["port"],
        "relay_topic_prefix": pulsar["prefix"],
        "relay_pulsar_root": str(pulsar["staging"].parent),
    }
    params_yml = tmp_path / "params.yml"
    params_yml.write_text(yaml.safe_dump(params))
    # Port 0: the job files API takes a free port.
    galaxy_config_json = galaxy_config.galaxy_job_config(json.loads(secrets.read_text()), params, "http://127.0.0.1:0")
    assert galaxy_config_json["destination"]["jobs_directory"] == str(pulsar["staging"])
    return galaxy_config_json


def test_a_job_runs_through_the_relay_and_its_outputs_come_back_over_http(relay, pulsar, tmp_path, monkeypatch):
    monkeypatch.setattr(galaxy_job, "RUNNING_SECONDS", 3)
    result = galaxy_job.run_job(config_for(relay, pulsar, tmp_path), timeout=90, root=tmp_path, bind="127.0.0.1")

    assert (result["status"], result["returncode"]) == ("complete", 0), result
    assert galaxy_job.succeeded(result)
    # Status updates came through the relay before the final one.
    assert result["states"][-1] == "complete"
    assert "running" in result["states"]
    # Pulsar fetched the input over HTTP and ran the job in its staging directory.
    assert result["inputs_served"] >= 1
    assert result["report"]["cwd"].startswith(str(pulsar["staging"]))
    assert result["received"]["output"] == "galaxy\npulsar\nrelay\n"


def test_main_reports_a_failed_job(relay, pulsar, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(galaxy_job, "COMMAND", "exit 3; {seconds} {input} {output} {report}")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_for(relay, pulsar, tmp_path)))

    assert galaxy_job.main([str(path), "--timeout", "90", "--bind", "127.0.0.1"]) == 1

    result = json.loads(capsys.readouterr().out)
    assert (result["status"], result["returncode"]) == ("complete", 3)
    assert result["received"]["report"] is None
    assert result["report"] == {}


def test_a_job_without_a_final_status_times_out(relay, pulsar, tmp_path):
    config = config_for(relay, pulsar, tmp_path)
    # No Pulsar consumes this prefix's topics.
    config["runner"]["relay_topic_prefix"] = "nobody"

    result = galaxy_job.run_job(config, timeout=2, root=tmp_path, bind="127.0.0.1")

    assert result["status"] == "timeout"
    assert not galaxy_job.succeeded(result)


def test_a_wrong_pair_password_gets_no_job_through(relay, tmp_path):
    secrets = {"PULSAR_PAIR_PASSWORD": "not-the-pair-password-1234"}
    config = galaxy_config.galaxy_job_config(secrets, {"relay_port": relay["port"]}, "http://127.0.0.1:0")
    config["destination"]["jobs_directory"] = str(tmp_path)

    with pytest.raises(Exception, match=r"(?i)401|auth|credential|login"):
        galaxy_job.run_job(config, timeout=2, root=tmp_path, bind="127.0.0.1")


def test_status_updates_of_other_jobs_are_ignored():
    updates = galaxy_job.StatusUpdates("mine")

    updates({"job_id": "other", "status": "complete"})
    updates({"job_id": "mine", "status": "running"})

    assert updates.states == ["running"]
    assert not updates.done.is_set()


@pytest.fixture
def job_files(tmp_path):
    (tmp_path / "job").mkdir()
    (tmp_path / "job" / "in.dat").write_text("data")
    files = galaxy_job.JobFiles(tmp_path, "key1", job_dir=tmp_path / "job" / "dir", outputs=[tmp_path / "out.dat"])
    server = galaxy_job.serve_job_files(files, ("127.0.0.1", 0))
    yield files, server.server_port, tmp_path
    server.shutdown()
    server.server_close()


def request(port, method, target, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


API = "/api/jobs/1/files"


@pytest.mark.parametrize(
    ("query", "code"),
    [
        pytest.param("job_key=wrong&path={job}/in.dat", 403, id="wrong-key"),
        pytest.param("path={job}/in.dat", 403, id="no-key"),
        pytest.param("job_key=key1&path={outside}", 403, id="outside-the-job"),
        pytest.param("job_key=key1&path={job}/../job/../../secret", 403, id="dot-dot"),
        pytest.param("job_key=key1&path={job}/missing", 404, id="missing"),
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_job_files_refuses(job_files, query, code, method):
    _, port, root = job_files
    query = query.format(job=root / "job", outside=root.parent / "secret")

    assert request(port, method, f"{API}?{query}")[0] == code


def test_job_files_serves_files_of_the_job(job_files):
    files, port, root = job_files
    target = f"{API}?job_key=key1&path={root / 'job' / 'in.dat'}"

    status, headers, _ = request(port, "HEAD", target)
    assert (status, headers["Content-Length"]) == (200, "4")
    assert request(port, "GET", target)[::2] == (200, b"data")
    assert files.served == [str(root / "job" / "in.dat")]


def multipart(*fields):
    boundary = "e2e-boundary"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="x"\r\n\r\n{value}\r\n'
        for name, value in fields
    ]
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


@pytest.mark.parametrize("path", ["out.dat", "job/dir/metadata/x"], ids=["output", "job-dir"])
def test_job_files_accepts_outputs_and_job_directory_files(job_files, path):
    files, port, root = job_files
    body, headers = multipart(("file", "result"))

    status = request(port, "POST", f"{API}?job_key=key1&path={root / path}", body, headers)[0]

    assert status == 200
    assert (root / path).read_text() == "result"
    assert files.received == {str(root / path): "result"}


@pytest.mark.parametrize(
    "target",
    ["/other", f"{API}?job_key=wrong&path=/x", f"{API}?job_key=key1&path={{root}}/job/in.dat"],
    ids=["not-the-api", "wrong-key", "an-input"],
)
def test_job_files_refuses_uploads(job_files, target):
    _, port, root = job_files
    body, headers = multipart(("file", "overwritten"))

    assert request(port, "POST", target.format(root=root), body, headers)[0] == 403
    assert (root / "job" / "in.dat").read_text() == "data"


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        pytest.param(b"plain", {"Content-Type": "text/plain"}, id="not-multipart"),
        pytest.param(*multipart(("other", "x")), id="no-file-field"),
        pytest.param(*multipart(("file", "a"), ("file", "b")), id="two-files"),
    ],
)
def test_job_files_rejects_bad_uploads(job_files, body, headers):
    _, port, root = job_files
    target = f"{API}?job_key=key1&path={root / 'out.dat'}"

    assert request(port, "POST", target, body, headers)[0] == 400
    assert not (root / "out.dat").exists()


def test_job_files_rejects_an_upload_without_length(job_files):
    _, port, root = job_files
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest("POST", f"{API}?job_key=key1&path={root / 'out.dat'}")
        connection.endheaders()
        assert connection.getresponse().status == 400
    finally:
        connection.close()
