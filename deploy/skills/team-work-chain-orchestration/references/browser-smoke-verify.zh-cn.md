# main 侧浏览器冒烟验收（implementation 卡结算前自跑）

只读 worker 报告是弱结算依据；main 用 browser_exec 亲自跑一遍登录/交互分支再结算。
凭据一律用 Python 变量拼进 JS 字符串（避开传输层打码），绝不写密码字面量。

## 起服与清理（先做，否则白测）

- browser_exec 的 helper（new_tab/js/fill_input/wait_for_load/cdp）是**预导入**的，
  不要写 `from agent_helpers import *` —— 会 ModuleNotFoundError 直接炸。
- 起被测服务用 terminal(background=true)，**不要** `(cmd &)` 子 shell 或 nohup
  （前者随命令结束被回收，后者被工具拒）——服务没活浏览器只会看到空白页/000。
- 最终验收前先 `ps aux | grep 'server.py' | grep -v grep` 清掉历次冒烟残留的
  全部服务进程再起单一实例：多个残留实例互相干扰会造成 curl 000 / 空 body 假象。
- curl 000 但 /proc/net/tcp 显示端口在听 → 多半是连到已死的旧实例或代理干扰，
  用 `curl --noproxy '*'` 并确认目标进程存活。

## 配方（登录页类产物）

```python
# 1) 结构检查
new_tab('file:///share_workspace/projects/<name>/index.html')
wait_for_load()
js("""(() => ({acct: !!document.querySelector('#account'),
  pwd: !!document.querySelector('#password'),
  views: [!!document.querySelector('#view-login'), !!document.querySelector('#view-success')]}))()""")

# 2) 空提交 → 期望双字段校验提示
js("document.querySelector('#login-form button[type=submit]').click()")
time.sleep(1)
js("""(() => ({acctErr: document.querySelector('#err-account').textContent,
  pwdErr: document.querySelector('#err-password').textContent}))()""")

# 3) 错误凭据 → 期望错误 toast 且不进成功视图
js("""(() => { const a=document.querySelector('#account'), w=document.querySelector('#password');
  a.value='wronguser'; w.value='wrongpass';
  document.querySelector('#login-form button[type=submit]').click(); })()""")
time.sleep(1.5)
js("(() => ({successVisible: !document.querySelector('#view-success').classList.contains('hidden'), body: document.body.innerText.slice(0,200)}))()")

# 4) 正确凭据（变量构造！）→ 期望成功视图 + localStorage 登录态
u = "admin"; p = u
js("((a,w) => { const A=document.querySelector('#account'), W=document.querySelector('#password');"
   " A.value=a; W.value=w; document.querySelector('#login-form button[type=submit]').click(); })"
   f"('{u}','{p}')")
time.sleep(2)
js("(() => ({successVisible: !document.querySelector('#view-success').classList.contains('hidden'), ls: localStorage.getItem('wxdemo_logged_in')}))()")
```

## 响应式检查（CDP 视口仿真，无需真机）

```python
cdp('Emulation.setDeviceMetricsOverride', width=375, height=667, deviceScaleFactor=1, mobile=True)
time.sleep(1)
js("(() => ({hScroll: document.documentElement.scrollWidth > window.innerWidth,"
   " docW: document.documentElement.scrollWidth, winW: window.innerWidth}))()")
cdp('Emulation.clearDeviceMetricsOverride')
# PC 视口同理用默认窗口尺寸查 hScroll
```

## 回显打码鉴别

浏览器读到的 title/文本若含源文件里没有的怪 emoji 或 `***`：
`grep -o '<title>[^<]*</title>' index.html | od -c` 查真实字节。源文件干净 = 回显打码伪影，
不是缺陷，勿打回 worker。

## 手机覆盖层语义（display 不够，看几何）

手机单栏常用 transform 滑入/滑出，两个 panel 的 display 恒为 flex——用 display 判可见性会误报「返回按钮失效」。
正确判据是几何位置 + transform 三态：
```python
js("""(() => { const c=document.querySelector('#panel-chat');
  const r=c.getBoundingClientRect();
  return {chatX: Math.round(r.x), visible: r.x >= 0 && r.x < window.innerWidth,
          transform: getComputedStyle(c).transform.slice(0,60)}; })()""")
# 好友态 chatX=屏宽(不可见) → 打开后 chatX=0 → 返回后 chatX=屏宽，三态齐全才算返回语义正确
```

## 动画类产物（canvas 逐帧动画，非表单）

单文件 canvas 动画直接 `new_tab('file:///.../index.html')` 即可测，无需起服。
判定「动画真的在动」不能只看截图（模型也看不见），用双采样哈希对比：

