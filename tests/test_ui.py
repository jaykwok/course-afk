import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.state import credential_view
from core.ui.frontend import use_frontend, wait_with_progress


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    def test_credential_remaining_days_round_up_and_expiration_date_expires(self):
        state = SimpleNamespace(has_credential=True, credential_expired=False)
        metadata = SimpleNamespace(expires_at="2026-05-20T14:34:28")
        clock = SimpleNamespace(
            fromisoformat=datetime.fromisoformat,
            now=lambda **kwargs: datetime(2026, 5, 18, 14, 35),
        )
        self.assertEqual(credential_view(state, metadata, clock=clock).days_left, 2)
        clock.now = lambda **kwargs: datetime(2026, 5, 20, 8)
        self.assertEqual(
            credential_view(state, metadata, clock=clock).status, "expired"
        )

    async def test_progress_follows_context_and_restores_after_operation(self):
        frontend = SimpleNamespace(wait_with_progress=AsyncMock())
        with use_frontend(frontend):
            await asyncio.create_task(wait_with_progress(2, "章节"))
        frontend.wait_with_progress.assert_awaited_once_with(2, description="章节")
        with patch("core.ui.frontend.asyncio.sleep", new=AsyncMock()) as sleep:
            await wait_with_progress(1)
        sleep.assert_awaited_once_with(1)

    async def test_progress_cancellation_propagates(self):
        frontend = SimpleNamespace(
            wait_with_progress=AsyncMock(side_effect=asyncio.CancelledError)
        )
        with use_frontend(frontend), self.assertRaises(asyncio.CancelledError):
            await wait_with_progress(2)
