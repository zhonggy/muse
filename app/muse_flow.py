"""muse.ai「邮箱 + 验证码」注册/登录全流程自动化。

严格对齐 muse-ai-login-sop.md 的实测结论：
  1. 打开 muse.ai
  2. 点「使用手机号或邮箱」展开表单
  3. 填邮箱（SPA 受控输入，需要补派发 input/change）
  4. 点 button[type=submit]（继续）—— URL 不变，只能看界面
  5. 填 6 位验证码（满 6 位自动提交，注意前导零）
  6. 注册补全页：有时会多出「名 / 姓」两栏，从英文姓名池随机组合填入
  7. 生日：3 个 Radix Select（必须完整 MouseEvent 序列），年份在 1995–2002 随机
  8. 提交 → /access/disclosure（期间页面上下文会短暂销毁）
  9. 点「开始」（该页有两个 submit，必须按文案精确匹配）
 10. /access/verification → 点「验证年龄」→ 结账页填卡 → 完成后回主页
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from .js_helpers import CARD_PROBE_JS, NAME_PROBE_JS
from .names import random_full_name
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
        # 兑现邀请码属于收尾：失败不影响账号本身，只记警告
        try:
            await self.step_redeem_invite()
        except FlowStopped:
            raise
        except Exception as exc:
            await self.log(f"兑现邀请码未完成：{exc}", "warn")

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
        await self.rt.set_step("填写注册资料")
        await self.log("检测到注册补全页（完成账户创建）")
        await self.rt.snap("profile-page")

        # 这一页有时只有生日，有时上面还会多出「名 / 姓」两栏
        await self.fill_name_fields()

        birthday = self.rt.task.get("birthday") or random_birthday(self.rt.settings)
        await self.set_birthday(birthday)
        await self.settle(800)
        await self.rt.snap("profile-filled")

        await self.rt.set_step("提交注册资料")
        await self.submit_profile()
        await self.log("已提交，等待账户创建（约 15–20 秒）")

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
        raise FlowError("提交后 90 秒内未跳转，可能账户创建失败")

    # --- 6 ---

    async def fill_name_fields(self) -> None:
        """muse 有时会在生日上方多出「名 / 姓」两栏，出现就填。"""
        try:
            info = await self.page.evaluate(NAME_PROBE_JS)
        except Exception as exc:
            await self.log(f"姓名输入框探测失败：{exc}", "warn")
            return

        if not info or not info.get("found"):
            count = (info or {}).get("inputCount", 0)
            await self.log(f"本页没有姓名输入框，跳过（可见文本输入框 {count} 个）")
            return

        await self.log(
            f"检测到姓名输入框：名={info.get('hasFirst')} 姓={info.get('hasLast')}"
        )

        first = str(self.rt.task.get("first_name") or "").strip()
        last = str(self.rt.task.get("last_name") or "").strip()
        if not first or not last:
            first, last = random_full_name()
            await self.rt.patch(first_name=first, last_name=last)
            await self.log(f"随机生成姓名：{first} {last}")

        if info.get("hasFirst") and first:
            await self._fill_name_field("first", first, "名")
        if info.get("hasLast") and last:
            await self._fill_name_field("last", last, "姓")

    async def _fill_name_field(self, kind: str, value: str, label: str) -> None:
        loc = self.page.locator(f'[data-muse-name="{kind}"]').first
        try:
            await loc.wait_for(state="visible", timeout=8000)
        except Exception as exc:
            raise FlowError(f"找不到{label}输入框") from exc
        try:
            await loc.click(timeout=5000)
        except Exception:
            pass
        try:
            await loc.fill("")
        except Exception:
            pass
        try:
            await loc.press_sequentially(value, delay=45)
        except Exception:
            pass
        got = ""
        try:
            got = (await loc.input_value()).strip()
        except Exception:
            pass
        if got != value:
            try:
                await loc.evaluate(
                    "(el, v) => window.__museHelpers.setValue(el, v)", value
                )
                got = (await loc.input_value()).strip()
            except Exception:
                pass
        await self.log(f"{label}已填入：{got or value}")

    async def submit_profile(self) -> None:
        """提交注册资料。

        比原来只找「确认」更宽松：
          1. 先按 Esc 关掉可能还开着的下拉，避免遮罩吃掉点击
          2. 按文案找（确认 / 提交 / 完成 / 下一步 …）
          3. 再退到任意可见的 button[type=submit]
        """
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await self.page.wait_for_timeout(600)

        if await self.click_button("confirm", timeout_ms=15000, required=False):
            return

        btn = await self.locate("submit", timeout_ms=5000)
        if btn is not None:
            await self.click_locator(btn, "提交按钮（submit 兜底）")
            return

        raise FlowError(f"找不到提交按钮（当前页面：{await self._page_brief()}）")

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
            # 关键：不能直接暂停。结账页是慢渲染的（Stripe Elements 这类分帧加载）
            # 所以要轮询等字段出现，而不是立刻拉人来手动填。
            filled = await self._retry_fill_after_render(target, card)
            if not filled:
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
    # 收尾：兑现邀请码
    # ------------------------------------------------------------------

    async def wait_home(self, timeout_s: int = 120) -> None:
        """等回到 muse 主界面（不再带 /access/）。"""
        for _ in range(timeout_s // 2):
            await self.check()
            try:
                info = await self.describe()
            except Exception:
                await asyncio.sleep(2)
                continue
            url = (info.get("url") or "").lower()
            if "/access/" not in url and url.rstrip("/") not in ("", "about:blank"):
                return
            await asyncio.sleep(2)
        await self.log("等待回到主界面超时，仍继续尝试兑现邀请码", "warn")

    async def _list_region(self, region: str) -> list[str]:
        try:
            return await self.page.evaluate(
                "(r) => window.__museHelpers.listClickables(r)", region
            )
        except Exception:
            return []

    async def _mark_region(self, region: str) -> None:
        """把当前区域内的可点元素标为「已存在」。

        弹菜单前先标一次，之后只数新冒出来的元素 —— 否则左下角那个入口
        按钮本身也会被计入，导致「第 4 项」实际点到第 3 项。
        """
        try:
            await self.page.evaluate(
                "(r) => window.__museHelpers.markSeen(r)", region
            )
        except Exception:
            pass

    async def _click_region(
        self,
        region: str,
        texts: list[str],
        label: str,
        nth: int = 0,
        only_new: bool = False,
    ) -> bool:
        """先在**该区域内**按文案点，再按序号/位置兜底。

        为什么限定区域：弹出菜单里的「设置」与左下角那个入口按钮可能同叫
        「设置」，全页搜索会点回入口本身，菜单根本展开不了。

        顺序：区域（或区域新增）文案 → 全页文案 → 区域第 N 项 → 区域最靠角落。
        """
        fn = "clickTextNewInRegion" if only_new else "clickTextInRegion"
        for t in texts:
            try:
                ok = await self.page.evaluate(
                    f"([l, r]) => window.__museHelpers.{fn}(l, r, true)",
                    [t, region],
                )
            except Exception:
                ok = False
            if ok:
                scope = f"{region} 区域{'新增' if only_new else ''}元素"
                await self.log(f"已点击「{t}」（{label}，限定 {scope}）")
                return True

        for t in texts:
            if await self._try_click(t, True):
                await self.log(f"已点击「{t}」（{label}，全页兜底）")
                return True

        if nth:
            if only_new:
                names = await self._list_new(region)
                expr = "([n, r]) => window.__museHelpers.clickNthNewInRegion(n, r)"
            else:
                names = await self._list_region(region)
                expr = "([n, r]) => window.__museHelpers.clickNthInRegion(n, r)"
            try:
                ok = await self.page.evaluate(expr, [nth, region])
            except Exception:
                ok = False
            if ok:
                await self.log(
                    f"{label}：按位置点第 {nth} 项"
                    f"（{'新增' if only_new else '区域'}可点元素：{names}）"
                )
                return True

        try:
            ok = await self.page.evaluate(
                "(r) => window.__museHelpers.clickCorner(r)", region
            )
        except Exception:
            ok = False
        if ok:
            await self.log(f"{label}：按位置点该区域最靠角落的元素")
            return True
        return False

    async def _list_new(self, region: str) -> list[str]:
        try:
            return await self.page.evaluate(
                "(r) => window.__museHelpers.listNewInRegion(r)", region
            )
        except Exception:
            return []

    async def step_redeem_invite(self) -> None:
        """验证通过、回到主页后兑现邀请码。

        实测路径：
          左下角设置按钮 → 弹出菜单第 4 项（设置）
          → 中间弹窗点「兑现邀请码」→ 输入邀请码 → 确定 → 完成
        """
        code = str(
            self.rt.task.get("invite_code")
            or self.rt.settings.get("invite_code")
            or ""
        ).strip()
        if not code:
            await self.log("未配置邀请码，跳过兑现步骤", "warn")
            return

        await self.rt.set_step("兑现邀请码")
        await self.wait_home()
        await self.rt.snap("home")

        # 左下角设置入口。先不按文案点 —— 入口按钮可能也叫「设置」，
        # 而它的位置（左下角）是确定的，按位置最稳。
        await self._mark_region("bottom-left")
        ok_entry = False
        try:
            ok_entry = bool(await self.page.evaluate(
                "() => window.__museHelpers.clickCorner('bottom-left')"
            ))
        except Exception:
            ok_entry = False
        if not ok_entry:
            await self.log(
                f"左下角没找到可点元素，可点列表：{await self._list_region('bottom-left')}",
                "warn",
            )
            raise FlowError("找不到左下角设置按钮")
        await self.log("已点开左下角菜单")
        await self.page.wait_for_timeout(1500)
        new_items = await self._list_new("bottom-left")
        await self.rt.snap("menu-open")
        await self.log(f"菜单新出现的可点项：{new_items}")

        # 弹出菜单里的第 4 项 = 设置。
        # 优先在「新出现」的元素里按文案找，找不到再按序号（第 4 项）。
        if not await self._click_region(
            "bottom-left", TEXTS["settings_menu"], "弹出菜单里的「设置」",
            nth=4, only_new=True,
        ):
            raise FlowError(f"找不到弹出菜单里的「设置」（新出现项：{new_items}）")
        await self.page.wait_for_timeout(1800)
        await self.rt.snap("settings-panel")

        # 3) 中间弹窗里的「兑现邀请码」
        if not await self._click_region(
            "center", TEXTS["redeem_invite"], "「兑现邀请码」"
        ):
            await self.log(
                f"找不到「兑现邀请码」，当前可见元素："
                f"{await self._list_region('center')}",
                "warn",
            )
            raise FlowError("找不到「兑现邀请码」")
        await self.page.wait_for_timeout(1800)
        await self.rt.snap("invite-input")

        # 4) 输入邀请码
        filled = False
        try:
            filled = bool(
                await self.page.evaluate(
                    "(v) => window.__museHelpers.fillFirstInput(v)", code
                )
            )
        except Exception:
            filled = False
        if not filled:
            el = await self.locate("invite_input", timeout_ms=8000)
            if el is None:
                raise FlowError("找不到邀请码输入框")
            await self.click_locator(el, "邀请码输入框")
            await el.press_sequentially(code, delay=50)
        await self.log(f"邀请码已填入：{code}")
        await self.rt.snap("invite-filled")

        # 5) 确定，并确认新界面真的出来了
        if not await self._click_region("center", TEXTS["confirm"], "「确定」"):
            raise FlowError("找不到确定按钮")
        await self._wait_invite_done()
        await self.rt.snap("invite-done")
        await self.log("邀请码兑现流程已走完", "success")

    async def _wait_invite_done(self, timeout_s: int = 30) -> None:
        """等「确定」后的新界面出现。

        判据：中间区域不再有「兑现/兑换」类的按钮 —— 换成新界面后原来的
        兑换入口就消失了。不依赖任何具体成功提示文案。
        """
        for _ in range(timeout_s * 2):
            await self.check()
            try:
                texts = await self.page.evaluate(
                    "(r) => window.__museHelpers.listClickables(r)", "center"
                ) or []
            except Exception:
                texts = []
            if not any(("兑现" in t or "兑换" in t) for t in texts):
                await self.log("兑现界面已关闭，任务完成")
                return
            await asyncio.sleep(0.5)
        await self.log("没等到兑现界面关闭，但不再阻塞流程", "warn")

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
            await self.log(
                f"在 {frame.url[:80] or 'main'} 发现支付字段：{info['marked']}"
            )
            try:
                await self._fill_marked(frame, card, info)
                return True
            except Exception as exc:
                await self.log(f"填写支付字段失败：{exc}", "warn")
        return False

    async def _retry_fill_after_render(self, page: Page, card: dict) -> bool:
        """结账页是慢渲染的（Stripe Elements 分帧加载 iframe），轮询等字段出现。

        Meta 的结账页是 auth.meta.com/payments/checkout，底层多半是
        Stripe Elements —— 卡号在专门的 iframe 里，页面打开后要过几秒才出现，
        所以「探一次没有」不等于「没有」。
        """
        for attempt in range(15):            # 15 x 2s = 30s
            await self.check()
            await page.wait_for_timeout(2000)
            if await self.fill_card_form(page, card):
                await self.log(f"支付字段在第 {attempt + 1} 次探测后出现并已填写")
                return True
            if attempt in (3, 8):
                await self.dump_payment_page(page)
        await self.log("30 秒内仍未等到支付表单字段", "warn")
        await self.dump_payment_page(page)
        await self.rt.snap("checkout-nofield", page=page)
        return False

    async def dump_payment_page(self, page: Page) -> None:
        """把结账页的结构打出来 —— 选结账页字段识别失败时，没这个就没法调。

        要能看到：各 frame 的 url、每个 frame 里 input 的关键属性、
        各 frame 里按钮的文案。敏感字段（卡号/CVV）的**值**绝不能打。
        """
        for i, frame in enumerate(page.frames):
            try:
                info = await frame.evaluate(
                    "() => window.__museHelpers ? window.__museHelpers.describe()"
                    " : {url: '', title: '', body: '', inputs: 0, buttons: 1}"
                )
                fields = await frame.evaluate(CARD_PROBE_JS, {})
                buttons = await frame.evaluate(
                    "() => window.__museHelpers"
                    " ? window.__museHelpers.listClickables('center') : []"
                )
            except Exception:
                continue

            urls = (frame.url or "")[:120]
            body = (info.get("body") or "")[:220].replace("\n", " ")
            await self.log(
                f"[frame {i}] url={urls}\n"
                f"        marked={fields.get('marked')}\n"
                f"        buttons={buttons[:8]}\n"
                f"        body={body}"
            )

            # input 的属性清单（不含 value，避免卡号泄露）
            inputs = []
            try:
                inputs = await frame.evaluate(
                    """() => (window.__museHelpers
                            ? window.__museHelpers.deepAll('input') : []).slice(0,8)
                       .map(e => ({ a: e.getAttribute('autocomplete'),
                                    n: e.getAttribute('name'),
                                    i: e.id,
                                    p: e.getAttribute('placeholder'),
                                    t: e.getAttribute('type') }))"""
                )
            except Exception:
                pass
            if inputs:
                await self.log(f"        inputs={inputs}")
        await self.rt.snap("checkout-dump")

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
        await self.page.wait_for_timeout(400)

        # 邮编：卡片自带的优先，否则用控制台配置的默认值
        postal = (card.get("postal") or "").strip() or str(
            self.rt.settings.get("checkout_postal") or ""
        ).strip()
        if postal:
            await self._fill_postal(frame, postal)

        await put("name", card.get("holder", ""))
        await put("address", (card.get("extra") or {}).get("address", ""))
        await put("city", (card.get("extra") or {}).get("city", ""))
        await put("state", (card.get("extra") or {}).get("state", ""))
        await self.log("支付表单已填写（卡号/CVV 不写入日志）")

    # ------------------------------------------------------------------
    # 邮编：填完 CVV 后焦点会自动跳过去，直接输即可
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_postal(*fields: str) -> bool:
        blob = " ".join(f or "" for f in fields).lower()
        return any(k in blob for k in (
            "postal", "zip", "邮编", "邮政编码", "郵遞區號", "邮递区号",
        ))

    async def _focused_input(self, page: Any) -> tuple[Any, dict]:
        """跨 frame 找当前真正聚焦的输入框，返回 (frame, 描述)。"""
        for f in page.frames:
            try:
                info = await f.evaluate("() => window.__museHelpers.focusedInfo()")
            except Exception:
                continue
            if info and info.get("tag") in ("INPUT", "TEXTAREA"):
                return f, info
        return None, {}

    async def _fill_postal(self, frame: Any, postal: str) -> None:
        """填邮编。

        实测：结账页在填完 CVV 后**会把焦点自动移到邮编输入框**。
        这时直接敲键盘就行 —— 多一次点击反而会把焦点移走，
        后面那串数字就不知道敲到哪去了。

        所以顺序是：
          1. 焦点已经落到邮编框 → 直接输
          2. 否则重新探一次表单（字段可能是刚展开的）
          3. 再不行才按属性找元素并点击后输入
        """
        page = frame.page

        # --- 1. 焦点已自动跳过去 ---
        focused_frame, info = await self._focused_input(page)
        if focused_frame is not None and self._looks_like_postal(
            info.get("autocomplete", ""),
            info.get("name", ""),
            info.get("placeholder", ""),
        ):
            try:
                await page.keyboard.type(postal, delay=60)
            except Exception as exc:
                await self.log(f"直接输入邮编失败：{exc}", "warn")
                return
            got = ""
            try:
                got = str(
                    await focused_frame.evaluate(
                        "() => window.__museHelpers.focusedValue()"
                    )
                )
            except Exception:
                pass
            await self.log(
                f"填完 CVV 后焦点已自动跳到邮编框，直接输入 {postal}"
                f"（回读 {got!r}）"
            )
            return

        # --- 2. 重新探一次表单 ---
        try:
            again = await frame.evaluate(CARD_PROBE_JS, {})
        except Exception:
            again = None
        re_marked = set((again or {}).get("marked") or [])
        if "postal" in re_marked:
            loc = frame.locator('[data-muse-fill="postal"]').first
            try:
                await loc.click(timeout=5000)
            except Exception:
                pass
            try:
                await loc.fill("", timeout=5000)
            except Exception:
                pass
            try:
                await loc.press_sequentially(postal, delay=60, timeout=10000)
            except Exception as exc:
                # 探针报了 postal 但元素实际不可用 —— 不能卡在这里
                await self.log(f"重探定位的邮编框不可用（{exc}），转属性兜底", "warn")
            else:
                await self.log(f"邮编已填入：{postal}（重探后定位）")
                return
            return

        # --- 3. 按常见属性兜底 ---
        el = await self.locate("card_postal", timeout_ms=5000)
        if el is not None:
            await self.click_locator(el, "邮编框")
            try:
                await el.fill("")
            except Exception:
                pass
            await el.press_sequentially(postal, delay=60)
            await self.log(f"邮编已填入：{postal}（属性兜底定位）")
            return

        await self.log("没找到邮编输入框，跳过", "warn")

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

    #: 结账页出现这些字样说明支付失败。
    #: 刻意保守 —— 宁可漏报也不能误报，误报会把已经成功的任务判失败。
    CHECKOUT_FAILURE_MARKERS = (
        "卡被拒", "银行卡被拒", "付款失败", "支付失败", "交易被拒",
        "无法处理你的付款", "无法完成付款", "请更换付款方式",
        "declined", "payment failed", "card was declined",
    )

    async def wait_verification_done(self, checkout: Page) -> None:
        """等年龄验证走完。

        实测正常路径：结账页提交后等一会儿 → 结账标签页**自动关闭**
        → 主页从 /access/verification 变成聊天界面。

        异常路径必须能分辨，不能干等：
          - 卡被拒 / 表单报错 → 结账页上有错误文案
          - 3DS → 需要人工
          - 什么都没发生 → 超时，把结账页内容 dump 出来
        """
        await self.rt.set_step("等待年龄验证完成")
        deadline = time.monotonic() + 300
        manual_prompted = False
        last_note = 0.0

        while time.monotonic() < deadline:
            await self.check()

            # 结账页关闭 = 正常完成
            if checkout is not self.page and checkout.is_closed():
                await self.log("结账标签页已关闭，验证完成", "success")
                break

            # 支付失败
            failure = await self._checkout_failure(checkout)
            if failure:
                await self.dump_payment_page(checkout)
                raise FlowError(f"结账页提示支付失败（命中「{failure}」）")

            # 3DS / 二次验证：交给人工
            if not manual_prompted:
                reason = await self._needs_manual(checkout)
                if reason:
                    manual_prompted = True
                    await self.log(f"检测到需要人工介入：{reason}", "warn")
                    await self.rt.wait_for_manual(
                        f"检测到 3DS 或二次验证（{reason[:70]}），"
                        "请在控制台实时画面上手动完成"
                    )

            info = await self.describe()
            url = (info.get("url") or "").lower()
            if URLS["verification"] not in url and "/access/" not in url:
                await self.log(f"已离开验证页：{info.get('url')}", "success")
                break

            # 每 20 秒把结账页状态记一笔 —— 卡住时这是唯一的线索
            now = time.monotonic()
            if now - last_note > 20:
                last_note = now
                await self._log_checkout_state(checkout)

            await asyncio.sleep(2)
        else:
            await self.log("等待验证完成超时（300s）", "warn")
            await self.dump_payment_page(checkout)
            await self.rt.snap("verify-timeout")

        ok = await self._back_to_home(checkout)
        await self.rt.snap("done")
        await self.rt.save_session()
        if not ok:
            # 回不到聊天界面 = 年龄验证实际没过，这个账号用不了。
            # 报成功会把没用的账号当成好的，所以这里直接判失败。
            raise FlowError(
                "年龄验证未真正通过：主页一直停在 /access/ 下，没进入聊天界面"
            )
        await self.log("年龄验证流程结束，登录态已保存", "success")
    async def _checkout_failure(self, page: Page) -> str | None:
        try:
            body = await page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
            ) or ""
        except Exception:
            return None
        low = body.lower()
        for marker in self.CHECKOUT_FAILURE_MARKERS:
            if marker.lower() in low:
                return marker
        return None

    async def _log_checkout_state(self, page: Page) -> None:
        """把结账页当前状态记一笔，便于判断它到底卡在哪。"""
        try:
            url = (page.url or "")[:110]
            body = await page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
            ) or ""
            buttons = await page.evaluate(
                "() => window.__museHelpers"
                " ? window.__museHelpers.listClickables('center').slice(0,6) : []"
            )
        except Exception as exc:
            await self.log(f"结账页状态读取失败：{type(exc).__name__}", "debug")
            return
        flat = " ".join(body.split())[:180]
        await self.log(
            f"结账页仍在等待：closed={page.is_closed()} url={url}\n"
            f"        buttons={buttons}\n"
            f"        body={flat}",
            "debug",
        )

    async def _back_to_home(self, checkout: Page, timeout_s: int = 120) -> bool:
        """关掉结账页，等主页回到聊天界面。返回是否真的回到了。

        注意不能用「reload 60 次」那种写法：主页停在 /access/ 时每次
        reload 都可能等 30s，60 次就是半小时 —— 之前就是这样卡死的。
        这里改用总时限控制，且只主动 reload 两次（多了没意义还慢）。
        """
        if checkout is not self.page:
            try:
                if not checkout.is_closed():
                    await checkout.close()
                    await self.log("已关闭结账标签页")
            except Exception:
                pass

        deadline = time.monotonic() + timeout_s
        reloaded = 0
        while time.monotonic() < deadline:
            await self.check()
            try:
                await self.page.bring_to_front()
            except Exception:
                pass
            info = await self.describe()
            url = (info.get("url") or "").lower()
            if "/access/" not in url:
                await self.log(f"已回到主界面：{info.get('url')}")
                return True
            if reloaded < 2:
                reloaded += 1
                try:
                    await self.page.reload(
                        wait_until="domcontentloaded", timeout=20_000
                    )
                except Exception:
                    pass
            await asyncio.sleep(3)

        await self.log(
            f"{timeout_s}s 内主页仍未离开验证页，可能验证没真正通过", "warn"
        )
        await self.dump_payment_page(self.page)
        return False

    async def _needs_manual(self, page: Page) -> str | None:
        """返回需要人工介入的**原因**，不需要时返回 None。

        之前只返回 bool 且对每个 frame 的 URL 子串做匹配 —— 而结账页
       自身的 URL 是 auth.meta.com，里面就带着 auth 字样，一打就误报。
        所以：
          - URL 判定只看**与主页面不同源**的 frame（3DS 的 ACS 页面是
            结账页里嵌套的独立 iframe，主结账页不可能算）
          - 同时返回具体是哪条命中，日志里能直接看到
        """
        main_url = ""
        try:
            main_url = (page.url or "").lower()
        except Exception:
            main_url = ""
        try:
            for frame in page.frames:
                url = (frame.url or "").lower()
                if not url or url == main_url:
                    continue
                if url in main_url or main_url in url:
                    continue            # 同一个页面/子页，不算 ACS
                hit = next((k for k in ("acs.", "3ds", "challenge", "authentication")
                            if k in url), None)
                if hit:
                    return f"发现疑似 ACS frame（含 {hit}）：{url[:120]}"
        except Exception:
            pass
        try:
            body = (await page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
            )) or ""
        except Exception:
            return None
        markers = (
            "3D Secure", "3DS", "短信验证码", "银行验证", "发卡行验证",
            "发卡行短信", "Verify your identity", "authentication code",
            "进入你的银行", "验证你的身份",
        )
        for m in markers:
            if m in body:
                return f"页面出现「{m}」"
        return None
