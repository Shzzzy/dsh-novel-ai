"""WebSocket 连接管理器
支持多连接、按 novel_id 分组广播、心跳保活。
"""

import asyncio
import time
import json
from typing import Optional
from fastapi import WebSocket


class ManagedConnection:
    """一个被管理的 WebSocket 连接"""

    def __init__(self, ws: WebSocket, conn_id: str):
        self.ws = ws
        self.conn_id = conn_id
        self.novel_id: Optional[str] = None
        self.connected_at = time.time()
        self.last_ping_at = time.time()
        self.active = True


class ConnectionManager:
    """管理所有 WebSocket 连接，按 novel_id 分组"""

    def __init__(self):
        self._connections: dict[str, ManagedConnection] = {}
        self._by_novel: dict[str, set[str]] = {}

    # ── 连接生命周期 ──

    def add(self, ws: WebSocket, conn_id: str) -> ManagedConnection:
        conn = ManagedConnection(ws, conn_id)
        self._connections[conn_id] = conn
        return conn

    def remove(self, conn_id: str) -> Optional[ManagedConnection]:
        conn = self._connections.pop(conn_id, None)
        if conn and conn.novel_id:
            group = self._by_novel.get(conn.novel_id)
            if group:
                group.discard(conn_id)
                if not group:
                    del self._by_novel[conn.novel_id]
        if conn:
            conn.active = False
        return conn

    def subscribe(self, conn_id: str, novel_id: str):
        conn = self._connections.get(conn_id)
        if not conn:
            return
        old = conn.novel_id
        if old and old in self._by_novel:
            self._by_novel[old].discard(conn_id)
            if not self._by_novel[old]:
                del self._by_novel[old]
        conn.novel_id = novel_id
        self._by_novel.setdefault(novel_id, set()).add(conn_id)

    def touch(self, conn_id: str):
        conn = self._connections.get(conn_id)
        if conn:
            conn.last_ping_at = time.time()

    def reset_idle(self, conn_id: str):
        """重置心跳计时器 — 收到客户端消息时调用"""
        conn = self._connections.get(conn_id)
        if conn:
            conn.last_ping_at = time.time()

    # ── 发送 ──

    async def send_json(self, conn_id: str, payload: dict) -> bool:
        """向单个连接发送 JSON，连接已关闭则返回 False"""
        conn = self._connections.get(conn_id)
        if not conn or not conn.active:
            return False
        try:
            await conn.ws.send_json(payload)
            return True
        except Exception:
            conn.active = False
            return False

    async def broadcast(self, novel_id: str, payload: dict) -> int:
        """向订阅同一 novel_id 的所有连接广播，返回成功发送数"""
        ids = self._by_novel.get(novel_id, set()).copy()
        count = 0
        for cid in ids:
            ok = await self.send_json(cid, payload)
            if ok:
                count += 1
        return count

    # ── 心跳清理 ──

    async def purge_idle(self, timeout_seconds: int = 60) -> int:
        """清理超过 timeout_seconds 无响应的连接，返回清理数"""
        now = time.time()
        dead = [
            cid
            for cid, conn in self._connections.items()
            if now - conn.last_ping_at > timeout_seconds
        ]
        for cid in dead:
            conn = self._connections.get(cid)
            if conn:
                try:
                    await conn.ws.close()
                except Exception:
                    pass
            self.remove(cid)
        return len(dead)

    async def start_ping_loop(self, interval: int = 15):
        """后台每 interval 秒发送 ping，检测死连接"""
        while True:
            await asyncio.sleep(interval)
            for cid in list(self._connections.keys()):
                ok = await self.send_json(cid, {"type": "ping"})
                if not ok:
                    self.remove(cid)

    # ── 查询 ──

    def get(self, conn_id: str) -> Optional[ManagedConnection]:
        return self._connections.get(conn_id)

    @property
    def total_connections(self) -> int:
        return len(self._connections)

    @property
    def active_connections(self) -> int:
        return sum(1 for c in self._connections.values() if c.active)

    def novel_connections(self, novel_id: str) -> int:
        return len(self._by_novel.get(novel_id, set()))

    def stats(self) -> dict:
        return {
            "total_connections": self.total_connections,
            "active_connections": self.active_connections,
            "novels_watching": len(self._by_novel),
            "novels": {
                nid: len(cids) for nid, cids in self._by_novel.items()
            },
        }


# 全局单例
manager = ConnectionManager()