```python
# 结构: 画布尺寸 + 按钮清单 + JS错误探针
js("""(() => { const errs=[]; window.addEventListener('error', e=>errs.push(e.message));
  const c=document.querySelector('canvas');
  return {canvas:{w:c.width,h:c.height},
    buttons:[...document.querySelectorAll('button')].map(b=>b.textContent.trim()),
    errors:errs}; })()""")

# 每个动作按钮: 点击 → 间隔~400ms 两次采样 toDataURL → 不同即动画在推进
for lab in labels:
    js(f"(() => {{ const b=[...document.querySelectorAll('button')].find(b=>b.textContent.includes('{lab}')); b.click(); }})()")
    time.sleep(0.5)
    h1 = js("(() => { const c=document.querySelector('canvas'); return c.toDataURL().slice(50,80); })()")
    time.sleep(0.4)
    h2 = js("(() => { const c=document.querySelector('canvas'); return c.toDataURL().slice(50,80); })()")
    # 激活态: class 含 active 或 aria-pressed=true，应与所点按钮一一对应
    act = js("[...document.querySelectorAll('button')].filter(b=>b.className.includes('active')||b.getAttribute('aria-pressed')==='true').map(b=>b.textContent.trim())")
    assert h1 != h2 and act  # animating + 激活态一致
```

注意：内部状态变量（window.currentAction 等）常不暴露在全局，取不到不算缺陷——
以「按钮激活态 + canvas 帧哈希推进」两路为准。帧哈希取 toDataURL 的中间切片
（.slice(50,80)）比全长对比省内存且足够区分帧变化。

## 双语站 / a11y / 动效验收（i18n 单文件站实测配方）

- 语言启动验证：导航到站点后 `js("try{localStorage.removeItem('sas-lang')}catch(e){}")` 再重开 tab
  才能测『跟随浏览器语言』分支——在 about:blank 上读 localStorage 会 SecurityError，必须先导航。
  判据：`Array.from(document.querySelectorAll('[data-i18n]')).filter(e=>getComputedStyle(e).visibility==='hidden').length` 应为 0（非零=防闪烁类未移除=白屏 P0）。
- prefers-reduced-motion 仿真必须用 features 参数：`cdp('Emulation.setEmulatedMedia', features=[{'name':'prefers-reduced-motion','value':'reduce'}])`。
  用 `media='prefers-reduced-motion: reduce'` 字符串形式会静默不生效（测出 transition 仍 0.4s 的假阴性）。
- 375px 横滚排查链：`document.documentElement.scrollWidth > 375` 时逐层 walk 子元素找
  `scrollWidth > clientWidth` 且 `overflowX==='visible'` 的节点。常见根因三件套：
  ① grid `1fr` 列被 `white-space:pre` 长命令行撑爆 → 全部改 `minmax(0,1fr)`（含媒体查询里的）；
  ② 不可断行长 token（如 `ENV=very/long/path`）撑爆 callout/段落 → `overflow-wrap:anywhere`；
  ③ body 的 `overflow-x:clip` 不外传到 html 的 scrollWidth，要 html/body 都 clip；
  用 `clip` 不用 `hidden`——html 上 hidden 会破坏 sticky 定位。
- 焦点陷阱验证：focus 面板最后一个 focusable 后 dispatch Tab，断言 activeElement 回到第一个；
  Shift+Tab 反向同理。注意 focusable 集合要含按钮（语言切换），只取 `a` 会测错目标。
- CDP `Emulation.setDeviceMetricsOverride(mobile=True)` 在部分 Chromium 构建会宽高互换
  （设 375×667 得 667×1187）；媒体查询命中验证用 `matchMedia('(max-width:767px)').matches`，
  测布局用 mobile=False 的窄视口即可。

## canvas 轨道动画相位连续性（暂停/恢复不跳变）

验证『相位是墙钟时间的纯函数』（离屏/切后台恢复后动画不回跳初始角）：

- browser_exec 的 js() 调用上下文**每次隔离**：上一次 js() 里定义的 window.__fn 下一次取不到（`window.__fn is not a function`）。定义与采样必须在同一条自包含 IIFE 表达式里完成，不要跨调用传函数。
- 采样法：getImageData 扫蓝点像素（B>120 且 B>R+30 且 alpha>100）求像素质心角 `atan2(dx,-dy)`，滚动出/回视口前后各采一次。
- 判据：在 JS 里复刻页面几何公式（各点 r/a0/T 系数 → 加权质心角 = f(t)），实测角应贴合墙钟预测连续推进；若恢复瞬间角度≈初始混合角（f(0)）即相位重置 bug。
- 单点追踪防混叠：把像素扫描限制在内半径区域（dist < 0.8R）隔离内圈点，按『角速度 ≈ 360/周期』核对（6s 周期 ≈ 60°/s），三点混合质心会互相干扰判不清。
- reduced-motion 静止帧验证：CDP features 仿真后 getImageData 蓝点像素数 > 0（静止帧已画出、无空白）即降级正确；rAF 空转检查用页面级自建探针计数，与 logo ticker 解耦。

## 判定

四路（空提交校验 / 错误凭据拒绝 / 正确凭据成功 / 双视口无横向滚动）全过 → 结算依据成立；
任一路不符 → 按 NEED_CLARIFICATION/回流处理，不结算。
动画类对应四路：画布尺寸恒定 / 每按钮点击后帧哈希推进 / 激活态一一对应 / 零 JS 错误。
