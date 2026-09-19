// -*- coding: utf-8 -*-
// 独立版 AirDrop 前端上传状态机 jsdom 验证
// 用 jsdom 自动执行真实上传脚本，验证：小文件整传成功 / 暂停 / 继续恢复 / 取消 / 卡住检测。
// 方法：读 standalone/templates/index.html，替换 Jinja；runScripts:'dangerously' + beforeParse 注入
//       mock XHR/fetch/confirm/时钟，让脚本在真实 DOMContentLoaded 时序下自动绑定；触发 UI 后断言 DOM 状态。
// 运行：node tools/verify_chunk_ui_jsdom.js
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const TPL = path.join(__dirname, '..', 'standalone', 'templates', 'index.html');
let html = fs.readFileSync(TPL, 'utf8');
html = html.replace(/\{\{\s*chunk_size_mb\s*\|\s*default\(\s*8\s*\)\s*\}\}/g, '8');
html = html.replace(/\{\{ expire_hours \}\}/g, '24');
html = html.replace(/\{\{ max_gb \}\}/g, '1');
html = html.replace(/\{\{ port \}\}/g, '9000');
html = html.replace(/\{%[^%]*%\}/g, '');
html = html.replace(/\{\{[^}]*\}\}/g, '');

const passed = [];
const failed = [];
function check(name, cond, detail) {
  if (cond) { passed.push(name); console.log(`  PASS  ${name}`); }
  else { failed.push(name); console.log(`  FAIL  ${name}  ${detail || ''}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeEnv() {
  let clock = 0;
  let hang = false;
  const intervalCallbacks = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => {
    if (/Not implemented/.test(e.message || '')) return;
    console.log('  [jsdomError]', (e.message || '').slice(0, 200));
  });
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:9000/',
    virtualConsole,
    beforeParse(window) {
      window.Date.now = () => clock;
      window.setInterval = (fn) => { intervalCallbacks.push(fn); return intervalCallbacks.length; };
      window.clearInterval = () => {};
      window.confirm = () => true;
      window.crypto = window.crypto || {};
      if (!window.crypto.randomUUID) {
        window.crypto.randomUUID = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
          const r = Math.random() * 16 | 0; const v = c === 'x' ? r : (r & 0x3 | 0x8); return v.toString(16);
        });
      }
      window.XMLHttpRequest = class MockXHR {
        constructor() { this.upload = {}; this.readyState = 0; this.status = 0; this.responseText = ''; }
        open(m, u) { this.method = m; this.url = u; this.readyState = 1; }
        setRequestHeader() {}
        send() {
          if (hang) return; // 挂起：模拟网络停滞
          this.readyState = 4;
          this.status = 200;
          let payload = { status: 'ok' };
          if (this.url === '/upload') payload = { status: 'success' };
          this.responseText = JSON.stringify(payload);
          if (this.upload.onprogress) this.upload.onprogress({ lengthComputable: true, loaded: 1, total: 1 });
          if (this.onload) this.onload();
          if (this.onreadystatechange) this.onreadystatechange();
        }
        abort() { if (this.onabort) this.onabort(); }
      };
      window.fetch = async (url) => {
        if (url.includes('/upload/status')) return { json: async () => ({ status: 'ok', received: [], total_chunks: 1 }) };
        if (url.includes('/upload/complete')) return { json: async () => ({ status: 'success', msg: '上传成功: t.bin' }) };
        if (url.includes('/upload/abort')) return { json: async () => ({ status: 'ok' }) };
        throw new Error('unexpected fetch: ' + url);
      };
    },
  });
  const { window } = dom;
  const doc = window.document;
  function selectFile(name, content) {
    const input = doc.getElementById('file-input');
    const file = new window.File([content], name, { type: 'text/plain' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new window.Event('change', { bubbles: true }));
  }
  const state = () => ({
    text: doc.getElementById('progressText').textContent,
    pause: doc.getElementById('pauseBtn').style.display,
    resume: doc.getElementById('resumeBtn').style.display,
    cancel: doc.getElementById('cancelBtn').style.display,
    hint: doc.getElementById('progressHint').style.display,
    hintText: doc.getElementById('progressHint').textContent,
    width: doc.getElementById('progressBar').style.width,
    uploadBtnDisabled: doc.getElementById('uploadBtn').disabled,
  });
  return {
    doc, state, selectFile,
    setHang: (v) => { hang = v; },
    advanceClock: (ms) => { clock += ms; },
    intervalCallbacks,
  };
}

(async () => {
  // ================= 1) 元素存在 =================
  console.log('[1] 上传控制元素存在');
  const env = makeEnv();
  await sleep(80); // 等 DOMContentLoaded 派发 + 事件绑定
  for (const id of ['pauseBtn', 'resumeBtn', 'cancelBtn', 'progressHint']) {
    check(`元素 #${id} 存在`, !!env.doc.getElementById(id));
  }

  // ================= 2) 小文件整传成功 =================
  console.log('[2] 小文件整传成功');
  env.selectFile('small.txt', 'hello standalone chunk');
  env.doc.getElementById('uploadBtn').click();
  await sleep(20);
  const s2 = env.state();
  check('进度到 100%', s2.width === '100%', s2.width);
  check('提示上传成功', s2.text === '上传成功，页面即将刷新', s2.text);
  check('控制按钮隐藏', s2.pause === 'none' && s2.resume === 'none', `${s2.pause}/${s2.resume}`);

  // ================= 3) 暂停 =================
  console.log('[3] 暂停');
  env.setHang(true);
  env.selectFile('pause.txt', 'pause content');
  env.doc.getElementById('uploadBtn').click();
  await sleep(10);
  env.doc.getElementById('pauseBtn').click();
  await sleep(10);
  const s3 = env.state();
  check('暂停后显示「继续」', s3.resume === 'inline-block', s3.resume);
  check('暂停提示出现', s3.hint === 'block', s3.hint);
  check('暂停提示文案', /已暂停/.test(s3.hintText), s3.hintText);
  check('进度仍保持百分比', s3.text === '0%', s3.text);

  // ================= 4) 继续恢复 =================
  console.log('[4] 继续恢复');
  env.setHang(false);
  env.doc.getElementById('resumeBtn').click();
  await sleep(20);
  const s4 = env.state();
  check('恢复后上传成功', s4.text === '上传成功，页面即将刷新', s4.text);
  check('恢复后进度 100%', s4.width === '100%', s4.width);

  // ================= 5) 取消 =================
  console.log('[5] 取消');
  env.setHang(true);
  env.selectFile('cancel.txt', 'cancel content');
  env.doc.getElementById('uploadBtn').click();
  await sleep(10);
  env.doc.getElementById('cancelBtn').click();
  await sleep(10);
  const s5 = env.state();
  check('取消后进度容器隐藏', s5.pause === 'none' && s5.resume === 'none', `${s5.pause}/${s5.resume}`);
  check('取消后「开始上传」重新可用', s5.uploadBtnDisabled === false, `disabled=${s5.uploadBtnDisabled}`);

  // ================= 6) 卡住检测（20s 无进展 → 提示） =================
  console.log('[6] 卡住检测');
  const env6 = makeEnv();
  await sleep(80);
  env6.selectFile('stuck.txt', 'stuck content');
  env6.setHang(true);
  const beforeLen = env6.intervalCallbacks.length;
  env6.doc.getElementById('uploadBtn').click();
  await sleep(10);
  env6.advanceClock(21000);
  const stuckCb = env6.intervalCallbacks[env6.intervalCallbacks.length - 1];
  if (typeof stuckCb === 'function') stuckCb();
  await sleep(10);
  const s6 = env6.state();
  check('卡住后显示「继续」', s6.resume === 'inline-block', s6.resume);
  check('卡住提示出现', s6.hint === 'block', s6.hint);
  check('卡住提示文案含解决建议', /卡住/.test(s6.hintText) && /继续/.test(s6.hintText), s6.hintText);

  console.log('\n' + '='.repeat(50));
  console.log(`结果: ${passed.length} 通过, ${failed.length} 失败`);
  if (failed.length) { console.log('失败项: ' + failed.join(', ')); process.exit(1); }
  console.log('全部通过');
})();
