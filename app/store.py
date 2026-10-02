"""极简 JSON 持久化（设置 / 卡片 / 任务记录），带 asyncio 锁。"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DEFAULT_SETTINGS
from .crypto import cipher, mask_pan

TASK_KEEP = 500  # 最多保留的任务记录条数


def _now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DATA_DIR / "store.json")
        self._lock = asyncio.Lock()
        self._dirty = False
        self.data: dict[str, Any] = {
            "settings": dict(DEFAULT_SETTINGS),
            "cards": [],
            "tasks": [],
        }

    # ---------- 生命周期 ----------

    async def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self.data["settings"] = {**DEFAULT_SETTINGS, **raw.get("settings", {})}
                    self.data["cards"] = list(raw.get("cards", []))
                    self.data["tasks"] = list(raw.get("tasks", []))
            except (json.JSONDecodeError, OSError):
                pass
        # 启动时把所有未完成的任务标记为中断
        for t in self.data["tasks"]:
            if t.get("status") in ("running", "paused"):
                t["status"] = "stopped"
                t["message"] = "服务重启，任务已中断"
        await self.save()

    async def save(self) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8"
            )
            tmp.replace(self.path)

    # ---------- 设置 ----------

    def get_settings(self) -> dict:
        return dict(self.data["settings"])

    async def update_settings(self, patch: dict) -> dict:
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                self.data["settings"][k] = v
        await self.save()
        return self.get_settings()

    # ---------- 卡片 ----------

    @staticmethod
    def public_card(card: dict) -> dict:
        """返回给前端的脱敏卡片（永不返回完整卡号 / CVV）。"""
        return {
            "id": card["id"],
            "label": card.get("label", ""),
            "brand": card.get("brand", ""),
            "pan_masked": card.get("pan_masked", "****"),
            "exp_month": card.get("exp_month", ""),
            "exp_year": card.get("exp_year", ""),
            "holder": card.get("holder", ""),
            "postal": card.get("postal", ""),
            "country": card.get("country", ""),
            "has_cvc": bool(card.get("cvc_enc")),
            "created_at": card.get("created_at"),
            "note": card.get("note", ""),
        }

    def list_cards(self) -> list[dict]:
        return [self.public_card(c) for c in self.data["cards"]]

    def get_card_secret(self, card_id: str) -> dict | None:
        """仅内部使用：返回解密后的卡信息。"""
        for c in self.data["cards"]:
            if c["id"] == card_id:
                return {
                    "id": c["id"],
                    "label": c.get("label", ""),
                    "number": cipher().decrypt(c.get("pan_enc", "")),
                    "cvc": cipher().decrypt(c.get("cvc_enc", "")),
                    "exp_month": str(c.get("exp_month", "")).zfill(2),
                    "exp_year": str(c.get("exp_year", "")),
                    "holder": c.get("holder", ""),
                    "postal": c.get("postal", ""),
                    "country": c.get("country", ""),
                    "extra": c.get("extra", {}),
                }
        return None

    async def add_card(self, payload: dict) -> dict:
        number = "".join(ch for ch in str(payload.get("number", "")) if ch.isdigit())
        card = {
            "id": new_id("card_"),
            "label": payload.get("label") or (f"卡 ****{number[-4:]}" if number else "未命名卡"),
            "brand": detect_brand(number),
            "pan_enc": cipher().encrypt(number),
            "pan_masked": mask_pan(number),
            "cvc_enc": cipher().encrypt(str(payload.get("cvc", ""))),
            "exp_month": str(payload.get("exp_month", "")).zfill(2),
            "exp_year": str(payload.get("exp_year", "")),
            "holder": payload.get("holder", ""),
            "postal": payload.get("postal", ""),
            "country": payload.get("country", ""),
            "extra": payload.get("extra", {}) or {},
            "note": payload.get("note", ""),
            "created_at": _now(),
        }
        self.data["cards"].append(card)
        await self.save()
        return self.public_card(card)

    async def update_card(self, card_id: str, payload: dict) -> dict | None:
        for c in self.data["cards"]:
            if c["id"] != card_id:
                continue
            for key in ("label", "holder", "postal", "country", "note"):
                if key in payload:
                    c[key] = payload[key]
            if payload.get("exp_month"):
                c["exp_month"] = str(payload["exp_month"]).zfill(2)
            if payload.get("exp_year"):
                c["exp_year"] = str(payload["exp_year"])
            if payload.get("number"):
                number = "".join(ch for ch in str(payload["number"]) if ch.isdigit())
                c["pan_enc"] = cipher().encrypt(number)
                c["pan_masked"] = mask_pan(number)
                c["brand"] = detect_brand(number)
            if payload.get("cvc"):
                c["cvc_enc"] = cipher().encrypt(str(payload["cvc"]))
            if payload.get("extra"):
                c["extra"] = payload["extra"]
            await self.save()
            return self.public_card(c)
        return None

    async def delete_card(self, card_id: str) -> bool:
        before = len(self.data["cards"])
        self.data["cards"] = [c for c in self.data["cards"] if c["id"] != card_id]
        if len(self.data["cards"]) != before:
            await self.save()
            return True
        return False

    def touch(self) -> None:
        """标记为脏，由后台 flush 循环落盘。"""
        self._dirty = True

    async def flush(self) -> None:
        if self._dirty:
            self._dirty = False
            await self.save()

    # ---------- 任务 ----------

    def list_tasks(self) -> list[dict]:
        return list(self.data["tasks"])

    def get_task(self, task_id: str) -> dict | None:
        for t in self.data["tasks"]:
            if t["id"] == task_id:
                return t
        return None

    async def add_tasks(self, tasks: list[dict]) -> list[dict]:
        self.data["tasks"].extend(tasks)
        if len(self.data["tasks"]) > TASK_KEEP:
            done = [t for t in self.data["tasks"] if t["status"] in
                    ("success", "failed", "stopped")]
            drop = len(self.data["tasks"]) - TASK_KEEP
            drop_ids = {t["id"] for t in done[:drop]}
            self.data["tasks"] = [
                t for t in self.data["tasks"] if t["id"] not in drop_ids
            ]
        await self.save()
        return tasks

    async def update_task(self, task_id: str, patch: dict) -> dict | None:
        for t in self.data["tasks"]:
            if t["id"] == task_id:
                t.update(patch)
                self.touch()          # 任务日志写入频繁，用防抖落盘
                return t
        return None

    async def delete_task(self, task_id: str) -> bool:
        before = len(self.data["tasks"])
        self.data["tasks"] = [t for t in self.data["tasks"] if t["id"] != task_id]
        if len(self.data["tasks"]) != before:
            await self.save()
            return True
        return False

    async def clear_tasks(self, statuses: tuple[str, ...] = ()) -> int:
        if not statuses:
            n = len(self.data["tasks"])
            self.data["tasks"] = []
        else:
            keep = [t for t in self.data["tasks"] if t["status"] not in statuses]
            n = len(self.data["tasks"]) - len(keep)
            self.data["tasks"] = keep
        await self.save()
        return n


def detect_brand(number: str) -> str:
    if not number:
        return ""
    if number.startswith("4"):
        return "Visa"
    if number[:2] in {"51", "52", "53", "54", "55"} or number[:4].isdigit() and 2221 <= int(number[:4] or 0) <= 2720:
        return "Mastercard"
    if number[:2] in {"34", "37"}:
        return "Amex"
    if number[:4] in {"6011"} or number[:2] == "65":
        return "Discover"
    if number[:2] in {"62", "81"}:
        return "UnionPay"
    if number[:2] == "35":
        return "JCB"
    return "Card"
