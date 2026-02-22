"""Tests for the collaboration core (Phase 3 / 6)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.collaboration import (
    CollaborationHub,
    CollaborativeSession,
    Participant,
    Permission,
    VotingApproval,
)
from src.core.types import Session


# =============================================================
# Participant Tests
# =============================================================

class TestParticipant:
    def test_defaults(self):
        p = Participant(user_id="u1", display_name="Alice")
        assert p.user_id == "u1"
        assert p.display_name == "Alice"
        assert p.adapter == "web"
        assert Permission.READ in p.permissions
        assert Permission.WRITE in p.permissions
        assert p.is_online is True
        assert p.is_typing is False

    def test_to_dict(self):
        p = Participant(user_id="u1", display_name="Alice")
        d = p.to_dict()
        assert d["user_id"] == "u1"
        assert d["display_name"] == "Alice"
        assert "read" in d["permissions"]
        assert "write" in d["permissions"]
        assert d["is_online"] is True

    def test_full_permissions(self):
        p = Participant(
            user_id="owner",
            display_name="Owner",
            permissions=set(Permission),
        )
        assert len(p.permissions) == 4
        for perm in Permission:
            assert perm in p.permissions


# =============================================================
# CollaborativeSession Tests
# =============================================================

class TestCollaborativeSession:
    def test_init(self):
        session = CollaborativeSession(name="Test", owner_user_id="u1")
        assert session.name == "Test"
        assert session.owner_user_id == "u1"
        assert len(session.participants) == 0
        assert len(session.invite_code) == 8

    def test_add_participant(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        p = Participant(user_id="u1", display_name="Alice")
        collab.add_participant(p)
        assert "u1" in collab.participants
        assert len(collab.history) == 1
        assert collab.history[0]["type"] == "join"

    def test_remove_participant(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        p = Participant(user_id="u1", display_name="Alice")
        collab.add_participant(p)
        collab.remove_participant("u1")
        assert "u1" not in collab.participants
        # History has join + leave
        assert len(collab.history) == 2
        assert collab.history[1]["type"] == "leave"

    def test_has_permission(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        p = Participant(
            user_id="u1",
            display_name="Alice",
            permissions={Permission.READ},
        )
        collab.add_participant(p)
        assert collab.has_permission("u1", Permission.READ) is True
        assert collab.has_permission("u1", Permission.WRITE) is False
        assert collab.has_permission("unknown", Permission.READ) is False

    def test_get_approvers(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        owner = Participant(
            user_id="u1",
            display_name="Owner",
            permissions=set(Permission),
        )
        reader = Participant(
            user_id="u2",
            display_name="Reader",
            permissions={Permission.READ},
        )
        collab.add_participant(owner)
        collab.add_participant(reader)
        approvers = collab.get_approvers()
        assert len(approvers) == 1
        assert approvers[0].user_id == "u1"

    def test_get_approvers_offline_excluded(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        p = Participant(
            user_id="u1",
            display_name="Owner",
            permissions=set(Permission),
            is_online=False,
        )
        collab.add_participant(p)
        assert len(collab.get_approvers()) == 0

    def test_set_typing(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        p = Participant(user_id="u1", display_name="Alice")
        collab.add_participant(p)
        collab.set_typing("u1", True)
        assert collab.participants["u1"].is_typing is True
        collab.set_typing("u1", False)
        assert collab.participants["u1"].is_typing is False

    def test_set_online(self):
        collab = CollaborativeSession(name="Test", owner_user_id="u1")
        p = Participant(user_id="u1", display_name="Alice")
        collab.add_participant(p)
        collab.set_online("u1", False)
        assert collab.participants["u1"].is_online is False

    def test_to_dict(self):
        collab = CollaborativeSession(name="MySession", owner_user_id="u1")
        p = Participant(user_id="u1", display_name="Alice")
        collab.add_participant(p)
        d = collab.to_dict()
        assert d["name"] == "MySession"
        assert "u1" in d["participants"]
        assert d["owner_user_id"] == "u1"
        assert "invite_code" in d


# =============================================================
# VotingApproval Tests
# =============================================================

class TestVotingApproval:
    def _make_session_with_approvers(self, count=2):
        collab = CollaborativeSession(name="Vote Test", owner_user_id="owner")
        for i in range(count):
            p = Participant(
                user_id=f"u{i}",
                display_name=f"User {i}",
                permissions=set(Permission),
                is_online=True,
            )
            collab.add_participant(p)
        return collab

    @pytest.mark.asyncio
    async def test_majority_approve(self):
        collab = self._make_session_with_approvers(3)
        voting = VotingApproval(collab)
        broadcast_calls = []

        async def broadcast(msg):
            broadcast_calls.append(msg)

        # Start vote in background
        vote_task = asyncio.create_task(
            voting.request_vote("aid1", "shell", {"cmd": "ls"}, "high", broadcast)
        )

        await asyncio.sleep(0.01)  # Let vote request broadcast
        assert len(broadcast_calls) == 1
        assert broadcast_calls[0]["type"] == "collab_approval_request"

        # 2 out of 3 approve → majority
        voting.cast_vote("aid1", "u0", True)
        voting.cast_vote("aid1", "u1", True)

        result = await vote_task
        assert result is True

    @pytest.mark.asyncio
    async def test_majority_deny(self):
        collab = self._make_session_with_approvers(3)
        voting = VotingApproval(collab)

        async def broadcast(msg):
            pass

        vote_task = asyncio.create_task(
            voting.request_vote("aid2", "delete", {"path": "/"}, "critical", broadcast)
        )

        await asyncio.sleep(0.01)

        # 2 out of 3 deny
        voting.cast_vote("aid2", "u0", False)
        voting.cast_vote("aid2", "u1", False)

        result = await vote_task
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout(self):
        collab = self._make_session_with_approvers(2)
        # Patch timeout to 0.1 seconds
        voting = VotingApproval(collab)
        broadcast_calls = []

        async def broadcast(msg):
            broadcast_calls.append(msg)

        # Patch asyncio.wait_for to time out quickly
        original_wait_for = asyncio.wait_for

        async def fast_timeout(coro, timeout):
            raise asyncio.TimeoutError()

        import unittest.mock as mock
        with mock.patch("asyncio.wait_for", side_effect=fast_timeout):
            result = await voting.request_vote("aid3", "shell", {}, "high", broadcast)
        assert result is False
        # Expired broadcast should have been sent
        expired = [m for m in broadcast_calls if m.get("type") == "collab_approval_expired"]
        assert len(expired) == 1

    def test_cast_vote_no_permission(self):
        collab = CollaborativeSession(name="Test", owner_user_id="owner")
        reader = Participant(
            user_id="reader",
            display_name="Reader",
            permissions={Permission.READ},  # No APPROVE_ACTIONS
        )
        collab.add_participant(reader)
        voting = VotingApproval(collab)
        voting._votes["aid4"] = {}
        import asyncio as _asyncio
        fut = _asyncio.get_event_loop().create_future()
        voting._futures["aid4"] = fut
        result = voting.cast_vote("aid4", "reader", True)
        assert result["status"] == "no_permission"

    def test_cast_vote_expired(self):
        collab = self._make_session_with_approvers(1)
        voting = VotingApproval(collab)
        result = voting.cast_vote("nonexistent", "u0", True)
        assert result["status"] == "expired"

    def test_cast_vote_pending_status(self):
        collab = self._make_session_with_approvers(4)
        voting = VotingApproval(collab)
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        voting._votes["aid5"] = {}
        voting._futures["aid5"] = fut

        result = voting.cast_vote("aid5", "u0", True)
        assert result["status"] == "pending"
        assert result["approve"] == 1
        assert result["deny"] == 0
        loop.close()


# =============================================================
# CollaborationHub Tests
# =============================================================

class TestCollaborationHub:
    def test_create_session(self):
        hub = CollaborationHub()
        collab = hub.create_session("My Session", "owner1", "Alice")
        assert collab.name == "My Session"
        assert collab.owner_user_id == "owner1"
        assert "owner1" in collab.participants
        owner = collab.participants["owner1"]
        assert Permission.APPROVE_ACTIONS in owner.permissions
        assert collab.id in [s.id for s in hub.list_sessions()]

    def test_join_by_invite(self):
        hub = CollaborationHub()
        collab = hub.create_session("Team Session", "owner", "Owner")
        invite = collab.invite_code

        joined = hub.join_by_invite(invite, "guest1", "Bob")
        assert joined is not None
        assert joined.id == collab.id
        assert "guest1" in joined.participants
        assert joined.participants["guest1"].display_name == "Bob"

    def test_join_invalid_invite(self):
        hub = CollaborationHub()
        result = hub.join_by_invite("badcode", "u1", "User")
        assert result is None

    def test_join_existing_participant(self):
        hub = CollaborationHub()
        collab = hub.create_session("Sess", "owner", "Owner")
        invite = collab.invite_code

        hub.join_by_invite(invite, "guest", "Guest")
        # Join again with same user_id — should return session, not add duplicate
        rejoined = hub.join_by_invite(invite, "guest", "Guest Renamed")
        assert rejoined is not None
        # Name should NOT change (already a participant)
        assert rejoined.participants["guest"].display_name == "Guest"

    def test_get_session(self):
        hub = CollaborationHub()
        collab = hub.create_session("Test", "u1", "User")
        fetched = hub.get_session(collab.id)
        assert fetched is not None
        assert fetched.id == collab.id

    def test_get_session_not_found(self):
        hub = CollaborationHub()
        assert hub.get_session("nonexistent") is None

    def test_get_session_by_invite(self):
        hub = CollaborationHub()
        collab = hub.create_session("Test", "u1", "User")
        fetched = hub.get_session_by_invite(collab.invite_code)
        assert fetched is not None
        assert fetched.id == collab.id

    def test_get_user_sessions(self):
        hub = CollaborationHub()
        c1 = hub.create_session("Sess 1", "u1", "User1")
        c2 = hub.create_session("Sess 2", "u2", "User2")
        hub.join_by_invite(c2.invite_code, "u1", "User1 again")

        sessions = hub.get_user_sessions("u1")
        ids = {s.id for s in sessions}
        assert c1.id in ids
        assert c2.id in ids

    def test_remove_session(self):
        hub = CollaborationHub()
        collab = hub.create_session("Temp", "u1", "User")
        sid = collab.id
        result = hub.remove_session(sid)
        assert result is True
        assert hub.get_session(sid) is None

    def test_remove_nonexistent_session(self):
        hub = CollaborationHub()
        result = hub.remove_session("nonexistent")
        assert result is False

    def test_list_sessions(self):
        hub = CollaborationHub()
        assert hub.list_sessions() == []
        hub.create_session("S1", "u1", "U1")
        hub.create_session("S2", "u2", "U2")
        assert len(hub.list_sessions()) == 2


# =============================================================
# Engine Session Locking Tests
# =============================================================

class TestEngineSessionLocking:
    @pytest.fixture
    def engine(self):
        from unittest.mock import MagicMock, AsyncMock
        from src.core.engine import Engine
        from src.config import KuroConfig
        config = KuroConfig()
        model_router = MagicMock()
        model_router.default_model = "test-model"
        tool_system = MagicMock()
        tool_system.registry = MagicMock()
        tool_system.registry.get_openai_tools = MagicMock(return_value=[])
        tool_system.registry.get_names = MagicMock(return_value=[])
        action_logger = MagicMock()
        action_logger.log_conversation = AsyncMock()
        return Engine(config, model_router, tool_system, action_logger)

    def test_get_session_lock_creates_lock(self, engine):
        lock = engine._get_session_lock("session_1")
        assert lock is not None
        assert isinstance(lock, asyncio.Lock)

    def test_get_session_lock_same_session_same_lock(self, engine):
        lock1 = engine._get_session_lock("session_1")
        lock2 = engine._get_session_lock("session_1")
        assert lock1 is lock2

    def test_get_session_lock_different_sessions_different_locks(self, engine):
        lock1 = engine._get_session_lock("session_1")
        lock2 = engine._get_session_lock("session_2")
        assert lock1 is not lock2

    @pytest.mark.asyncio
    async def test_concurrent_messages_serialized(self, engine):
        """Two concurrent messages to the same session must be serialized."""
        session = Session(adapter="test")
        order = []

        original_process = engine._process_message_locked

        async def tracking_process(user_text, sess, model=None, author_user_id=None):
            order.append(f"start:{user_text}")
            await asyncio.sleep(0.05)
            order.append(f"end:{user_text}")
            return f"Reply to {user_text}"

        engine._process_message_locked = tracking_process

        # Fire two messages concurrently
        results = await asyncio.gather(
            engine.process_message("msg1", session),
            engine.process_message("msg2", session),
        )

        # Each start must be followed by its own end before the next start
        assert order[0].startswith("start:")
        assert order[1].startswith("end:")
        assert order[2].startswith("start:")
        assert order[3].startswith("end:")
        # Both messages processed
        assert len(results) == 2
