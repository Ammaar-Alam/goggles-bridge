// Offline UI regression checks. Run: node --test tests/test_virtual_ui.mjs
// Uses an installed Edge/Chromium; set GOGGLES_UI_BROWSER to override its path.
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const browser = process.env.GOGGLES_UI_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const available = existsSync(browser);
const page = pathToFileURL(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../goggles_bridge/app/ui/index.html'));
const captures = process.env.GOGGLES_UI_SCREENSHOTS;
const errors = [];
let processHandle, profile, socket, sequence = 0;
const pending = new Map(), events = new Map();

async function until(check, description) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await check()) return;
    await delay(100);
  }
  throw new Error(`Timed out: ${description}`);
}
function call(method, params = {}) {
  const id = ++sequence;
  let timeout;
  return new Promise((resolve, reject) => {
    timeout = setTimeout(() => reject(new Error(`Browser debugger timed out: ${method}`)), 10000);
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  }).finally(() => {
    clearTimeout(timeout);
    pending.delete(id);
  });
}
function event(method) {
  return new Promise(resolve => events.set(method, resolve));
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  assert.equal(result.exceptionDetails, undefined, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function open(query = '', width = 1120, height = 740) {
  await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  const loaded = event('Page.loadEventFired');
  await call('Page.navigate', { url: `${page}?demo&${query}` });
  await loaded;
  await until(() => evaluate("document.querySelector('#version').textContent.includes('Goggles Bridge')"), 'UI ready');
}
async function capture(name) {
  if (!captures) return;
  await mkdir(captures, { recursive: true });
  const { data } = await call('Page.captureScreenshot', { format: 'png' });
  await writeFile(path.join(captures, `${name}.png`), Buffer.from(data, 'base64'));
}
const visible = key => evaluate(`!document.querySelector('[data-option="${key}"]').hidden`);
const uiTest = (name, run) => test(name, { skip: !available, timeout: 45000 }, run);

before(async () => {
  if (!available) return;
  profile = await mkdtemp(path.join(tmpdir(), 'goggles-ui-'));
  processHandle = spawn(browser, ['--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  const portFile = path.join(profile, 'DevToolsActivePort');
  await until(() => existsSync(portFile), 'browser debugger');
  const port = (await readFile(portFile, 'utf8')).split('\n')[0];
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(5000) })).json();
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return; // A response can arrive after its request timed out.
      message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    if (events.has(message.method)) {
      events.get(message.method)(message.params);
      events.delete(message.method);
    }
  });
  await call('Page.enable');
  await call('Runtime.enable');
  await call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
}, { timeout: 15000 });
after(async () => {
  if (!available) return;
  if (socket?.readyState === WebSocket.OPEN) await call('Browser.close').catch(() => {});
  socket?.close();
  events.clear();
  processHandle?.kill();
  if (profile) {
    assert.equal(path.dirname(profile), path.resolve(tmpdir()));
    assert.ok(path.basename(profile).startsWith('goggles-ui-'));
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  }
});

