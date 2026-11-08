FROM python:3.12-slim-bookworm

ARG RELAY_REF=048689a2b55192fda43fb4422ba99630051f2e09
RUN pip install --no-cache-dir "https://github.com/galaxyproject/pulsar-relay/archive/${RELAY_REF}.tar.gz"
RUN useradd --uid 10001 --create-home relay
COPY relay_src /opt/relay-src/relay_src
ENV PYTHONPATH=/opt/relay-src
USER relay
WORKDIR /home/relay
EXPOSE 8080
# Trust X-Forwarded-For only from Docker's default bridge pools: the port is
# published on 127.0.0.1, so the only HTTP peer is the bridge gateway (nginx
# and local clients via docker-proxy). uvicorn then takes the rightmost
# untrusted entry, i.e. the address nginx appended, not a client-chosen one.
# Read by uvicorn when --forwarded-allow-ips is not given.
ENV FORWARDED_ALLOW_IPS=172.16.0.0/12,192.168.0.0/16
CMD ["uvicorn", "relay_src.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
