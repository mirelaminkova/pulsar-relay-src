FROM python:3.12-slim-bookworm

ARG RELAY_REF=048689a2b55192fda43fb4422ba99630051f2e09
# Upstream declares only lower bounds, so pin the whole dependency set: a
# rebuild must not pull a newer FastAPI/Starlette/uvicorn under relay_src.
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt \
        "https://github.com/galaxyproject/pulsar-relay/archive/${RELAY_REF}.tar.gz" \
    && pip check && rm /tmp/constraints.txt
RUN useradd --uid 10001 --create-home relay
COPY relay_src /opt/relay-src/relay_src
ENV PYTHONPATH=/opt/relay-src
USER relay
WORKDIR /home/relay
EXPOSE 8080
CMD ["uvicorn", "relay_src.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
