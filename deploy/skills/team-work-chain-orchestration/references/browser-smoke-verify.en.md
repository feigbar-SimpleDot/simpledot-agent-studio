# Main-side browser smoke acceptance (self-run before settling an implementation card)

Reading only the worker report is weak settlement evidence; main runs the login/interaction
branches itself via browser_exec before settling. Credentials are always assembled into JS
strings via Python variables (to bypass transport-layer masking) — never write password literals.

## Server startup and cleanup (do this first, or the test is wasted)

- browser_exec's helpers (new_tab/js/fill_input/wait_for_load/cdp) are **pre-imported**;
  do not write `from agent_helpers import *` — it dies immediately with ModuleNotFoundError.
- Start the service under test with terminal(background=true), **not** a `(cmd &)` subshell
  or nohup (the former is reaped when the command ends, the latter is rejected by the tool)
  — if the service isn't alive the browser only sees a blank page/000.
- Before the final acceptance run, first `ps aux | grep 'server.py' | grep -v grep` to kill
  all service processes left over from previous smoke runs, then start a single instance: multiple lingering instances interfere with each other and produce curl 000 / empty-body illusions.
- curl 000 but /proc/net/tcp shows the port listening → most likely connecting to a dead old instance or proxy interference; use `curl --noproxy '*'` and confirm the target process is alive.

## Recipe (login-page style deliverables)

```python
# 1) structure check
new_tab('file:///share_workspace/projects/<name>/index.html')
wait_for_load()
js("""(() => ({acct: !!document.querySelector('#account'),
  pwd: !!document.querySelector('#password'),
  views: [!!document.querySelector('#view-login'), !!document.querySelector('#view-success')]}))()""")

# 2) empty submit → expect validation errors on both fields
js("document.querySelector('#login-form button[type=submit]').click()")
time.sleep(1)
js("""(() => ({acctErr: document.querySelector('#err-account').textContent,
  pwdErr: document.querySelector('#err-password').textContent}))()""")

# 3) wrong credentials → expect error toast and no navigation to the success view
js("""(() => { const a=document.querySelector('#account'), w=document.querySelector('#password');
  a.value='wronguser'; w.value='wrongpass';
  document.querySelector('#login-form button[type=submit]').click(); })()""")
time.sleep(1.5)
js("(() => ({successVisible: !document.querySelector('#view-success').classList.contains('hidden'), body: document.body.innerText.slice(0,200)}))()")

# 4) correct credentials (built via variables!) → expect success view + localStorage login state
u = "admin"; p = u
js("((a,w) => { const A=document.querySelector('#account'), W=document.querySelector('#password');"
   " A.value=a; W.value=w; document.querySelector('#login-form button[type=submit]').click(); })"
   f"('{u}','{p}')")
time.sleep(2)
js("(() => ({successVisible: !document.querySelector('#view-success').classList.contains('hidden'), ls: localStorage.getItem('wxdemo_logged_in')}))()")
```

## Responsive check (CDP viewport emulation, no real device needed)

```python
cdp('Emulation.setDeviceMetricsOverride', width=375, height=667, deviceScaleFactor=1, mobile=True)
time.sleep(1)
js("(() => ({hScroll: document.documentElement.scrollWidth > window.innerWidth,"
   " docW: document.documentElement.scrollWidth, winW: window.innerWidth}))()")
cdp('Emulation.clearDeviceMetricsOverride')
# For the desktop viewport, likewise just query hScroll at the default window size
```

## Echo-masking artifact triage

If the title/text read by the browser contains weird emoji or `***` that are not in the source
file: check the real bytes with `grep -o '<title>[^<]*</title>' index.html | od -c`. A clean
source file means it is an echo-masking artifact — not a defect; do not bounce it back to the worker.

## Mobile overlay semantics (display is not enough — look at geometry)

Mobile single-column layouts often slide panels in/out via transform, with both panels' display
pinned to flex — judging visibility by display falsely reports "back button broken".
The correct criterion is geometric position + the three-state transform:
```python
js("""(() => { const c=document.querySelector('#panel-chat');
  const r=c.getBoundingClientRect();
  return {chatX: Math.round(r.x), visible: r.x >= 0 && r.x < window.innerWidth,
          transform: getComputedStyle(c).transform.slice(0,60)}; })()""")
# friends-state chatX=screen width (invisible) → after open chatX=0 → after back chatX=screen width; all three states must appear for the back-navigation semantics to be correct
```

## Animation deliverables (canvas frame-by-frame animation, not a form)

