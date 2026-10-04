// 把 index.html 内联 script 抠出来做语法检查（只解析，不执行）。
// 来历：批量改动单文件前端时，括号计数不可信，必须让 JS 引擎自己判一次。
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const file = path.resolve(__dirname, 'frontend/index.html');
const html = fs.readFileSync(file, 'utf8');

const re = /<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi;
let m, idx = 0, failed = 0;
while ((m = re.exec(html)) !== null) {
  idx++;
  const code = m[1];
  try {
    new vm.Script(code, { filename: `index.html#script${idx}` });
    console.log(`script#${idx}: OK (${code.split('\n').length} lines)`);
  } catch (e) {
    failed++;
    console.log(`script#${idx}: SYNTAX ERROR\n  ${e.message}`);
  }
}
// 第二条：$('id') / getElementById('id') 引用的节点必须在 HTML 里真实存在。
// 来历：hide('data-section') 引用了一个已删除的节点，classList 在 null 上抛
// TypeError，把整个点击处理器打断在第一行——按钮点了毫无反应。
const ids = new Set();
const idRe = /\bid="([^"]+)"/g;
while ((m = idRe.exec(html)) !== null) ids.add(m[1]);

const refs = new Set();
const refRe = /(?:\$|getElementById)\(\s*'([a-zA-Z][\w-]*)'\s*\)/g;
const scriptBodies = [];
re.lastIndex = 0;
while ((m = re.exec(html)) !== null) scriptBodies.push(m[1]);
for (const body of scriptBodies) {
  refRe.lastIndex = 0;
  let r;
  while ((r = refRe.exec(body)) !== null) refs.add(r[1]);
}

const missing = [...refs].filter((id) => !ids.has(id));
if (missing.length) {
  console.log(`\n引用了 ${missing.length} 个不存在的 id：`);
  missing.forEach((id) => console.log(`  - ${id}`));
  process.exit(1);
}
console.log(`id 引用检查: OK（${refs.size} 个引用，${ids.size} 个节点）`);

// 第三条：querySelector(All)('.cls') 用到的 class 必须在 HTML 里真实存在。
// 来历：两个「一键体验」按钮原先用同一个 id，只有第一个绑得上；改成按 class
// 绑定后，有人把按钮上的 class 又改回 id —— 选择器静默匹配到 0 个元素，
// 两个按钮一起失灵，而 id 引用检查看不见（没人再按 id 取它了）。
const classes = new Set();
const classRe = /\bclass="([^"]*)"/g;
while ((m = classRe.exec(html)) !== null) {
  m[1].split(/\s+/).forEach((c) => c && classes.add(c));
}

const selRefs = new Set();
const selRe = /querySelector(?:All)?\(\s*'\.([\w-]+)'\s*\)/g;
for (const body of scriptBodies) {
  selRe.lastIndex = 0;
  let r;
  while ((r = selRe.exec(body)) !== null) selRefs.add(r[1]);
}

const missingCls = [...selRefs].filter((c) => !classes.has(c));
if (missingCls.length) {
  console.log(`\n选择器引用了 ${missingCls.length} 个 HTML 里没有的 class：`);
  missingCls.forEach((c) => console.log(`  - .${c}`));
  process.exit(1);
}
console.log(`class 选择器检查: OK（${selRefs.size} 个选择器）`);
process.exit(failed ? 1 : 0);
