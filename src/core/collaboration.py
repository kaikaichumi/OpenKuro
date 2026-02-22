"""Live collaboration: multi-user shared sessions with presence and voting.

Provides:
- CollaborativeSession: a shared AI conversation session for multiple users
- Participant: a user in a collaborative session with per-user permissions
- VotingApproval: majority-vote approval for high-risk tools
- CollaborationHub: manages all active collaborative sessions
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

import structlog

from src.core.types import Session

logger = structlog.get_logger()


class Permission(str, Enum):
    """Permissions for collaborative session participants."""

    READ = "read"
    WRITE = "write"
    EXECUTE_TOOLS = "execute_tools"
    APPROVE_ACTIONS = "approve_actions"


@dataclass
class Participant:
    """A participant in a collaborative session."""

    user_id: str
    display_name: str
    adapter: str = "web"  # "web", "telegram", "slack", etc.
    permissions: set[Permission] = field(default_factory=lambda: {
        Permission.READ,
        Permission.WRITE,
    })
    trust_level: str = "low"
    joined_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    is_online: bool = True
    is_typing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "adapter": self.adapter,
            "permissions": [p.value for p in self.permissions],
            "trust_level": self.trust_level,
            "is_online": self.is_online,
            "is_typing": self.is_typing,
        }


@dataclass
class CollaborativeSession:
    """A shared session for multiple users.

    Wraps a regular Session so the engine can process messages normally.
    All participants share the same conversation history.
    """

    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    owner_user_id: str = ""
    invite_code: str = field(default_factory=lambda: uuid4().hex[:8])
    session: Session = field(default_factory=lambda: Session(adapter="collab"))
    participants: dict[str, Participant] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    # Concurrency: one message processed at a time per session
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Collaboration event history (who joined, left, voted, etc.)
    history: list[dict[str, Any]] = field(default_factory=list)

    def add_participant(self, participant: Participant) -> None:
        self.participants[participant.user_id] = participant
        self._log_event("join", participant.user_id, participant.display_name)
        logger.info(
            "collab_participant_joined",
            session_id=self.id,
            user_id=participant.user_id,
            display_name=participant.display_name,
        )

    def remove_participant(self, user_id: str) -> None:
        if user_id in self.participants:
            name = self.participants[user_id].display_name
            del self.participants[user_id]
            self._log_event("leave", user_id, name)

    def has_permission(self, user_id: str, perm: Permission) -> bool:
        p = self.participants.get(user_id)
        return p is not None and perm in p.permissions

    def get_approvers(self) -> list[Participant]:
        """Return online participants who can approve actions."""
        return [
            p for p in self.participants.values()
            if Permission.APPROVE_ACTIONS in p.permissions and p.is_online
        ]

    def set_typing(self, user_id: str, is_typing: bool) -> None:
        if user_id in self.participants:
            self.participants[user_id].is_typing = is_typing
            self.participants[user_id].last_active = time.time()

    def set_online(self, user_id: str, online: bool) -> None:
        if user_id in self.participants:
            self.participants[user_id].is_online = online
            if online:
                self.participants[user_id].last_active = time.time()

    def _log_event(self, event_type: str, user_id: str, detail: str = "") -> None:
        self.history.append({
            "type": event_type,
            "user_id": user_id,
            "detail": detail,
            "timestamp": time.time(),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "owner_user_id": self.owner_user_id,
            "invite_code": self.invite_code,
            "participants": {uid: p.to_dict() for uid, p in self.participants.items()},
            "created_at": self.created_at,
            "message_count": len(self.session.messages),
        }


class VotingApproval:
    """Multi-user majority-vote approval for tool execution."""

    def __init__(self, collab_session: CollaborativeSession) -> None:
        self.collab = collab_session
        self._votes: dict[str, dict[str, bool]] = {}  # approval_id -> {user_id: vote}
        self._futures: dict[str, asyncio.Future[bool]] = {}

    async def request_vote(
        self,
        approval_id: str,
        tool_name: str,
        params: dict[str, Any],
        risk_level: str,
        broadcast_fn: Callable,
    ) -> bool:
        """Broadcast an approval request and wait for majority consensus.

        Args:
            broadcast_fn: async function(message_dict) to send to all participants.
        """
        approvers = self.collab.get_approvers()
        if not approvers:
            logger.warning("collab_no_approvers", session_id=self.collab.id)
            return False

        required = (len(approvers) // 2) + 1  # Majority
        self._votes[approval_id] = {}

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._futures[approval_id] = future

        await broadcast_fn({
            "type": "collab_approval_request",
            "approval_id": approval_id,
            "tool_name": tool_name,
            "params": params,
            "risk_level": risk_level,
            "required_votes": required,
            "total_approvers": len(approvers),
            "approver_ids": [a.user_id for a in approvers],
        })

        try:
            result = await asyncio.wait_for(future, timeout=120)
            return result
        except asyncio.TimeoutError:
            await broadcast_fn({
                "type": "collab_approval_expired",
                "approval_id": approval_id,
                "tool_name": tool_name,
            })
            return False
        finally:
            self._votes.pop(approval_id, None)
            self._futures.pop(approval_id, None)

    def cast_vote(
        self,
        approval_id: str,
        user_id: str,
        approve: bool,
    ) -> dict[str, Any]:
        """Cast a vote for an approval request.

        Returns a status dict including whether the vote resolved the request.
        """
        votes = self._votes.get(approval_id)
        if votes is None:
            return {"status": "expired"}

        # Only allow votes from approvers
        if not self.collab.has_permission(user_id, Permission.APPROVE_ACTIONS):
            return {"status": "no_permission"}

        votes[user_id] = approve

        approvers = self.collab.get_approvers()
        required = (len(approvers) // 2) + 1

        approve_count = sum(1 for v in votes.values() if v)
        deny_count = sum(1 for v in votes.values() if not v)

        future = self._futures.get(approval_id)
        if future and not future.done():
            if approve_count >= required:
                future.set_result(True)
                return {
                    "status": "approved",
                    "approve": approve_count,
                    "deny": deny_count,
                    "required": required,
                }
            elif deny_count >= required:
                future.set_result(False)
                return {
                    "status": "denied",
                    "approve": approve_count,
                    "deny": deny_count,
                    "required": required,
                }

        return {
            "status": "pending",
            "approve": approve_count,
            "deny": deny_count,
            "required": required,
            "votes": len(votes),
        }


class CollaborationHub:
    """Central registry for all active collaborative sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, CollaborativeSession] = {}
        self._invite_codes: dict[str, str] = {}  # invite_code -> session_id
        self._user_sessions: dict[str, set[str]] = {}  # user_id -> set of session_ids

    def create_session(
        self,
        name: str,
        owner_user_id: str,
        owner_display_name: str,
        owner_adapter: str = "web",
    ) -> CollaborativeSession:
        """Create a new collaborative session owned by the given user."""
        collab = CollaborativeSession(
            name=name,
            owner_user_id=owner_user_id,
            session=Session(adapter="collab", user_id=owner_user_id),
        )

        # Owner gets all permissions
        owner = Participant(
            user_id=owner_user_id,
            display_name=owner_display_name,
            adapter=owner_adapter,
            permissions=set(Permission),  # All permissions
            trust_level="high",  # Owner is trusted
        )
        collab.add_participant(owner)

        self._sessions[collab.id] = collab
        self._invite_codes[collab.invite_code] = collab.id
        self._user_sessions.setdefault(owner_user_id, set()).add(collab.id)

        logger.info(
            "collab_session_created",
            session_id=collab.id,
            name=name,
            owner=owner_user_id,
            invite_code=collab.invite_code,
        )
        return collab

    def join_by_invite(
        self,
        invite_code: str,
        user_id: str,
        display_name: str,
        adapter: str = "web",
    ) -> CollaborativeSession | None:
        """Join an existing session using an invite code.

        Returns the session, or None if the invite code is invalid.
        """
        session_id = self._invite_codes.get(invite_code)
        if session_id is None:
            return None

        collab = self._sessions.get(session_id)
        if collab is None:
            return None

        # If already a participant, just return the session
        if user_id in collab.participants:
            return collab

        participant = Participant(
            user_id=user_id,
            display_name=display_name,
            adapter=adapter,
            permissions={Permission.READ, Permission.WRITE, Permission.APPROVE_ACTIONS},
        )
        collab.add_participant(participant)
        self._user_sessions.setdefault(user_id, set()).add(session_id)

        return collab

    def get_session(self, session_id: str) -> CollaborativeSession | None:
        return self._sessions.get(session_id)

    def get_session_by_invite(self, invite_code: str) -> CollaborativeSession | None:
        session_id = self._invite_codes.get(invite_code)
        if session_id:
            return self._sessions.get(session_id)
        return None

    def get_user_sessions(self, user_id: str) -> list[CollaborativeSession]:
        session_ids = self._user_sessions.get(user_id, set())
        return [self._sessions[sid] for sid in session_ids if sid in self._sessions]

    def remove_session(self, session_id: str) -> bool:
        collab = self._sessions.pop(session_id, None)
        if collab:
            self._invite_codes.pop(collab.invite_code, None)
            for uid in list(collab.participants):
                user_sess = self._user_sessions.get(uid, set())
                user_sess.discard(session_id)
            logger.info("collab_session_removed", session_id=session_id)
            return True
        return False

    def list_sessions(self) -> list[CollaborativeSession]:
        return list(self._sessions.values())
