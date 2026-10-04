// 用 jsdom 真实渲染 step-6 的报告，验证「🧠 你的知识画像」这一节能正常出现。
//
// 来历：知识画像是两阶段分析的第一轮产物，靠后端注入 report.knowledge_profile。
// 前端一旦取错字段名或者对缺失字段不免疫，报告页会整块空白 —— 而这类问题
// 静态检查看不见（字段来自 JSON，不是 DOM id）。所以真的渲染一次。
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  ({ JSDOM } = require(path.join(
    process.env.HOME, '.workbuddy/binaries/node/workspace/node_modules/jsdom')));
}

const html = fs.readFileSync(path.resolve(__dirname, '../../frontend/index.html'), 'utf8');
const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://127.0.0.1:8777/' });
const { window } = dom;

const routes = {
  'POST /api/auth/session': { uid: 'uid-test' },
  'POST /api/llm/config/': { status: 'ok' },
  'GET /api/data/result/': { books: [], stats: {}, localFiles: [] },
  'GET /api/analysis/estimate/': {
    chars: 100, tokens: 42, stage1_tokens: 25, stage2_tokens: 17, books: 3,
    kept: { bookmarks: true, reviews: true, bookReviews: true, ratedOnly: false },
    counts: { bookmarks: 3, reviews: 2, bookReviews: 1, ratedBooks: 1 },
  },
};
routes.__quiet = true;

window.fetch = async (url, opts) => {
  const method = (opts && opts.method) || 'GET';
  const clean = String(url).replace(/^http:\/\/[^/]+/, '')
    .replace(/\?.*$/, '').replace('uid-test', '').split('/').filter(Boolean).join('/');
  const key = `${method} /${clean}`;
  const data = routes[key] || routes[`${method} /${clean}/`];
  return { ok: data !== undefined, status: data === undefined ? 404 : 200,
           json: async () => data || { detail: 'not found' } };
};
window.EventSource = function () { this.close = () => {}; this.readyState = 0; };

const errs = [];
window.addEventListener('error', (e) => errs.push(String(e.message || e.error)));

function check(name, cond, extra) {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${extra && !cond ? ' — ' + extra : ''}`);
  if (!cond) process.exitCode = 1;
}

function reportBox() {
  return window.document.getElementById('ai-report');
}

(async () => {
  await new Promise((r) => window.document.addEventListener('DOMContentLoaded', r));
  await window.ensureUid();

  console.log('[1] 有知识画像时渲染出画像区块');
  window.renderAiReport({
    overall_score: 7,
    summary: '总评',
    knowledge_profile: {
      headline: '一个爱追问机制的人',
      axes: [
        { name: '关注领域', line: '集中在制度与技术如何互相塑造' },
        { name: '思维方式', line: '追因果链，不接受结论式断言' },
      ],
      signature_quotes: [
        { book: '置身事内', text: '政府不只是裁判', note: '一眼看穿结构' },
      ],
      blind_spots: ['很少写自己的反面意见'],
    },
    profile_note: '书目里的小说比痕迹显示的多。',
    dimensions: [{ name: '思考质量', score: 8, comment: 'c', evidence: 'e' }],
    strengths: ['s'], weaknesses: ['w'],
    book_picks: { top: [{ title: 'A', reason: 'r' }], flop: [] },
    recommendations: ['r1'], one_liner: '一句话',
  });

  const box = reportBox();
  check('画像区块存在', !!box.querySelector('.kp-box'));
  check('标题正确', /🧠 你的知识画像/.test(box.textContent));
  check('headline 渲染', /一个爱追问机制的人/.test(box.textContent));
  const axes = box.querySelectorAll('.kp-axis');
  check('两个维度都渲染', axes.length === 2, `实际 ${axes.length}`);
  check('维度名与结论都在', /关注领域/.test(axes[0].textContent)
    && /制度与技术/.test(axes[0].textContent));
  check('代表性原话带书名', /《置身事内》/.test(box.textContent));
  check('证据缺口渲染', /很少写自己的反面意见/.test(box.textContent));
  check('第二轮补充渲染', /第二轮补充/.test(box.textContent));
  check('原有维度评分未被挤掉', /思考质量/.test(box.textContent));

  console.log('\n[2] 第一轮失败（无画像）时报告照常渲染');
  window.renderAiReport({
    overall_score: 6, summary: '没有画像的报告',
    dimensions: [{ name: '阅读广度', score: 6, comment: 'c' }],
    strengths: ['s'], weaknesses: [], book_picks: { top: [], flop: [] },
    recommendations: [], one_liner: 'x',
  });
  check('无画像区块', !reportBox().querySelector('.kp-box'));
  check('报告主体仍在', /没有画像的报告/.test(reportBox().textContent));
  check('维度评分仍在', /阅读广度/.test(reportBox().textContent));

  console.log('\n[3] 画像字段残缺时不炸');
  window.renderAiReport({
    overall_score: 5, summary: 's', knowledge_profile: { headline: '' },
    dimensions: [], strengths: [], weaknesses: [], book_picks: {},
    recommendations: [], one_liner: 'x',
  });
  check('空 headline 不渲染画像区块', !reportBox().querySelector('.kp-box'));
  window.renderAiReport({
    overall_score: 5, summary: 's',
    knowledge_profile: { headline: 'h', axes: [{ name: '思维方式' }] },
    dimensions: [], strengths: [], weaknesses: [], book_picks: {},
    recommendations: [], one_liner: 'x',
  });
  check('缺 line 的维度不炸', /思维方式/.test(reportBox().textContent));

  console.log('\n[4] 运行期没有未捕获错误');
  check('无 error 事件', errs.length === 0, errs.join(' | '));

  console.log(process.exitCode ? '\n有失败项 ✗' : '\njsdom 报告渲染全部通过 ✓');
  window.close();
  process.exit(process.exitCode || 0);
})();
