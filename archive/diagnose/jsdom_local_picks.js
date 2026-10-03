// 用 jsdom 真实渲染 step-4，验证本地文件面板与 ratedOnly 筛选不炸、能操作。
//
// 来历：这个项目吃过一次大亏 —— 事件处理器第一行引用了不存在的节点，
// TypeError 让按钮点了毫无反应，前后端都不报错。静态检查看不见这类问题，
// 只有真的点一次才知道。
const fs = require('fs');
const path = require('path');

// 本机装在托管 workspace 里，CI 上装在 ./node_modules，两边都要能跑
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

// 拦掉所有真实网络请求，喂我们自己的假数据
const routes = {
  // let currentUid 是词法变量，挂不到 window 上，只能走真实的建会话路径
  'POST /api/auth/session': { uid: 'uid-test' },
  'POST /api/llm/config/': { status: 'ok' },
  'GET /api/data/local-picks/': {
    slots: ['印象最深', '最想推荐', '启发性最大', '平时最有用'],
    files: [
      { bookId: 'L1', title: '2023年度技术规划.pdf', author: '', myRating: null,
        sourceSignals: ['书名带电子书扩展名'], totalBookmarks: 6, totalReviews: 3, totalBookReviews: 0 },
      { bookId: 'L2', title: '扫描件_城市研究笔记.epub', author: '', myRating: 4,
        sourceSignals: ['出版物元数据字段全空'], totalBookmarks: 4, totalReviews: 1, totalBookReviews: 0 },
    ],
    picks: {}, overrides: {},
  },
  'POST /api/data/local-picks/': {
    status: 'ok',
    picks: { L1: { slot: '最想推荐', note: '这份规划后来兑现了七成' } },
  },
  'POST /api/data/source-override/': { status: 'ok', overrides: { L1: 'weread' } },
  'POST /api/data/content-filter/': {
    status: 'ok',
    content_filter: { keepBookmarks: true, keepReviews: true, keepBookReviews: true,
                      ratedOnly: true, maxBookmarksPerBook: 0, maxReviewsPerBook: 0 },
  },
  'GET /api/data/result/': {
    books: [
      { bookId: 'B1', title: '三体', author: '刘慈欣', category: '科幻', rating: 9.3,
        myRating: null, source: 'weread', intro: '', totalBookmarks: 2, totalReviews: 1,
        totalBookReviews: 1, bookmarks: [{ markText: 'x' }], reviews: [{ content: 'y' }],
        bookReviews: [{ content: 'z' }] },
      { bookId: 'L1', title: '2023年度技术规划.pdf', author: '', category: '', rating: 0,
        myRating: null, source: 'local', userSlot: '印象最深', userNote: '我自己写的规划',
        intro: '', totalBookmarks: 1, totalReviews: 1, totalBookReviews: 0,
        bookmarks: [{ markText: 'a' }], reviews: [{ content: 'b' }], bookReviews: [] },
    ],
    stats: { totalBooks: 2, totalBookmarks: 3, totalReviews: 2, totalBookReviews: 1,
             localFiles: 2, localPicked: 1, ratedBooks: 1, avgMyRating: 4.0,
             topCategories: [], topAuthors: [] },
    localFiles: [],
  },
  'GET /api/analysis/estimate/': {
    chars: 100, tokens: 42, books: 2,
    kept: { bookmarks: true, reviews: true, bookReviews: true, ratedOnly: false },
    counts: { bookmarks: 3, reviews: 2, bookReviews: 1, ratedBooks: 1 },
  },
};

