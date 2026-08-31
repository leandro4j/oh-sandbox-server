"""Real Docker proof for two independent Sandbox Server runtimes.

This is deliberately an explicit local lane. The Product Integration command
builds the frozen full Agent Server image and supplies its tag through
``SANDBOX_INTEGRATION_IMAGE``. The test mounts the public Sandbox Server router
over a DockerSandboxService and uses Docker's runtime observations only for
namespace and resource assertions that mocks cannot prove.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from contextlib import suppress
from dataclasses import dataclass
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    OpenerDirector,
    build_opener,
    urlopen,
)
from uuid import uuid4

import docker  # type: ignore[import-untyped]
import httpx
import pytest
from docker.errors import (  # type: ignore[import-untyped]
    DockerException,
    ImageNotFound,
    NotFound,
)
from fastapi import FastAPI
from playwright.async_api import async_playwright, expect

from openhands.app_server.sandbox import sandbox_router as sandbox_router_module
from openhands.app_server.sandbox.docker_sandbox_service import (
    DockerSandboxService,
    ExposedPort,
)
from openhands.app_server.sandbox.preset_sandbox_spec_service import (
    PresetSandboxSpecService,
)
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    VSCODE,
    SandboxInfo,
    SandboxPage,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_spec_models import SandboxSpecInfo
from openhands.app_server.utils.dependencies import check_session_api_key

pytestmark = pytest.mark.integration

RUN_INTEGRATION_ENV = 'RUN_DOCKER_INTEGRATION_TESTS'
IMAGE_ENV = 'SANDBOX_INTEGRATION_IMAGE'
MARKER_FILENAME = 'openhands-editor-marker.txt'
MARKER_PATH = f'/workspace/project/{MARKER_FILENAME}'
APPLICATION_PORT = 8080
DATABASE_PORT = 5432

if os.getenv(RUN_INTEGRATION_ENV, '').lower() not in ('1', 'true', 'yes'):
    pytest.skip(
        f'set {RUN_INTEGRATION_ENV}=true to run the Docker integration lane',
        allow_module_level=True,
    )

INTEGRATION_IMAGE = os.getenv(IMAGE_ENV) or ''
if not INTEGRATION_IMAGE:
    pytest.fail(f'{IMAGE_ENV} must name the locally built full Agent Server image')


PROBE_SCRIPT = f"""
import http.server
import os
from pathlib import Path
import socket
import threading
import time

marker_path = Path({MARKER_PATH!r})
marker = os.environ["OH_ISSUE_2_MARKER"]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = marker_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


http_server = http.server.ThreadingHTTPServer(("0.0.0.0", {APPLICATION_PORT}), Handler)
database_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
database_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
database_socket.bind(("0.0.0.0", {DATABASE_PORT}))
database_socket.listen()
marker_path.parent.mkdir(parents=True, exist_ok=True)
marker_path.write_text(marker, encoding="utf-8")
threading.Thread(target=http_server.serve_forever, daemon=True).start()

while True:
    connection, _ = database_socket.accept()
    with connection:
        connection.sendall(marker.encode("utf-8"))
