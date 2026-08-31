"""HTTP contract tests for the sandbox lifecycle routes."""

import asyncio
from unittest.mock import AsyncMock

from openhands.app_server.sandbox.sandbox_router import (
    delete_sandbox,
)
from openhands.app_server.sandbox.sandbox_router import (
    router as sandbox_router,
)


def test_delete_sandbox_accepts_the_sandbox_id_path_parameter():
    """The raw delete route must bind the identifier used by SDK clients."""
    sandbox_service = AsyncMock()
    sandbox_service.delete_sandbox.return_value = True

    delete_routes = [
        route
        for route in sandbox_router.routes
        if 'DELETE' in getattr(route, 'methods', set())
    ]
    assert len(delete_routes) == 1
    assert delete_routes[0].path == '/sandboxes/{sandbox_id}'

    response = asyncio.run(delete_sandbox('oh-test-123', sandbox_service))

    assert response is not None
    sandbox_service.delete_sandbox.assert_awaited_once_with('oh-test-123')