const posted = [];
window.fetch = async (url, opts) => {
  const method = (opts && opts.method) || 'GET';
// apiUrl(path, tail) 会把 uid 拼在路径末尾，匹配前先抹掉
const clean = String(url).replace(/^http:\/\/[^/]+/, '')
  .replace(/\?.*$/, '').replace('uid-test', '').split('/').filter(Boolean).join('/');
const key = `${method} /${clean}`;
if (method === 'POST') {
  posted.push({ key, body: JSON.parse((opts && opts.body) || '{}') });
}
const data = routes[key] || routes[`${method} /${clean}/`];
if (data === undefined && !routes.__quiet) {
  console.log(`    (未拦截: ${key})`);
}
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

(async () => {
  await new Promise((r) => window.document.addEventListener('DOMContentLoaded', r));

  console.log('[1] 本地文件面板渲染');
  // currentUid 是 let 声明的词法变量，外部改不动，只能让页面自己建会话
  await window.ensureUid();
  await window.loadLocalFiles();

  const box = window.document.getElementById('local-files-box');
  check('面板已显示', !box.classList.contains('hidden'));
  const rows = window.document.querySelectorAll('#lf-rows .lf-row');
  check('渲染出 2 行', rows.length === 2, `实际 ${rows.length}`);
  check('计数文案正确', /共 2 本，已选入 0 本/.test(
    window.document.getElementById('lf-count').textContent),
    window.document.getElementById('lf-count').textContent);
  check('判定依据已展示', /书名带电子书扩展名/.test(rows[0].textContent));
  check('有评分的显示星级', /★/.test(rows[1].textContent), rows[1].textContent.slice(0, 80));

  console.log('\n[2] 选分类 + 写感悟 → 保存');
  rows[0].querySelector('[data-role=slot]').value = '最想推荐';
  rows[0].querySelector('[data-role=note]').value = '这份规划后来兑现了七成';
  window.document.getElementById('lf-save-btn').click();
  await new Promise((r) => setTimeout(r, 60));
  const save = posted.find((p) => p.key === 'POST /api/data/local-picks');
  check('发出了保存请求', !!save);
  check('body 带 slot 与 note',
    save && save.body.picks.L1 && save.body.picks.L1.slot === '最想推荐'
    && /兑现了七成/.test(save.body.picks.L1.note),
    save && JSON.stringify(save.body));
  check('未选中的书不进 picks',
    save && save.body.picks.L2 === undefined, save && JSON.stringify(save.body.picks));

  console.log('\n[3] 新增自定义分类不丢草稿');
  window.document.getElementById('lf-new-slot-name').value = '通勤读物';
  window.document.getElementById('lf-add-slot-btn').click();
  await new Promise((r) => setTimeout(r, 30));
  const rows2 = window.document.querySelectorAll('#lf-rows .lf-row');
  check('新分类出现在下拉里',
    /通勤读物/.test(rows2[0].querySelector('[data-role=slot]').innerHTML));
  check('已写的感悟没被重绘吃掉',
    /兑现了七成/.test(rows2[0].querySelector('[data-role=note]').value),
    rows2[0].querySelector('[data-role=note]').value);

  console.log('\n[4] 「其实是上架书籍」改判');
  rows2[0].querySelector('.lf-unsplit').click();
  await new Promise((r) => setTimeout(r, 80));
  const ov = posted.find((p) => p.key === 'POST /api/data/source-override');
  check('发出了改判请求', !!ov);
  check('source=weread', ov && ov.body.source === 'weread', ov && JSON.stringify(ov.body));

  console.log('\n[5] 书卡上的我的评价 / 本地标记');
  await window.loadReviewData();
  const cards = window.document.querySelectorAll('#books-container .book-card');
  check('渲染出 2 张卡', cards.length === 2, `实际 ${cards.length}`);
  check('本地书带标记与分类', /本地上传｜印象最深/.test(cards[1].textContent),
    cards[1].textContent.slice(0, 60));
  check('用户自述显示在卡片里', /我自己写的规划/.test(cards[1].textContent));
  check('统计区有「我打过分的书」', /我打过分的书/.test(
    window.document.getElementById('stats-summary').textContent));

  console.log('\n[6] ratedOnly 复选框接进内容筛选');
  const cb = window.document.getElementById('cf-rated-only');
  cb.checked = true;
  cb.dispatchEvent(new window.Event('change'));
  await new Promise((r) => setTimeout(r, 80));
  const cf = posted.filter((p) => p.key === 'POST /api/data/content-filter').pop();
  check('筛选请求带 ratedOnly=true', cf && cf.body.ratedOnly === true,
    cf && JSON.stringify(cf.body));
  check('ratedBooks 数显示出来', /打过分的 1 本/.test(
    window.document.getElementById('cf-rated-count').textContent),
    window.document.getElementById('cf-rated-count').textContent);

  console.log('\n[7] 运行期没有未捕获错误');
  check('无 error 事件', errs.length === 0, errs.join(' | '));

  console.log(process.exitCode ? '\n有失败项 ✗' : '\njsdom 端到端全部通过 ✓');
  // 页面里有个 5 秒一次的后端探活 setInterval，不 close 的话 node 永远不退出
  window.close();
  process.exit(process.exitCode || 0);
})();
