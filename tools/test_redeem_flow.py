"""兑现邀请码的收尾流程自测（合成页面）。

muse 的真实 DOM 看不到，所以用合成页面把「左下角入口 → 弹出菜单第 4 项 →
中间弹窗 → 兑现邀请码 → 输入 → 确定」这套位置定位逻辑跑一遍。

重点验证 markSeen / clickNthNewInRegion：点开菜单前先标记已有元素，
之后只数**新出现**的，否则左下角那个入口按钮本身会被计入，序号整体偏移。

用法：python tools/test_redeem_flow.py
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from app import browser as bm

PAGE = """<!doctype html><html><body style="margin:0;height:100vh;background:#111">
<button id="gear" style="position:absolute;left:16px;bottom:16px;width:40px;height:40px">gear</button>
<script>
document.getElementById('gear').addEventListener('click', function () {
  var m = document.createElement('div');
  m.id = 'menu';
  m.style.cssText = 'position:absolute;left:16px;bottom:70px;width:200px;background:#222';
  ['个人资料','外观','通知','设置','帮助'].forEach(function (t, i) {
    var b = document.createElement('button');
    b.textContent = t; b.style.cssText = 'display:block;width:100%;height:36px';
    if (i === 3) b.addEventListener('click', function () {
      var d = document.createElement('div');
      d.id = 'modal';
      d.style.cssText = 'position:absolute;left:35%;top:30%;width:30%;height:40%;background:#333';
      d.innerHTML = '<button id="redeem">兑现邀请码</button>'
        + '<input id="code" placeholder="请输入邀请码">'
        + '<button id="ok">确定</button>';
      document.body.appendChild(d);
      document.getElementById('redeem').addEventListener('click', function () {
        document.getElementById('code').style.cssText =
          'position:absolute;left:38%;top:45%;width:20%;height:30px';
      });
      document.getElementById('ok').addEventListener('click', function () {
        document.body.innerHTML = '<h1 id="done">完成</h1>';
      });
    });
    m.appendChild(b);
  });
  document.body.appendChild(m);
});
</script></body></html>"""

S = {"browser_engine": "camoufox", "headless": True, "slow_mo": 0,
     "viewport_w": 1280, "viewport_h": 820, "locale": "zh-CN",
     "timezone": "Asia/Shanghai", "step_timeout": 30}
BTN = 'button, [role="button"]'

async def main():
    ctx = await bm.manager.new_context(S)
    p = await ctx.new_page()
    await p.set_content(PAGE)
    H = "window.__museHelpers"
    async def ev(js): return await p.evaluate(js)
    ok = []

    await ev(f"{H}.markSeen('bottom-left')")
    r1 = await ev(f"{H}.clickCorner('bottom-left')")
    ok.append(("点左下角入口", r1))
    await p.wait_for_timeout(400)

    new = await ev(f"{H}.listNewInRegion('bottom-left')")
    print(f"  点开后「新增」元素 = {new}")

    r2 = await ev(f"{H}.clickNthNewInRegion(4, 'bottom-left')")
    ok.append(("点新增第4项(设置)", r2))
    await p.wait_for_timeout(400)
    ok.append(("弹窗出现", await ev("!!document.getElementById('modal')")))

    ok.append(("文案点「兑现邀请码」",
               await ev(f"{H}.clickByText({BTN!r}, '兑现邀请码', true)")))
    await p.wait_for_timeout(300)
    ok.append(("填第一个输入框", await ev(f"{H}.fillFirstInput('INV-9527')")))
    ok.append(("输入框值正确",
               await ev("document.getElementById('code').value") == 'INV-9527'))
    ok.append(("文案点「确定」", await ev(f"{H}.clickByText({BTN!r}, '确定', true)")))
    await p.wait_for_timeout(500)
    ok.append(("进入完成界面", await ev("!!document.getElementById('done')")))

    # 歧义场景：入口按钮和菜单项同名时，必须只数「新出现」的元素
    print()
    print("  --- 同名歧义：入口与菜单项都叫「设置」---")
    await p.set_content(PAGE.replace('>gear<', '>设置<'))
    await ev(f"{H}.markSeen('bottom-left')")
    await ev(f"{H}.clickCorner('bottom-left')")
    await p.wait_for_timeout(400)
    before = await ev(f"{H}.listNewInRegion('bottom-left')")
    r = await ev(f"{H}.clickTextInRegion('设置', 'bottom-left', true)")
    ok.append(("（反例）区域内按文案会点回入口", r is True))
    r2 = await ev(f"{H}.clickTextNewInRegion('设置', 'bottom-left', true)")
    ok.append(("只在新增元素里按文案 -> 点到菜单项", r2 is True))
    await p.wait_for_timeout(400)
    ok.append(("弹窗出现", await ev("!!document.getElementById('modal')")))
    print(f"      新增项 = {before}")

    print()
    for name, v in ok:
        print(f"  {'✓' if v else '✗'} {name}")
    print(f"\n  {sum(1 for _, v in ok if v)}/{len(ok)} 通过")
    await ctx.close(); await bm.manager.stop()

asyncio.run(main())