"""


@dataclass(frozen=True)
class RuntimeSnapshot:
    sandbox_id: str
    session_api_key: str
    agent_server_url: str
    vscode_url: str
    application_url: str
    database_url: str
    marker: str
    pid_namespace: str
    network_namespace: str


def _build_sandbox_service(
    image: str,
    prefix: str,
    httpx_client: httpx.AsyncClient,
    docker_client: docker.DockerClient,
) -> DockerSandboxService:
    spec = SandboxSpecInfo(
        id=image,
        command=None,
        initial_env={'PYTHONUNBUFFERED': '1'},
        working_dir='/workspace/project',
    )
    return DockerSandboxService(
        sandbox_spec_service=PresetSandboxSpecService(specs=[spec]),
        container_name_prefix=prefix,
        host_port=3000,
        container_url_pattern='http://127.0.0.1:{port}',
        mounts=[],
        exposed_ports=[
            ExposedPort(
                name=AGENT_SERVER,
                description='Agent Server runtime',
                container_port=8000,
            ),
            ExposedPort(
                name=VSCODE,
                description='VS Code editor',
                container_port=8001,
            ),
            ExposedPort(
                name='APPLICATION',
                description='Issue-2 application probe',
                container_port=APPLICATION_PORT,
            ),
            ExposedPort(
                name='DATABASE',
                description='Issue-2 database probe',
                container_port=DATABASE_PORT,
            ),
        ],
        health_check_path=None,
        httpx_client=httpx_client,
        max_num_sandboxes=2,
        extra_hosts={},
        docker_client=docker_client,
    )


def _exposed_url(info: SandboxInfo, name: str) -> str:
    assert info.exposed_urls is not None
    for exposed_url in info.exposed_urls:
        if exposed_url.name == name:
            return exposed_url.url
    raise AssertionError(f'{name} URL missing from sandbox {info.id}')


def _exec_checked(container, command: list[str]) -> str:
    result = container.exec_run(command)
    output = result.output.decode('utf-8', errors='replace').strip()
    assert result.exit_code == 0, f'{command!r} failed: {output}'
    return output


def _start_probe(container, marker: str) -> None:
    result = container.exec_run(
        ['python3', '-c', PROBE_SCRIPT],
        environment={'OH_ISSUE_2_MARKER': marker},
        detach=True,
    )
    assert result is not None


def _http_marker(url: str) -> str:
    with urlopen(url, timeout=5) as response:
        assert response.status == 200
        return response.read().decode('utf-8')


def _http_status(url: str, opener: OpenerDirector | None = None) -> int:
    if opener is None:
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        with opener.open(url, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code


def _editor_url(info: SandboxInfo, opener: OpenerDirector | None = None) -> str:
    url = _exposed_url(info, VSCODE)
    parsed = urlsplit(url)
    assert parsed.path == f'/vscode/{info.id}/'
    assert f'tkn={info.session_api_key}' in parsed.query
    assert _http_status(url, opener) == 200
    return url


def _url_is_unreachable(url: str) -> bool:
    try:
        with urlopen(url, timeout=5):
            return False
    except (HTTPError, OSError, TimeoutError, URLError):
        return True


async def _assert_editor_workspace(
    page, url: str, expected_marker: str, reload: bool = False
) -> None:
    if reload:
        await page.reload(wait_until='domcontentloaded', timeout=30000)
    else:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    marker_item = page.locator(f'[role="treeitem"][aria-label="{MARKER_FILENAME}"]')
    await marker_item.wait_for(state='visible', timeout=30000)
    await marker_item.dblclick()
    await expect(page.locator('.view-lines')).to_contain_text(
        expected_marker, timeout=30000
    )


async def _assert_editor_workspaces(
    first_url: str,
    first_marker: str,
    second_url: str,
    second_marker: str,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        try:
            first_page = await context.new_page()
            second_page = await context.new_page()
            await asyncio.gather(
                _assert_editor_workspace(first_page, first_url, first_marker),
                _assert_editor_workspace(second_page, second_url, second_marker),
            )
            await asyncio.gather(
                _assert_editor_workspace(
                    first_page, first_url, first_marker, reload=True
                ),
                _assert_editor_workspace(
                    second_page, second_url, second_marker, reload=True
                ),
            )
        finally:
            await context.close()
            await browser.close()


def _database_marker(url: str) -> str:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as conn:
        return conn.recv(4096).decode('utf-8')


def _wait_for_marker(container, expected: str) -> None:
    deadline = time.monotonic() + 30
    last_output = ''
    while time.monotonic() < deadline:
        try:
            last_output = _exec_checked(container, ['cat', MARKER_PATH])
        except AssertionError:
            pass
        if last_output == expected:
            return
        time.sleep(0.25)
    raise AssertionError(f'marker did not become ready: {last_output!r}')


def _build_sandbox_api(service: DockerSandboxService) -> FastAPI:
    """Mount the real sandbox router with a Docker service dependency."""
    app = FastAPI()
    app.dependency_overrides[check_session_api_key] = lambda: None
    app.dependency_overrides[
        sandbox_router_module.sandbox_service_dependency.dependency
    ] = lambda: service
    app.dependency_overrides[
        sandbox_router_module.user_context_dependency.dependency
    ] = lambda: None
    app.include_router(sandbox_router_module.router, prefix='/api/v1')
    return app


async def _api_start_sandbox(client: httpx.AsyncClient) -> SandboxInfo:
    response = await client.post('/sandboxes')
    response.raise_for_status()
    return SandboxInfo.model_validate(response.json())


async def _api_get_sandbox(
    client: httpx.AsyncClient, sandbox_id: str
) -> SandboxInfo | None:
    response = await client.get('/sandboxes', params={'id': sandbox_id})
    response.raise_for_status()
    payload = response.json()
    assert len(payload) == 1
    return SandboxInfo.model_validate(payload[0]) if payload[0] else None


async def _wait_for_api_running(
    client: httpx.AsyncClient, sandbox_id: str
) -> SandboxInfo:
    deadline = time.monotonic() + 30
    last_info: SandboxInfo | None = None
    while time.monotonic() < deadline:
        last_info = await _api_get_sandbox(client, sandbox_id)
        if last_info and last_info.status == SandboxStatus.RUNNING:
            return last_info
        await asyncio.sleep(0.25)
    raise AssertionError(f'sandbox did not become running: {last_info!r}')


async def _api_search_sandboxes(client: httpx.AsyncClient) -> SandboxPage:
    response = await client.get('/sandboxes/search')
    response.raise_for_status()
    return SandboxPage.model_validate(response.json())


async def _api_action(client: httpx.AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, path)
    response.raise_for_status()


def _owned_containers(client: docker.DockerClient, prefix: str):
    return [
        container
        for container in client.containers.list(all=True)
        if container.name and container.name.startswith(prefix)
    ]


def _snapshot(container, info: SandboxInfo, marker: str) -> RuntimeSnapshot:
    _wait_for_marker(container, marker)
    agent_server_url = _exposed_url(info, AGENT_SERVER)
    vscode_url = _editor_url(info)
    application_url = _exposed_url(info, 'APPLICATION')
    database_url = _exposed_url(info, 'DATABASE')
    assert _http_marker(application_url) == marker
    assert _database_marker(database_url) == marker
    return RuntimeSnapshot(
        sandbox_id=info.id,
        session_api_key=info.session_api_key or '',
        agent_server_url=agent_server_url,
        vscode_url=vscode_url,
        application_url=application_url,
        database_url=database_url,
        marker=marker,
        pid_namespace=_exec_checked(
            container,
            ['python3', '-c', 'import os; print(os.readlink("/proc/1/ns/pid"))'],
        ),
        network_namespace=_exec_checked(
            container,
            ['python3', '-c', 'import os; print(os.readlink("/proc/1/ns/net"))'],
        ),
    )


@pytest.fixture(scope='module')
def docker_client() -> docker.DockerClient:
    client = docker.from_env()
    try:
        client.ping()
        client.images.get(INTEGRATION_IMAGE)
    except ImageNotFound:
        client.close()
        pytest.fail(f'{IMAGE_ENV} is not available locally: {INTEGRATION_IMAGE}')
    except DockerException as exc:
        client.close()
        pytest.fail(f'Docker daemon/image unavailable: {exc}')
    yield client
    client.close()


@pytest.mark.asyncio
async def test_two_conversations_are_isolated_across_lifecycle_and_restart(
    docker_client: docker.DockerClient,
):
    """Prove two-runtime routing; path scoping fixes cookie collisions, not hostile-service security."""
    run_id = uuid4().hex[:12]
    prefix = f'oh-issue-2-{run_id}-'
    conversation_markers = {
        'conversation-a': f'conversation-a-{run_id}',
        'conversation-b': f'conversation-b-{run_id}',
    }
    started_ids: list[str] = []
    unrelated = None
    httpx_client = httpx.AsyncClient()
    restarted_httpx_client = httpx.AsyncClient()
    service = _build_sandbox_service(
        INTEGRATION_IMAGE, prefix, httpx_client, docker_client
    )
    restarted_service = _build_sandbox_service(
        INTEGRATION_IMAGE, prefix, restarted_httpx_client, docker_client
    )
    control_app = _build_sandbox_api(service)
    control_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app),
        base_url='http://sandbox.test/api/v1',
    )
    restarted_control_client: httpx.AsyncClient | None = None

    try:
        unrelated = docker_client.containers.run(
            INTEGRATION_IMAGE,
            name=f'issue-2-unrelated-{run_id}',
            command=['-c', 'import time; time.sleep(600)'],
            entrypoint=['python3'],
            detach=True,
            remove=False,
        )

        # The Compose profile supplies this explicit no-grouping contract. The
        # service receives one fresh sandbox request per conversation below.
        assert os.getenv('OH_SANDBOX_NO_GROUPING', '').lower() in (
            '1',
            'true',
            'yes',
        )

        first = await _api_start_sandbox(control_client)
        started_ids.append(first.id)
        second = await _api_start_sandbox(control_client)
        started_ids.append(second.id)

        owned = _owned_containers(docker_client, prefix)
        assert {container.name for container in owned} == {first.id, second.id}
        assert len(owned) == 2

        first_info = await _wait_for_api_running(control_client, first.id)
        second_info = await _wait_for_api_running(control_client, second.id)
        assert first_info.id != second_info.id
        assert first_info.session_api_key
        assert second_info.session_api_key
        assert first_info.session_api_key != second_info.session_api_key
        assert _exposed_url(first_info, AGENT_SERVER) != _exposed_url(
            second_info, AGENT_SERVER
        )
        browser_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        first_editor_url = _editor_url(first_info, browser_opener)
        second_editor_url = _editor_url(second_info, browser_opener)
        assert first_editor_url != second_editor_url
        assert first_info.session_api_key not in second_editor_url
        assert second_info.session_api_key not in first_editor_url
        # Reuse the same cookie jar to model one browser alternating between
        # both editors; path-scoped cookies must keep each workspace routed.
        assert _http_status(first_editor_url, browser_opener) == 200
        assert _http_status(second_editor_url, browser_opener) == 200
        wrong_token_url = first_editor_url.replace(
            f'tkn={first_info.session_api_key}', 'tkn=invalid-editor-token'
        )
        assert _http_status(wrong_token_url) in (401, 403)

        first_container = docker_client.containers.get(first.id)
        second_container = docker_client.containers.get(second.id)
        _start_probe(first_container, conversation_markers['conversation-a'])
        _start_probe(second_container, conversation_markers['conversation-b'])
        first_snapshot = _snapshot(
            first_container, first_info, conversation_markers['conversation-a']
        )
        second_snapshot = _snapshot(
            second_container, second_info, conversation_markers['conversation-b']
        )
        await _assert_editor_workspaces(
            first_snapshot.vscode_url,
            first_snapshot.marker,
            second_snapshot.vscode_url,
            second_snapshot.marker,
        )

        # Both containers bind the same application and database ports inside
        # their own namespaces, while their marker files remain distinct.
        assert first_snapshot.application_url != second_snapshot.application_url
        assert first_snapshot.database_url != second_snapshot.database_url
        assert first_snapshot.marker != second_snapshot.marker
        assert first_snapshot.pid_namespace != second_snapshot.pid_namespace
        assert first_snapshot.network_namespace != second_snapshot.network_namespace

        # Pausing one runtime must not interrupt the other runtime or mutate its
        # marker, URLs, key, or namespaces.
        await _api_action(control_client, 'POST', f'/sandboxes/{first.id}/pause')
        paused = await _api_get_sandbox(control_client, first.id)
        assert paused is not None
        assert paused.status == SandboxStatus.PAUSED
        unaffected = await _wait_for_api_running(control_client, second.id)
        assert _snapshot(second_container, unaffected, second_snapshot.marker) == (
            second_snapshot
        )

        # A fresh app and service instance represent Sandbox Server restart. It
        # must rediscover both Docker containers, resume the paused one, and
        # retain its session identity and writable marker.
        restarted_control_app = _build_sandbox_api(restarted_service)
        restarted_control_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_control_app),
            base_url='http://sandbox.test/api/v1',
        )
        assert restarted_control_client is not None
        discovered = await _api_search_sandboxes(restarted_control_client)
        discovered_by_id = {info.id: info for info in discovered.items}
        assert set(discovered_by_id) == {first.id, second.id}
        assert discovered_by_id[first.id].status == SandboxStatus.PAUSED
        assert discovered_by_id[second.id].status == SandboxStatus.RUNNING
        await _api_action(
            restarted_control_client, 'POST', f'/sandboxes/{first.id}/resume'
        )
        resumed_first = await _wait_for_api_running(restarted_control_client, first.id)
        assert resumed_first.session_api_key == first_snapshot.session_api_key
        assert _editor_url(resumed_first) == first_snapshot.vscode_url
        assert _exec_checked(first_container, ['cat', MARKER_PATH]) == (
            first_snapshot.marker
        )

        # Simulate an externally stopped runtime, then verify restart discovery
        # still exposes the same resume contract.
        first_container.stop(timeout=10)
        stopped = await _api_get_sandbox(restarted_control_client, first.id)
        assert stopped is not None
        assert stopped.status == SandboxStatus.PAUSED

        # The second runtime remains reachable while the first is stopped.
        assert (
            _snapshot(
                second_container,
                await _wait_for_api_running(restarted_control_client, second.id),
                second_snapshot.marker,
            )
            == second_snapshot
        )

        await _api_action(
            restarted_control_client, 'POST', f'/sandboxes/{first.id}/resume'
        )
        resumed_after_stop = await _wait_for_api_running(
            restarted_control_client, first.id
        )
        assert resumed_after_stop.session_api_key == first_snapshot.session_api_key
        assert _exec_checked(first_container, ['cat', MARKER_PATH]) == (
            first_snapshot.marker
        )

        await _api_action(restarted_control_client, 'DELETE', f'/sandboxes/{first.id}')
        await _api_action(restarted_control_client, 'DELETE', f'/sandboxes/{second.id}')
        assert _url_is_unreachable(first_snapshot.vscode_url)
        assert _url_is_unreachable(second_snapshot.vscode_url)
        assert _owned_containers(docker_client, prefix) == []
        assert docker_client.containers.get(unrelated.name).status == 'running'
        assert all(
            docker_client.volumes.list(
                filters={'name': f'openhands-workspace-{sandbox_id}'}
            )
            == []
            for sandbox_id in started_ids
        )
    finally:
        for sandbox_id in started_ids:
            with suppress(Exception):
                await restarted_service.delete_sandbox(sandbox_id)
        if unrelated is not None:
            with suppress(NotFound, DockerException):
                unrelated.remove(force=True)
        await control_client.aclose()
        if restarted_control_client is not None:
            await restarted_control_client.aclose()
        await httpx_client.aclose()
        await restarted_httpx_client.aclose()
