"""FastAPI 入口：REST + WebSocket + 静态控制台。"""
from __future__ import annotations

import asyncio
import base64
import copy
import csv
import hashlib
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
from . import resin as resin_mod
from .config import CONSOLE_PASSWORD, CONSOLE_USER, STATIC_DIR, ensure_dirs
from .events import EventBus
from .runner import STATUS_PENDING, STATUS_STOPPED, TaskRunner
from .names import random_full_name
from .selectors import load_overrides
from .skymail import SkymailClient
from .store import Store, new_id
from .util import random_birthday

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
        "resin": runner.resin.describe() if runner.resin else None,
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
    # 这个探测也走 Resin，用的就是正式的身份策略
    client.set_resin(runner.resin, email)
    try:
        info = await client.me()
        accounts = await client.list_accounts()
        domains = await client.list_domains()
        result = {
            "ok": True,
            "user": info.get("name") or info.get("email"),
            "domains": domains,
            "accounts": [
                {"accountId": a.get("accountId"), "email": a.get("email")}
                for a in accounts
            ],
        }
        # 探一下 /api/email/list 是否可用（有些实例是坏的，恒返 500 D1_TYPE_ERROR）
        if accounts and accounts[0].get("accountId"):
            try:
                await client.list_emails(int(accounts[0]["accountId"]), size=1, full=0)
                result["email_list_ok"] = True
            except Exception as exc:
                result["email_list_ok"] = False
                result["email_list_error"] = str(exc)[:160]
        # 顺手验证「自动建邮箱」是否可用
        if payload.get("create"):
            created = await client.create_random_email()
            result["created"] = created
        return result
    except Exception as exc:
        raise HTTPException(400, f"连接失败：{exc}") from exc
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Resin 代理池
# ----------------------------------------------------------------------


@app.post("/api/resin/test")
async def api_resin_test(payload: dict | None = Body(default=None),
                         _: None = Depends(auth)) -> dict:
    """探测 Resin 连通性与粘性。

    反向代理与正向代理各打一次 IP 回显，并验证：
      - 同一身份连续两次是否拿到同一个 IP（粘性）
      - 反代与正代是否落在同一个出口 IP
    """
    settings = store.get_settings()
    payload = payload or {}
    merged = dict(settings)
    for key in ("resin_url", "resin_platform_name"):
        if payload.get(key) is not None:
            merged[key] = payload[key]
    merged["resin_enabled"] = True
    try:
        cfg = resin_mod.from_settings(merged)
    except resin_mod.ResinError as exc:
        raise HTTPException(400, str(exc)) from exc
    if cfg is None:
        raise HTTPException(400, "请先填写 resin_url")

    account = str(payload.get("account") or settings.get("skymail_email") or "probe")
    out: dict = {"config": cfg.describe(), "account": account}

    try:
        first = await resin_mod.probe(cfg, account, "reverse")
        second = await resin_mod.probe(cfg, account, "reverse")
        out["reverse"] = {"ip": first["ip"], "sticky": first["ip"] == second["ip"]}
    except Exception as exc:
        out["reverse_error"] = str(exc)[:200]

    try:
        fwd = await resin_mod.probe(cfg, account, "forward")
        out["forward"] = {"ip": fwd["ip"]}
    except Exception as exc:
        out["forward_error"] = str(exc)[:200]

    if "reverse" in out and "forward" in out:
        out["same_ip"] = out["reverse"]["ip"] == out["forward"]["ip"]

    # 认证类错误给个针对性提示：Resin 的管理端令牌与代理令牌是两回事
    blob = " ".join(str(out.get(k, "")) for k in ("reverse_error", "forward_error"))
    if "AUTH_FAILED" in blob or "407" in blob or "403" in blob:
        out["hint"] = (
            "认证失败。resin_url 末尾的 Token 必须是「代理令牌」"
            "（服务端 RESIN_PROXY_TOKEN），不是控制台登录用的「管理端令牌」"
            "（RESIN_ADMIN_TOKEN），两者不同。"
            "到 Resin 面板用管理令牌登录 →「接入」页填入代理令牌，"
            "或从 Resin 部署的 RESIN_PROXY_TOKEN 环境变量取值。"
            "若该部署本就未设代理令牌（代理免认证），resin_url 填纯基础地址即可。"
        )
    return out