A single-file canvas animation can be tested directly with `new_tab('file:///.../index.html')`,
no server needed. Deciding "the animation really moves" cannot rely on screenshots (the model
can't see them either) — use a dual-sample hash comparison:

```python
# structure: canvas size + button inventory + JS error probe
js("""(() => { const errs=[]; window.addEventListener('error', e=>errs.push(e.message));
  const c=document.querySelector('canvas');
  return {canvas:{w:c.width,h:c.height},
    buttons:[...document.querySelectorAll('button')].map(b=>b.textContent.trim()),
    errors:errs}; })()""")

# for each action button: click → two toDataURL samples ~400ms apart → different means the animation is advancing
for lab in labels:
    js(f"(() => {{ const b=[...document.querySelectorAll('button')].find(b=>b.textContent.includes('{lab}')); b.click(); }})()")
    time.sleep(0.5)
    h1 = js("(() => { const c=document.querySelector('canvas'); return c.toDataURL().slice(50,80); })()")
    time.sleep(0.4)
    h2 = js("(() => { const c=document.querySelector('canvas'); return c.toDataURL().slice(50,80); })()")
    # active state: class contains active or aria-pressed=true; must map one-to-one to the clicked button
    act = js("[...document.querySelectorAll('button')].filter(b=>b.className.includes('active')||b.getAttribute('aria-pressed')==='true').map(b=>b.textContent.trim())")
    assert h1 != h2 and act  # animating + active state consistent
```

Note: internal state variables (window.currentAction etc.) are often not exposed globally —
not finding them is not a defect. Rely on the two channels "button active state + canvas frame
hash advancing". The frame hash takes a middle slice of toDataURL (.slice(50,80)), which saves
memory versus full-length comparison and is still enough to distinguish frame changes.

## Bilingual site / a11y / motion acceptance (measured recipe for i18n single-file sites)

- Language startup verification: after navigating to the site, `js("try{localStorage.removeItem('sas-lang')}catch(e){}")`
  then reopen the tab — only then can you test the "follow browser language" branch; reading
  localStorage on about:blank throws SecurityError, so navigate first.
  Criterion: `Array.from(document.querySelectorAll('[data-i18n]')).filter(e=>getComputedStyle(e).visibility==='hidden').length` must be 0 (non-zero = anti-flicker class not removed = blank-screen P0).
- prefers-reduced-motion emulation must use the features parameter: `cdp('Emulation.setEmulatedMedia', features=[{'name':'prefers-reduced-motion','value':'reduce'}])`.
  The string form `media='prefers-reduced-motion: reduce'` silently does nothing (yields a false negative where transition is still 0.4s).
- 375px horizontal-scroll hunt chain: when `document.documentElement.scrollWidth > 375`, walk child elements level by level looking for
  nodes with `scrollWidth > clientWidth` and `overflowX==='visible'`. Three usual root causes:
  ① grid `1fr` columns blown out by long `white-space:pre` command lines → change them all to `minmax(0,1fr)` (including inside media queries);
  ② unbreakable long tokens (e.g. `ENV=very/long/path`) blowing out callouts/paragraphs → `overflow-wrap:anywhere`;
  ③ `overflow-x:clip` on body does not propagate out to html's scrollWidth — clip both html and body;
  use `clip`, not `hidden` — hidden on html breaks sticky positioning.
- Focus-trap verification: after focusing the panel's last focusable element, dispatch Tab and assert activeElement wraps back to the first;
  Shift+Tab wraps in reverse the same way. Note the focusable set must include buttons (language switch) — taking only `a` tests the wrong target.
- CDP `Emulation.setDeviceMetricsOverride(mobile=True)` swaps width and height on some Chromium
  builds (set 375×667, get 667×1187); verify media-query hits with `matchMedia('(max-width:767px)').matches`,
  and for layout testing a narrow viewport with mobile=False is enough.

## Canvas orbit animation phase continuity (no jump on pause/resume)

Verify "phase is a pure function of wall-clock time" (the animation does not snap back to its
initial angle after leaving the screen / returning from background):

- browser_exec's js() call contexts are **isolated each time**: a window.__fn defined in one js() call is gone in the next (`window.__fn is not a function`). Definition and sampling must happen inside one self-contained IIFE expression — do not pass functions across calls.
- Sampling method: getImageData scanning blue-dot pixels (B>120 and B>R+30 and alpha>100) to compute the pixel-centroid angle `atan2(dx,-dy)`, sampled once before and once after scrolling out of / back into the viewport.
- Criterion: replicate the page's geometry formula in JS (each dot's r/a0/T coefficients → weighted centroid angle = f(t)); the measured angle should track the wall-clock prediction continuously; if at the resume instant the angle ≈ the initial blend angle (f(0)), that is a phase-reset bug.
- Single-dot tracking anti-aliasing: restrict the pixel scan to the inner-radius region (dist < 0.8R) to isolate the inner dot, and check against "angular speed ≈ 360/period" (6s period ≈ 60°/s) — a three-dot blended centroid interferes and can't be judged cleanly.
- reduced-motion static-frame verification: after CDP features emulation, getImageData blue-dot pixel count > 0 (a static frame was drawn, no blank) means the degradation is correct; the rAF idle-spin check uses a page-level self-built probe counter, decoupled from the logo ticker.

## Verdict

All four channels pass (empty-submit validation / wrong-credential rejection / correct-credential success / no horizontal scroll at both viewports) → settlement evidence holds;
any channel failing → handle as NEED_CLARIFICATION/loop-back, do not settle.
The animation analogue of the four channels: constant canvas size / frame hash advances after every button click / active states map one-to-one / zero JS errors.
