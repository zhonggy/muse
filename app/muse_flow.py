"""muse.ai「邮箱 + 验证码」注册/登录全流程自动化。

严格对齐 muse-ai-login-sop.md 的实测结论：
  1. 打开 muse.ai
  2. 点「使用手机号或邮箱」展开表单
  3. 填邮箱（SPA 受控输入，需要补派发 input/change）
  4. 点 button[type=submit]（继续）—— URL 不变，只能看界面
  5. 填 6 位验证码（满 6 位自动提交，注意前导零）
  6. 新邮箱 → 生日页：3 个 Radix Select（必须完整 MouseEvent 序列）
  7. 提交生日 → /access/disclosure（期间页面上下文会短暂销毁）
  8. 点「开始」（该页有两个 submit，必须按文案精确匹配）
  9. /access/verification → 点「验证年龄」→ 结账页填卡 → 完成后回主页
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from .js_helpers import CARD_PROBE_JS
from .selectors import SELECTORS, TEXTS, URLS
from .util import random_birthday

MONTH_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


class FlowStopped(Exception):
    """用户主动停止任务。"""


class FlowError(Exception):
    """流程失败（可重试）。"""


class FlowNeedsHelp(Exception):
    """需要人工介入（暂停等待控制台操作）。"""


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


class MuseFlow:
    def __init__(self, rt: Any) -> None:
        self.rt = rt

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    @property
    def page(self) -> Page:
        return self.rt.page

    async def log(self, msg: str, level: str = "info") -> None:
        await self.rt.log(msg, level)

    async def check(self) -> None:
        await self.rt.check_stop()

    async def settle(self, ms: int = 800, label: str = "") -> None:
        await self.page.wait_for_timeout(ms)
        await self.check()

    async def describe(self) -> dict:
        try:
            return await self.page.evaluate("() => window.__museHelpers.describe()")
        except Exception:
            return {"url": "", "title": "", "body": "", "inputs": 0,
                    "buttons": 0, "comboboxes": 0, "options": 0}

    async def locate(self, key: str, timeout_ms: int = 10000) -> Locator | None:
        """按 SELECTORS[key] 轮询找第一个可见元素（Playwright 选择器自动穿透 shadow DOM）。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            await self.check()
            for sel in SELECTORS.get(key, []):
                try:
                    loc = self.page.locator(sel)
                    count = await loc.count()
                except Exception:
                    continue
                for i in range(min(count, 10)):
                    el = loc.nth(i)
                    try:
                        if await el.is_visible():
                            return el
                    except Exception:
                        continue
            if time.monotonic() >= deadline:
                return None
            await self.page.wait_for_timeout(300)

    # ------------------------------------------------------------------
    # 点击 / 输入
    # ------------------------------------------------------------------

    async def _try_click(self, label: str, exact: bool = True) -> bool:
        # a) Playwright 语义定位
        try:
            loc = self.page.get_by_role("button", name=label, exact=exact)
            count = await loc.count()
            for i in range(min(count, 6)):
                el = loc.nth(i)
                try:
                    if await el.is_visible() and await el.is_enabled():
                        await el.click(timeout=6000)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        # b) JS 全量扫描（含 shadow DOM），派发完整鼠标事件序列
        try:
            ok = await self.page.evaluate(
                "([sel, label, exact]) => window.__museHelpers.clickByText(sel, label, exact)",
                [
                    "button, [role='button'], input[type='submit'], "
                    "a[role='button'], [role='tab'], [role='option'], [role='menuitem']",
                    label,
                    exact,
                ],
            )
            if ok:
                return True
        except Exception:
            pass
        # c) has-text 兜底
        try:
            loc = self.page.locator(f'button:has-text("{label}")')
            if await loc.count():
                el = loc.first
                if await el.is_visible():
                    await el.click(timeout=6000)
                    return True
        except Exception:
            pass
        return False

    async def click_button(
        self,
        texts_key: str,
        timeout_ms: int = 20000,
        exact: bool = True,
        required: bool = True,
        labels: list[str] | None = None,
    ) -> bool:
        candidates = labels or TEXTS.get(texts_key, [])
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            await self.check()
            for label in candidates:
                if await self._try_click(label, exact):
                    await self.log(f"已点击「{label}」")
                    return True
            if time.monotonic() >= deadline:
                if required:
                    raise FlowError(
                        f"找不到按钮 {candidates}（当前页面：{await self._page_brief()}）"
                    )
                return False
            await self.page.wait_for_timeout(400)

    async def click_locator(self, el: Locator, label: str = "") -> None:
        try:
            await el.click(timeout=8000)
        except PWTimeout:
            # 被遮挡时退回 JS 真实事件序列
            try:
                await el.evaluate("(el) => window.__museHelpers.realClick(el)")
            except Exception as exc:
                raise FlowError(f"点击失败 {label}: {exc}") from exc
        if label:
            await self.log(f"已点击「{label}」")

    async def fill_field(self, key: str, value: str, label: str,
                         timeout_ms: int = 20000) -> None:
        el = await self.locate(key, timeout_ms=timeout_ms)
        if el is None:
            raise FlowError(f"找不到{label}输入框（{SELECTORS.get(key)}）")
        try:
            await el.click(timeout=5000)
        except Exception:
            pass
        try:
            await el.fill("")
        except Exception:
            pass
        # 逐字符输入：保证框架能感知每一次 input
        try:
            await el.press_sequentially(value, delay=50)
        except Exception:
            try:
                await el.type(value, delay=50)  # 旧版兜底
            except Exception:
                pass
        got = ""
        try:
            got = (await el.input_value()).strip()
        except Exception:
            pass
        if got != value:
            # 兜底：原生 setter + input/change
            try:
                await el.evaluate(
                    "(el, v) => window.__museHelpers.setValue(el, v)", value
                )
                got = (await el.input_value()).strip()
            except Exception:
                pass
        if got != value:
            raise FlowError(f"{label}写入失败：期望 {value!r}，实际 {got!r}")
        await self.log(f"{label}已填入：{value}")

    # ------------------------------------------------------------------
    # Radix Select
    # ------------------------------------------------------------------

    async def combo_labels(self) -> list[str]:
        try:
            return await self.page.evaluate(
                "() => window.__museHelpers.comboboxLabels()"
            )
        except Exception:
            return []

    def _classify_combos(self, labels: list[str]) -> dict[str, int]:
        idx = {"year": 0, "month": 1, "day": 2}
        found: dict[str, int] = {}
        for i, label in enumerate(labels):
            low = (label or "").lower()
            if "year" in low or "年" in low:
                found.setdefault("year", i)
            elif "month" in low or "月" in low:
                found.setdefault("month", i)
            elif "day" in low or "日" in low or "天" in low:
                found.setdefault("day", i)
        for k, v in found.items():
            idx[k] = v
        return idx

    async def _click_option(self, candidates: list[str]) -> str | None:
        for cand in candidates:
            # a) 显式 [role="option"]（排除 Radix 隐藏的原生 <option>）
            try:
                opts = self.page.locator(SELECTORS["option"][0])
                count = await opts.count()
                for i in range(min(count, 400)):
                    el = opts.nth(i)
                    try:
                        txt = (await el.inner_text()).strip()
                    except Exception:
                        continue
                    if txt == cand or _norm(txt) == _norm(cand):
                        await el.click(timeout=6000)
                        return cand
            except Exception:
                pass
            # b) JS 完整事件序列
            try:
                if await self.page.evaluate(
                    "(l) => window.__museHelpers.clickOption(l)", cand
                ):
                    return cand
            except Exception:
                pass
        return None

    async def select_combo(self, index: int, candidates: list[str],
                           label: str = "") -> str:
        combos = self.page.locator(SELECTORS["combobox"][0])
        try:
            combo = combos.nth(index)
            await combo.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            raise FlowError(f"找不到第 {index + 1} 个下拉框（{label}）") from exc

        for attempt in range(2):
            await self.click_locator(combo, f"{label}下拉")
            await self.page.wait_for_timeout(450)
            picked = await self._click_option(candidates)
            if picked:
                await self.page.wait_for_timeout(300)
                await self.log(f"{label}已选择：{picked}")
                return picked
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await self.page.wait_for_timeout(300)
        raise FlowError(f"{label}下拉选择失败，候选：{candidates}")

    async def set_birthday(self, birthday: str) -> None:
        year, month, day = self._parse_birthday(birthday)
        labels = await self.combo_labels()
        idx = self._classify_combos(labels)
        await self.log(
            f"生日下拉识别：{labels or '（无标签，按 年/月/日 顺序）'} → {idx}"
        )

        y, m, d = str(year), str(month), str(day)
        await self.select_combo(
            idx["year"], [y, f"{y} 年", f"{y}年", f"{y} 年（{y}）"], "年"
        )
        await self.page.wait_for_timeout(400)
        await self.select_combo(
            idx["month"],
            [f"{m} 月", f"{m}月", m, MONTH_EN[month - 1], f"{month}月"],
            "月",
        )
        await self.page.wait_for_timeout(400)
        await self.select_combo(idx["day"], [d, f"{d} 日", f"{d}日", f"{d} 号"], "日")

    @staticmethod
    def _parse_birthday(birthday: str) -> tuple[int, int, int]:
        raw = (birthday or "").strip().replace("/", "-").replace(".", "-")
        parts = [p for p in raw.split("-") if p.strip()]
        if len(parts) == 3:
            try:
                return int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                pass
        return 1996, 7, 22

    # ------------------------------------------------------------------
    # 阶段判定
    # ------------------------------------------------------------------

    async def _page_brief(self) -> str:
        info = await self.describe()
        body = (info.get("body") or "")[:120].replace("\n", " ")
        return f"{info.get('url', '')[:90]} | {body}"

    async def wait_stage(self, timeout_ms: int, want: list[str]) -> str:
        """轮询直到进入 want 中的任一阶段。"""
        deadline = time.monotonic() + timeout_ms / 1000
        last_log = 0.0
        while True:
            await self.check()
            stage = await self.detect_stage()
            if stage in want:
                await self.log(f"进入阶段：{stage}")
                return stage
            if stage == "blocked":
                raise FlowError("页面提示需要启用弹窗或出现未知拦截")
            now = time.monotonic()
            if now - last_log > 6:
                info = await self.describe()
                await self.log(
                    f"等待 {want} … 当前 {info.get('url', '')[:70]} | "
                    f"{(info.get('body') or '')[:80]}",
                    "debug",
                )
                last_log = now
            if now >= deadline:
                raise FlowError(
                    f"等待 {want} 超时（{timeout_ms // 1000}s），当前：{await self._page_brief()}"
                )
            await self.page.wait_for_timeout(700)

    async def detect_stage(self) -> str:
        info = await self.describe()
        url = (info.get("url") or "").lower()
        body = info.get("body") or ""
        if URLS["verification"] in url:
            return "verification"
        if URLS["disclosure"] in url or "须知事项" in body:
            return "disclosure"
        if info.get("comboboxes", 0) >= 3 and "生日" in body:
            return "birthday"
        if info.get("comboboxes", 0) >= 3 and info.get("inputs", 0) == 0:
            return "birthday"
        if "验证码" in body or "安全码" in body:
            return "code"
        if "无法打开结账" in body or "启用弹窗" in body:
            return "blocked"
        if "生日" in body:
            return "birthday"
        if "登录或创建账户" in body or "使用手机号或邮箱" in body:
            return "login"
        if url.rstrip("/") in ("https://muse.ai", "https://www.muse.ai"):
            if "须知事项" in body or "开始" in body:
                return "disclosure"
            return "home"
        return "unknown"

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self.step_open()
        await self.step_expand_login()
        await self.step_email()
        await self.step_code()
        stage = await self.wait_stage(120_000, ["birthday", "disclosure", "verification"])
        if stage == "birthday":
            await self.step_birthday()
            await self.wait_stage(180_000, ["disclosure", "verification"])
        await self.step_disclosure()
        await self.step_age_verification()

    # --- 1 ---

    async def step_open(self) -> None:
        await self.rt.set_step("打开站点")
        url = self.rt.settings.get("muse_url") or "https://muse.ai/"
        await self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await self.settle(2500)
        await self.log(f"已打开 {url}（标题：{await self.page.title()}）")
        await self.rt.snap("open")

    # --- 2 ---

    async def step_expand_login(self) -> None:
        await self.rt.set_step("展开登录表单")
        if await self.locate("email_input", timeout_ms=2000):
            await self.log("邮箱输入框已可见，跳过展开步骤")
            return
        if not await self.click_button("expand_login", timeout_ms=15000, required=False):
            await self.log("未找到「使用手机号或邮箱」按钮，可能已直接进入表单", "warn")
        await self.settle(1200)
        await self.rt.snap("login-form")

    # --- 3 ---

    async def step_email(self) -> None:
        await self.rt.set_step("填写邮箱")
        email = self.rt.task["email"]
        await self.rt.capture_mail_baseline()
        await self.fill_field("email_input", email, "邮箱")
        await self.rt.snap("email-filled")

    # --- 4/5 ---

    async def step_code(self) -> None:
        await self.rt.set_step("提交邮箱并等待验证码")
        await self.click_button("continue", timeout_ms=20000)
        await self.settle(1500)

        if await self.locate("code_input", timeout_ms=20000) is None:
            raise FlowError(f"未出现验证码输入框：{await self._page_brief()}")
        await self.log("已进入验证码界面，开始轮询 skymail 收件箱")
        await self.rt.snap("code-page")

        await self.rt.set_step("获取验证码")
        code = await self.rt.fetch_code()
        await self.log(f"取到验证码：{code}（按字符串写入，保留前导零）")

        await self.rt.set_step("输入验证码")
        await self.fill_field("code_input", code, "验证码")
        await self.log("已输入 6 位验证码，页面会自动提交")
        await self.rt.snap("code-filled")
        await self.page.wait_for_timeout(3000)

    # --- 6/7 ---

    async def step_birthday(self) -> None:
        await self.rt.set_step("填写生日")
        await self.log("检测到注册补全页（请输入生日）")
        await self.rt.snap("birthday-page")
        birthday = self.rt.task.get("birthday") or random_birthday(self.rt.settings)
        await self.set_birthday(birthday)
        await self.settle(800)
        await self.rt.snap("birthday-filled")

        await self.rt.set_step("提交生日")
        await self.click_button("confirm", timeout_ms=20000)
        await self.log("已提交生日，等待账户创建（约 15–20 秒）")

        # 期间页面上下文可能短暂销毁，容忍报错
        for _ in range(60):
            await self.check()
            try:
                info = await self.describe()
                url = (info.get("url") or "").lower()
                if URLS["disclosure"] in url or URLS["verification"] in url:
                    await self.log(f"账户创建成功，已跳转 {info.get('url')}", "success")
                    await self.rt.mark_account_created()
                    return
                if "无法创建" in (info.get("body") or ""):
                    raise FlowError("页面提示「我们无法创建你的账户，请重试。」")
            except FlowError:
                raise
            except Exception:
                pass  # Execution context destroyed → 正常现象
            await asyncio.sleep(1.5)
        raise FlowError("提交生日后 90 秒内未跳转，可能账户创建失败")

    # --- 8 ---

    async def step_disclosure(self) -> None:
        await self.rt.set_step("同意须知事项")
        await self.rt.snap("disclosure")
        # 该页有两个 button[type=submit]（开始 / 设置），必须按文案精确匹配
        await self.click_button("disclosure_start", timeout_ms=30000, exact=True)
        await self.log("已点击「开始」，代表同意 Muse 条款 / AI 条款 / 隐私政策")
        await self.wait_stage(60_000, ["verification", "home"])

    # --- 9 ---

    async def step_age_verification(self) -> None:
        await self.rt.set_step("年龄验证")
        await self.rt.snap("verification")
        await self.rt.mark_reached_verification()

        if self.rt.settings.get("stop_at_verification"):
            await self.log("配置为「到年龄验证即停止」，保存登录态后结束", "warn")
            await self.rt.save_session()
            return

        new_pages: list[Page] = []

        def _on_page(p: Page) -> None:
            new_pages.append(p)

        self.page.context.on("page", _on_page)
        await self.click_button("verify_age", timeout_ms=30000)
        await self.page.wait_for_timeout(4000)

        checkout: Page | None = next(
            (p for p in new_pages if not p.is_closed()), None
        )
        if checkout is None:
            await self.log("未弹出结账标签页，尝试兜底按钮「打开安全结账」", "warn")
            if await self.click_button("open_checkout", timeout_ms=15000, required=False):
                await self.page.wait_for_timeout(4000)
                checkout = next((p for p in new_pages if not p.is_closed()), None)

        target = checkout or self.page
        if checkout:
            await self.log(f"结账页已在标签页打开：{checkout.url}")
            try:
                await checkout.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass

        await self.rt.snap("checkout", page=target)

        card = await self.rt.resolve_card()
        filled = await self.fill_card_form(target, card)
        if not filled:
            await self.log("未能自动识别支付表单字段", "warn")
            await self.rt.wait_for_manual(
                "未识别到支付表单，请在控制台实时画面上手动完成绑卡"
            )
        else:
            await self.rt.snap("card-filled", page=target)
            await self.rt.set_step("提交支付")
            submitted = await self.click_card_submit(target)
            if not submitted:
                await self.rt.wait_for_manual("未找到提交按钮，请手动点击提交")

        await self.wait_verification_done(target)

    # ------------------------------------------------------------------
    # 支付表单
    # ------------------------------------------------------------------

    async def fill_card_form(self, page: Page, card: dict) -> bool:
        for frame in page.frames:
            try:
                info = await frame.evaluate(CARD_PROBE_JS, {})
            except Exception:
                continue
            if not info or not info.get("marked"):
                continue
            await self.log(f"在 {frame.url[:80] or 'main'} 发现支付字段：{info['marked']}")
            try:
                await self._fill_marked(frame, card, info)
                return True
            except Exception as exc:
                await self.log(f"填写支付字段失败：{exc}", "warn")
        return False

    async def _fill_marked(self, frame: Any, card: dict, info: dict) -> None:
        marked = set(info.get("marked") or [])

        async def put(kind: str, value: str, delay: int = 60) -> bool:
            if kind not in marked or not value:
                return False
            loc = frame.locator(f'[data-muse-fill="{kind}"]').first
            try:
                await loc.click(timeout=6000)
            except Exception:
                pass
            try:
                await loc.fill("")
            except Exception:
                pass
            await loc.press_sequentially(value, delay=delay)
            return True

        if not await put("number", card["number"]):
            raise FlowError("卡号字段填写失败")

        if info.get("splitExp"):
            await self._fill_split_expiry(frame, card, marked)
        else:
            await put("exp", f'{card["exp_month"]}{card["exp_year"][-2:]}')

        await put("cvc", card["cvc"])
        if not await put("postal", card.get("postal", "")):
            pass
        await put("name", card.get("holder", ""))
        await put("address", (card.get("extra") or {}).get("address", ""))
        await put("city", (card.get("extra") or {}).get("city", ""))
        await put("state", (card.get("extra") or {}).get("state", ""))
        await self.log("支付表单已填写（卡号/CVV 不写入日志）")

    async def _fill_split_expiry(self, frame: Any, card: dict, marked: set[str]) -> None:
        month = str(int(card["exp_month"]))
        year2 = card["exp_year"][-2:]
        year4 = card["exp_year"]

        async def put(kind: str, value: str) -> bool:
            """同时兼容 <select> 与 <input> 两种分体式有效期。"""
            if kind not in marked or not value:
                return False
            loc = frame.locator(f'[data-muse-fill="{kind}"]').first
            try:
                tag = await loc.evaluate("el => el.tagName")
            except Exception:
                return False
            if tag == "SELECT":
                for cand in (value, value.zfill(2), f"{value} 月", f"{value}月"):
                    for by in ("label", "value"):
                        try:
                            await loc.select_option(**{by: cand}, timeout=4000)
                            return True
                        except Exception:
                            continue
                return False
            try:
                await loc.click(timeout=6000)
            except Exception:
                pass
            try:
                await loc.fill("")
            except Exception:
                pass
            await loc.press_sequentially(value, delay=60)
            return True

        ok_m = await put("exp-month", month)
        ok_y = await put("exp-year", year4) or await put("exp-year", year2)
        if not (ok_m and ok_y):
            await self.log("分体式有效期填写不完整，退回单输入框尝试", "warn")
            try:
                loc = frame.locator('[data-muse-fill="exp"]').first
                await loc.click(timeout=4000)
                await loc.press_sequentially(f"{month}{year2}", delay=60)
            except Exception:
                pass

    async def click_card_submit(self, page: Page) -> bool:
        candidates = TEXTS["card_submit"]
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            await self.check()
            for label in candidates:
                for frame in page.frames:
                    try:
                        loc = frame.get_by_role("button", name=label, exact=False)
                        count = await loc.count()
                    except Exception:
                        continue
                    for i in range(min(count, 5)):
                        el = loc.nth(i)
                        try:
                            if await el.is_visible() and await el.is_enabled():
                                await el.click(timeout=6000)
                                await self.log(f"已点击支付提交按钮「{label}」")
                                return True
                        except Exception:
                            continue
            await page.wait_for_timeout(600)
        return False

    # ------------------------------------------------------------------
    # 等待验证完成
    # ------------------------------------------------------------------

    async def wait_verification_done(self, checkout: Page) -> None:
        await self.rt.set_step("等待年龄验证完成")
        deadline = time.monotonic() + 300
        manual_prompted = False
        while time.monotonic() < deadline:
            await self.check()

            # 3DS / 额外验证：交给人工
            if not manual_prompted and await self._needs_manual(checkout):
                manual_prompted = True
                await self.log("检测到 3DS / 二次验证页面", "warn")
                await self.rt.wait_for_manual(
                    "检测到 3DS 或二次验证，请在控制台实时画面上手动完成"
                )

            if checkout is not self.page:
                if checkout.is_closed():
                    await self.log("结账标签页已关闭", "success")
                    break
            info = await self.describe()
            url = (info.get("url") or "").lower()
            if URLS["verification"] not in url and "/access/" not in url:
                await self.log(f"已离开验证页：{info.get('url')}", "success")
                break
            if "验证成功" in (info.get("body") or "") or "已完成" in (info.get("body") or ""):
                await self.log("页面提示验证完成", "success")
                break
            await asyncio.sleep(2)

        # 回到主页
        for _ in range(60):
            await self.check()
            if checkout is not self.page and not checkout.is_closed():
                try:
                    await checkout.close()
                except Exception:
                    pass
            await self.page.bring_to_front()
            info = await self.describe()
            url = (info.get("url") or "").lower()
            if "/access/" not in url:
                break
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            await asyncio.sleep(2)

        await self.rt.snap("done")
        await self.rt.save_session()
        await self.log("年龄验证流程结束，登录态已保存", "success")

    async def _needs_manual(self, page: Page) -> bool:
        try:
            for frame in page.frames:
                url = (frame.url or "").lower()
                if any(k in url for k in ("acs.", "3ds", "challenge", "authentication")):
                    return True
        except Exception:
            pass
        try:
            body = (await page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
            )) or ""
        except Exception:
            return False
        markers = ("3D Secure", "3DS", "短信验证码", "银行验证", "发卡行验证",
                   "Verify your identity", "authentication code")
        return any(m in body for m in markers)