@app.post("/api/resin/inherit-lease")
async def api_resin_inherit_lease(payload: dict = Body(...),
                                  _: None = Depends(auth)) -> dict:
    """把临时身份的 IP 租约继承给稳定身份。

    本项目正常流程不需要（任务邮箱在第一个请求前就已确定）；
    留给后续「登录前拿不到标识」的场景，别把 TempIdentity 写死。
    """
    cfg = resin_mod.from_settings(store.get_settings())
    if cfg is None:
        raise HTTPException(400, "未启用 Resin 或未配置 resin_url")
    try:
        return await resin_mod.inherit_lease(
            cfg,
            str(payload.get("parent_account") or ""),
            str(payload.get("new_account") or ""),
        )
    except resin_mod.ResinError as exc:
        raise HTTPException(400, str(exc)) from exc


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


@app.post("/api/tasks")
async def api_create_tasks(payload: dict = Body(...), _: None = Depends(auth)) -> dict:
    """新建一批任务。

    不再需要邮箱：每个任务在启动时通过 skymail API 自动创建收件邮箱。
    入参：count（跑多少次）、concurrency（并发数）、card_id、live_view、autostart
    """
    try:
        count = int(payload.get("count") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "「跑多少次」必须是数字") from None
    if not 1 <= count <= 500:
        raise HTTPException(400, "「跑多少次」需在 1–500 之间")

    # 并发数与实时画面开关顺手写回全局设置
    patch: dict = {}
    if payload.get("concurrency") is not None:
        try:
            patch["concurrency"] = max(1, min(int(payload["concurrency"]), 8))
        except (TypeError, ValueError):
            pass
    if payload.get("live_view") is not None:
        patch["live_view"] = bool(payload["live_view"])
    if patch:
        await store.update_settings(patch)
        await runner.refresh_settings()
        bus.publish({"type": "settings", "settings": _public_settings(store.get_settings())})

    settings = store.get_settings()
    card_id = str(payload.get("card_id") or "")
    auto_card = card_id == "auto"
    if auto_card:
        card_id = ""
    cards = [c["id"] for c in store.list_cards()]

    tasks: list[dict] = []
    for i in range(count):
        cid = card_id
        if auto_card and cards:
            cid = cards[i % len(cards)]
        first, last = random_full_name()
        tasks.append({
            "id": new_id("t_"),
            "email": "",                      # 启动时通过 skymail API 生成
            "status": STATUS_PENDING,
            "needs": None,
            "step": "",
            "message": "",
            "error": "",
            "birthday": random_birthday(settings),
            "first_name": first,               # 注册补全页出现「名/姓」时用
            "last_name": last,
            "invite_code": str(settings.get("invite_code") or ""),
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
    return {
        "created": len(tasks),
        "tasks": [copy.deepcopy(t) for t in tasks],
        "concurrency": store.get_settings().get("concurrency", 1),
    }


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
            # 先 accept 再以 4401 关闭，客户端才能拿到关闭码并重取 token；
            # 直接 close 会变成 HTTP 403，前端无法区分“鉴权失败”和“服务没起来”
            await ws.accept()
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
            "resin": runner.resin.describe() if runner.resin else None,
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


def _asset_version() -> str:
    """静态资源的版本号（内容哈希）。

    为什么需要：Cloudflare 默认会按扩展名缓存 .js/.css（4 小时）。
    发布新版本后浏览器/边缘节点拿到的还是旧文件，表现就是
    「按钮点了没反应」（旧 JS 里没有新的处理逻辑）。
    把哈希拼到 URL 后面，内容一变 URL 就变，缓存自然失效。
    """
    digest = hashlib.sha1()
    for name in ("app.js", "style.css"):
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


@app.get("/")
async def index_page() -> HTMLResponse:
    """动态渲染 index.html，注入资源版本号。

    HTML 本身不带扩展名，Cloudflare 默认不缓存，所以每次都能拿到新版本号。
    """
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>static/index.html 缺失</h1>", status_code=500)
    html = html_path.read_text("utf-8").replace("__ASSET_VERSION__", _asset_version())
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:  # pragma: no cover
    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse("<h1>static/ 目录缺失</h1>")
