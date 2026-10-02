"""任务运行时（TaskRuntime）与调度器（TaskRunner）。

TaskRuntime 是流程与外界之间的唯一接口：
  - 日志 / 截图 → EventBus
  - 停止 / 暂停 / 人工接管
  - 验证码获取（skymail）
  - 卡信息解析
"""
from __future__ import annotations

import asyncio
import base64
import copy
import logging
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from . import browser as browser_mod
from .config import SHOT_DIR, SESSION_DIR
from .events import EventBus
from .muse_flow import FlowError, FlowStopped, MuseFlow
from .skymail import SkymailClient, SkymailError
from .store import Store

log = logging.getLogger("muse.runner")

LOG_KEEP = 200
STEP_TIMEOUT = 60

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"


class TaskRuntime:
    def __init__(self, task: dict, runner: "TaskRunner") -> None:
        self.task = task
        self.runner = runner
        self.store: Store = runner.store
        self.bus: EventBus = runner.bus
        self.settings: dict = runner.settings

        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._active_page: Page | None = None

        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._provided_card: dict | None = None
        self._seen_mail_ids: set[int] = set()
        self._mail_source: tuple[str, Any] = ("none", None)
        self._shot_task: asyncio.Task | None = None
        self._last_shot: bytes | None = None

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self.task["id"]

    @property
    def active_page(self) -> Page:
        return self._active_page or self.page  # type: ignore[return-value]

    def set_active_page(self, page: Page | None) -> None:
        if page is not None:
            self._active_page = page

    async def set_step(self, step: str) -> None:
        self.task["step"] = step
        await self.log(f"▶ {step}", "step")

    async def patch(self, **fields: Any) -> None:
        self.task.update(fields)
        await self.publish_task()

    async def publish_task(self) -> None:
        self.bus.publish({"type": "task", "task": copy.deepcopy(self.task)})

    # ------------------------------------------------------------------
    # 日志 / 截图
    # ------------------------------------------------------------------

    async def log(self, msg: str, level: str = "info") -> None:
        entry = {"ts": time.time(), "level": level, "msg": msg}
        logs = self.task.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > LOG_KEEP:
            del logs[: len(logs) - LOG_KEEP]
        self.store.touch()
        self.bus.publish({"type": "log", "task_id": self.id, **entry})
        if level in ("error", "success", "step"):
            self.bus.publish({"type": "task", "task": copy.deepcopy(self.task)})

    async def snap(self, label: str = "", page: Page | None = None) -> None:
        target = page or self.active_page
        if target is None:
            return
        live = bool(self.settings.get("live_view", True))
        if not label and not live:
            return  # 关掉实时画面时，无标签的定时截图直接跳过
        try:
            data = await target.screenshot(
                type="jpeg",
                quality=int(self.settings.get("screenshot_quality", 55)),
                timeout=15_000,
            )
        except Exception:
            return
        self._last_shot = data
        if label:
            path = Path(SHOT_DIR) / f"{self.id}-{label}.jpg"
            try:
                path.write_bytes(data)
                self.task["last_shot"] = path.name
                self.store.touch()
            except OSError:
                pass
        if live:
            self.bus.publish({
                "type": "shot",
                "task_id": self.id,
                "label": label,
                "data": base64.b64encode(data).decode(),
                "w": int(self.settings.get("viewport_w", 1280)),
                "h": int(self.settings.get("viewport_h", 820)),
                "ts": time.time(),
            })

    async def _shot_loop(self) -> None:
        interval = float(self.settings.get("screenshot_interval", 1.5))
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                if not self.settings.get("live_view", True):
                    continue
                if self.bus.subscriber_count:
                    await self.snap()
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    async def check_stop(self) -> None:
        if self._stop.is_set():
            raise FlowStopped("任务已被停止")
        if self.task.get("status") == STATUS_STOPPED:
            raise FlowStopped("任务已被停止")

    def request_stop(self) -> None:
        self._stop.set()
        self._resume.set()

    def is_paused(self) -> bool:
        return not self._resume.is_set()

    async def _wait_resume(self) -> None:
        while not self._resume.is_set():
            await self.check_stop()
            try:
                await asyncio.wait_for(self._resume.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    async def _pause(self, reason: str, needs: str) -> None:
        self._resume.clear()
        self.task["status"] = STATUS_PAUSED
        self.task["needs"] = needs
        self.task["message"] = reason
        await self.publish_task()
        await self.log(f"⏸ 任务暂停：{reason}", "warn")
        await self._wait_resume()
        self.task["status"] = STATUS_RUNNING
        self.task["needs"] = None
        self.task["message"] = ""
        await self.publish_task()
        await self.log("▶ 任务已恢复", "info")

    async def wait_for_manual(self, reason: str) -> None:
        await self._pause(reason, "manual")

    async def wait_for_card(self) -> dict:
        self._provided_card = None
        await self._pause("等待提供支付卡信息", "card")
        if not self._provided_card:
            raise FlowError("恢复时未收到卡信息")
        return self._provided_card

    def provide_card(self, card: dict) -> None:
        self._provided_card = card
        self._resume.set()

    def manual_done(self) -> None:
        self._resume.set()

    # ------------------------------------------------------------------
    # 邮箱（通过 skymail API 自动创建）
    # ------------------------------------------------------------------

    async def prepare_email(self) -> str:
        """任务启动时自动生成并注册一个新邮箱，返回邮箱地址。"""
        email = self.task.get("email")
        if email:
            return str(email)
        await self.set_step("创建收件邮箱")
        info = await self.runner.skymail.create_random_email(
            domain=(self.settings.get("email_domain") or "").strip() or None
        )
        await self.patch(
            email=info["email"],
            skymail_account_id=info.get("accountId"),
        )
        await self.log(f"已通过 skymail API 创建邮箱：{info['email']}")
        return str(info["email"])

    # ------------------------------------------------------------------
    # 验证码
    # ------------------------------------------------------------------

    async def capture_mail_baseline(self) -> None:
        client: SkymailClient = self.runner.skymail
        email = self.task["email"]

        # 1) 优先用 account 接口（若该 skymail 实例支持）
        if not client.email_list_broken:
            account_id = self.task.get("skymail_account_id")
            if not account_id:
                try:
                    acc = await client.find_account(email)
                    account_id = (acc or {}).get("accountId")
                except SkymailError as exc:
                    await self.log(f"查找邮箱账号失败：{exc}", "warn")
            if account_id:
                try:
                    mails = await client.poll_once(int(account_id), size=5)
                except SkymailError as exc:
                    await self.log(
                        f"/api/email/list 不可用（{exc}），改用全局邮件接口", "warn"
                    )
                else:
                    self._mail_source = ("account", int(account_id))
                    self._seen_mail_ids = {int(m.get("emailId") or 0) for m in mails}
                    await self.patch(skymail_account_id=int(account_id))
                    await self.log(
                        f"收件箱就绪：accountId={account_id}，"
                        f"基线邮件 {len(self._seen_mail_ids)} 封"
                    )
                    return

        # 2) 全局接口（需管理员权限，但 /api/email/list 挂了时是唯一选择）
        self._mail_source = ("all", email)
        try:
            data = await client.list_all_emails(email, size=5, full=0)
            self._seen_mail_ids = {
                int(m.get("emailId") or 0) for m in (data or {}).get("list", [])
            }
            await self.log(
                "收件箱就绪（全局接口 /api/allEmail/list）：基线邮件 "
                f"{len(self._seen_mail_ids)} 封"
            )
        except SkymailError as exc:
            self._seen_mail_ids = set()
            await self.log(f"全局邮件接口也不可用（{exc}），收码会失败", "warn")

    async def _fetch_mails(self) -> list[dict]:
        client: SkymailClient = self.runner.skymail
        kind, ref = self._mail_source
        if kind == "account":
            return await client.poll_once(int(ref), size=8)
        data = await client.list_all_emails(str(ref), size=8, full=1)
        return list((data or {}).get("list") or [])

    async def fetch_code(self) -> str:
        client: SkymailClient = self.runner.skymail
        timeout = float(self.settings.get("code_timeout", 240))
        interval = float(self.settings.get("code_poll_interval", 3.0))

        async def tick() -> None:
            await self.check_stop()

        try:
            code, mail = await client.wait_for_code(
                self._fetch_mails,
                self._seen_mail_ids,
                timeout=timeout,
                interval=interval,
                on_tick=tick,
            )
        except SkymailError as exc:
            raise FlowError(str(exc)) from exc

        await self.log(
            f"收到邮件：「{str(mail.get('subject') or '')[:60]}」"
            f"（{mail.get('createTime')}）"
        )
        await self.patch(code=code)
        return code

    # ------------------------------------------------------------------
    # 卡信息
    # ------------------------------------------------------------------

    async def resolve_card(self) -> dict:
        card_id = self.task.get("card_id") or self.settings.get("default_card_id") or ""
        if card_id and self.settings.get("auto_fill_card", True):
            card = self.store.get_card_secret(str(card_id))
            if card:
                await self.log(
                    f"使用控制台保存的卡片：{card.get('label') or ''} "
                    f"****{card['number'][-4:]}"
                )
                return card
            await self.log(f"卡片 {card_id} 不存在，转为等待手动提供", "warn")
        return await self.wait_for_card()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    async def mark_account_created(self) -> None:
        await self.patch(account_created=True)

    async def mark_reached_verification(self) -> None:
        await self.patch(reached_verification=True)

    async def save_session(self) -> None:
        if self.context is None:
            return
        path = Path(SESSION_DIR) / f"{self.id}.json"
        try:
            state = await self.context.storage_state()
            import json

            path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
            await self.patch(session_file=path.name)
            await self.log(f"登录态已保存：sessions/{path.name}")
        except Exception as exc:
            await self.log(f"保存登录态失败：{exc}", "warn")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def open_browser(self) -> None:
        settings = self.settings
        storage_state = None
        session_name = self.task.get("reuse_session")
        if session_name:
            p = Path(SESSION_DIR) / session_name
            if p.exists():
                import json

                storage_state = json.loads(p.read_text("utf-8"))
                await self.log(f"复用登录态：{session_name}")

        self.context = await browser_mod.manager.new_context(settings, storage_state)
        self.page = await self.context.new_page()
        self._active_page = self.page
        self.page.set_default_timeout(int(settings.get("step_timeout", STEP_TIMEOUT)) * 1000)

        def _on_page(p: Page) -> None:
            self.set_active_page(p)
            try:
                p.set_default_timeout(int(settings.get("step_timeout", STEP_TIMEOUT)) * 1000)
            except Exception:
                pass

        self.context.on("page", _on_page)
        self._shot_task = asyncio.create_task(self._shot_loop())

    async def close(self) -> None:
        self._stop.set()
        self._resume.set()
        if self._shot_task:
            self._shot_task.cancel()
            try:
                await self._shot_task
            except (asyncio.CancelledError, Exception):
                pass
            self._shot_task = None
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        self.page = None
        self._active_page = None


class TaskRunner:
    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        self.settings: dict = store.get_settings()
        self.skymail = SkymailClient(
            self.settings["skymail_base_url"],
            self.settings["skymail_email"],
            self.settings["skymail_password"],
        )
        self._runtimes: dict[str, TaskRuntime] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0

    # ------------------------------------------------------------------

    async def refresh_settings(self) -> None:
        self.settings = self.store.get_settings()
        await self.skymail.configure(
            self.settings.get("skymail_base_url") or "https://skymail.ink",
            self.settings.get("skymail_email") or "",
            self.settings.get("skymail_password") or "",
        )

    @property
    def active_ids(self) -> list[str]:
        return [tid for tid in list(self._tasks) if self.is_running(tid)]

    def _semaphore(self) -> asyncio.Semaphore:
        size = max(1, int(self.settings.get("concurrency", 1)))
        if self._sem is None or size != self._sem_size:
            self._sem = asyncio.Semaphore(size)
            self._sem_size = size
        return self._sem

    def runtime(self, task_id: str) -> TaskRuntime | None:
        return self._runtimes.get(task_id)

    def is_running(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        return bool(t and not t.done())

    # ------------------------------------------------------------------

    def start(self, task_id: str, *, reset: bool = True) -> bool:
        if self.is_running(task_id):
            return False
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if reset:
            task.update({
                "status": STATUS_PENDING,
                "needs": None,
                "step": "",
                "message": "",
                "error": "",
                "logs": [],
                "account_created": False,
                "reached_verification": False,
            })
            self.store.touch()
        self._tasks[task_id] = asyncio.create_task(self._run(task_id))
        return True

    def stop(self, task_id: str) -> bool:
        rt = self._runtimes.get(task_id)
        if rt:
            rt.request_stop()
            return True
        t = self._tasks.get(task_id)
        if t and not t.done():
            t.cancel()
            return True
        return False

    def stop_all(self) -> int:
        n = 0
        for tid in list(self._tasks):
            if self.stop(tid):
                n += 1
        return n

    def provide_card(self, task_id: str, card: dict) -> bool:
        rt = self._runtimes.get(task_id)
        if not rt:
            return False
        rt.provide_card(card)
        return True

    def manual_done(self, task_id: str) -> bool:
        rt = self._runtimes.get(task_id)
        if not rt:
            return False
        rt.manual_done()
        return True

    # ------------------------------------------------------------------
    # 实时接管（受 manual_takeover 开关控制）
    # ------------------------------------------------------------------

    def _takeover_page(self, task_id: str) -> Page | None:
        if not self.settings.get("manual_takeover", True):
            return None
        rt = self._runtimes.get(task_id)
        if not rt or not rt.page:
            return None
        return rt.active_page

    async def send_click(self, task_id: str, x: float, y: float,
                         double: bool = False) -> bool:
        page = self._takeover_page(task_id)
        if page is None:
            return False
        try:
            await page.mouse.click(x, y, click_count=2 if double else 1)
            return True
        except Exception:
            return False

    async def send_key(self, task_id: str, key: str) -> bool:
        page = self._takeover_page(task_id)
        if page is None:
            return False
        try:
            await page.keyboard.press(key)
            return True
        except Exception:
            return False

    async def send_type(self, task_id: str, text: str) -> bool:
        page = self._takeover_page(task_id)
        if page is None:
            return False
        try:
            await page.keyboard.type(text, delay=40)
            return True
        except Exception:
            return False

    async def send_scroll(self, task_id: str, dy: int) -> bool:
        page = self._takeover_page(task_id)
        if page is None:
            return False
        try:
            await page.mouse.wheel(0, dy)
            return True
        except Exception:
            return False

    def screenshot_bytes(self, task_id: str) -> bytes | None:
        rt = self._runtimes.get(task_id)
        return rt._last_shot if rt else None

    async def shutdown(self) -> None:
        for tid in list(self._tasks):
            self.stop(tid)
        for t in list(self._tasks.values()):
            try:
                await asyncio.wait_for(t, timeout=10)
            except Exception:
                pass
        self._tasks.clear()
        self._runtimes.clear()
        await browser_mod.manager.stop()
        await self.skymail.close()

    # ------------------------------------------------------------------

    async def _run(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        sem = self._semaphore()
        async with sem:
            if task.get("status") == STATUS_STOPPED:
                return
            rt = TaskRuntime(task, self)
            self._runtimes[task_id] = rt
            task.update({
                "status": STATUS_RUNNING,
                "started_at": time.time(),
                "finished_at": None,
                "attempts": int(task.get("attempts") or 0) + 1,
                "error": "",
                "needs": None,
            })
            await rt.publish_task()

            try:
                await rt.log(f"开始处理（第 {task['attempts']} 次）")
                await rt.prepare_email()
                await rt.log(f"目标邮箱：{task['email']}")
                await rt.open_browser()
                flow = MuseFlow(rt)
                await asyncio.wait_for(
                    flow.run(),
                    timeout=max(300, int(self.settings.get("code_timeout", 240)) + 420),
                )
                task.update({
                    "status": STATUS_SUCCESS,
                    "needs": None,
                    "step": "完成",
                    "message": "已进入主页",
                    "finished_at": time.time(),
                })
                await rt.log("✅ 全流程完成", "success")
            except FlowStopped as exc:
                task.update({
                    "status": STATUS_STOPPED,
                    "needs": None,
                    "message": str(exc),
                    "finished_at": time.time(),
                })
                await rt.log(f"⏹ {exc}", "warn")
            except asyncio.TimeoutError:
                task.update({
                    "status": STATUS_FAILED,
                    "needs": None,
                    "error": "整体超时",
                    "finished_at": time.time(),
                })
                await rt.log("❌ 任务整体超时", "error")
            except (FlowError, SkymailError) as exc:
                task.update({
                    "status": STATUS_FAILED,
                    "needs": None,
                    "error": str(exc),
                    "finished_at": time.time(),
                })
                await rt.log(f"❌ {exc}", "error")
            except asyncio.CancelledError:
                task.update({
                    "status": STATUS_STOPPED,
                    "needs": None,
                    "message": "已取消",
                    "finished_at": time.time(),
                })
                raise
            except Exception as exc:  # 兜底
                log.exception("任务异常")
                task.update({
                    "status": STATUS_FAILED,
                    "needs": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": time.time(),
                })
                await rt.log(f"❌ 未预期错误：{type(exc).__name__}: {exc}", "error")
            finally:
                self.store.touch()
                await rt.close()
                self._runtimes.pop(task_id, None)
                self._tasks.pop(task_id, None)
                await rt.publish_task()
                await self.store.flush()
