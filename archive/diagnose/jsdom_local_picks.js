// 用 jsdom 真实渲染 step-4，验证本地文件面板与书单的折叠、分页、跨页草稿。
//
// 来历：这个项目吃过一次大亏 —— 事件处理器第一行引用了不存在的节点，
// TypeError 让按钮点了毫无反应，前后端都不报错。静态检查看不见这类问题，
// 只有真的点一次才知道。
//
// 数据量刻意做到 45 个本地文件 / 65 本书：分页是按 20 条一页切的，
// 数据不够一页就永远测不出"翻页之后别的页还活着吗"。
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

// ── 造数据：超过一页才测得出分页 ──
const LF_TOTAL = 45;
const BOOK_TOTAL = 65;

const localFiles = [
  { bookId: 'L1', title: '2023年度技术规划.pdf', author: '', myRating: null, rating: 0,
    sourceSignals: ['书名带电子书扩展名'], totalBookmarks: 6, totalReviews: 3, totalBookReviews: 0 },
  // 带评分却躺在本地面板里 —— 就是"有评分必然是上架书籍"那条要捞的误判
  { bookId: 'L2', title: '扫描件_城市研究笔记.epub', author: '', myRating: 4, rating: 0,
    sourceSignals: ['出版物元数据字段全空'], totalBookmarks: 4, totalReviews: 1, totalBookReviews: 0 },
];
for (let i = 3; i <= LF_TOTAL; i++) {
  localFiles.push({
    bookId: 'L' + i, title: '资料' + i + '.pdf', author: '', myRating: null, rating: 0,
    sourceSignals: ['书名带电子书扩展名'], totalBookmarks: i % 7, totalReviews: i % 3,
    totalBookReviews: 0,
  });
}

const books = [
  { bookId: 'B1', title: '三体', author: '刘慈欣', category: '科幻', rating: 9.3,
    myRating: null, source: 'weread', sourceConfidence: 'high', intro: '',
    totalBookmarks: 2, totalReviews: 1, totalBookReviews: 1,
    bookmarks: [{ markText: 'x' }], reviews: [{ content: 'y' }], bookReviews: [{ content: 'z' }] },
  { bookId: 'L1', title: '2023年度技术规划.pdf', author: '', category: '', rating: 0,
    myRating: null, source: 'local', userSlot: '印象最深', userNote: '我自己写的规划',
    intro: '', totalBookmarks: 1, totalReviews: 1, totalBookReviews: 0,
    bookmarks: [{ markText: 'a' }], reviews: [{ content: 'b' }], bookReviews: [] },
];
// 从 3 开始：books 里已经有 B1 和 L1 两本，凑够 BOOK_TOTAL
for (let i = 3; i <= BOOK_TOTAL; i++) {
  books.push({
    bookId: 'B' + i, title: '书' + i, author: '作者' + i, category: '社科', rating: 8.0,
    myRating: null, source: 'weread',
    // 每隔几本塞一个"详情未取到"的低置信样本，验证灰标
    sourceConfidence: i % 5 === 0 ? 'low' : 'high',
    sourceSignals: i % 5 === 0 ? ['书籍详情未取到（errcode=-10102），按上架书籍收录'] : ['ISBN'],
    intro: '', totalBookmarks: 1, totalReviews: 1, totalBookReviews: 0,
    bookmarks: [{ markText: 'm' + i }], reviews: [{ content: 'r' + i }], bookReviews: [],
  });
}

const routes = {
  // let currentUid 是词法变量，挂不到 window 上，只能走真实的建会话路径
  'POST /api/auth/session': { uid: 'uid-test' },
  'POST /api/llm/config/': { status: 'ok' },
  'GET /api/data/local-picks/': {
    slots: ['印象最深', '最想推荐', '启发性最大', '平时最有用'],
    files: localFiles,
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
    books: books,
    stats: { totalBooks: BOOK_TOTAL, totalBookmarks: 3, totalReviews: 2, totalBookReviews: 1,
             localFiles: LF_TOTAL, localPicked: 1, ratedBooks: 1, avgMyRating: 4.0,
             uncertainBooks: 13, topCategories: [], topAuthors: [] },
    localFiles: [],
  },
  'GET /api/analysis/estimate/': {
    chars: 100, tokens: 42, books: BOOK_TOTAL,
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

const $ = (id) => window.document.getElementById(id);
const rows = () => window.document.querySelectorAll('#lf-rows .lf-row');
const cards = () => window.document.querySelectorAll('#books-container .book-card');
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 40));

