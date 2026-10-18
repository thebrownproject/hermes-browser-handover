"""Phone viewer page. Plain HTML/JS, no framework, served with a per-response CSP nonce.

Input handling (tap probe for the soft keyboard, hidden textarea bridge with
sentinel/backspace handling, pan-to-wheel, multi-click counting, resize-to-view
and visualViewport tracking) is adapted from T3 Code's preview-stream viewer,
MIT, Copyright 2026 T3 Tools Inc. See third_party/NOTICE.md.
"""
import html
import json

# Colours follow Hermes's shared Nous preset; no external assets or font requests.
_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--panel:#f6f8fa;--muted:#656d76;--line:#d0d7de;--blue:#0053fd;--on-blue:#fff;--soft:#deeaff;--danger:#cf222e;--bar-height:82px}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--panel:#161b22;--muted:#7d8590;--line:#30363d;--blue:#4a84fe;--on-blue:#161616;--soft:#1d2e4f;--danger:#f85149}}
*{box-sizing:border-box}html,body{height:100%;margin:0;overflow:hidden;background:var(--bg);color:var(--fg);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
#bar{position:fixed;inset:0 0 auto;height:var(--bar-height);display:grid;grid-template-columns:1fr auto auto;grid-template-rows:20px 40px;align-content:center;align-items:center;gap:4px 8px;padding:0 10px;padding-top:env(safe-area-inset-top);box-sizing:content-box;background:var(--bg);border-bottom:1px solid var(--line);z-index:3}
#bar .site{display:contents}#site{grid-column:1/-1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600}#sub{grid-column:1;grid-row:2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#bar .site small{display:block;color:var(--muted);font-size:11px;font-weight:400;margin-top:3px}
button{flex-shrink:0;border:1px solid transparent;border-radius:8px;padding:10px 11px;min-height:40px;font:600 13px -apple-system,system-ui,sans-serif;color:var(--on-blue);background:var(--blue);white-space:nowrap}
button.release{background:var(--soft);color:var(--blue);border-color:var(--blue)}button:disabled{opacity:.45}button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
button.finish{background:var(--panel);color:var(--fg);border-color:var(--line)}button.finish.arm{background:var(--danger);color:#fff}button.finish[hidden]{display:none}
#stage{position:fixed;left:0;right:0;top:calc(var(--bar-height) + 1px + env(safe-area-inset-top));bottom:0;overflow:hidden;background:var(--panel)}
#fin{grid-column:2;grid-row:2}#ctl{grid-column:3;grid-row:2}
@media(min-width:600px){:root{--bar-height:56px}#bar{display:flex}#bar .site{display:block;flex:1;min-width:0}#site{display:block}}
canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}
#msg{position:fixed;left:12px;right:12px;bottom:calc(14px + env(safe-area-inset-bottom));background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.4;z-index:4;box-shadow:0 4px 16px #0001}
#msg:empty{display:none}textarea{position:fixed;left:0;top:0;width:1px;height:1px;padding:0;margin:-1px;border:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;font-size:16px}
.border-you #sub{color:var(--blue)}.finished{padding:32px 20px}.border-you #stage{outline:2px solid var(--blue);outline-offset:-2px}
"""

_SCRIPT = r"""
(function(){
  const cfg = window.__HBH__;
  const bar = document.getElementById('bar'), site = document.getElementById('site'), sub = document.getElementById('sub');
  const btn = document.getElementById('ctl'), fin = document.getElementById('fin'), stage = document.getElementById('stage'), canvas = document.getElementById('cv');
  const msg = document.getElementById('msg'), input = document.getElementById('kb');
  const ctx2d = canvas.getContext('2d');
  const SENTINEL = '\u200b', TAP_SLOP = 8, MULTI_MS = 500, MULTI_SLOP = 4, PROBE_PX = 24, PROBE_MS = 10000;
  let ws = null, state = null, controller = 'agent', viewport = null, size = null, cap = null, closedForGood = false;
  let retries = 0, resizeTimer = null, pendingWheel = null, wheelFrame = null, probe = null, probeCache = null;
  let keySent = false, touch = null, lastTap = null, decoding = false, waiting = null, finArm = null;
  const show = t => { msg.textContent = t || ''; };
  const send = o => { if (ws && ws.readyState === 1) { ws.send(JSON.stringify(o)); return true; } return false; };

  function paint(buf){
    if (decoding) { waiting = buf; return; }
    decoding = true;
    createImageBitmap(new Blob([buf], {type:'image/jpeg'})).then(bm => {
      if (canvas.width !== bm.width || canvas.height !== bm.height) { canvas.width = bm.width; canvas.height = bm.height; }
      ctx2d.drawImage(bm, 0, 0); bm.close();
    }).catch(()=>{}).finally(()=>{ decoding = false; const n = waiting; waiting = null; if (n) paint(n); });
  }
  function measure(){
    resizeTimer = null;
    const r = stage.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    const next = {width: Math.round(r.width), height: Math.round(r.height)};
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const nextCap = {maxWidth: Math.round(next.width*ratio), maxHeight: Math.round(next.height*ratio)};
    size = next; cap = nextCap;
    if (controller === 'you') send({type:'resize', ...next});
  }
  new ResizeObserver(() => { if (resizeTimer) clearTimeout(resizeTimer); resizeTimer = setTimeout(measure, size ? 150 : 0); }).observe(stage);
  const vv = window.visualViewport;
  const follow = () => { if (!vv) return; stage.style.top = (vv.offsetTop + bar.offsetHeight) + 'px'; stage.style.height = (vv.height - bar.offsetHeight) + 'px'; };
  if (vv) { vv.addEventListener('resize', follow); vv.addEventListener('scroll', follow); }

  const HELD = ['human','human_disconnected','target_lost','superseded'];
  function render(){
    const st = state ? state.state : '', cycles = state && state.cycles ? state.cycles : 0;
    const takeLabel = cycles > 0 ? 'Take control again' : 'Take control';
    document.body.classList.toggle('border-you', controller === 'you');
    site.textContent = state && state.origin ? state.origin.replace(/^https?:\/\//,'') : 'Hermes browser';
    sub.textContent = controller === 'you' ? 'Your control' : 'Hermes';
    sub.title = cfg.purpose;
    site.title = state && state.origin ? state.origin : 'Hermes browser';
    if (controller === 'you') { btn.textContent = 'Release control'; btn.className = 'release'; btn.disabled = false;
      show(st === 'target_lost' ? 'Page unavailable. Release control to continue.' : ''); }
    else if (controller === 'other') { btn.textContent = takeLabel; btn.className = ''; btn.disabled = true; show('Browser in use elsewhere.'); }
    else if (st === 'viewing' || st === 'prepared') { btn.textContent = takeLabel; btn.className = ''; btn.disabled = !(state && state.page_alive);
      show(''); }
    else { btn.disabled = true; }
    // Finish session: available while this page is active and nobody else holds the browser.
    // Ending it while this handover holds hands back first; ending it while someone else holds is refused.
    if (!finArm) { fin.textContent = 'Finish session'; fin.className = 'finish'; }
    fin.hidden = closedForGood || !state;
    fin.disabled = closedForGood || !state || (controller === 'other' && HELD.indexOf(st) >= 0);
    if (state && state.notices && state.notices.length && controller === 'you') show(state.notices[state.notices.length-1]);
    window.__hbhView = {state: st || null, controller: controller, pageAlive: state ? state.page_alive : null, cycles: cycles,
                        button: btn.textContent, finish: fin.hidden ? null : fin.textContent, finishDisabled: fin.disabled};
  }
  function onMessage(ev){
    if (ev.data instanceof ArrayBuffer) { paint(ev.data); return; }
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'state') { state = m; controller = m.controller; if (m.viewport) viewport = m.viewport; retries = 0;
      input.disabled = controller !== 'you'; if (controller !== 'you') clearInput(); else if (size) send({type:'resize', ...size}); render(); }
    else if (m.type === 'viewport') { viewport = {width:m.width, height:m.height}; }
    else if (m.type === 'origin') { if (state) { state.origin = m.origin; state.notices = m.notices; } render(); }
    else if (m.type === 'notices') { if (state) state.notices = m.notices; render(); }
    else if (m.type === 'probe') { onProbe(m); }
    else if (m.type === 'error') { show(m.message); }
    else if (m.type === 'closed') { closedForGood = true; controller = 'agent'; disarmFinish(); clearInput(); render();
      show(m.reason === 'finished' ? 'Session finished.' :
           m.reason === 'returned' ? 'Handed back to Hermes.' :
           m.reason === 'replaced' ? 'This page was opened somewhere else.' :
           m.reason === 'viewer_restarting' ? 'The viewer restarted. Reopen your link to continue.' :
           m.reason === 'expired' ? 'Link expired. Ask Hermes for a new one.' :
           'This handover has finished. Ask Hermes for a new link if needed.'); btn.disabled = true; fin.disabled = true; }
  }
  async function connect(){
    if (closedForGood) return;
    show('Connecting…');
    let ticket;
    try {
      const r = await fetch('/api/ticket/' + cfg.id, {headers: {'X-HBH-CSRF': cfg.csrf}, credentials: 'same-origin', cache: 'no-store'});
      if (!r.ok) { show(r.status === 410 ? 'This handover is no longer active.' : 'Not authorised. Open the link on your own phone.'); closedForGood = r.status === 410; return; }
      ticket = (await r.json()).ticket;
    } catch { show('Could not reach the viewer. Check your tailnet connection, then reload.'); return; }
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const sock = new WebSocket(scheme + location.host + '/ws/' + cfg.id + '?t=' + encodeURIComponent(ticket));
    sock.binaryType = 'arraybuffer';
    ws = sock;
    sock.onopen = () => { if (!size) measure(); sock.send(JSON.stringify({type:'hello', ...(cap||{}), ...(size||{})})); };
    sock.onmessage = onMessage;
    sock.onclose = () => { if (ws !== sock) return; ws = null; clearInput();
      if (closedForGood) return;
      if (retries < 5) { retries++; show('Reconnecting…'); setTimeout(connect, 1200 * retries); }
      else show('Connection lost. Reload this page to continue; your control is kept until you release it.'); };
  }
  btn.addEventListener('click', () => { disarmFinish(); if (controller === 'you') { btn.disabled = true; send({type:'release'}); } else send({type:'take'}); });
  // Finish is deliberate: a first tap arms it for a few seconds, a second tap sends it.
  function disarmFinish(){ if (finArm) { clearTimeout(finArm); finArm = null; } fin.textContent = 'Finish session'; fin.className = 'finish'; }
  fin.addEventListener('click', () => {
    if (fin.disabled) return;
    if (!finArm) { fin.textContent = 'Confirm finish'; fin.className = 'finish arm';
      finArm = setTimeout(disarmFinish, 5000); render(); return; }
    disarmFinish(); fin.disabled = true; send({type:'finish'}); });

  // ── input bridge (adapted from T3 Code, MIT) ─────────────────────────────
  function pagePoint(cx, cy, clamp){
    if (!viewport || canvas.width === 0 || canvas.height === 0 || controller !== 'you') return null;
    const rect = canvas.getBoundingClientRect();
    const fit = Math.min(rect.width / canvas.width, rect.height / canvas.height);
    const w = canvas.width * fit, h = canvas.height * fit;
    if (!(w > 0 && h > 0)) return null;
    const scale = viewport.width / w;
    const x = (cx - rect.left - (rect.width - w)/2) * scale, y = (cy - rect.top - (rect.height - h)/2) * (viewport.height / h);
    const inside = x >= 0 && y >= 0 && x <= viewport.width && y <= viewport.height;
    if (!inside && !clamp) return null;
    return {x: Math.min(Math.max(x,0), viewport.width), y: Math.min(Math.max(y,0), viewport.height), scale};
  }
  function clearInput(){ pendingWheel = null; probe = probeCache = touch = lastTap = null; keySent = false; input.value = SENTINEL; input.disabled = controller !== 'you'; if (controller !== 'you') input.blur(); }
  function flushWheel(){ wheelFrame = null; const w = pendingWheel; pendingWheel = null; if (w) send(w); }
  function queueWheel(p, dx, dy){ pendingWheel = {type:'wheel', x:p.x, y:p.y, deltaX:(pendingWheel?pendingWheel.deltaX:0)+dx, deltaY:(pendingWheel?pendingWheel.deltaY:0)+dy, modifiers:0}; if (!wheelFrame) wheelFrame = requestAnimationFrame(flushWheel); }
  function countClick(ev){ const l = lastTap; const c = l && ev.timeStamp - l.time < MULTI_MS && Math.hypot(ev.clientX-l.x, ev.clientY-l.y) < MULTI_SLOP ? l.count+1 : 1; lastTap = {time:ev.timeStamp, x:ev.clientX, y:ev.clientY, count:c}; return c; }
  function onProbe(r){
    if (controller !== 'you') return;
    const cur = probe; if (!cur || cur.x !== r.x || cur.y !== r.y) return;
    probeCache = {clientX:cur.clientX, clientY:cur.clientY, editable:r.editable, time:performance.now()};
    if (cur.end === 'touching') { cur.editable = r.editable; return; }
    probe = null; if (cur.end === 'panned') return;
    if (cur.end === 'acted' && cur.editable === r.editable) return;
    if (r.editable) input.focus({preventScroll:true}); else input.blur();
  }
  const nativeTouch = 'ontouchstart' in window;
  const startPointer = ev => {
    if (controller !== 'you' || !ev.isPrimary) return; ev.preventDefault();
    if (ev.pointerType !== 'touch') { const p = pagePoint(ev.clientX, ev.clientY, false); if (!p) return; input.focus({preventScroll:true});
      const cc = countClick(ev); send({type:'mouse', action:'down', x:p.x, y:p.y, button:'left', buttons:1, clickCount:cc, modifiers:0}); return; }
    touch = {start:ev, last:ev, panning:false};
    const p = pagePoint(ev.clientX, ev.clientY, false); if (!p) { probe = null; return; }
    const c = probeCache, reuse = c && performance.now()-c.time < PROBE_MS && Math.hypot(ev.clientX-c.clientX, ev.clientY-c.clientY) <= PROBE_PX;
    probe = {x:p.x, y:p.y, clientX:ev.clientX, clientY:ev.clientY, editable: reuse ? c.editable : null, end:'touching'};
    send({type:'probe', x:p.x, y:p.y});
  };
  const movePointer = ev => {
    if (controller !== 'you') return;
    if (ev.pointerType !== 'touch') { const p = pagePoint(ev.clientX, ev.clientY, true); if (p) send({type:'mouse', action:'move', x:p.x, y:p.y, button: ev.buttons&1 ? 'left':'none', buttons:ev.buttons&7, clickCount:0, modifiers:0}); return; }
    if (!touch || touch.start.pointerId !== ev.pointerId) return;
    if (!touch.panning && Math.hypot(ev.clientX-touch.start.clientX, ev.clientY-touch.start.clientY) < TAP_SLOP) return;
    touch.panning = true;
    const p = pagePoint(ev.clientX, ev.clientY, true);
    if (p) queueWheel(p, (touch.last.clientX-ev.clientX)*p.scale, (touch.last.clientY-ev.clientY)*p.scale);
    touch.last = ev;
  };
  const end = ev => {
    const cancelled = ev.type === 'pointercancel';
    if (ev.pointerType !== 'touch') { const p = pagePoint(ev.clientX, ev.clientY, true); if (p) send({type:'mouse', action:'up', x:p.x, y:p.y, button:'left', buttons:0, clickCount:1, modifiers:0}); return; }
    const t = touch; if (!t || t.start.pointerId !== ev.pointerId) return; touch = null;
    const a = probe;
    if (cancelled || t.panning) { if (a) a.end = 'panned'; flushWheel(); return; }
    const p = pagePoint(ev.clientX, ev.clientY, false); if (!p) return;
    if (a && a.editable === true) input.focus({preventScroll:true}); else if (a && a.editable === false) input.blur();
    if (a) a.end = a.editable === null ? 'late' : 'acted';
    flushWheel();
    const cc = countClick(ev), at = {x:p.x, y:p.y, modifiers:0};
    send({type:'mouse', action:'move', ...at, button:'none', buttons:0, clickCount:0});
    send({type:'mouse', action:'down', ...at, button:'left', buttons:1, clickCount:cc});
    send({type:'mouse', action:'up', ...at, button:'left', buttons:0, clickCount:cc});
  };
  // iOS Safari delivers Touch Events reliably during a finger pan. Use one
  // event family for touch so browsers emitting both cannot double the gesture.
  const pointer = fn => ev => { if (!(nativeTouch && ev.pointerType === 'touch')) fn(ev); };
  canvas.addEventListener('pointerdown', pointer(startPointer));
  canvas.addEventListener('pointermove', pointer(movePointer));
  canvas.addEventListener('pointerup', pointer(end));
  canvas.addEventListener('pointercancel', pointer(end));
  if (nativeTouch) {
    const finger = (ev, ending) => {
      const list = ending ? ev.changedTouches : ev.touches;
      if (!list.length || (!ending && ev.touches.length !== 1)) return null;
      const t = list[0];
      return {clientX:t.clientX, clientY:t.clientY, pointerId:t.identifier, pointerType:'touch',
              isPrimary:true, timeStamp:ev.timeStamp, type:ev.type === 'touchcancel' ? 'pointercancel' : '',
              preventDefault:() => { if (ev.cancelable) ev.preventDefault(); }};
    };
    canvas.addEventListener('touchstart', ev => { const f=finger(ev,false); if(f) startPointer(f); }, {passive:false});
    canvas.addEventListener('touchmove', ev => {
      const f=finger(ev,false); if(!f) return;
      if(controller === 'you' && ev.cancelable) ev.preventDefault();
      movePointer(f);
    }, {passive:false});
    for(const name of ['touchend','touchcancel']) canvas.addEventListener(name, ev => {
      const f=finger(ev,true); if(f) end(f);
    }, {passive:false});
  }
  canvas.addEventListener('wheel', ev => { const p = pagePoint(ev.clientX, ev.clientY, false); if (!p) return; ev.preventDefault();
    const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? (viewport ? viewport.height : 0) : 1; queueWheel(p, ev.deltaX*unit, ev.deltaY*unit); }, {passive:false});
  canvas.addEventListener('contextmenu', ev => ev.preventDefault());
  const onKey = (action, ev) => {
    if (controller !== 'you') return;
    if (ev.isComposing || ev.keyCode === 229 || ev.key === 'Process' || ev.key === 'Unidentified') return;
    const shortcut = ev.ctrlKey || ev.metaKey;
    if (shortcut && ev.key.toLowerCase() === 'v') return;
    const text = shortcut ? undefined : [...ev.key].length === 1 ? ev.key : ev.key === 'Enter' ? '\r' : undefined;
    send({type:'key', action, key:ev.key, code:ev.code, keyCode:ev.keyCode, ...(action==='down' && text!==undefined ? {text} : {}),
          modifiers:(ev.altKey?1:0)|(ev.ctrlKey?2:0)|(ev.metaKey?4:0)|(ev.shiftKey?8:0)});
    keySent = action === 'down' && !shortcut && (text !== undefined || ev.key === 'Backspace' || ev.key === 'Delete');
    if (!shortcut) ev.preventDefault();
  };
  const resetInput = () => { input.value = SENTINEL; input.setSelectionRange(SENTINEL.length, SENTINEL.length); };
  const pressKey = (key, keyCode) => { for (const action of ['down','up']) send({type:'key', action, key, code:key, keyCode, modifiers:0}); };
  input.addEventListener('keydown', ev => onKey('down', ev));
  input.addEventListener('keyup', ev => onKey('up', ev));
  input.addEventListener('input', ev => {
    if (ev.isComposing) return;
    const it = ev.inputType || '';
    if (keySent) keySent = false;
    else if (it === 'deleteContentBackward') pressKey('Backspace', 8);
    else if (it === 'deleteContentForward') pressKey('Delete', 46);
    else { const v = input.value, t = v.startsWith(SENTINEL) ? v.slice(SENTINEL.length) : v; if (t) send({type:'text', text:t}); }
    resetInput();
  });
  input.addEventListener('compositionend', ev => { const t = ev.data.startsWith(SENTINEL) ? ev.data.slice(SENTINEL.length) : ev.data; if (t) send({type:'text', text:t}); resetInput(); });
  input.addEventListener('focus', resetInput);
  input.addEventListener('copy', ev => ev.preventDefault()); input.addEventListener('cut', ev => ev.preventDefault());
  resetInput(); render(); connect();
})();
"""


def viewer_page(info: dict, csrf: str, nonce: str) -> str:
    config = json.dumps({"id": info["id"], "csrf": csrf, "purpose": info["purpose"]}).replace("<", "\\u003c")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><title>Hermes browser</title>
<style nonce="{nonce}">{_STYLE}</style></head>
<body><div id="bar"><div class="site"><span id="site">Hermes browser</span><small id="sub">Hermes</small></div>
<button id="fin" class="finish" hidden disabled>Finish session</button><button id="ctl" disabled>Take control</button></div>
<div id="stage"><canvas id="cv" role="img" aria-label="Browser page"></canvas></div>
<textarea id="kb" aria-label="Browser page input" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false" disabled></textarea>
<div id="msg"></div>
<script nonce="{nonce}">window.__HBH__={config};</script>
<script nonce="{nonce}">{_SCRIPT}</script></body></html>"""


def finished_page(nonce: str, state: str | None) -> str:
    text = {"returned": "This page was handed back to Hermes.", "finished": "This session was finished from the phone.",
            "cancelled": "Hermes cancelled this handover.",
            "expired": "This link expired before it was used.", None: "This link is not active."}.get(state,
            "This handover has finished.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes browser</title><style nonce="{nonce}">{_STYLE}</style></head>
<body><main class="finished"><h1>Hermes</h1><p>{html.escape(text)}</p></main></body></html>"""



def demo_page(nonce: str = "") -> str:
    """Harmless synthetic form with obviously fake prefilled values. No external requests."""
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Handover demo form</title><style nonce="__DEMO_NONCE__">
body{font:17px system-ui,sans-serif;margin:0;padding:20px;background:#f6f7f9;color:#1b2430}
main{max-width:520px;margin:0 auto;background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 18px #0001}
label{display:block;margin:14px 0 6px;font-weight:600}input,select,textarea{box-sizing:border-box;width:100%;padding:12px;font-size:17px;border:1px solid #9aa5b1;border-radius:8px}
button{margin-top:18px;width:100%;padding:14px;font-size:17px;border:0;border-radius:8px;background:#2a5bd7;color:#fff}
#out{margin-top:16px;padding:12px;background:#eef3ff;border-radius:8px;min-height:24px;word-break:break-word}
.tall{height:900px;background:repeating-linear-gradient(#fafbfc 0 40px,#eef1f4 40px 80px);border-radius:8px;margin-top:18px;display:flex;align-items:center;justify-content:center;color:#6b7785}
</style></head><body><main>
<h1 style="font-size:22px;margin:0 0 6px">Synthetic demo form</h1>
<p style="margin:0;color:#4b5563">Fake data only. Nothing here is sent anywhere.</p>
<form id="f" onsubmit="return false">
<label for="name">Name</label><input id="name" name="name" value="Demo Person" autocomplete="off">
<label for="email">Email</label><input id="email" name="email" type="email" value="demo@example.invalid" autocomplete="off">
<label for="plate">Vehicle plate</label><input id="plate" name="plate" value="DEMO123" autocomplete="off">
<label for="notes">Notes</label><textarea id="notes" name="notes" rows="3">prefilled note</textarea>
<label for="pick">Option</label><select id="pick" name="pick"><option>Standard</option><option selected>Covered</option><option>Premium</option></select>
<button id="go" type="button">Confirm (demo only)</button>
</form><div id="out" aria-live="polite"></div>
<div class="tall">scroll area</div>
</main><script nonce="__DEMO_NONCE__">
document.getElementById('go').addEventListener('click',function(){
  var f=document.getElementById('f');document.getElementById('out').textContent='Confirmed locally: '+f.name.value+' / '+f.email.value+' / '+f.plate.value+' / '+f.pick.value+' / '+f.notes.value;
});
</script></body></html>"""

    return page.replace("__DEMO_NONCE__", nonce)
