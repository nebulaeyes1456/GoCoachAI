// Mock 数据与 Mock API（窗口4）。
// 与契约同构（docs/architecture.md §4）：一局 9 路复盘 + 3 道题 + 1 条答疑。
// 页面顶部"数据源：Mock/真实"开关切换，默认 Mock。

// v2 红线：禁止自写坐标换算。棋谱一律内置为 SGF 字符串常量，
// 解析/落子/提子/变化播放全部交给 WGo.js（WGo.SGF.parse / WGo.KifuReader）。

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const randDelay = (min = 250, max = 700) =>
  delay(min + Math.random() * (max - min));

// ---------------------------------------------------------------------------
// 9 路示例复盘（20 手）
// ---------------------------------------------------------------------------

// [color, coord]，与 winrate 曲线一一对应
const REVIEW_MOVES = [
  ['B', 'E5'], ['W', 'E3'], ['B', 'G4'], ['W', 'C6'], ['B', 'C4'],
  ['W', 'D3'], ['B', 'G6'], ['W', 'H7'], ['B', 'F6'], ['W', 'J6'],
  ['B', 'H5'], ['W', 'J4'], ['B', 'G2'], ['W', 'B6'], ['B', 'D5'],
  ['W', 'D7'], ['B', 'F7'], ['W', 'F8'], ['B', 'H3'], ['W', 'E7'],
];

// 每手落下后"当前方"胜率（与契约 MoveInfo.winrate 同口径）
const WINRATES = [
  0.52, 0.50, 0.53, 0.49, 0.54, 0.47, 0.49, 0.51, 0.53, 0.46,
  0.55, 0.33, 0.66, 0.36, 0.65, 0.36, 0.715, 0.30, 0.70, 0.32,
];

// 关键手（手数从 1 起）：7 疑问手、12 坏手、17 好手
// PV 至少 7 步（≥5 步要求），坐标均避开示例棋谱已有落点，播放不会撞子
const KEY_MOVE_EXTRA = {
  7: {
    best_coord: 'H6', pv: ['H6', 'H5', 'J5', 'H4', 'H3', 'J3', 'J2'],
    candidates: [
      { order: 0, move: 'H6', winrate: 0.58, visits: 320 },
      { order: 1, move: 'G7', winrate: 0.52, visits: 180 },
      { order: 2, move: 'H5', winrate: 0.51, visits: 120 },
    ],
  },
  12: {
    best_coord: 'J5', pv: ['J5', 'H4', 'J3', 'J2', 'H3', 'J1', 'H2'],
    candidates: [
      { order: 0, move: 'J5', winrate: 0.66, visits: 420 },
      { order: 1, move: 'H4', winrate: 0.55, visits: 210 },
      { order: 2, move: 'J3', winrate: 0.52, visits: 140 },
    ],
  },
  17: {
    best_coord: 'G7', pv: ['G7', 'F8', 'E8', 'F7', 'E7', 'E6', 'D8'],
    candidates: [
      { order: 0, move: 'G7', winrate: 0.72, visits: 460 },
      { order: 1, move: 'F8', winrate: 0.65, visits: 230 },
      { order: 2, move: 'G6', winrate: 0.63, visits: 150 },
    ],
  },
};

// 内置最终 SGF 字符串常量（与 REVIEW_MOVES 一一对应，坐标固定写在 SGF 中，
// 不经过任何自写坐标换算；解析与落子坐标一律由 WGo.js 处理）。
const REVIEW_SGF =
  '(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[6.5]PB[示例·黑棋]PW[示例·白棋]RE[B+3.5]' +
  ';B[ee];W[eg];B[gf];W[cd];B[cf];W[dg];B[gd];W[hc];B[fd];W[id]' +
  ';B[he];W[if];B[gh];W[bd];B[de];W[dc];B[fc];W[fb];B[hg];W[ec])';

