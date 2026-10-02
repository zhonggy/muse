"""FastAPI 入口：REST + WebSocket + 静态控制台。"""
from __future__ import annotations

import asyncio
import base64
import copy
import csv
import io
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from . import browser as browser_mod
from .config import CONSOLE_PASSWORD, CONSOLE_USER, STATIC_DIR, ensure_dirs
from .events import EventBus
from .runner import STATUS_PENDING, STATUS_STOPPED, TaskRunner
from .selectors import load_overrides
from .skymail import SkymailClient
from .store import Store, new_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("muse")

ensure_dirs()
load_overrides()

store = Store()
bus = EventBus()
runner = TaskRunner(store, bus)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.load()
    await runner.refresh_settings()
    flusher = asyncio.create_task(_flush_loop())
    log.info("muse-auto 控制台已就绪")
    try:
        yield
    finally:
        flusher.cancel()
        try:
            await runner.shutdown()
        except Exception:
            log.exception("关闭时出错")
        await store.save()


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(3)
        try:
            await store.flush()
        except Exception:
            log.exception("落盘失败")


app = FastAPI(title="muse-auto", version="1.0.0", lifespan=lifespan)

security = HTTPBasic(auto_error=False)


def auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not CONSOLE_PASSWORD:
        return
    if credentials is None:
        raise HTTPException(401, "需要登录", headers={"WWW-Authenticate": "Basic"})
    ok_user = secrets.compare_digest(credentials.username, CONSOLE_USER)
    ok_pass = secrets.compare_digest(credentials.password, CONSOLE_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(401, "用户名或密码错误",
                            headers={"WWW-Authenticate": "Basic"})


# ----------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------


@app.get("/api/state")
async def api_state(_: None = Depends(auth)) -> dict:
    return {
        "settings": _public_settings(store.get_settings()),
        "cards": store.list_cards(),
        "tasks": [copy.deepcopy(t) for t in store.list_tasks()],
        "running": runner.active_ids,
        "skymail_configured": bool(
            store.get_settings().get("skymail_email")
            and store.get_settings().get("skymail_password")
        ),
    }


def _public_settings(settings: dict) -> dict:
    out = dict(settings)
    if out.get("skymail_password"):
        out["skymail_password_set"] = True
        out["skymail_password"] = "********"
    else:
        out["skymail_password_set"] = False
    return out


@app.get("/api/settings")
async def api_get_settings(_: None = Depends(auth)) -> dict:
    return _public_settings(store.get_settings())


@app.put("/api/settings")
async def api_put_settings(payload: dict = Body(...), _: None = Depends(auth)) -> dict:
    patch = dict(payload)
    # 前端回传掩码时忽略
    if patch.get("skymail_password") in ("********", None):
        patch.pop("skymail_password", None)
    patch.pop("skymail_password_set", None)
    settings = await store.update_settings(patch)
    await runner.refresh_settings()
    bus.publish({"type": "settings", "settings": _public_settings(settings)})
    return _public_settings(settings)


@app.post("/api/skymail/test")
async def api_skymail_test(payload: dict | None = Body(default=None),
                           _: None = Depends(auth)) -> dict:
    settings = store.get_settings()
    payload = payload or {}
    base = (payload.get("skymail_base_url") or settings["skymail_base_url"]).rstrip("/")
    email = payload.get("skymail_email") or settings["skymail_email"]
    password = payload.get("skymail_password") or settings["skymail_password"]
    if password == "********":
        password = settings["skymail_password"]
    if not email or not password:
        raise HTTPException(400, "请先填写 skymail 邮箱和密码")
    client = SkymailClient(base, email, password)
    try:
        info = await client.me()
        accounts = await client.list_accounts()
        return {
            "ok": True,
            "user": info.get("name") or info.get("email"),
            "accounts": [
                {"accountId": a.get("accountId"), "email": a.get("email")}
                for a in accounts
            ],
        }
    except Exception as exc:
        raise HTTPException(400, f"连接失败：{exc}") from exc
    finally:
        await client.close()


# ----------------------------------------------------------------------
# 卡片
# ----------------------------------------------------------------------


@app.get("/api/cards")
async def api_cards(_: None = Depends(auth)) -> list[dict]:
    return store.list_cards()


@app.post("/api/cards")
async def api_add_card(payload: dict = Body(...), _: None = Depends(auth)) -> dict:
    number = "".join(ch for ch in str(payload.get("number", "")) if ch.isdigit())
    if len(number) < 12:
        raise HTTPException(400, "卡号无效")
    if not payload.get("exp_month") or not payload.get("exp_year"):
        raise HTTPException(400, "请填写有效期")
    card = await store.add_card(payload)
    bus.publish({"type": "cards", "cards": store.list_cards()})
    return card


@app.put("/api/cards/{card_id}")
async def api_update_card(card_id: str, payload: dict = Body(...),
                          _: None = Depends(auth)) -> dict:
    card = await store.update_card(card_id, payload)
    if card is None:
        raise HTTPException(404, "卡片不存在")
    bus.publish({"type": "cards", "cards": store.list_cards()})
    return card


@app.delete("/api/cards/{card_id}")
async def api_delete_card(card_id: str, _: None = Depends(auth)) -> dict:
    ok = await store.delete_card(card_id)
    if not ok:
        raise HTTPException(404, "卡片不存在")
    bus.publish({"type": "cards", "cards": store.list_cards()})
    return {"ok": True}


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------


def _parse_emails(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = raw.replace(",", "\n").replace(";", "\n").replace(" ", "\n").split("\n")
    else:
        parts = [str(x) for x in (raw or [])]
    seen: list[str] = []
    for p in parts:
        e = p.strip()
        if e and "@" in e and e.lower() not in {x.lower() for x in seen}:
            seen.append(e)
    return seen


@app.post("/api/tasks")
async def api_create_tasks(payload: dict = Body(...), _: None = Depends(auth)) -> dict:
    emails = _parse_emails(payload.get("emails"))
    if not emails:
        raise HTTPException(400, "请至少提供一个邮箱")
    settings = store.get_settings()
    birthday = payload.get("birthday") or settings["birthday"]
    card_id = payload.get("card_id") or ""
    if card_id == "auto":
        card_id = ""
    auto_card = payload.get("card_id") == "auto"

    cards = [c["id"] for c in store.list_cards()]
    tasks: list[dict] = []
    for i, email in enumerate(emails):
        cid = card_id
        if auto_card and cards:
            cid = cards[i % len(cards)]
        tasks.append({
            "id": new_id("t_"),
            "email": email,
            "status": STATUS_PENDING,
            "needs": None,
            "step": "",
            "message": "",
            "error": "",
            "birthday": birthday,
            "card_id": cid,
            "code": "",
            "logs": [],
            "attempts": 0,
            "account_created": False,
            "reached_verification": False,
            "skymail_account_id": None,
            "session_file": "",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
        })
    await store.add_tasks(tasks)
    for t in tasks:
        bus.publish({"type": "task", "task": copy.deepcopy(t)})

    if payload.get("autostart", True):
        for t in tasks:
            runner.start(t["id"])
    return {"created": len(tasks), "tasks": [copy.deepcopy(t) for t in tasks]}


@app.post("/api/tasks/{task_id}/start")
async def api_start_task(task_id: str, _: None = Depends(auth)) -> dict:
    if store.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    if not runner.start(task_id):
        raise HTTPException(409, "任务已在运行")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/stop")
async def api_stop_task(task_id: str, _: None = Depends(auth)) -> dict:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    runner.stop(task_id)
    task["status"] = STATUS_STOPPED
    task["needs"] = None
    task["message"] = "已手动停止"
    await store.flush()
    bus.publish({"type": "task", "task": copy.deepcopy(task)})
    return {"ok": True}


@app.post("/api/tasks/{task_id}/card")
async def api_task_card(task_id: str, payload: dict = Body(...),
                        _: None = Depends(auth)) -> dict:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if payload.get("card_id"):
        card = store.get_card_secret(str(payload["card_id"]))
        if not card:
            raise HTTPException(404, "卡片不存在")
        task["card_id"] = payload["card_id"]
    else:
        card = {
            "id": "adhoc",
            "label": "临时卡",
            "number": "".join(ch for ch in str(payload.get("number", "")) if ch.isdigit()),
            "cvc": str(payload.get("cvc", "")),
            "exp_month": str(payload.get("exp_month", "")).zfill(2),
            "exp_year": str(payload.get("exp_year", "")),
            "holder": payload.get("holder", ""),
            "postal": payload.get("postal", ""),
            "country": payload.get("country", ""),
            "extra": payload.get("extra") or {},
        }
        if payload.get("save_as_card"):
            saved = await store.add_card({**payload, "label": payload.get("label") or ""})
            task["card_id"] = saved["id"]
            bus.publish({"type": "cards", "cards": store.list_cards()})
    if not card.get("number"):
        raise HTTPException(400, "卡信息不完整")
    if not runner.provide_card(task_id, card):
        raise HTTPException(409, "任务当前不在等待卡信息")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/manual-done")
async def api_task_manual_done(task_id: str, _: None = Depends(auth)) -> dict:
    if not runner.manual_done(task_id):
        raise HTTPException(409, "任务当前不在等待人工操作")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry")
async def api_retry_task(task_id: str, _: None = Depends(auth)) -> dict:
    if store.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    if not runner.start(task_id):
        raise HTTPException(409, "任务已在运行")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str, _: None = Depends(auth)) -> dict:
    runner.stop(task_id)
    ok = await store.delete_task(task_id)
    if not ok:
        raise HTTPException(404, "任务不存在")
    await store.flush()
    bus.publish({"type": "tasks_deleted", "ids": [task_id]})
    return {"ok": True}


@app.post("/api/tasks/start-all")
async def api_start_all(payload: dict | None = Body(default=None),
                        _: None = Depends(auth)) -> dict:
    payload = payload or {}
    statuses = payload.get("statuses") or [STATUS_PENDING, "failed", "stopped"]
    n = 0
    for t in store.list_tasks():
        if t["status"] in statuses:
            if runner.start(t["id"]):
                n += 1
    return {"started": n}


@app.post("/api/tasks/stop-all")
async def api_stop_all(_: None = Depends(auth)) -> dict:
    n = runner.stop_all()
    for t in store.list_tasks():
        if t["status"] in ("running", "paused", "pending"):
            t["status"] = STATUS_STOPPED
            t["needs"] = None
            t["message"] = "已手动停止"
            bus.publish({"type": "task", "task": copy.deepcopy(t)})
    await store.flush()
    return {"stopped": n}


@app.post("/api/tasks/clear")
async def api_clear_tasks(payload: dict | None = Body(default=None),
                          _: None = Depends(auth)) -> dict:
    payload = payload or {}
    only_done = payload.get("only_done", True)
    for t in store.list_tasks():
        if t["status"] in ("running", "paused"):
            runner.stop(t["id"])
    statuses = ("success", "failed", "stopped") if only_done else ()
    n = await store.clear_tasks(statuses)
    await store.flush()
    bus.publish({"type": "tasks_cleared"})
    return {"removed": n}


@app.get("/api/tasks/{task_id}/shot")
async def api_task_shot(task_id: str, _: None = Depends(auth)) -> Response:
    data = runner.screenshot_bytes(task_id)
    if not data:
        raise HTTPException(404, "暂无截图")
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/export")
async def api_export(fmt: str = Query("json", alias="format"),
                     _: None = Depends(auth)) -> Response:
    rows = []
    for t in store.list_tasks():
        rows.append({
            "email": t.get("email"),
            "status": t.get("status"),
            "step": t.get("step"),
            "code": t.get("code"),
            "account_created": t.get("account_created"),
            "reached_verification": t.get("reached_verification"),
            "session_file": t.get("session_file"),
            "error": t.get("error"),
            "attempts": t.get("attempts"),
            "started_at": _fmt_ts(t.get("started_at")),
            "finished_at": _fmt_ts(t.get("finished_at")),
        })
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["email"])
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="muse-tasks-{stamp}.csv"'},
        )
    return Response(
        content=json.dumps(rows, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="muse-tasks-{stamp}.json"'},
    )


def _fmt_ts(ts: Any) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


@app.get("/api/ws-token")
async def api_ws_token(_: None = Depends(auth)) -> dict:
    """供前端建立 WebSocket 时使用（Basic 凭据已被浏览器缓存）。"""
    if not CONSOLE_PASSWORD:
        return {"token": ""}
    return {"token": base64.b64encode(
        f"{CONSOLE_USER}:{CONSOLE_PASSWORD}".encode()
    ).decode()}


@app.get("/api/sessions")
async def api_sessions(_: None = Depends(auth)) -> list[dict]:
    from .config import SESSION_DIR

    out = []
    for p in sorted(SESSION_DIR.glob("*.json")):
        out.append({"name": p.name, "size": p.stat().st_size,
                    "mtime": _fmt_ts(p.stat().st_mtime)})
    return out


@app.get("/api/sessions/{name}")
async def api_session_download(name: str, _: None = Depends(auth)) -> Response:
    from .config import SESSION_DIR

    p = (SESSION_DIR / name).resolve()
    if not str(p).startswith(str(SESSION_DIR.resolve())) or not p.exists():
        raise HTTPException(404, "不存在")
    return Response(
        content=p.read_bytes(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


# ----------------------------------------------------------------------
# WebSocket
# ----------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if CONSOLE_PASSWORD:
        # 优先用浏览器缓存的 Basic 凭据（同源 WS 握手会带上 Authorization）
        header = ws.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, CONSOLE_USER) and \
                    secrets.compare_digest(pwd, CONSOLE_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            token = ws.query_params.get("token", "")
            expected = base64.b64encode(
                f"{CONSOLE_USER}:{CONSOLE_PASSWORD}".encode()
            ).decode()
            ok = secrets.compare_digest(token, expected)
        if not ok:
            await ws.close(code=4401)
            return
    await ws.accept()
    queue = await bus.subscribe()
    try:
        await ws.send_json({
            "type": "hello",
            "settings": _public_settings(store.get_settings()),
            "cards": store.list_cards(),
            "tasks": [copy.deepcopy(t) for t in store.list_tasks()],
        })
        receiver = asyncio.create_task(_ws_receiver(ws))
        try:
            while True:
                msg = await queue.get()
                await ws.send_json(msg)
        finally:
            receiver.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("ws 断开：%s", exc)
    finally:
        await bus.unsubscribe(queue)


async def _ws_receiver(ws: WebSocket) -> None:
    """处理来自控制台的实时接管指令（点击 / 输入 / 滚动）。"""
    try:
        while True:
            payload = await ws.receive_json()
            kind = payload.get("type")
            task_id = payload.get("task_id", "")
            if kind == "click":
                await runner.send_click(
                    task_id, float(payload["x"]), float(payload["y"]),
                    bool(payload.get("double")),
                )
            elif kind == "type":
                await runner.send_type(task_id, str(payload.get("text", "")))
            elif kind == "key":
                await runner.send_key(task_id, str(payload.get("key", "Enter")))
            elif kind == "scroll":
                await runner.send_scroll(task_id, int(payload.get("dy", 300)))
            elif kind == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        return
    except Exception:
        return


# ----------------------------------------------------------------------
# 静态资源
# ----------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "ts": time.time(), "browser": bool(
        browser_mod.manager._browser and browser_mod.manager._browser.is_connected()
    )}


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:  # pragma: no cover
    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse("<h1>static/ 目录缺失</h1>")