// 翻到指定页：点翻页器里的按钮
function goLfPage(n) {
  const btn = $('lf-pager-top').querySelector(`button[data-pg="${n}"]`);
  if (!btn) throw new Error(`第 ${n} 页的按钮不存在`);
  btn.click();
}

(async () => {
  await new Promise((r) => window.document.addEventListener('DOMContentLoaded', r));

  console.log('[1] 本地文件面板渲染（45 本，每页 20 条）');
  await window.ensureUid();
  await window.loadLocalFiles();

  const box = $('local-files-box');
  check('面板已显示', !box.classList.contains('hidden'));
  check('渲染出 20 行而不是 45 行', rows().length === 20, `实际 ${rows().length}`);
  check('计数文案给的是全量', new RegExp(`共 ${LF_TOTAL} 本，已选入 0 本`).test($('lf-count').textContent),
    $('lf-count').textContent);
  check('判定依据已展示', /书名带电子书扩展名/.test(rows()[0].textContent));
  check('有评分的显示星级', /★/.test(rows()[1].textContent), rows()[1].textContent.slice(0, 80));

  console.log('\n[2] 两块列表都可折叠');
  check('本地面板是 details 容器', $('lf-details') && $('lf-details').tagName === 'DETAILS');
  check('本地面板默认收起', $('lf-details').open === false);
  check('书单是 details 容器', $('books-details') && $('books-details').tagName === 'DETAILS');

  console.log('\n[3] 翻页');
  check('翻页器显示全量本数', new RegExp(`共 ${LF_TOTAL} 本`).test($('lf-pager-top').textContent),
    $('lf-pager-top').textContent);
  check('首页按钮禁用', $('lf-pager-top').querySelector('button[data-pg="1"]').disabled);
  goLfPage(2);
  await tick();
  check('第 2 页首行是第 21 本', /资料21/.test(rows()[0].textContent), rows()[0].textContent.slice(0, 40));
  check('第 2 页仍是 20 行', rows().length === 20, `实际 ${rows().length}`);
  goLfPage(3);
  await tick();
  check('末页只剩 5 行', rows().length === 5, `实际 ${rows().length}`);
  check('到了末页，"下一页"与"末页"都禁用',
    $('lf-pager-top').querySelector('button[data-pg="4"]').disabled === true
    && $('lf-pager-top').querySelector('button[data-pg="3"]').disabled === true);

  console.log('\n[4] 跨页草稿不丢（分页最容易在这里悄悄吃数据）');
  goLfPage(1);
  await tick();
  const row5 = rows()[4];                       // L5
  row5.querySelector('[data-role=note]').value = '第1页写的感悟';
  goLfPage(3);
  await tick();
  rows()[0].querySelector('[data-role=slot]').value = '最想推荐';  // L41
  goLfPage(1);
  await tick();
  check('翻回第 1 页，感悟还在',
    /第1页写的感悟/.test(rows()[4].querySelector('[data-role=note]').value),
    rows()[4].querySelector('[data-role=note]').value);

  console.log('\n[5] 选分类 + 写感悟 → 保存（要带上别的页的内容）');
  $('lf-save-btn').click();
  await tick(60);
  const save = posted.filter((p) => p.key === 'POST /api/data/local-picks').pop();
  check('发出了保存请求', !!save);
  check('带上了第 1 页的感悟',
    save && save.body.picks.L5 && /第1页写的感悟/.test(save.body.picks.L5.note),
    save && JSON.stringify(save.body.picks.L5));
  check('带上了第 3 页的分类',
    save && save.body.picks.L41 && save.body.picks.L41.slot === '最想推荐',
    save && JSON.stringify(save.body.picks.L41));
  check('未选中的书不进 picks', save && save.body.picks.L6 === undefined,
    save && Object.keys(save.body.picks).join(','));

  console.log('\n[6] 新增自定义分类不丢草稿');
  $('lf-new-slot-name').value = '通勤读物';
  $('lf-add-slot-btn').click();
  await tick(30);
  check('新分类出现在下拉里',
    /通勤读物/.test(rows()[0].querySelector('[data-role=slot]').innerHTML));
  check('已写的感悟没被重绘吃掉',
    /第1页写的感悟/.test(rows()[4].querySelector('[data-role=note]').value));

  console.log('\n[7] 「有评分必然是上架书籍」批量出口');
  const ratedBtn = $('lf-restore-rated');
  check('按钮出现并点名本数', ratedBtn.hidden === false && /把 1 本有评分的放回书单/.test(ratedBtn.textContent),
    ratedBtn.textContent);
  ratedBtn.click();
  await tick(80);
  const batch = posted.filter((p) => p.key === 'POST /api/data/source-override').pop();
  check('批量改判发出 bookIds', batch && Array.isArray(batch.body.bookIds)
    && batch.body.bookIds.indexOf('L2') >= 0, batch && JSON.stringify(batch.body));

  console.log('\n[8] 单本改判出口仍在');
  rows()[0].querySelector('.lf-unsplit').click();
  await tick(80);
  const one = posted.filter((p) => p.key === 'POST /api/data/source-override').pop();
  check('source=weread 单本也能改', one && one.body.bookId === 'L1' && one.body.source === 'weread',
    one && JSON.stringify(one.body));

  console.log('\n[9] 搜索过滤');
  const lfSearch = $('lf-search');
  lfSearch.value = '资料7';
  lfSearch.dispatchEvent(new window.Event('input'));
  await tick();
  const hit = Array.from(rows()).map((r) => r.dataset.bookId);
  check('按书名过滤生效', hit.length === 1 && hit[0] === 'L7', hit.join(','));

  console.log('\n[10] 书单分页（65 本，每页 20 条）');
  lfSearch.value = '';
  lfSearch.dispatchEvent(new window.Event('input'));
  await tick();
  await window.loadReviewData();
  check('书单容器已显示', !$('books-details').classList.contains('hidden'));
  check('只渲染 20 张卡', cards().length === 20, `实际 ${cards().length}`);
  check('计数是全量而不是当前页', new RegExp(`共 ${BOOK_TOTAL} 本`).test($('books-count').textContent),
    $('books-count').textContent);
  check('继续按钮给的是全量本数（不是 20）',
    new RegExp(`\\(${BOOK_TOTAL} 本\\)`).test($('to-ai-btn').textContent),
    $('to-ai-btn').textContent);
  check('本地书带标记与分类', /本地上传｜印象最深/.test(cards()[1].textContent),
    cards()[1].textContent.slice(0, 60));
  check('用户自述显示在卡片里', /我自己写的规划/.test(cards()[1].textContent));
  check('统计区有「我打过分的书」', /我打过分的书/.test($('stats-summary').textContent));
  check('统计区有「详情未取到」', /详情未取到/.test($('stats-summary').textContent),
    $('stats-summary').textContent.slice(0, 120));

  console.log('\n[11] 低置信书带灰标');
  const low = Array.from(cards()).find((c) => /详情未取到/.test(c.textContent));
  check('书卡上有「详情未取到」标记', !!low);
  check('灰标 title 挂着判定依据',
    low && /errcode=-10102/.test(low.querySelector('.uncertain-badge').title),
    low && low.querySelector('.uncertain-badge').title);

  console.log('\n[12] 书单翻页后 data-bi 不串台');
  const btnNext = $('books-pager-top').querySelector('button[data-pg="2"]');
  btnNext.click();
  await tick();
  check('第 2 页仍是 20 张卡', cards().length === 20, `实际 ${cards().length}`);
  check('第 2 页首本是第 21 本', /书2[0-9]|作者2[0-9]/.test(cards()[0].textContent),
    cards()[0].textContent.slice(0, 40));
  check('翻页后继续按钮仍报全量', new RegExp(`\\(${BOOK_TOTAL} 本\\)`).test($('to-ai-btn').textContent),
    $('to-ai-btn').textContent);

  console.log('\n[13] 书单搜索');
  const bSearch = $('books-search');
  bSearch.value = '三体';
  bSearch.dispatchEvent(new window.Event('input'));
  await tick();
  check('按书名过滤生效', cards().length === 1 && /三体/.test(cards()[0].textContent),
    `实际 ${cards().length} 张`);

  console.log('\n[14] ratedOnly 复选框接进内容筛选');
  bSearch.value = '';
  bSearch.dispatchEvent(new window.Event('input'));
  await tick();
  const cb = $('cf-rated-only');
  cb.checked = true;
  cb.dispatchEvent(new window.Event('change'));
  await tick(80);
  const cf = posted.filter((p) => p.key === 'POST /api/data/content-filter').pop();
  check('筛选请求带 ratedOnly=true', cf && cf.body.ratedOnly === true,
    cf && JSON.stringify(cf.body));
  check('ratedBooks 数显示出来', /打过分的 1 本/.test($('cf-rated-count').textContent),
    $('cf-rated-count').textContent);

  console.log('\n[15] 时间范围筛选链路（scan → 面板 → extract 带筛选参数）');
  // 这一节专门盯一条走过的弯路：后端改成「scan 拿时间线 → 筛选条件随 extract
  // 一起提交」之后，前端有一段时间还停在旧流程（等 SSE 的 timeline 帧、
  // 再 POST 已经删掉的 /data/set-filter）。后端冒烟测不到这一段——
  // 只有真的点一遍才知道面板出不出来、参数带没带上。
  posted.length = 0;
  const nowSec = Math.floor(Date.now() / 1000);
  const day = 86400;
  const timeline = [
    { bookId: 'T1', title: '书甲', author: '甲', lastReadingTime: nowSec - day, cover: '' },
    { bookId: 'T2', title: '书乙', author: '乙', lastReadingTime: nowSec - day * 3, cover: '' },
    { bookId: 'T3', title: '书丙', author: '丙', lastReadingTime: nowSec - day * 10, cover: '' },
  ];
  routes['POST /api/data/scan/'] = { books: timeline, total: timeline.length };
  routes['POST /api/data/extract/'] = { status: 'started' };

  $('extract-btn').click();
  await tick();
  check('点「开始提取」先弹是否筛选的选择层',
    !$('filter-choice-modal').classList.contains('hidden'));

  $('choice-yes-btn').click();
  await tick(80);
  check('选「是」先调 scan', posted.some((p) => /data\/scan/.test(p.key)),
    posted.map((p) => p.key).join(','));
  check('scan 后时间范围面板出现', !$('filter-area').classList.contains('hidden'));
  const fboxes = () => $('filter-book-list').querySelectorAll('input[type=checkbox]');
  check('面板渲染出 3 本可选', fboxes().length === 3, `实际 ${fboxes().length}`);
  check('向导停在「时间范围」这一步', !$('step-3').classList.contains('hidden'));

  $('filter-start').value = '2020-01-01';
  $('filter-end').value = new Date().toISOString().slice(0, 10);
  fboxes()[0].checked = false;
  fboxes()[0].dispatchEvent(new window.Event('change'));
  $('filter-confirm-btn').click();
  await tick(120);

  const ex = posted.filter((p) => /data\/extract/.test(p.key)).pop();
  check('确认后调 extract', !!ex, posted.map((p) => p.key).join(','));
  check('筛选条件随 extract 一起提交（不再有中间态）',
    ex && typeof ex.body.startDate === 'number' && typeof ex.body.endDate === 'number'
      && Array.isArray(ex.body.excludedBookIds),
    ex && JSON.stringify(ex.body));
  check('取消勾选的书进了 excludedBookIds',
    ex && ex.body.excludedBookIds.indexOf('T1') >= 0, ex && JSON.stringify(ex.body));
  check('不再打已删除的 set-filter 端点',
    !posted.some((p) => /set-filter/.test(p.key)));

  // 反向：选「否」走全量，提交体里一个筛选字段都不带
  posted.length = 0;
  routes['POST /api/data/preflight/'] = { ok: true, elapsedMs: 12, checks: [] };
  $('extract-btn').click();
  await tick();
  $('choice-no-btn').click();
  await tick(120);
  const ex2 = posted.filter((p) => /data\/extract/.test(p.key)).pop();
  check('选「否」直接全量提取', !!ex2, posted.map((p) => p.key).join(','));
  check('  ↳ 提交体里不带任何筛选字段',
    ex2 && ex2.body.startDate === undefined && ex2.body.excludedBookIds === undefined,
    ex2 && JSON.stringify(ex2.body));

  console.log('\n[16] 运行期没有未捕获错误');
  check('无 error 事件', errs.length === 0, errs.join(' | '));

  console.log(process.exitCode ? '\n有失败项 ✗' : '\njsdom 端到端全部通过 ✓');
  // 页面里有个 5 秒一次的后端探活 setInterval，不 close 的话 node 永远不退出
  window.close();
  process.exit(process.exitCode || 0);
})();
