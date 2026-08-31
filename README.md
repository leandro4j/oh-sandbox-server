# OpenHands Sandbox Server

This repository contains the standalone OpenHands API and sandbox control plane.
It is the history-preserving extraction of `openhands/app_server/` from the
former `OpenHands/OpenHands` monorepo.

The repository includes:

- the FastAPI application in `openhands/app_server/`;
- the small local `openhands/analytics/` and `openhands/db/` support packages;
- bundled skills and the focused app-server test suite;
- pinned Python packaging, Docker, and Compose configuration.

The frontend is intentionally not bundled. Use
[OpenHands Agent Canvas](https://github.com/OpenHands/agent-canvas) as the web
client.

## Local development

Requirements:

- Python 3.12 or 3.13
- Poetry 2.3.4 or newer
- Docker when using Docker-backed sandboxes

```bash
make install
make start
```

The server listens on `http://localhost:3000` by default.

Run validation with:

```bash
make lint
make test
```

## Docker

Build the frozen full Agent Server image from the SDK checkout first, then set
the control-plane key before starting Compose:

```bash
export AGENT_BOX_MVP_PROJECT=agent-box-mvp-run-id
export AGENT_BOX_CONTROL_PLANE_KEY="set-this-outside-the-repository"
docker compose up --build
```

Compose derives the local image tag as
`agent-box/${AGENT_BOX_MVP_PROJECT}-software-agent-sdk:local`, allows the Agent
Canvas origin `http://localhost:3001`, publishes the control plane on port
`3000`, and keeps two sandboxes available. Set `SANDBOX_SERVER_PORT` when the
published port differs. Sandbox containers use Docker's ephemeral writable
layer; no host workspace is mounted into them.

## Extraction provenance

The extraction is based on:

- source repository: `OpenHands/OpenHands`
- source commit: `ee9e78b7defdfa744e0bbe48c9cafa90b6135ad7`
- permanent monorepo snapshot: [OpenHands/legacy](https://github.com/OpenHands/legacy)
- migration tracker: [OpenHands/OpenHands#15396](https://github.com/OpenHands/OpenHands/issues/15396)

History was filtered to the server and its required repository-local support
files. The dependencies on `openhands-agent-server`, `openhands-sdk`, and
`openhands-tools` remain explicit pinned package dependencies.
