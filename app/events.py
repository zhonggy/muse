"""极简 pub/sub 事件总线，用于把任务日志 / 截图推给 WebSocket 客户端。"""
from __future__ import annotations

import asyncio
from typing import Any

MAX_QUEUE = 256


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        async with self._lock:
            self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subs.discard(q)

    def publish(self, msg: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 客户端消费不过来就丢最旧的，保证不阻塞任务
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
