"""In-memory Headscale for LAN Play tests: users, nodes, pre-auth keys and the policy text.

Mirrors the server rules the bot relies on: listing nodes of an unknown user is an error,
deleting an unknown node is a 404. ``down = True`` makes every call fail like an outage,
``file_mode = True`` makes PUT /policy fail like ``policy.mode: file``.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from src.core.exceptions import HeadscaleError, HeadscaleNotFound, HeadscalePolicyReadOnly
from src.infrastructure.headscale.client import HsKeyInfo, HsNode, HsPreAuthKey, HsUser


class FakeHeadscale:
    def __init__(self) -> None:
        self.users: dict[str, HsUser] = {}
        self.nodes: dict[int, HsNode] = {}
        self.keys: list[dict[str, Any]] = []
        self.policy = ""
        self.policy_puts = 0
        self.expired: list[int] = []
        self.down = False
        self.fail_put = False
        self.file_mode = False
        self.renamed: list[int] = []
        self._ids = itertools.count(1)
        self.opened: list[tuple[str, str]] = []

    # the AppContainer.headscale_factory signature
    def factory(self, login_server: str, api_key: str) -> FakeHeadscale:
        self.opened.append((login_server, api_key))
        return self

    async def __aenter__(self) -> FakeHeadscale:
        return self

    async def __aexit__(self, *exc: object) -> None: ...

    def _check(self) -> None:
        if self.down:
            raise HeadscaleError("headscale unreachable: ConnectError")

    def add_node(
        self,
        user: str,
        *,
        name: str = "pc",
        ip: str | None = None,
        online: bool = True,
        expiry: dt.datetime | None = None,
        created_at: dt.datetime | None = None,
    ) -> HsNode:
        node_id = next(self._ids)
        self.users.setdefault(user, HsUser(id=next(self._ids), name=user))
        node = HsNode(
            id=node_id,
            name=name,
            user=user,
            ips=(ip or f"100.64.0.{node_id}", f"fd7a:115c:a1e0::{node_id}"),
            online=online,
            last_seen=None,
            expiry=expiry,
            created_at=created_at
            or dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=node_id),
            user_id=self.users[user].id,
        )
        self.nodes[node_id] = node
        return node

    def register(self, key: str, *, name: str) -> HsNode:
        """A daemon logs in with ``key`` (single-use): the key is spent, a node appears."""
        entry = next(k for k in self.keys if k["key"] == key)
        assert not entry["used"] and not entry["expired"], "key is spent or expired"
        entry["used"] = True
        user = next(u.name for u in self.users.values() if u.id == entry["user"])
        return self.add_node(user, name=name)

    async def health(self) -> bool:
        self._check()
        return True

    async def list_users(self, name: str | None = None) -> list[HsUser]:
        self._check()
        return [u for u in self.users.values() if name is None or u.name == name]

    async def ensure_user(self, name: str) -> HsUser:
        self._check()
        if name not in self.users:
            self.users[name] = HsUser(id=next(self._ids), name=name)
        return self.users[name]

    async def create_preauth_key(self, user_id: int, *, expires_at: dt.datetime) -> HsPreAuthKey:
        self._check()
        key = f"hskey-{len(self.keys) + 1}"
        self.keys.append(
            {
                "id": len(self.keys) + 1,
                "user": user_id,
                "key": key,
                "expires_at": expires_at,
                "used": False,
                "expired": False,
            }
        )
        return HsPreAuthKey(key=key, expiration=expires_at)

    async def list_preauth_keys(self, user_id: int) -> list[HsKeyInfo]:
        self._check()
        past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        return [
            HsKeyInfo(
                id=k["id"],
                user_id=user_id,
                used=k["used"],
                expiration=past if k["expired"] else k["expires_at"],
            )
            for k in self.keys
            if k["user"] == user_id
        ]

    async def expire_preauth_key(self, key_id: int) -> None:
        self._check()
        next(k for k in self.keys if k["id"] == key_id)["expired"] = True

    def pending_keys(self) -> int:
        return sum(1 for k in self.keys if not k["used"] and not k["expired"])

    async def list_nodes(self, user: str | None = None) -> list[HsNode]:
        self._check()
        if user is not None and user not in self.users:
            raise HeadscaleError("headscale 500: user not found")
        return [n for n in self.nodes.values() if user is None or n.user == user]

    async def delete_node(self, node_id: int) -> None:
        self._check()
        if self.nodes.pop(node_id, None) is None:
            raise HeadscaleNotFound("node not found")

    async def expire_node(self, node_id: int) -> None:
        self._check()
        node = self.nodes[node_id]
        past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        self.nodes[node_id] = HsNode(
            id=node.id,
            name=node.name,
            user=node.user,
            ips=node.ips,
            online=node.online,
            last_seen=node.last_seen,
            expiry=past,
            created_at=node.created_at,
            user_id=node.user_id,
        )
        self.expired.append(node_id)

    async def rename_node(self, node_id: int, name: str) -> None:
        self._check()
        node = self.nodes[node_id]
        self.nodes[node_id] = HsNode(
            id=node.id,
            name=name,
            user=node.user,
            ips=node.ips,
            online=node.online,
            last_seen=node.last_seen,
            expiry=node.expiry,
            created_at=node.created_at,
            user_id=node.user_id,
        )
        self.renamed.append(node_id)

    async def get_policy(self) -> str:
        self._check()
        if not self.policy:
            raise HeadscaleError("headscale 500: loading ACL from database: record not found")
        return self.policy

    async def set_policy(self, policy: str) -> None:
        self._check()
        if self.file_mode:
            raise HeadscalePolicyReadOnly("headscale policy.mode is not database", status=500)
        if self.fail_put:
            raise HeadscaleError("headscale 500", status=500)
        self.policy = policy
        self.policy_puts += 1
