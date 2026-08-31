"""Contract tests for the local frozen Agent Server Compose profile."""

from pathlib import Path

import yaml

COMPOSE_FILE = Path(__file__).parents[3] / 'docker-compose.yml'


def _compose_environment() -> dict[str, str]:
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    entries = compose['services']['sandbox-server']['environment']
    return dict(entry.split('=', 1) for entry in entries if '=' in entry)


def test_compose_requires_the_frozen_runtime_and_control_plane_key():
    environment = _compose_environment()

    assert environment['AGENT_SERVER_IMAGE_REPOSITORY'].endswith('-software-agent-sdk}')
    assert environment['AGENT_SERVER_IMAGE_TAG'] == '${AGENT_SERVER_IMAGE_TAG:-local}'
    assert environment['SESSION_API_KEY'].startswith('${AGENT_BOX_CONTROL_PLANE_KEY:?')
    assert environment['OH_PERMITTED_CORS_ORIGINS_0'] == 'http://localhost:3001'
    assert environment['OH_SANDBOX_NO_GROUPING'] == 'true'
    assert environment['SANDBOX_MAX_NUM_SANDBOXES'] == (
        '${SANDBOX_MAX_NUM_SANDBOXES:-2}'
    )


def test_compose_publishes_browser_urls_without_a_shared_workspace_mount():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    service = compose['services']['sandbox-server']
    environment = _compose_environment()

    assert environment['SANDBOX_HOST_PORT'] == '${SANDBOX_SERVER_PORT:-3000}'
    assert environment['SANDBOX_CONTAINER_URL_PATTERN'] == (
        '${SANDBOX_CONTAINER_URL_PATTERN:-http://localhost:{port}}'
    )
    assert service['ports'] == ['${SANDBOX_SERVER_PORT:-3000}:3000']
    assert all('/opt/workspace_base' not in volume for volume in service['volumes'])
