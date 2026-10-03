#!/usr/bin/env node
/**
 * 前端 apiUrl 的URL 拼接验证。
 *
 * 为什么单独一个脚本：`apiUrl('/api/x/') + uid` 这种写法在本地（没设
 * ACCESS_TOKEN）完全正常，一旦部署到公网设了口令就变成
 * `?token=口令uid` —— 口令与 uid 粘连，路由 404、二维码空白、SSE 连不上。
 * 这个 bug 上线过一次，而本地怎么测都测不出来，所以只能静态验证。
 *
 * 做法：把 index.html 里真实的 apiUrl 抠出来，带着假的 location /
 * ACCESS_TOKEN 跑一遍，逐个断言最终 URL。
 *
 * CI 与本地均可运行：node frontend_url_test.js
 */

const fs = require('fs');
const path = require('path');

// 用 cwd 定位而不是 __dirname：部分运行时的 shim 会把 __dirname 解析偏
const root = process.cwd();
const target = fs.existsSync(path.join(root, 'frontend', 'index.html'))
  ? path.join(root, 'frontend', 'index.html')
  : path.join(__dirname, 'frontend', 'index.html');

const html = fs.readFileSync(target, 'utf8');

let bad = 0;
const fail = (m) => { console.error('✗ ' + m); bad++; };
const pass = (m) => console.log('✓ ' + m);

// ── 抠出 apiUrl ────────────────────────────────────────
// 按大括号配对截，不能用正则：函数体里的注释含有 } 会把截取截断。
const start = html.indexOf('function apiUrl(');
if (start < 0) {
  console.error('✗ 找不到 apiUrl 定义');
  process.exit(1);
}
let depth = 0, end = -1;
const open = html.indexOf('{', start);
for (let i = open; i < html.length; i++) {
  if (html[i] === '{') depth++;
  else if (html[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
const fnSrc = html.slice(start, end);

// API_BASE 是三元表达式，按分号截
const bm = html.match(/const API_BASE\s*=\s*([\s\S]*?);/);
if (!bm) {
  console.error('✗ 找不到 API_BASE 定义');
  process.exit(1);
}

// 用假的浏览器全局把apiUrl 编译成可调用的函数
const build = (token) => new Function(
  "const location={protocol:'https:',origin:'https://example.test'};\n"
  + 'const ACCESS_TOKEN=' + JSON.stringify(token) + ';\n'
  + 'const API_BASE=' + bm[1] + ';\n'
  + fnSrc + '\nreturn apiUrl;'
)();

let authed, anon;
try {
  authed = build('test-token-123');
  anon = build('');
} catch (e) {
  console.error('✗ apiUrl 无法编译: ' + e.message);
  process.exit(1);
}

// ── 1. 带口令：uid/sid 落在路径段，token 独立在 query ──
for (const [p, tail] of [
  ['/api/browser/qrcode/', 'abc123'],
  ['/api/browser/status/', 'sid42'],
  ['/api/data/result/', 'uid7'],
  ['/api/auth/cookies/', 'uid9'],
]) {
  const want = `https://example.test${p}${tail}?token=test-token-123`;
  const got = authed(p, tail);
  if (got !== want) fail(`${p} 拼接错误\n  实际: ${got}\n  期望: ${want}`);
  else pass(got);
}

// ── 2. 口令与 id 不能粘连（线上 404 的直接原因）────────
if (authed('/api/browser/qrcode/', 'abc123').includes('test-token-123abc123')) {
  fail('token 与 uid 粘在一起了');
}

// ── 3. 无口令（本地双击启动）：行为不能变，且不能带 token ──
const localWant = 'https://example.test/api/browser/qrcode/abc123';
const localGot = anon('/api/browser/qrcode/', 'abc123');
if (localGot !== localWant) fail(`无口令时 URL 变了\n  实际: ${localGot}\n  期望: ${localWant}`);
else pass(localGot);

// ── 4. 老写法（apiUrl(x) + y）必须清零 ────────────────
const legacy = html.match(/apiUrl\([^)]*\)\s*\+/g);
if (legacy) fail('仍有 apiUrl(x) + y 老写法: ' + legacy.slice(0, 3).join(', '));
else pass('没有 apiUrl(x) + y 老写法');

console.log(bad ? `\n${bad} 项失败` : '\n全部通过');
process.exit(bad ? 1 : 0);