uiTest('physical and virtual source round trip preserves advanced controls and values', async () => {
  await open();
  for (const key of ['source', 'display_index', 'capture', 'h264_profile', 'adaptive_quant', 'keyframe_seconds', 'preset', 'low_latency', 'usb_write_size']) assert.equal(await visible(key), true, key);
  assert.equal(await evaluate("document.querySelector('#opt-source option[value=virtual]').textContent"), 'Virtual Display');
  await evaluate("window.demoBackend.set_options({capture:'ddagrab',h264_profile:'baseline',adaptive_quant:true,keyframe_seconds:1,source:'virtual'})");
  await until(async () => !(await visible('capture')), 'virtual controls');
  assert.equal(await visible('display_index'), false);
  for (const key of ['virtual_resolution', 'h264_profile', 'adaptive_quant', 'keyframe_seconds', 'preset', 'low_latency', 'usb_write_size']) assert.equal(await visible(key), true, key);
  await capture('virtual-advanced');
  await evaluate("window.demoBackend.set_options({source:'display'})");
  await until(() => visible('capture'), 'physical controls restored');
  assert.equal(await evaluate("document.querySelector('#opt-capture').value"), 'ddagrab');
  assert.equal(await evaluate("document.querySelector('#opt-h264_profile').value"), 'baseline');
});
uiTest('driver setup locks options and foreign drivers are left alone', async () => {
  await open('state=novirtual', 940, 660);
  assert.equal(await evaluate("document.querySelector('#action').textContent"), 'Open setup guide');
  await evaluate("document.querySelector('#action').click()");
  assert.equal(await evaluate("document.querySelector('#guide-action').textContent"), 'Install display driver');
  assert.equal(await evaluate("document.querySelector('[data-option=virtual_driver]')"), null);
  await evaluate("document.querySelector('#guide-action').click()");
  await until(() => evaluate("document.querySelector('#options').disabled"), 'driver setup lock');
  await until(() => evaluate("!document.querySelector('#options').disabled"), 'driver setup finished');
  await open('state=foreignvirtual');
  await evaluate("document.querySelector('#action').click()");
  assert.equal(await evaluate("document.querySelector('#guide-action').disabled"), true);
  assert.equal(await evaluate("document.querySelector('#guide-remove-display').hidden"), true);
  await capture('foreign-driver');
});
uiTest('mac keeps its original options without virtual-display controls', async () => {
  await open('mac');
  for (const key of ['source', 'virtual_resolution', 'capture']) assert.equal(await visible(key), false, key);
  for (const key of ['display_index', 'keyframe_seconds', 'h264_profile', 'low_latency', 'usb_write_size']) assert.equal(await visible(key), true, key);
  assert.deepEqual(errors, []);
});

uiTest('unverified old installation explains removal instead of offering unsafe repair', async () => {
  await open('state=previousvirtual');
  await evaluate("document.querySelector('#action').click()");
  assert.equal(await evaluate("document.querySelector('#guide-action').textContent"), 'Uninstall previous driver first');
  assert.equal(await evaluate("document.querySelector('#guide-action').disabled"), true);
  assert.equal(await evaluate("document.querySelector('#guide-remove-display').hidden"), true);
  assert.match(await evaluate("document.querySelector('#guide-status-text').textContent"), /original app or its Windows uninstall entry/);
  await evaluate("window.demoBackend.set_options({source:'display'})");
  await until(() => evaluate("!document.querySelector('#guide-action').disabled"), 'physical source remains available');
  assert.equal(await evaluate("document.querySelector('#guide-action').textContent"), 'Continue');
});

uiTest('720p desktop is described as enlarged 1080p output', async () => {
  await open('state=streaming');
  await evaluate("window.demoBackend.set_options({source:'virtual',virtual_resolution:'720p'})");
  await until(() => evaluate("document.querySelector('#subline').textContent.includes('Enlarged from a 1280 × 720 desktop')"), 'resolution description');
  assert.match(await evaluate("document.querySelector('#subline').textContent"), /1920 × 1080/);
});
uiTest('repair and removal remain available in the guide', async () => {
  await open('state=brokenvirtual');
  await evaluate("window.demoBackend.set_options({source:'display'})");
  await until(() => visible('capture'), 'physical source');
  await evaluate("document.querySelector('#open-guide').click()");
  assert.equal(await evaluate("document.querySelector('#guide-action').textContent"),'Repair display driver');
  assert.equal(await evaluate("document.querySelector('#guide-remove-display').hidden"),false);
  await evaluate("document.querySelector('#guide-action').click()");
  await until(() => evaluate("document.querySelector('#guide-status-text').textContent.includes('ready')"),'repair complete');
  await evaluate("document.querySelector('#guide-remove-display').click()");
  await until(() => evaluate("document.querySelector('#guide-remove-display').hidden"),'driver removed');
  assert.equal(await evaluate("document.querySelector('#guide-action').textContent"), 'Continue');
});