function buildCurve() {
  const curve = [];
  for (let i = 0; i < REVIEW_MOVES.length; i++) {
    const [color, coord] = REVIEW_MOVES[i];
    const winrate = WINRATES[i];
    const before = i === 0 ? 0.5 : 1 - WINRATES[i - 1];
    const delta = Math.round((winrate - before) * 1000) / 1000;
    let category = 'normal';
    if (delta <= -0.12) category = 'blunder';
    else if (delta <= -0.06) category = 'question';
    else if (delta >= 0.06) category = 'good';
    const extra = KEY_MOVE_EXTRA[i + 1] || {};
    curve.push({
      move: i + 1,
      color,
      coord,
      winrate: Math.round(winrate * 1000) / 1000,
      score_lead: Math.round((winrate - 0.5) * 30 * 10) / 10,
      category,
      delta,
      best_coord: extra.best_coord || null,
      pv: extra.pv || [],
      candidates: extra.candidates || [],
      score_stdev: Math.round((1.5 + ((i * 7) % 9) * 0.5) * 10) / 10,
      visits: 600 + ((i * 37) % 200),
    });
  }
  return curve;
}

const REVIEW_CURVE = buildCurve();

function reviewDetail() {
  const keyMoves = REVIEW_CURVE.filter((m) => m.category !== 'normal');
  return {
    id: 'mock-review-9x9',
    board_size: 9,
    black: '示例·黑棋',
    white: '示例·白棋',
    profile: 'fast',
    status: 'done',
    winrate_curve: REVIEW_CURVE,
    key_moves: keyMoves,
    stats: {
      blunders: keyMoves.filter((m) => m.category === 'blunder').length,
      questions: keyMoves.filter((m) => m.category === 'question').length,
      good: keyMoves.filter((m) => m.category === 'good').length,
    },
    // 目数热图（9 路 81 值伪数据：角部黑控制、左上白控制）
    final_ownership: (() => {
      const arr = [];
      for (let y = 0; y < 9; y++) {
        for (let x = 0; x < 9; x++) {
          const v = (8 - x - y) / 16 + ((x * 3 + y * 5) % 3 - 1) * 0.08;
          arr.push(Math.max(-1, Math.min(1, Math.round(v * 100) / 100)));
        }
      }
      return arr;
    })(),
  };
}

// ---------------------------------------------------------------------------
// 3 道 mock 题（9 路）
// ---------------------------------------------------------------------------

const MOCK_PROBLEMS = [
  {
    id: 'mock-p1',
    source: 'library',
    theme: 'life_death',
    goal: '做活',
    rank_min: -8, rank_max: -4,
    setup_sgf: '(;GM[1]FF[4]CA[UTF-8]SZ[9]PL[B]' +
      'AB[dg][eg][ef][ff][gf][gg]' +
      'AW[ch][dh][eh][fh][gh][hg][hf][df][cf])',
    answer: 'F5',
    branches: '[]',
    // 正解变化（至少 5 步，与 explanation 吻合，坐标均避开题面已有子）
    variation: ['F5', 'H2', 'G5', 'J4', 'J5'],
    verdict: '黑棋先手做活右上角，正解后形成两眼。',
    hint: '黑先，请做活右上角的一块棋。',
    explanation: '黑 F5 扩大眼位，白无法点入。若白 H2 点，黑 G5 挡即可做眼；注意不要先走 H5，否则被白点 F5 后眼位不足。',
  },
  {
    id: 'mock-p2',
    source: 'library',
    theme: 'capturing_race',
    rank_min: -7, rank_max: -3,
    setup_sgf: '(;GM[1]FF[4]CA[UTF-8]SZ[9]PL[B]' +
      'AB[ee][ed][fd]' +
      'AW[de][dd][ef][dc])',
    answer: 'C6',
    branches: '[]',
    // 正解变化（至少 5 步，与 explanation 吻合：白 C7 后黑 E7 延气）
    variation: ['C6', 'C7', 'E7', 'B7', 'B8'],
    verdict: '黑先紧气，对杀黑快一气获胜。',
    hint: '黑先，与白棋对杀，请先紧气。',
    explanation: '黑 C6 是双方必争的要点：既紧住白 D5/D6 两子的气，又护住自己的气。白 D7 后黑 F7 延气，最终快一气吃白。',
  },
  {
    id: 'mock-p3',
    source: 'library',
    theme: 'endgame',
    rank_min: -9, rank_max: -2,
    setup_sgf: '(;GM[1]FF[4]CA[UTF-8]SZ[9]PL[B]' +
      'AB[cg][dg][cf][df][fc][gc]' +
      'AW[bd][cd][bc][cc][fb][gb])',
    answer: 'B8',
    branches: '[]',
    // 正解变化（至少 5 步，与 explanation 吻合：白 B9 挡后黑 C8 接）
    variation: ['B8', 'B9', 'C8', 'C9', 'D8'],
    verdict: '黑 B8 是先手官子，白须 B9 挡，黑得利约 2 目。',
    hint: '黑先，请收束左上角官子。',
    explanation: '黑 B8 扳是当前最大官子，白若脱先黑可继续爬入。白 B9 挡后黑 C8 接，先手定型；注意不要从 C8 爬，那是后手。',
  },
];

