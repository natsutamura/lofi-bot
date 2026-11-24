#!/usr/bin/env python3
"""Regression test for the rejoin feedback loop.

The bug: `_hard_rejoin()` disconnects, our own voice state goes to channel=None, and
`on_voice_state_update` reacted to that by setting `_rejoin` — the exact event that asks
the supervisor to rejoin again. Result: a teardown/rejoin cycle every ~15 s, forever,
each one killing the stream.

Run: .venv/bin/python -m unittest tools/test_rejoin_loop.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("VOICE_CHANNEL_ID", "2")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as lofibot  # noqa: E402

BOT_ID = 702922423917346886


class FakeUser:
    id = BOT_ID


class FakeMember:
    def __init__(self, member_id: int) -> None:
        self.id = member_id


class FakeState:
    def __init__(self, channel):
        self.channel = channel


def make_bot() -> lofibot.LofiBot:
    """Bare instance: bypass Client.__init__, keep only what the methods touch."""
    b = lofibot.LofiBot.__new__(lofibot.LofiBot)
    b._rejoin = asyncio.Event()
    b._tearing_down = False
    b._rejoins = []
    b._last_rejoin_try = 0.0
    b.get_guild = lambda _gid: None
    return b


class RejoinLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # `Client.user` is a read-only property backed by the live connection.
        patcher = mock.patch.object(
            lofibot.LofiBot, "user", new_callable=mock.PropertyMock
        )
        self.user_mock = patcher.start()
        self.user_mock.return_value = FakeUser()
        self.addCleanup(patcher.stop)

    async def test_real_kick_sets_rejoin(self) -> None:
        bot = make_bot()
        await bot.on_voice_state_update(
            FakeMember(BOT_ID), FakeState("☕ Chill Area"), FakeState(None)
        )
        self.assertTrue(bot._rejoin.is_set(), "a genuine kick must request a rejoin")

    async def test_our_own_teardown_is_not_a_kick(self) -> None:
        bot = make_bot()
        bot._tearing_down = True
        await bot.on_voice_state_update(
            FakeMember(BOT_ID), FakeState("☕ Chill Area"), FakeState(None)
        )
        self.assertFalse(bot._rejoin.is_set(), "our own disconnect must not request a rejoin")

    async def test_other_members_voice_updates_are_ignored(self) -> None:
        bot = make_bot()
        await bot.on_voice_state_update(
            FakeMember(999), FakeState("☕ Chill Area"), FakeState(None)
        )
        self.assertFalse(bot._rejoin.is_set())

    async def test_teardown_clears_the_event_its_disconnect_created(self) -> None:
        """The core regression: the disconnect fires the handler mid-teardown."""
        bot = make_bot()

        async def fake_sleep(_seconds: float) -> None:
            # Discord delivers our own voice state update while we are tearing down.
            await bot.on_voice_state_update(
                FakeMember(BOT_ID), FakeState("☕ Chill Area"), FakeState(None)
            )

        with mock.patch.object(lofibot.asyncio, "sleep", fake_sleep):
            await bot._hard_rejoin()

        self.assertFalse(
            bot._rejoin.is_set(),
            "after a teardown the rejoin event must be clear, or the supervisor loops",
        )
        self.assertFalse(bot._tearing_down)

    async def test_rejoin_storm_backs_off(self) -> None:
        bot = make_bot()
        bot._rejoins = [lofibot.time.time()] * 5  # five rejoins already this window
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with mock.patch.object(lofibot.asyncio, "sleep", fake_sleep):
            await bot._hard_rejoin()

        self.assertIn(60, sleeps, "a 6th rejoin within 5 min must trigger a backoff")
        self.assertEqual(bot._rejoins, [], "backoff resets the storm counter")


if __name__ == "__main__":
    unittest.main()