function problemBrief(p) {
  return {
    id: p.id, theme: p.theme, rank_min: p.rank_min,
    rank_max: p.rank_max, setup_sgf: p.setup_sgf, hint: p.hint,
  };
}

// ---------------------------------------------------------------------------
// Mock 系统状态（可被 PUT /settings 更新）
// ---------------------------------------------------------------------------

const mockSettings = {
  profile: 'fast',
  blunder_threshold: 0.12,
  question_threshold: 0.06,
  good_threshold: 0.06,
};

// ---------------------------------------------------------------------------
// 成长视图 mock 状态与画像特征
// ---------------------------------------------------------------------------

const mockProgress = {
  profiles: [
    { id: 'ppdemo0001', name: '小明', note: '九路练习档案',
      created_at: '2026-09-01T08:00:00', games_count: 2 },
  ],
  games: {
    ppdemo0001: [
      { review_id: 'rvmockg1', created_at: '2026-09-03T20:12:00',
        board_size: 9, profile: 'fast', moves_count: 34,
        blunders: 1, questions: 3, good: 2, black: '你', white: '对手' },
      { review_id: 'rvmockg2', created_at: '2026-09-05T19:40:00',
        board_size: 9, profile: 'standard', moves_count: 42,
        blunders: 2, questions: 4, good: 3, black: '对手', white: '你' },
    ],
  },
  insight: {},
};

const mockFeatures = {
  n_games: 2,
  avg_loss_per_move: 0.052,
  blunders: 3, questions: 7, good_moves: 5,
  direction_errors: 1, complexity_errors: 2, reading_errors: 1,
  weakest: '官子', weakest_loss: 0.063,
  phases: {
    layout: { n: 42, avg_loss: 0.038, worst_move: 11 },
    middle: { n: 55, avg_loss: 0.047, worst_move: 47 },
    endgame: { n: 20, avg_loss: 0.063, worst_move: 77 },
  },
  trend: 'flat',
};

// ---------------------------------------------------------------------------
// Mock API（与 api.js 的 real 实现同构）
// ---------------------------------------------------------------------------

export const mockApi = {
  async analyze(sgfText, profile) {
    await randDelay(300, 600);
    return { review_id: 'mock-review-9x9' };
  },

  async reviewStatus(reviewId) {
    await delay(120);
    return { status: 'done', progress: 1.0, error: null };
  },

  async reviewDetail(reviewId) {
    await randDelay(300, 700);
    return reviewDetail();
  },

  async explain(reviewId, moveNumber) {
    await randDelay(600, 1200);
    const m = REVIEW_CURVE.find((x) => x.move === moveNumber) || REVIEW_CURVE[0];
    const kindName = { blunder: '坏手', question: '疑问手', good: '好手' }[m.category] || '疑问手';
    const rec = m.best_coord
      ? `推荐下在 ${m.best_coord}：先手抢占要点，同时瞄着后续手段，胜率可以稳住。`
      : '这一步方向正确，注意保持棋形完整。';
    return {
      move_number: m.move,
      kind: 'move',
      content: {
        problem: `第 ${m.move} 手${m.color === 'B' ? '黑' : '白'}棋 ${m.coord} 是${kindName}，让当前方胜率变化了 ${(m.delta * 100).toFixed(1)} 个百分点。`,
        reason: `因为对方可以借 ${m.best_coord || '正确应法'} 展开反击，这一手的效率不足，局部棋形留下了弱点。`,
        recommendation: rec,
        necessity: `${m.best_coord || '正解'} 是必须的：这里一旦脱先，对方马上点入急所，这块棋将失去根据地，后续每一步都只能被动应对；如果去下别处，比如单纯围空或补棋，对方顺势一夹，本方的形状会从「有根」变成「飘棋」，目数和厚薄双输。所以这一手不是可选项，而是这个局部的唯一解。`,
        variation: m.pv && m.pv.length ? m.pv : [m.best_coord || m.coord],
        alternatives: [
          `不追求一选的话，可以简明地拆边抢占大场，胜率只差约 1 个百分点，棋形更厚实，适合对中盘计算信心不足的时候选择。`,
          `也可以先肩冲压迫上方白棋，借劲定型，虽然实地稍亏，但外势厚壮，后续战斗好下。`,
        ],
        takeaway: `本手提醒：下棋要先看双方的急所，${m.color === 'B' ? '黑' : '白'}方此处应优先处理棋形要点。`,
        level_note: '（针对 5K 的解释深度）',
        result_type: '',
        can_tenuki: '',
        segments: [
          { text: `这一手是${kindName}：表面看没问题，但忽略了${m.best_coord || '急所'}的重要性。`,
            variation: [] },
          { text: `正确思路是先占 ${m.best_coord || '要点'}，抢先定型，让对方失去借劲的机会。`,
            variation: [[m.color, m.best_coord || m.coord]] },
          { text: '对方如果从另一侧逼住，本方可顺势整形，局部两分。',
            variation: [] },
        ],
      },
      model: 'mock-llm',
      cost: 0.0027,
    };
  },

  async playMove(size, moves, komi, rank) {
    void rank; // mock 随机应手，棋力档不模拟
    await randDelay(300, 800);
    const pool9 = ['E5', 'E3', 'C3', 'G5', 'G3', 'C5', 'C7', 'G7', 'D4', 'F4',
      'D6', 'F6', 'E7', 'B5', 'H5', 'B3', 'H3', 'D2', 'F2', 'E8'];
    const pool19 = ['Q16', 'D4', 'Q4', 'D16', 'R10', 'K10', 'F17', 'C17', 'Q17',
      'C3', 'R3', 'O17', 'K16', 'D10', 'Q10', 'K4', 'R14', 'C14', 'F3', 'C6'];
    const pool = size === 9 ? pool9 : pool19;
    const used = new Set((moves || []).map((m) => m[1]));
    const ai = pool.find((c) => !used.has(c)) || 'E5';
    const n = (moves || []).length + 1;
    let level = 'ok'; let delta = -0.01; let best = ''; let bestWr = 0.5;
    let userWr = 0.49; let hint = null;
    if (n % 6 === 4) {
      level = 'bad'; delta = -0.16; bestWr = 0.55; userWr = 0.39;
      best = size === 9 ? 'C7' : (size === 13 ? 'F8' : 'K3');
    } else if (n % 6 === 2) {
      level = 'question'; delta = -0.07; bestWr = 0.5; userWr = 0.43;
      best = size === 9 ? 'F6' : (size === 13 ? 'G7' : 'D10');
    }
    if (level === 'bad') {
      hint = { title: '坏手', text: `这手棋掉得有点多（损失 16 个百分点）。好点在${size === 9 ? '左上三线' : '下方星位旁'}——棋盘上已用绿圈标出。` };
    } else if (level === 'question') {
      hint = { title: '疑问手', text: `这手棋略缓（损失 7 个百分点）。更好的点在${size === 9 ? '右上四线' : '左侧边上'}，棋盘上已用绿圈标出。` };
    }
    return {
      size, ai_move: ai, ai_winrate: 1 - userWr,
      user_winrate: userWr, best, best_winrate: bestWr,
      delta, level, score_lead: 2.5, hint, move_number: n,
    };
  },

  async playTip(size, moves, coord, best, delta, level) {
    await randDelay(500, 1200);
    return {
      kind: 'play_tip',
      content: {
        problem: '这手棋把自己的断点送给了对方。',
        reason: '新手最容易只顾眼前：这手棋没有和周围的子连上，对方一断，你反而要多花好几手去补棋，越下越被动。',
        recommendation: '常规下 ' + best + '：先补住断点、把自己的棋走厚，再考虑进攻别人。',
        proverb: '棋从断处生',
      },
      model: 'mock',
      cost: 0,
    };
  },

  async penalty(reviewId, moveNumber) {
    await randDelay(400, 900);
    return {
      kind: 'penalty',
      content: {
        penalty_pv: ['H6', 'H5', 'J5', 'H4', 'H3'],
        punisher: 'W',
        punisher_winrate: 0.82,
        focused: true,
        steps: [
          { step: 1, role: '好手', text: '白 H6 靠下，严厉：黑三子被压得抬不起头，同时补了断点。' },
          { step: 2, role: '应对', text: '黑 H5 只能长出，否则角上被吃。' },
          { step: 3, role: '先手', text: '白 J5 后手是必须应的先手，黑不应就要被攻破角地。' },
          { step: 4, role: '俗手', text: '黑 H4 这手是俗手交换，帮白补强还自紧一气，不如脱先。' },
          { step: 5, role: '应对', text: '白 H3 收尾，局部定型。' },
        ],
        summary: '这串交换下来，黑角地被压缩，白外势完整，黑亏约 2 目。正确下法是先在角上拉手补强。',
      },
      model: 'kata',
      cost: 0,
    };
  },

  async deep(reviewId) {
    await randDelay(1200, 2600);
    return {
      kind: 'deep',
      content: {
        title: '一着缓手引发的中盘雪崩',
        overview: '本局黑棋布局扎实、白棋取势积极，双方前半盘咬得很紧。转折点出现在第 12 手：白棋为抢回主动强行打入，反而暴露断点，之后一路被动，黑棋抓住机会第 17 手一举锁定胜局。',
        stages: [
          '布局阶段双方各占大场，黑棋第 7 手稍缓，让白棋在左上取得主动，但差距不大。',
          '中盘白棋第 12 手冒进，黑棋趁机冲击断点，局面急转直下；黑第 17 手好手一举定局。',
          '官子阶段黑棋稳扎稳打，没有给白棋任何翻盘机会，最终中盘胜。',
        ],
        key_moves: [
          { move: 7, analysis: '黑 G6 偏缓，错过了先手消长的要点，应下 H6 抢占中央。' },
          { move: 12, analysis: '白 J4 强行打入是败招，自身断点太多，被黑棋抓住后难以两全。' },
          { move: 17, analysis: '黑 F7 严厉，一石二鸟，既补强自身又冲击白棋断点，是决定胜负的好手。' },
        ],
        causality: '第 7 手的缓手让出先机，白棋尝到甜头后在第 12 手贪功冒进，暴露断点；黑棋顺势反击，白棋为救断点处处落后手，最终第 19 手不得不弃子止损，败局就此铸成。',
        strengths: ['黑棋抓住对手失误后转换果断', '黑棋局部对杀计算准确'],
        weaknesses: ['白棋战斗中的断点意识薄弱', '白棋落后时容易强行用强'],
        homework: ['本周重点练习：接触战断点 10 题', '复盘时多关注中盘每手棋的断点', '学习黑棋第 17 手的一石二鸟思路'],
      },
      model: 'mock-llm',
      cost: 0.018,
    };
  },

  async summary(reviewId) {
    await randDelay(800, 1600);
    return {
      kind: 'summary',
      content: {
        opening: '布局阶段双方互围，黑棋第 7 手 G6 稍有疑问，但整体差距不大，白棋借机在左上取得主动。',
        middle: '中盘第 12 手白棋 J4 是全局败招，胜率大幅下降；黑棋抓住机会第 17 手 F7 是好手，一举扭转局势。',
        endgame: '官子阶段黑棋稳扎稳打，没有给白棋翻盘机会，最终中盘胜。',
        strengths: ['抓住对手失误后转换果断', '局部对杀计算准确'],
        weaknesses: ['布局方向感不足', '中盘容易走出随手棋'],
        suggestions: ['本周重点练习：接触战断点 10 题', '复盘时多关注胜率曲线上的黄点与红点'],
      },
      model: 'mock-llm',
      cost: 0.0036,
    };
  },

  async generateProblems(reviewId, themes, maxProblems, targetRank) {
    await randDelay(1200, 2200);
    let list = MOCK_PROBLEMS;
    if (themes && themes.length) list = list.filter((p) => themes.includes(p.theme));
    list = list.slice(0, Math.max(1, maxProblems || 6));
    return {
      problems: list.map((p) => ({ ...problemBrief(p), target: targetRank })),
      failed: 1,
    };
  },

  async library({ theme = null, rank = null, limit = 20, offset = 0 } = {}) {
    await randDelay(300, 600);
    let list = MOCK_PROBLEMS;
    if (theme) list = list.filter((p) => p.theme === theme);
    if (rank !== null && rank !== undefined && rank !== '') {
      const r = parseInt(rank, 10);
      list = list.filter((p) => p.rank_min <= r && r <= p.rank_max);
    }
    return {
      problems: list.slice(offset, offset + limit).map(problemBrief),
      total: list.length,
    };
  },

  async problem(id) {
    await randDelay(200, 450);
    const p = MOCK_PROBLEMS.find((x) => x.id === id);
    if (!p) throw Object.assign(new Error('题目不存在'), { status: 404 });
    return { ...p };
  },

  async attempt(problemId, coord) {
    await randDelay(500, 1200);
    const p = MOCK_PROBLEMS.find((x) => x.id === problemId);
    if (!p) throw Object.assign(new Error('题目不存在'), { status: 404 });
    if (coord === p.answer) {
      return {
        correct: true,
        response: '答对了！这步正是要点，棋局因此牢牢掌握。',
        variation: [],
        solved: true,
        explanation: p.explanation,
      };
    }
    return {
      correct: false,
      response: `这步不是正解，白棋可以应一手后化解黑棋的意图。再想想局部的要点。`,
      variation: p.variation || [p.answer],
      solved: false,
      explanation: null,
    };
  },

  async problemExplain(problemId) {
    await randDelay(600, 1200);
    const p = MOCK_PROBLEMS.find((x) => x.id === problemId);
    if (!p) throw Object.assign(new Error('题目不存在'), { status: 404 });
    return {
      kind: 'problem_explain',
      problem_id: problemId,
      content: {
        result_type: '净活',
        can_tenuki: '不能脱先',
        segments: [
          { text: `这道题要看清棋形：正解 ${p.answer} 是唯一的活棋急所，先手方必须立即处理。`,
            variation: [[p.solver || 'B', p.answer]] },
          { text: '若脱先，对方抢占要点后局部就无棋可做了。',
            variation: [] },
        ],
        takeaway: '急所必争，脱先即亡。',
      },
      model: 'mock-llm',
      cost: 0.0035,
    };
  },

  async extractProblem(reviewId, moveNumber) {
    await randDelay(800, 1500);
    return {
      problem_id: 'pmock' + moveNumber + 'abc',
      theme: 'life_death',
      answer: 'D4',
      setup_sgf: '(;GM[1]FF[4]SZ[9]AB[cb][cc][db][dc])',
      created: true,
    };
  },

  async ask(sgfText, question, level) {
    await randDelay(800, 1500);
    const PRESETS = [
      { key: /占角/, conclusion: '因为角部用最少的子就能围住最多的空。',
        reasoning: '角部两边靠边线，只需围两条边就能成空，效率最高；边上次之，中央四面漏风最难成空。所以开局先占角、再挂角拆边，是效率最高的次序。' },
      { key: /气|眼|活棋/, conclusion: '气是棋子与棋盘相连的空点；眼是被己方棋子围住的独立空点。',
        reasoning: '一块棋被对方全部堵住气就会被提掉。眼是对方永远无法填入的点（填进去自己没有气），所以一块棋做出两只真眼就永远安全，称为活棋。' },
      { key: /定式/, conclusion: '定式是角部双方都可以接受的固定下法，新手可以学但不必死背。',
        reasoning: '定式是前人总结的角部两分变化，背熟能避免开局吃亏；但定式数量庞大，死背不如理解其中「取地」与「取势」的思路。' },
      { key: /金角银边/, conclusion: '这句棋谚说的是：角部价值最大，边次之，中央最小。',
        reasoning: '同样多的棋子，在角上能围的空最多，边上其次，中央最少，所以叫金角、银边、草肚皮。这是布局阶段选择落点最重要的原则。' },
      { key: /厚势/, conclusion: '厚势是己方棋子连接紧密、无后顾之忧的外势，价值在于辐射和借用。',
        reasoning: '厚势不是用来围空的，而是用来攻击的：用厚势逼对方薄棋，一边攻击一边成空。切忌贴着厚势围空，那样厚势就浪费了。' },
      { key: /先手/, conclusion: '先手是对方必须应的棋；后手是对方可以不应的棋。',
        reasoning: '谁拿到先手，谁就能先抢占下一个大场。所以下棋要追求「先手利」：先逼对方应一手，自己再转身抢别的点。收官时先手官子特别珍贵。' },
      { key: /打劫|劫/, conclusion: '打劫是双方可以互相提回一子的特殊状态，劫材是逼对方应一手的外围棋。',
        reasoning: '提劫后对方不能马上提回，必须先在别处下一手（找劫材），你应了之后对方才能回提。所以打劫的本质是双方比拼谁的劫材多。' },
      { key: /弃子/, conclusion: '当一块棋价值变小时，果断放弃它转身取利，往往比苦活更划算。',
        reasoning: '弃子是把已经不重要或救不活的棋子当包袱扔掉，用它换取先手、外势或更大利益。「弃子争先」「弃子取势」都是高级战术。' },
    ];
    const hit = PRESETS.find((p) => p.key.test(question || ''));
    if (hit) {
      return {
        answer: {
          conclusion: hit.conclusion,
          reasoning: hit.reasoning,
          variation: [],
          kata_winrate: null,
        },
        model: 'mock-llm',
        cost: 0.0008,
      };
    }
    return {
      answer: {
        conclusion: '应该先断，再补回自己。',
        reasoning: `先断可以借力打力：对方若吃断，你顺势转身补强自己；对方若退让，你则白得实利。直接补棋虽然安全，但会错过先手，让对方从容补强，等于把选择权交了出去。`,
        variation: ['D15', 'E16', 'C16'],
        kata_winrate: 0.62,
      },
      model: 'mock-llm',
      cost: 0.0021,
    };
  },

  // ---- 成长视图 mock ----
  async createProfile(name, note) {
    await randDelay(300, 600);
    const p = {
      id: 'pp' + Math.random().toString(16).slice(2, 10),
      name, note: note || '',
      created_at: new Date().toISOString(),
      games_count: 0,
    };
    mockProgress.profiles.push(p);
    return { profile: p };
  },

  async listProfiles() {
    await randDelay(300, 600);
    return { profiles: mockProgress.profiles.map((p) => ({
      ...p, games_count: (mockProgress.games[p.id] || []).length,
    })) };
  },

  async deleteProfile(profileId) {
    await randDelay(300, 600);
    mockProgress.profiles = mockProgress.profiles.filter((x) => x.id !== profileId);
    return { deleted: true };
  },

  async profileDetail(profileId) {
    await randDelay(400, 800);
    const p = mockProgress.profiles.find((x) => x.id === profileId);
    if (!p) throw Object.assign(new Error('档案不存在'), { status: 404 });
    return {
      profile: p,
      games: mockProgress.games[profileId] || [],
      insight: mockProgress.insight[profileId] || null,
    };
  },

  async attachReview(profileId, reviewId) {
    await randDelay(400, 800);
    const list = mockProgress.games[profileId] || (mockProgress.games[profileId] = []);
    if (!list.some((g) => g.review_id === reviewId)) {
      list.push({
        review_id: reviewId,
        created_at: new Date().toISOString(),
        board_size: 9, profile: 'fast',
        moves_count: 34,
        blunders: 1, questions: 3, good: 2,
        black: '你', white: '对手',
      });
    }
    return { attached: true };
  },

  async importSgf(profileId, sgfText, reviewProfile) {
    await randDelay(600, 1000);
    const reviewId = 'rvmock' + Math.random().toString(16).slice(2, 10);
    const list = mockProgress.games[profileId] || (mockProgress.games[profileId] = []);
    list.push({
      review_id: reviewId,
      created_at: new Date().toISOString(),
      board_size: 9, profile: reviewProfile || 'fast',
      moves_count: 40,
      blunders: 2, questions: 4, good: 3,
      black: '导入', white: '对手',
    });
    return { review_id: reviewId, status: 'pending', profile_id: profileId };
  },

  async profileInsight(profileId) {
    await randDelay(700, 1200);
    mockProgress.insight[profileId] = {
      games_count: (mockProgress.games[profileId] || []).length,
      features: mockFeatures,
      rank_estimate: '约 3 级 ~ 1 级',
      advice: null,
      updated_at: new Date().toISOString(),
    };
    return { insight: mockProgress.insight[profileId] };
  },

  async profileAdvice(profileId) {
    await randDelay(900, 1600);
    const ins = mockProgress.insight[profileId] || {
      games_count: (mockProgress.games[profileId] || []).length,
      features: mockFeatures,
      rank_estimate: '约 3 级 ~ 1 级',
      updated_at: new Date().toISOString(),
    };
    ins.advice = {
      summary: '你布局扎实、中盘有一定战斗力，但收官阶段每手损失偏大，是当前最值得改进的环节。',
      strengths: ['布局阶段选点合理，方向失误少', '中盘攻防敢于出手，好手率不错'],
      weaknesses: ['收官次序感弱，小官子爱随手', '复杂局面读棋深度不足，会出现简单失误'],
      plan: [
        { focus: '收官最大', why: '官子阶段平均每手损失最高（0.063），是拖累胜率的主要来源。',
          practice: '每天做 2 题收官题，先大后小、先厚后薄。',
          theme: '收官最大' },
        { focus: '中盘要点', why: '中盘出现 1 次计算失误，关键处要多算一步。',
          practice: '每周复盘 1 盘，用「关键手」找出最大损失的那几步。',
          theme: '中盘要点' },
      ],
      homework: ['本周完成 10 道收官题', '复盘 1 盘自己的对局，找出最大的 3 个失误'],
    };
    mockProgress.insight[profileId] = ins;
    return { insight: ins };
  },

  async systemInfo() {
    await delay(150);
    return {
      version: '0.9.0',
      engine_ready: true,
      model_ready: true,
      profile: mockSettings.profile,
      token_usage_month: 0.0103,
      token_limit_month: 30,
    };
  },

  async updateSettings(updates) {
    await randDelay(200, 500);
    Object.assign(mockSettings, updates);
    return this.systemInfo();
  },

  // ---- T0 可维护性接口的 mock 实现（与 api.js real 实现同构，¥0、无 fetch） ----
  async getSystemInfo() {
    await delay(150);
    return this.systemInfo();
  },

  async getHealth() {
    await delay(120);
    return { status: 'ok', engine: 'idle', db: 'ok' };
  },

  async getVersion() {
    await delay(100);
    return { version: '0.9.0', schema_version: 1 };
  },

  async restartEngine(payload = {}) {
    await randDelay(400, 900);
    return { ok: true, backend: payload.backend || 'auto' };
  },

  async clearCache(payload = {}) {
    await randDelay(200, 500);
    return { ok: true, cleared: 0 };
  },
};

// 供页面展示用的样例 SGF（"导入示例"按钮）
export const MOCK_SAMPLE_SGF = REVIEW_SGF;
// 供 M0 复盘页直接使用的 mock 结构（页面无需再写任何坐标逻辑）
export const MOCK_REVIEW_MOVES = REVIEW_MOVES;
export const MOCK_WINRATES = WINRATES;
export const MOCK_KEY_MOVE_EXTRA = KEY_MOVE_EXTRA;
export const MOCK_REVIEW_CURVE = REVIEW_CURVE;
export const MOCK_REVIEW_DETAIL = reviewDetail();
