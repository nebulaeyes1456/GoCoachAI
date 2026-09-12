/* 弈友 v2 · 复盘页脚本（M0 棋盘命门，窗口4）
 *
 * 铁律：棋盘渲染 / 坐标 / 落子 / 提子 / 变化播放 100% 使用 WGo.js：
 *   - WGo.SGF.parse      解析 SGF（含 SZ 棋盘尺寸、坐标）
 *   - WGo.Board          渲染棋盘、addObject/removeObject/update 增删棋子
 *   - WGo.KifuReader     手数定位（goTo）、步进（next/previous）、
 *                        从头到尾（first/last）、变化路径（rememberPath）
 * 本文件自绘的只有「胜率曲线」canvas —— 曲线不是棋盘，属任务书允许范围。
 * 本文件不含任何手写 getX/getY 坐标换算、落子/提子算法、变化树逻辑。
 */
import { createApp } from '../assets/vue.esm-browser.prod.js';
import {
  MOCK_SAMPLE_SGF,
  MOCK_WINRATES,
  MOCK_REVIEW_CURVE,
  MOCK_REVIEW_DETAIL,
} from './mock.js?v=20260910a';
import { api, getSource, setSource } from './api.js?v=20260910a';

// ---------------- WGo 运行时对象（不进 Vue 响应式，避免代理干扰） ----------------
let board = null;    // WGo.Board
let reader = null;   // WGo.KifuReader：播放 / 定位 / 变化，全部由其驱动
let kifu = null;     // WGo.SGF.parse 的解析结果
let mainNodes = [];  // 主变化中有落子的节点序列（仅用于展示手数/颜色）
let playTimer = null;
let pollTimer = null; // 真实复盘状态轮询句柄（1.5s 间隔）
let reviewToken = 0;  // 请求代际标记：载入新棋谱后旧任务全部作废

// ---- 练习视图的 WGo 运行时（同复盘：棋盘/落子/摆子全部走 WGo） ----
let practiceBoard = null;   // WGo.Board（题面棋盘）
let practiceReader = null;  // WGo.KifuReader（初始题面摆子由其 change 提供）
let practiceTrial = [];     // 试下已摆的子（addObject 的原始对象数组，便于 removeObject）
let practiceTrialColor = 'B'; // 试下摆子的当前颜色（从题面行棋方开始交替）

// ---- 复盘试摆模式（叠在 KifuReader 当前位置上，退出时全量重放恢复） ----
let trialStones = [];   // 试摆已摆的子
let trialColor = 'B';   // 试摆当前颜色
let trialGame = null;       // 试摆裁判：WGo.Game（提子/自杀/Ko 判定全部由它完成）
let trialSavedPath = null;  // 进入试摆时 reader.path 快照（退出/撤销全量恢复用）
let practiceTrialGame = null; // 练习试下裁判：WGo.Game

// ---- 对弈视图（新手板块）的 WGo 运行时 ----
let playBoard = null;   // WGo.Board（对弈棋盘）
let playGame = null;    // WGo.Game（合法性/提子判定）
let playMoves = [];     // [[color, coord], ...] 全部行棋序列
let playBusy = false;   // AI 思考/请求进行中（防止连点）

// ---- 记谱视图（对弈记录 + 胜率 + 试下研究；复盘功能本身不动） ----
let noteBoard = null;       // WGo.Board（记谱棋盘）
let noteGame = null;        // 主线裁判：WGo.Game
let noteMoves = [];         // 主线落子序列 [{x, y, c}]（含被提子历史，棋谱本身就是这样）
let noteSetup = [];         // 载入棋谱的初始摆子（AB/AW）
let noteBaseTurn = 'B';     // 主线首手行棋方
let noteTrialStones = [];   // 试下研究模式已摆的子
let noteTrialGame = null;   // 试下研究裁判：WGo.Game
let noteCurve = [];         // 胜率曲线（黑方视角）[{move, winrate}]
let noteToken = 0;          // 分析代际标记

// ---- 变化播放（PV 步进）：用 WGo KifuReader 驱动临时变化棋谱，不自写变化逻辑 ----
let pvReader = null;      // 变化播放 KifuReader
let pvTimer = null;       // 自动播放定时器
let pvSavedPath = null;   // 复盘：进入播放前主 reader 的 path 快照
let pvTarget = 'review';  // 'review' | 'practice'：播放的目标棋盘

// ---- 同步讲棋（讲解分段与棋盘变化联动播放） ----
let syncReader = null;    // 当前段变化 KifuReader
let syncTimer = null;     // 步进定时器
let syncTimeout = null;   // 段间延时
let syncSavedPath = null; // 复盘：进入同步播放前主 reader 的 path 快照

const KEY_CAT_NAMES = { blunder: '坏手', question: '疑问手', good: '好手' };
const KEY_CAT_COLORS = { blunder: '#c53030', question: '#d69e2e', good: '#2f855a' };

// 纯数据适配：后端 GTP 坐标（如 "D4"）→ WGo 逻辑坐标 {x, y}（0-based）。
// 列字母跳过 I：A-H → 0-7，J 及以后下标 -1；行 y = size - 数字。
// 仅做 API 数据格式映射，与红线无关（非棋盘渲染、非像素换算）。
function gtpToBoardCoord(coord, size) {
  if (!coord || !size || coord === 'pass') return null;
  const col = coord.charCodeAt(0) - 65; // 'A' = 0
  const x = col >= 8 ? col - 1 : col;   // 跳过 I
  const y = size - parseInt(coord.slice(1), 10);
  if (Number.isNaN(y) || x < 0 || x >= size || y < 0 || y >= size) return null;
  return { x, y };
}

// 自定义坐标标注：WGo 自带坐标画在画布最边缘（字母上半截被裁掉，几乎不可见）。
// 这里把字母数字完整画在边距区内，四边清晰可见。
const BOARD_COORDS = {
  grid: {
    draw(args, board) {
      const size = board.size;
      const gap = board.getX(0);               // 首交点到画布左缘 = 坐标区宽度
      const fontPx = Math.max(9, Math.min(15, gap * 0.4));
      const off = gap * 0.42;                  // 字母中心距画布边缘
      this.fillStyle = board.theme.coordinatesColor || '#2c2417';
      this.font = 'bold ' + fontPx + 'px "Microsoft YaHei", sans-serif';
      this.textBaseline = 'middle';
      this.textAlign = 'center';
      const ty = off, by = board.height - off;
      const lx = off, rx = board.width - off;
      for (let l = 0; l < size; l++) {
        let code = l + 65;
        if (code >= 73) code += 1;             // 跳过 I
        const ch = String.fromCharCode(code);
        const y = board.getY(l);
        this.fillText(String(size - l), lx, y);
        this.fillText(String(size - l), rx, y);
        const x = board.getX(l);
        this.fillText(ch, x, ty);
        this.fillText(ch, x, by);
      }
    },
  },
};

// gtpToBoardCoord 的逆函数：WGo 逻辑坐标 {x, y}（0-based，y 从上往下）→ 界面 GTP 坐标。
// 同样只做 API 数据格式映射（非棋盘渲染、非像素换算）。
function boardCoordToGtp(x, y, size) {
  if (!size || x < 0 || y < 0 || x >= size || y >= size) return null;
  const col = x >= 8 ? x + 1 : x; // 跳过 I
  const letter = String.fromCharCode(65 + col);
  return letter + (size - y);
}

// 黑方视角胜率：后端 winrate 为当前行棋方视角，白方手翻转成黑方视角。
function blackViewWinrate(m) {
  return m && m.color === 'W' ? 1 - m.winrate : m.winrate;
}

// 3 手中心滑动平均（端点取原值）：压掉 KataGo 估值噪声，仅用于曲线绘图；
// 关键手判定、讲解数据仍用原始值，不被平滑掩盖。
function smoothWinrate(arr, i) {
  if (i > 0 && i < arr.length - 1) {
    return (arr[i - 1].winrate + arr[i].winrate + arr[i + 1].winrate) / 3;
  }
  return arr[i].winrate;
}

// 数据映射：GTP 坐标（如 "D15"）→ SGF 坐标文本（如 "dp"）。仅做 API 数据格式映射。
function gtpToSgf(coord, size) {
  if (!coord || coord === 'pass' || !size) return null;
  const x = coord.charCodeAt(0) - 65;
  const col = x >= 8 ? x - 1 : x; // 跳过 I（前端逻辑坐标 = SGF 列索引，含 i）
  const y = size - parseInt(coord.slice(1), 10);
  if (Number.isNaN(y) || col < 0 || col >= size || y < 0 || y >= size) return null;
  return String.fromCharCode(97 + col) + String.fromCharCode(97 + y);
}

// 从 WGo.Board 当前状态导出摆子 SGF（数据导出，全部走 WGo 官方 getState）。
function buildPvSgf(targetBoard, pvGtpList, firstColor) {
  const size = targetBoard.size;
  const st = targetBoard.getState();
  const ab = [];
  const aw = [];
  for (let x = 0; x < size; x++) {
    for (let y = 0; y < size; y++) {
      const objs = (st.objects[x] && st.objects[x][y]) || [];
      for (const o of objs) {
        const s = String.fromCharCode(97 + x) + String.fromCharCode(97 + y);
        // WGo 2.3.1 石头对象字段是 c（WGo.B=1 黑 / WGo.W=-1 白），不是 type
        if (o.c === WGo.B) ab.push(s);
        else if (o.c === WGo.W) aw.push(s);
      }
    }
  }
  let sgf = `(;GM[1]FF[4]SZ[${size}]`;
  if (ab.length) sgf += 'AB' + ab.map((c) => `[${c}]`).join('');
  if (aw.length) sgf += 'AW' + aw.map((c) => `[${c}]`).join('');
  let color = firstColor === 'W' ? 'W' : 'B';
  let steps = 0;
  for (const g of pvGtpList) {
    const s = gtpToSgf(g, size);
    if (!s) continue; // 越界/非法坐标跳过（步数以实际有效为准）
    sgf += `;${color}[${s}]`;
    color = color === 'B' ? 'W' : 'B';
    steps += 1;
  }
  return { sgf: sgf + ')', steps };
}

// 当前手数：沿 reader.node 回溯到根，统计落子节点（不写任何树遍历/落子逻辑，
// 只是数数；变化定位依然由 KifuReader.path 负责）。
function currentMoveNumber() {
  if (!reader) return 0;
  let n = 0;
  let node = reader.node;
  while (node && node.parent) {
    if (node.move) n += 1;
    node = node.parent;
  }
  return n;
}

function boardWidth() {
  const host = document.getElementById('board-host');
  const w = host && host.clientWidth ? host.clientWidth : 0;
  return w || Math.min(520, window.innerWidth - 80);
}

function noteBoardWidth() {
  const host = document.getElementById('note-board-host');
  const w = host && host.clientWidth ? host.clientWidth : 0;
  return w || Math.min(520, window.innerWidth - 80);
}

// WGo 逻辑坐标 → SGF 坐标文本（纯数据映射，非棋盘渲染）
function sgfCoord(x, y) {
  return String.fromCharCode(97 + x) + String.fromCharCode(97 + y);
}

// mock 记谱胜率：确定性伪随机游走（黑方视角，0.15~0.85）
function buildMockNoteCurve(n) {
  const curve = [];
  let w = 0.5;
  for (let i = 1; i <= n; i++) {
    const r = Math.sin(i * 12.9898) * 0.05 + Math.cos(i * 4.1414) * 0.03;
    w = Math.min(0.85, Math.max(0.15, w + r));
    curve.push({ move: i, winrate: Math.round(w * 1000) / 1000 });
  }
  return curve;
}

function practiceBoardWidth() {
  const host = document.getElementById('practice-board-host');
  const w = host && host.clientWidth ? host.clientWidth : 0;
  return w || Math.min(520, window.innerWidth - 420);
}

// 从棋盘显示状态构造 WGo.Game（子用 getState 导出、提子/自杀判定全部交给 WGo.Game.play，
// 本文件不写任何气/提子算法）。turnColor 为 'B' | 'W' 的当前行棋方。
function gameFromBoard(b, turnColor) {
  const g = new WGo.Game(b.size);
  const st = b.getState();
  for (let x = 0; x < b.size; x++) {
    for (let y = 0; y < b.size; y++) {
      const objs = (st.objects[x] && st.objects[x][y]) || [];
      for (const o of objs) {
        if (o.c === WGo.B || o.c === WGo.W) g.addStone(x, y, o.c);
      }
    }
  }
  g.turn = turnColor === 'W' ? WGo.W : WGo.B;
  return g;
}

// 复盘棋谱全量重放到指定路径：清盘 + 新 reader 从根逐步 next（每步增量在盘上累加），
// 与 pvStop 的恢复同一模式；退出试摆/撤销试摆子后棋盘与 reader 都恢复一致。
function restoreReviewBoardTo(path) {
  if (!board || !kifu) return;
  board.removeAllObjects();
  const tmp = new WGo.KifuReader(kifu, true, false);
  board.update(tmp.change);
  let guard = 0;
  while (tmp.path && path && tmp.path.m < path.m && guard < 10000) {
    tmp.next();
    board.update(tmp.change);
    guard++;
  }
  reader = tmp;
}

;

// WGo 官方用法：把 KifuReader 每次步进产生的 change（增/删子集合）
// 直接喂给 board.update()，棋子与提子全部由 WGo 内部坐标完成。
function applyReaderChange() {
  if (board && reader && reader.change) board.update(reader.change);
}

createApp({
  data() {
    return {
      // 正式版（桌面 exe 以 ?prod=1 加载）隐藏数据源切换、强制真实后端；
      // 浏览器直接打开（无参数）为开发模式，保留 Mock 切换。
      prodMode: new URLSearchParams(location.search).has('prod'),
      source: new URLSearchParams(location.search).has('prod') ? 'real' : getSource(),
      sgfText: MOCK_SAMPLE_SGF,
      loadedSgf: MOCK_SAMPLE_SGF,
      loadError: '',
      loadedInfo: '',
      sgfCollapsed: true,   // 载入棋谱面板默认折叠（给棋盘腾出完整高度）
      boardLabel: '',
      totalMoves: 0,
      kifuSize: 0,
      currentMove: 0,
      sliderValue: 0,
      moveColors: [],
      playing: false,
      settingsOpen: false,
      sysLoading: false,
      sysInfo: {
        version: '0.9.0-dev（占位）',
        backend: 'opencl',
        schema: 'v1（占位）',
        engineReady: false,
        modelReady: false,
        profile: 'fast',
        healthStatus: '',
        healthNote: '',
      },
      turn: 'B',
      // ---- M1：真实复盘数据流 ----
      busy: false,          // 分析中：禁用载入按钮
      reviewStatus: 'idle', // idle/pending/analyzing/done/failed
      progress: 0,
      reviewError: '',
      reviewId: '',
      reviewDetail: null,   // ReviewDetailResponse
      curveData: [],        // winrate_curve（MoveInfo[]）
      finalOwnership: [],   // 终局目数热图 [-1,1]（KataGo ownership）
      showHeat: true,       // 目数热图开关
      // ---- M1：讲解面板 ----
      explainOpen: false,
      explainMoveNumber: 0,
      explainLoading: false,
      explainData: null,
      explainFallback: '',
      // ---- M2：三视图切换 ----
      view: 'review',   // review / practice / ask
      // ---- M3：变化播放（PV 步进，KifuReader 驱动） ----
      pvOpen: false,     // 播放模式中（浮层控制条）
      pvIndex: 0,        // 当前播放步（0 = 初始局面）
      pvTotal: 0,        // 变化总步数
      pvPlaying: false,  // 自动播放中
      // ---- M2：练习（试下） ----
      practiceTheme: '',
      practiceList: [],
      practicePage: 0,        // 题库分页（0 基，每页 50 题）
      practiceSort: 'easiest', // 难度排序：easiest 从易到难 | hardest 从难到易 | '' 最新
      practiceMode: 'list',   // 'list' 题库列表 | 'solve' 做题子页面
      deepLoading: false,     // 深度分析生成中
      deepData: null,         // 深度分析报告
      deepError: '',          // 深度分析降级提示
      deepOpen: false,        // 报告面板开关
      voicePersona: 'gentle', // 配音人设：gentle 温柔大姐姐 | tsundere 傲娇小萝莉 | default
      voicePlaying: false,    // 正在播放配音
      penaltyLoading: false,  // 惩罚变化分析中
      // ---- 对弈（新手板块） ----
      playBoardSize: 9,
      playAiRank: '10k',   // AI 棋力档：''=最强 / 20k~1d（humanSL 级位模拟）
      playAiRankOptions: [
        { v: '', label: '最强（全强度）' },
        { v: '1d', label: '业余 1 段' },
        { v: '1k', label: '业余 1 级' },
        { v: '5k', label: '业余 5 级' },
        { v: '10k', label: '业余 10 级（新手推荐）' },
        { v: '15k', label: '业余 15 级（入门）' },
        { v: '20k', label: '业余 20 级（零基础）' },
      ],
      playBoardLabel: '',
      playTurn: 'B',
      playMoveCount: 0,
      playAiThinking: false,
      playEnded: false,
      playUserWinrateText: '',
      playStatusText: '',
      playHint: null,        // {move, color, coord, level, title, text, best, delta}
      playSuggestion: [],     // 棋盘建议标记 [{obj, x, y}]：绿圈=AI 一选，红圈=你的落点
      playTipText: '',
      playTipLoading: false,
      penaltyNotes: [],       // 惩罚变化分步解说 [{step, role, text}]
      penaltySummary: '',     // 惩罚变化总结（播放完显示）
      penaltyNoteText: '',    // 当前步解说文案
      // ---- 同步讲棋（讲解与棋盘变化联动） ----
      syncOpen: false,        // 同步播放中
      syncSegs: [],           // 当前讲解分段 [{text, variation:[[色,坐标]...]}]
      syncIdx: -1,            // 当前段索引
      syncStep: 0,            // 段内已播步数
      syncTotal: 0,           // 段内总步数
      syncPlaying: false,     // 段变化自动播放中
      syncTarget: 'review',   // 'review' | 'practice'
      practiceExplain: null,      // 练习深度讲解结果
      practiceExplainLoading: false,
      practiceExplainError: '',
      extracting: false,      // 收录本题请求中
      // ---- 成长视图（棋手档案 / 棋谱库 / 水平画像） ----
      progressProfiles: [],
      progressProfileId: '',
      progressDetail: null,    // {profile, games, insight}
      progressLoading: false,
      progressInsight: null,   // 画像响应（含 features/rank_estimate）
      progressInsightLoading: false,
      progressAdvice: null,
      progressAdviceLoading: false,
      progressImportOpen: false,
      progressImportText: '',
      progressImporting: false,
      progressNewName: '',
      attachProfileId: '',     // 复盘页收藏目标档案
      practiceTotal: 0,
      practiceLoading: false,
      practiceError: '',
      practiceProblem: null,
      practiceListIndex: -1,
      practiceSolver: 'B',
      practiceWrongCount: 0,
      attemptLoading: false,
      attemptResult: null,
      practiceTrialN: 0,   // 响应式计数（practiceTrial 为模块级数组，Vue 无法追踪）
      practiceShownAnswer: false,
      // ---- M2：答疑 ----
      askPresets: [
        '围棋为什么先占角？',
        '什么是「气」和「眼」？怎样才算活棋？',
        '什么是定式？新手需要背定式吗？',
        '为什么说「金角银边草肚皮」？',
        '什么是厚势？厚势该怎么用？',
        '「先手」和「后手」是什么意思？',
        '什么是打劫？劫材是什么？',
        '什么时候该弃子？弃子有什么好处？',
      ],
      askQuestion: '',
      askSgf: '',
      askLevel: '-5',
      askLoading: false,
      askResult: null,
      askError: '',
      levelOptions: [
        { value: '-15', label: '15K' }, { value: '-14', label: '14K' },
        { value: '-13', label: '13K' }, { value: '-12', label: '12K' },
        { value: '-11', label: '11K' }, { value: '-10', label: '10K' },
        { value: '-9', label: '9K' }, { value: '-8', label: '8K' },
        { value: '-7', label: '7K' }, { value: '-6', label: '6K' },
        { value: '-5', label: '5K' }, { value: '-4', label: '4K' },
        { value: '-3', label: '3K' }, { value: '-2', label: '2K' },
        { value: '-1', label: '1K' }, { value: '1', label: '1D' },
        { value: '2', label: '2D' }, { value: '3', label: '3D' },
      ],
      // ---- M2：复盘试摆 -------
      trialMode: false,
      // ---- M2：复盘档位（设置面板切换后同步，analyze 时生效） ----
      reviewProfile: 'fast',
      // ---- 记谱视图 ----
      noteBoardSize: 19,     // 默认 19 路（9/13 为可选）
      noteBoardLabel: '19 路',
      noteViewPos: 0,        // 当前浏览位置（响应式；noteMoves 为模块数组）
      noteCount: 0,          // 主线手数（响应式计数）
      noteTurn: 'B',         // 当前行棋方
      noteTrialMode: false,  // 试下研究模式
      noteTrialCount: 0,     // 试下子计数（响应式）
      noteWinrateText: '',   // 当前胜率文本
      noteCurveLabel: '',    // 胜率曲线说明
      noteAnalyzing: false,
      noteCandidates: [],    // 当前局面 KataGo 选点推荐（黑方视角）
      noteOwnership: [],     // 当前局面目数热图
      noteSgfInput: '',
      noteTip: '点击棋盘落子（黑白交替），落子后自动分析胜率',
      // ---- Toast ----
      toasts: [],
    };
  },

  computed: {
    viewName() {
      return { review: '复盘', practice: '练习', note: '记谱', play: '对弈', ask: '答疑', progress: '成长' }[this.view] || '复盘';
    },
    noteMoveNumbers() {
      return Array.from({ length: this.noteCount }, (_, i) => i + 1);
    },
    noteMoveColors() {
      const n = this.noteCount; // 响应式依赖（noteMoves 是模块数组）
      const base = noteBaseTurn === 'W' ? 'W' : 'B';
      return noteMoves.map((m, i) => (i % 2 === 0 ? base : (base === 'B' ? 'W' : 'B')));
    },
    practiceTrialCount() {
      return this.practiceTrialN;
    },
    moveNumbers() {
      return Array.from({ length: this.totalMoves }, (_, i) => i + 1);
    },
    progressPct() {
      return Math.round(this.progress * 100) + '%';
    },
    // 关键手：真实模式取后端 category != normal；mock 与 M0 一致
    keyMoves() {
      if (this.source === 'real') {
        return this.curveData.filter((m) => m.category && m.category !== 'normal');
      }
      if (!this.isBuiltin()) return [];
      return MOCK_REVIEW_CURVE.filter((m) => m.category !== 'normal');
    },
    keyStats() {
      if (this.source === 'real' && this.reviewDetail && this.reviewDetail.stats) {
        const s = this.reviewDetail.stats;
        return { blunders: s.blunders || 0, questions: s.questions || 0, good: s.good || 0 };
      }
      const ks = this.keyMoves;
      return {
        blunders: ks.filter((m) => m.category === 'blunder').length,
        questions: ks.filter((m) => m.category === 'question').length,
        good: ks.filter((m) => m.category === 'good').length,
      };
    },
    curveSourceLabel() {
      if (this.source !== 'real') return '黑方视角胜率（mock 数据 · 3 手平滑）';
      return this.reviewStatus === 'done'
        ? `黑方视角胜率（真实 KataGo · ${this.curveData.length} 手 · 3 手平滑）`
        : '黑方视角胜率（真实后端）';
    },
    explainMove() {
      const n = this.explainMoveNumber;
      if (!n) return null;
      if (this.source === 'real') return this.curveData.find((m) => m.move === n) || null;
      return MOCK_REVIEW_CURVE.find((m) => m.move === n) || null;
    },
  },

  methods: {
    isBuiltin() {
      // 注意：只用响应式字段判定（kifu 是模块变量，非响应式，
      // computed 依赖它会导致缓存不刷新——keyMoves 曾因此恒为空）
      return this.kifuSize === 9 && this.totalMoves === MOCK_WINRATES.length;
    },
    catName(c) {
      return KEY_CAT_NAMES[c] || c;
    },
    showToast(msg, type) {
      const t = { id: Date.now() + Math.random(), msg, type: type || '' };
      this.toasts.push(t);
      setTimeout(() => {
        this.toasts = this.toasts.filter((x) => x.id !== t.id);
      }, 3500);
    },
    // showToast 的别名（历史调用点统一走这里）
    toast(msg, type) {
      this.showToast(msg, type);
    },

    // ---------------- M2：三视图切换 ----------------
    switchView(v) {
      if (['review', 'practice', 'note', 'play', 'ask', 'progress'].indexOf(v) < 0) v = 'review';
      this.view = v;
      document.body.dataset.view = v;
      if (v === 'progress') {
        this.loadProfiles();
      } else if (v === 'practice') {
        // 首次进入练习视图时加载题库
        if (!this.practiceList.length && !this.practiceLoading && !this.practiceError) {
          this.loadLibrary();
        }
      } else if (v === 'note') {
        // 记谱视图：懒初始化棋盘（新对局）
        this.$nextTick(() => {
          if (!noteBoard) this.noteNewGame();
        });
      } else if (v === 'play') {
        // 对弈视图（新手板块）：懒初始化棋盘
        this.$nextTick(() => {
          if (!playBoard) this.playNewGame();
        });
      } else if (v === 'ask') {
        // 预填复盘页已载入的 SGF（可选）
        if (!this.askSgf && this.loadedSgf) this.askSgf = this.loadedSgf;
      } else if (v === 'review') {
        this.$nextTick(() => {
          if (board) board.setWidth(boardWidth());
          this.drawCurve();
        });
      }
    },
    practiceGoalText() {
      if (!this.practiceProblem) return '';
      const p = this.practiceProblem;
      const side = (this.practiceSolver === 'W' ? '白' : '黑') + '先';
      if (p.goal) {
        const map = {
          '做活': `做活自己的棋（${side}）`,
          '杀棋': `杀净对方（${side}）`,
          '对杀': `对杀取胜：比对方快一气（${side}）`,
          '对杀取胜': `对杀取胜：比对方快一气（${side}）`,
          '逃棋筋': `逃出己方棋筋（${side}）`,
          '吃棋筋': `吃掉对方棋筋（${side}）`,
          '收官最大': '收官：找出当前局面价值最大的一手',
          '中盘要点': '中盘：找到当前局面最要紧的要点',
        };
        if (map[p.goal]) return map[p.goal];
        return `${p.goal}（${side}）`;
      }
      const t = p.theme;
      return {
        life_death: '做活自己，或杀掉对方——看清现在该谁走',
        capturing_race: '对杀取胜：比对方快一步紧气',
        endgame: '官子：找出当前局面价值最大的一手',
        middle: '中盘：找到当前局面最要紧的要点',
      }[t] || '找到当前局面最好的一手';
    },
    themeName(t) {
      return { life_death: '死活', capturing_race: '对杀', endgame: '官子', middle: '中盘' }[t] || t;
    },
    rankLabel(min, max) {
      const fmt = (r) => {
        if (r === null || r === undefined) return '?';
        if (r < 0) return (-r) + 'K';
        if (r === 0) return '1D';
        return r + 'D';
      };
      if (min === null || min === undefined || max === null || max === undefined) return '不限';
      if (min === max) return fmt(min);
      return fmt(min) + '~' + fmt(max);
    },

    // ---------------- M2：练习（试下） ----------------
    async loadLibrary() {
      this.practiceLoading = true;
      this.practiceError = '';
      try {
        const r = await api.library({
          theme: this.practiceTheme || undefined,
          limit: 50,
          offset: this.practicePage * 50,
          sort: this.practiceSort || undefined,
        });
        this.practiceList = (r && r.problems) || [];
        this.practiceTotal = (r && r.total) || this.practiceList.length;
        this.practiceTotalPages = Math.max(1, Math.ceil(this.practiceTotal / 50));
        if (this.practicePage + 1 > this.practiceTotalPages) {
          this.practicePage = this.practiceTotalPages - 1;
        }
        document.body.dataset.practiceCount = String(this.practiceList.length);
      } catch (e) {
        this.practiceError = '题库加载失败：' + ((e && e.message) || e);
        this.practiceList = [];
      } finally {
        this.practiceLoading = false;
      }
    },
    onThemeChange() {
      this.practicePage = 0;
      this.loadLibrary();
    },
    onSortChange() {
      this.practicePage = 0;
      this.loadLibrary();
    },
    gotoPracticePage(pg) {
      const total = this.practiceTotalPages || 1;
      if (pg < 0 || pg >= total || pg === this.practicePage) return;
      this.practicePage = pg;
      this.loadLibrary();
    },
    async selectProblem(p, i) {
      this.practiceError = '';
      this.attemptResult = null;
      this.practiceWrongCount = 0;
      this.practiceShownAnswer = false;
      this.practiceListIndex = i;
      this.practiceProblem = null; // 先清旧题，展示加载态
      try {
        const detail = await api.problem(p.id);
        this.practiceProblem = detail;
        this.practiceMode = 'solve'; // 点击条目进入做题子页面
        this.$nextTick(() => this.buildPracticeBoard(detail));
      } catch (e) {
        this.toast('加载题目失败：' + ((e && e.message) || e), 'error');
      }
    },
    backToPracticeList() {
      // 返回题库列表（清空做题状态，下次进入重新加载）
      this.practiceMode = 'list';
      this.practiceProblem = null;
      this.attemptResult = null;
      this.practiceWrongCount = 0;
      this.practiceShownAnswer = false;
      this.practiceTrialN = 0;
      practiceTrial = [];
      practiceBoard = null;
      practiceReader = null;
      practiceTrialGame = null;
    },
    // 题面渲染：WGo.SGF.parse → WGo.Board → WGo.KifuReader 初始 change（含 AB/AW 摆子）
    buildPracticeBoard(detail) {
      const host = document.getElementById('practice-board-host');
      if (!host) return;
      host.innerHTML = '';
      practiceBoard = null;
      practiceReader = null;
      let k;
      try {
        k = WGo.SGF.parse(detail.setup_sgf);
      } catch (e) {
        this.toast('题面 SGF 解析失败：' + ((e && e.message) || e), 'error');
        return;
      }
      if (!k || !k.size) {
        this.toast('题面 SGF 缺少 SZ 属性', 'error');
        return;
      }
      practiceBoard = new WGo.Board(host, {
        size: k.size,
        width: practiceBoardWidth(),
        background: '',
        font: 'Microsoft YaHei, sans-serif',
        theme: { coordinatesColor: '#2c2417' },
        section: { top: 0.25, right: 0.25, bottom: 0.25, left: 0.25 },
      });
      practiceBoard.addCustomObject(BOARD_COORDS);
      this.$nextTick(() => this.syncBoardWidths());
      practiceReader = new WGo.KifuReader(k, true, false);
      practiceBoard.update(practiceReader.change); // 题面摆子全部由 WGo 完成

      // 行棋方：优先 branches.solver（后端白先题用 ;B[tt] 修奇偶、无 PL），
      // 其次 PL 解析出的 node.turn，默认黑先。
      let solver = practiceReader.game.turn === WGo.W ? 'W' : 'B';
      try {
        const b = JSON.parse(detail.branches || '[]');
        if (b && (b.solver === 'W' || b.solver === 'B')) solver = b.solver;
      } catch (e) {
        /* branches 为空数组等，忽略 */
      }
      this.practiceSolver = solver;
      practiceTrial = [];
      practiceTrialColor = solver;
      practiceTrialGame = gameFromBoard(practiceBoard, solver); // 提子/自杀裁判
      this.practiceTrialN = 0;
      this.practiceWrongCount = 0;
      this.attemptResult = null;
      document.body.dataset.practiceSize = String(k.size);
      document.body.dataset.practiceSolver = solver;

      // 试下交互：WGo 官方点击事件（回调给出逻辑坐标 x/y，无任何自写像素换算）
      practiceBoard.addEventListener('click', (x, y) => this.onPracticeBoardClick(x, y));
    },
    onPracticeBoardClick(x, y) {
      if (!practiceBoard || !practiceTrialGame || x < 0 || y < 0) return;
      const st = practiceBoard.getState();
      const objs = (st.objects[x] && st.objects[x][y]) || [];
      if (objs.some((o) => o.c === WGo.B || o.c === WGo.W)) {
        return; // 该点已有棋子（题面或已摆）
      }
      const c = practiceTrialColor === 'W' ? WGo.W : WGo.B;
      // 合法性/提子判定全部交给 WGo.Game.play（返回数字=非法，数组=被提子）
      const res = practiceTrialGame.play(x, y, c);
      if (typeof res === 'number') {
        this.toast('非法落子（自杀或禁着）', 'error');
        return;
      }
      const captured = res || [];
      for (const cap of captured) {
        practiceBoard.removeObject(cap); // 显示棋盘提掉（可能是题面子）
        practiceTrial = practiceTrial.filter((o) => !(o.x === cap.x && o.y === cap.y));
      }
      const o = { x, y, c };
      practiceBoard.addObject(o);
      practiceTrial.push(o);
      practiceTrialColor = practiceTrialColor === 'B' ? 'W' : 'B';
      this.attemptResult = null; // 摆新子后旧反馈作废
      this.practiceShownAnswer = false;
      this.practiceTrialN = practiceTrial.length;
      document.body.dataset.practiceTrial = String(practiceTrial.length);
    },
    undoPracticeTrial() {
      if (!practiceBoard || !practiceTrial.length) return;
      practiceTrial.pop();
      this.replayPracticeTrial();
    },
    // 撤销后全量重放：题面 + 剩余试下子逐手 play（提子效果同步重现）
    replayPracticeTrial() {
      if (!practiceBoard || !practiceReader) return;
      practiceBoard.removeAllObjects();
      practiceBoard.update(practiceReader.change); // 题面全量（被提的题面子也恢复）
      practiceTrialGame = gameFromBoard(practiceBoard, this.practiceSolver);
      const stones = practiceTrial.slice();
      practiceTrial = [];
      for (const o of stones) {
        const res = practiceTrialGame.play(o.x, o.y, o.c);
        if (typeof res === 'number') continue;
        for (const cap of res) practiceBoard.removeObject(cap);
        practiceBoard.addObject(o);
        practiceTrial.push(o);
      }
      const base = this.practiceSolver === 'W' ? 'W' : 'B';
      practiceTrialColor = practiceTrial.length % 2 === 1
        ? (base === 'B' ? 'W' : 'B')
        : base;
      this.practiceTrialN = practiceTrial.length;
      document.body.dataset.practiceTrial = String(practiceTrial.length);
    },
    resetPracticeTrial() {
      if (!practiceBoard || !practiceTrial.length) return;
      practiceBoard.removeAllObjects();
      practiceBoard.update(practiceReader.change); // 题面全量（被提的题面子也恢复）
      practiceTrial = [];
      practiceTrialGame = gameFromBoard(practiceBoard, this.practiceSolver);
      practiceTrialColor = this.practiceSolver;
      this.practiceTrialN = 0;
      document.body.dataset.practiceTrial = '0';
    },
    async submitAttempt() {
      if (!this.practiceProblem || !practiceBoard || !practiceTrial.length) {
        this.toast('请先在棋盘上落子试下', 'error');
        return;
      }
      if (this.attemptLoading) return;
      const first = practiceTrial[0];
      const coord = boardCoordToGtp(first.x, first.y, practiceBoard.size);
      if (!coord) {
        this.toast('落点无效', 'error');
        return;
      }
      this.attemptLoading = true;
      this.attemptResult = null;
      try {
        const r = await api.attempt(this.practiceProblem.id, coord);
        this.attemptResult = r;
        if (r.correct) {
          this.toast('答对了！', 'success');
          // 触发方式融入讲解：答对后自动生成并播放同步深度讲解
          this.loadPracticeExplain();
        } else {
          this.practiceWrongCount += 1;
        }
        document.body.dataset.attemptCorrect = r.correct ? '1' : '0';
      } catch (e) {
        // 判题失败不阻断做题：显示降级反馈
        this.attemptResult = {
          correct: false,
          response: '判定服务暂不可用：' + ((e && e.message) || e),
          variation: [],
          solved: false,
          explanation: null,
        };
        this.practiceWrongCount += 1;
        document.body.dataset.attemptCorrect = '0';
      } finally {
        this.attemptLoading = false;
      }
    },
    showPracticeAnswer() {
      if (!this.practiceProblem || !practiceBoard) return;
      if (this.practiceWrongCount < 3) return;
      const p = gtpToBoardCoord(this.practiceProblem.answer, practiceBoard.size);
      if (p) {
        practiceBoard.addObject({ type: 'CR', x: p.x, y: p.y, c: '#2f855a', lineWidth: 2 });
      }
      this.practiceShownAnswer = true;
      document.body.dataset.practiceAnswer = '1';
    },
    nextProblem() {
      const list = this.practiceList;
      if (!list.length) return;
      const ni = (this.practiceListIndex + 1) % list.length;
      this.selectProblem(list[ni], ni);
    },

    // ---------------- M2：答疑（单次调用、失败降级、绝不重试风暴） ----------------
    askPreset(q) {
      if (!q || this.askLoading) return;
      this.askQuestion = q;
      this.submitAsk();
    },
    async submitAsk() {
      const q = (this.askQuestion || '').trim();
      if (!q || this.askLoading) return;
      this.askLoading = true;
      this.askResult = null;
      this.askError = '';
      try {
        this.askResult = await api.ask(
          (this.askSgf || '').trim(), q, String(this.askLevel || '-5')
        );
        document.body.dataset.askResult = '1';
      } catch (e) {
        this.askError = '答疑暂不可用（需配置 DeepSeek key）';
        document.body.dataset.askFallback = '1';
      } finally {
        this.askLoading = false;
      }
    },

    // ---------------- M2：复盘试摆（提子/自杀判定走 WGo.Game，退出全量重放恢复） ----------------
    toggleTrial() {
      if (!board) return;
      if (this.trialMode) {
        board.removeEventListener('click', this.onTrialBoardClick);
        restoreReviewBoardTo(trialSavedPath); // 全量恢复：被提的棋谱子也回来
        trialStones = [];
        trialGame = null;
        trialSavedPath = null;
        this.trialMode = false;
        document.body.dataset.trial = '0';
        document.body.dataset.trialStones = '0';
        this.syncUI();
        this.toast('已退出试摆，恢复棋谱定位');
      } else {
        this.stopPlay();
        trialStones = [];
        trialColor = this.turn || 'B';
        trialSavedPath = reader ? JSON.parse(JSON.stringify(reader.path)) : { m: 0 };
        trialGame = gameFromBoard(board, this.turn);
        board.addEventListener('click', this.onTrialBoardClick);
        this.trialMode = true;
        document.body.dataset.trial = '1';
        document.body.dataset.trialStones = '0';
        this.toast('试摆模式：点击棋盘摆子（黑白交替），退出后恢复棋谱');
      }
    },
    onTrialBoardClick(x, y) {
      if (!board || !trialGame || x < 0 || y < 0) return;
      const st = board.getState();
      const objs = (st.objects[x] && st.objects[x][y]) || [];
      if (objs.some((o) => o.c === WGo.B || o.c === WGo.W)) {
        return; // 已有棋子
      }
      const c = trialColor === 'W' ? WGo.W : WGo.B;
      // 合法性/提子判定全部交给 WGo.Game.play（返回数字=非法，数组=被提子）
      const res = trialGame.play(x, y, c);
      if (typeof res === 'number') {
        this.toast('非法落子（自杀或禁着）', 'error');
        return;
      }
      const captured = res || [];
      for (const cap of captured) {
        board.removeObject(cap); // 显示棋盘提掉（可能是棋谱原有子）
        trialStones = trialStones.filter((o) => !(o.x === cap.x && o.y === cap.y));
      }
      const o = { x, y, c };
      board.addObject(o);
      trialStones.push(o);
      trialColor = trialColor === 'B' ? 'W' : 'B';
      document.body.dataset.trialStones = String(trialStones.length);
    },
    undoTrialStone() {
      if (!board || !trialStones.length) return;
      trialStones.pop();
      this.replayTrialStones();
    },
    // 撤销后全量重放：棋谱定位 + 剩余试摆子逐手 play（提子效果同步重现）
    replayTrialStones() {
      if (!board) return;
      restoreReviewBoardTo(trialSavedPath);
      trialGame = gameFromBoard(board, this.turn);
      const stones = trialStones.slice();
      trialStones = [];
      for (const o of stones) {
        const res = trialGame.play(o.x, o.y, o.c);
        if (typeof res === 'number') continue;
        for (const cap of res) board.removeObject(cap);
        board.addObject(o);
        trialStones.push(o);
      }
      const base = this.turn === 'W' ? 'W' : 'B';
      trialColor = trialStones.length % 2 === 1
        ? (base === 'B' ? 'W' : 'B')
        : base;
      document.body.dataset.trialStones = String(trialStones.length);
    },

    onSourceChange() {
      setSource(this.source);
      if (this.trialMode) this.toggleTrial();
      this.resetReview();
      if (this.source === 'real' && kifu) {
        this.runReview(this.loadedSgf);
      } else {
        this.finalOwnership = (MOCK_REVIEW_DETAIL && MOCK_REVIEW_DETAIL.final_ownership) || [];
        this.$nextTick(() => {
          this.drawCurve();
          this.drawComplexityCurve();
          this.drawScoreCurve();
          if (this.showHeat) this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
        });
      }
      // 数据源切换后练习题库随之刷新（真实题库 ↔ mock 题库）
      if (this.view === 'practice') this.loadLibrary();
    },
    async requestDeep() {
      if (!this.reviewId || this.deepLoading) return;
      this.deepLoading = true;
      this.deepError = '';
      this.deepData = null;
      this.deepOpen = true;
      try {
        const r = await api.deep(this.reviewId);
        this.deepData = r;
      } catch (e) {
        this.deepError = (e && e.message) || String(e);
      } finally {
        this.deepLoading = false;
      }
    },
    closeDeep() {
      this.deepOpen = false;
    },
    async speakText(text, prefix = '') {
      if (!text || this.voicePlaying) return;
      const full = prefix + text;
      try {
        this.voicePlaying = true;
        const blob = await api.ttsBlob(full, this.voicePersona);
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audio.onended = () => {
          this.voicePlaying = false;
          URL.revokeObjectURL(url);
        };
        audio.onerror = () => {
          this.voicePlaying = false;
          this.toast('语音播放失败', 'error');
        };
        audio.play();
      } catch (e) {
        this.voicePlaying = false;
        this.toast('语音合成失败：' + ((e && e.message) || e), 'error');
      }
    },
    speakExplain() {
      if (!this.explainData || !this.explainData.content) return;
      const c = this.explainData.content;
      const text = [
        c.problem, c.reason, c.recommendation, c.necessity,
        (c.alternatives || []).join('；'), c.takeaway,
      ].filter(Boolean).join('。');
      this.speakText(text, '这是第 ' + (this.explainMove ? this.explainMove.move : '') + ' 手的讲解。');
    },
    speakDeep() {
      if (!this.deepData || !this.deepData.content) return;
      const c = this.deepData.content;
      const km = (c.key_moves || [])
        .map((k) => '第 ' + k.move + ' 手，' + k.analysis).join('。');
      const text = [
        c.title, c.overview, (c.stages || []).join(''),
        km, c.causality, (c.strengths || []).join('；'),
        (c.weaknesses || []).join('；'), (c.homework || []).join('；'),
      ].filter(Boolean).join('。');
      this.speakText(text, '整盘深度分析报告。');
    },
    openSettings() {
      this.settingsOpen = true;
      this.loadSysInfo();
    },

    // ---------------- 设置面板（T0 真实端点：info/health/version） ----------------
    async loadSysInfo() {
      this.sysLoading = true;
      try {
        const [info, ver, health] = await Promise.all([
          api.getSystemInfo(),
          api.getVersion(),
          api.getHealth(),
        ]);
        this.sysInfo.version = info.version || '';
        this.sysInfo.backend = info.engine_backend || 'auto';
        const schema = ver.schema_version || info.schema_version;
        this.sysInfo.schema = schema ? 'v' + schema : '未知';
        this.sysInfo.engineReady = !!info.engine_ready;
        this.sysInfo.modelReady = !!info.model_ready;
        this.sysInfo.profile = info.profile || 'fast';
        this.reviewProfile = info.profile || 'fast'; // 复盘 analyze 使用当前档位
        this.sysInfo.healthStatus = (health && health.status) || '';
        const checks = health && health.checks ? health.checks : null;
        if (checks) {
          const parts = [];
          for (const k of ['database', 'model_file', 'deepseek_key', 'engine_process']) {
            const c = checks[k];
            if (c) parts.push(k + ':' + (c.ok ? 'ok' : 'failed'));
          }
          this.sysInfo.healthNote = parts.join(' · ');
        } else {
          this.sysInfo.healthNote = '';
        }
      } catch (e) {
        this.toast('读取系统信息失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.sysLoading = false;
      }
    },
    async changeBackend() {
      try {
        // 任务书：引擎后端切换走 PUT /api/v1/system/settings，写入 katago.backend（auto|opencl|cpu）
        const info = await api.updateSettings({ katago: { backend: this.sysInfo.backend } });
        if (info && info.engine_backend) {
          this.sysInfo.backend = info.engine_backend;
        }
        this.toast('引擎后端已写入配置：' + this.sysInfo.backend, 'success');
      } catch (e) {
        this.toast('切换引擎后端失败：' + ((e && e.message) || e), 'error');
      }
    },
    async changeProfile() {
      try {
        await api.updateSettings({ profile: this.sysInfo.profile });
        this.reviewProfile = this.sysInfo.profile; // 后续复盘按新档位分析
        this.toast('复盘档位已切换为 ' + this.sysInfo.profile + '（写入配置）', 'success');
      } catch (e) {
        this.toast('切换复盘档位失败：' + ((e && e.message) || e), 'error');
      }
    },
    async clearCache(kind) {
      try {
        const r = await api.clearCache({ kind });
        const n = r && r.cleared ? ' ' + JSON.stringify(r.cleared) : '';
        this.toast('缓存已清除（' + kind + '）' + n, 'success');
        if (kind === 'review' || kind === 'all') {
          this.resetReview(); // 同谱下次载入将重新分析
        }
      } catch (e) {
        this.toast('清缓存失败：' + ((e && e.message) || e), 'error');
      }
    },

    // ---------------- 载入 SGF ----------------
    loadSample() {
      this.sgfText = MOCK_SAMPLE_SGF;
      this.loadSgf();
    },
    loadSgf(text) {
      if (this.busy) return; // 分析进行中：禁用载入
      if (this.trialMode) this.toggleTrial(); // 载入新棋谱前退出试摆
      const src = text || this.sgfText;
      this.stopPlay();
      this.loadError = '';
      let k;
      try {
        k = WGo.SGF.parse(src); // 坐标/尺寸解析全部交给 WGo
      } catch (e) {
        this.loadError = 'SGF 解析失败：' + (e && e.message ? e.message : e);
        return;
      }
      if (!k || !k.size) {
        this.loadError = 'SGF 缺少 SZ（棋盘大小）属性，无法载入';
        return;
      }
      kifu = k;
      this.kifuSize = kifu.size;
      this.loadedSgf = src;

      // 主变化节点序列（仅统计落子节点用于手数展示）
      mainNodes = [];
      let node = kifu.root;
      while (node && node.children && node.children.length) {
        node = node.children[0];
        if (node.move) mainNodes.push(node);
      }
      this.totalMoves = mainNodes.length;
      this.moveColors = mainNodes.map((n) => (n.move.c === WGo.B ? 'B' : 'W'));

      // 重建棋盘（尺寸从 SZ 读取，支持 9/13/19 等任意规格）
      const host = document.getElementById('board-host');
      host.innerHTML = '';
      board = new WGo.Board(host, {
        size: kifu.size,
        width: boardWidth(),
        background: '', // 棋盘底色由 CSS 提供（本地未 vendor wood1.jpg）
        font: 'Microsoft YaHei, sans-serif',
        theme: { coordinatesColor: '#2c2417' },
        section: { top: 0.25, right: 0.25, bottom: 0.25, left: 0.25 },
      });
      board.addCustomObject(BOARD_COORDS); // WGo 自带坐标标注

      // 播放器：rememberPath=true → 变化分支可定位；落子合法性由 WGo 判定
      reader = new WGo.KifuReader(kifu, true, false);
      board.update(reader.change);

      this.boardLabel = kifu.size + ' 路';
      this.$nextTick(() => this.syncBoardWidths());
      const pb = (kifu.info.black && kifu.info.black.name) || '';
      const pw = (kifu.info.white && kifu.info.white.name) || '';
      this.loadedInfo = `已载入：${pb || '黑'} vs ${pw || '白'} · ${this.totalMoves} 手`;
      this.syncUI();
      document.body.dataset.ready = '1';

      // M1：数据源分流——真实后端跑「提交→轮询→详情」；mock 保持 M0 行为
      if (this.source === 'real') {
        this.runReview(src);
      } else {
        this.resetReview();
        this.finalOwnership = (MOCK_REVIEW_DETAIL && MOCK_REVIEW_DETAIL.final_ownership) || [];
        this.$nextTick(() => {
          this.drawCurve();
          this.drawComplexityCurve();
          this.drawScoreCurve();
          if (this.showHeat) this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
        });
      }
    },

    // ---------------- 状态同步（手数 / 轮次 / 曲线） ----------------
    syncUI() {
      if (this.pvOpen) return; // 变化播放中，不干扰播放棋盘
      this.currentMove = currentMoveNumber();
      this.sliderValue = this.currentMove;
      this.turn = reader && reader.game && reader.game.turn === WGo.W ? 'W' : 'B';
      this.$nextTick(() => {
        this.drawCurve();
        this.drawComplexityCurve();
        this.drawScoreCurve();
        if (this.showHeat) this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
      });
      this.updateBoardMarkers(); // M1：当前手的一选/落点标记（WGo.Board.addObject）
    },

    // ---------------- 播放控制（全部由 KifuReader 驱动） ----------------
    goNext() {
      if (this.pvOpen || !reader) return;
      reader.next();
      if (!reader.change) {
        this.stopPlay();
        this.syncUI();
        return;
      }
      applyReaderChange();
      this.syncUI();
    },
    goPrev() {
      if (this.pvOpen || !reader) return;
      this.stopPlay();
      reader.previous();
      applyReaderChange();
      this.syncUI();
    },
    goFirst() {
      if (this.pvOpen || !reader) return;
      this.stopPlay();
      reader.first();
      applyReaderChange();
      this.syncUI();
    },
    goLast() {
      if (this.pvOpen || !reader) return;
      this.stopPlay();
      reader.last();
      applyReaderChange();
      this.syncUI();
    },
    goToMove(n) {
      if (this.pvOpen || !reader) return;
      this.stopPlay();
      const target = Math.max(0, Math.min(this.totalMoves, Math.round(Number(n))));
      if (target === 0) {
        this.goFirst();
        return;
      }
      reader.goTo({ m: target }); // 主变化第 target 手（WGo path 定位）
      applyReaderChange();
      this.syncUI();
    },
    onSlider() {
      this.goToMove(this.sliderValue);
    },
    togglePlay() {
      if (this.pvOpen) return;
      if (this.playing) this.stopPlay();
      else this.startPlay();
    },
    startPlay() {
      if (!reader || this.playing) return;
      this.playing = true;
      playTimer = setInterval(() => {
        if (!reader) {
          this.stopPlay();
          return;
        }
        reader.next();
        if (!reader.change) {
          this.stopPlay();
          return;
        }
        applyReaderChange();
        this.syncUI();
      }, 700);
    },
    stopPlay() {
      this.playing = false;
      if (playTimer) {
        clearInterval(playTimer);
        playTimer = null;
      }
    },

    // ---------------- 胜率曲线（自绘 canvas；曲线非棋盘，允许自绘） ----------------
    drawCurve() {
      const canvas = document.getElementById('curve-canvas');
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 600;
      const cssH = 170;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      // 真实后端：按分析状态显示占位
      if (this.source === 'real') {
        if (this.reviewStatus === 'pending' || this.reviewStatus === 'analyzing') {
          this.drawPlaceholder(ctx, cssW, cssH, 'KataGo 分析中…（' + this.progressPct + '）');
          return;
        }
        if (this.reviewStatus === 'failed') {
          this.drawPlaceholder(ctx, cssW, cssH, '复盘分析失败：' + this.reviewError);
          return;
        }
        if (!this.curveData.length) {
          this.drawPlaceholder(ctx, cssW, cssH, '载入棋谱后自动分析（真实后端）');
          return;
        }
      }

      // 数据点：后端为当前行棋方视角 → 归一为黑方视角（消除相邻手视角翻转锯齿）
      let points = [];
      if (this.source === 'real') {
        points = this.curveData
          .map((m) => ({ move: m.move, winrate: blackViewWinrate(m) }))
          .filter((p) => typeof p.winrate === 'number');
      } else if (this.isBuiltin()) {
        // mock 口径为当前方（黑白交替），同样归一
        points = MOCK_WINRATES.map((v, i) => ({
          move: i + 1,
          winrate: i % 2 === 0 ? v : 1 - v,
        }));
      }
      if (!points.length) {
        this.drawPlaceholder(ctx, cssW, cssH, '当前棋谱无 mock 胜率数据（仅内置 9 路示例提供）');
        return;
      }

      // 3 手滑动平均（绘图专用）：锯齿主要来自 KataGo 单机估值的噪声
      points = points.map((p, i) => ({
        move: p.move,
        winrate: smoothWinrate(points, i),
      }));

      const maxMove = Math.max(this.totalMoves || 0, points[points.length - 1].move);
      const padL = 30;
      const padR = 10;
      const padT = 14;
      const padB = 20;
      const px = (i) => padL + (i * (cssW - padL - padR)) / Math.max(1, maxMove - 1);
      const py = (v) => padT + (1 - v) * (cssH - padT - padB);
      const byMove = new Map(points.map((p) => [p.move, p.winrate]));

      // 50% 参考线与刻度
      ctx.strokeStyle = '#e2dccd';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, py(0.5));
      ctx.lineTo(cssW - padR, py(0.5));
      ctx.stroke();
      ctx.fillStyle = '#8a8375';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText('100%', padL - 4, py(1) + 3);
      ctx.fillText('50%', padL - 4, py(0.5) + 3);
      ctx.fillText('0%', padL - 4, py(0) + 3);

      // 折线
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = px(p.move - 1);
        const y = py(p.winrate);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = '#2b6cb0';
      ctx.lineWidth = 2;
      ctx.stroke();

      // 关键手圆点（红=坏手 / 黄=疑问 / 绿=好手）
      this.keyMoves.forEach((m) => {
        if (typeof m.winrate !== 'number') return;
        ctx.beginPath();
        ctx.arc(px(m.move - 1), py(blackViewWinrate(m)), 4.5, 0, Math.PI * 2);
        ctx.fillStyle = KEY_CAT_COLORS[m.category] || '#8a8375';
        ctx.fill();
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      });

      // 当前手：虚竖线 + 大圆点 + 手数标签（随播放联动）
      const cur = byMove.get(this.currentMove);
      if (typeof cur === 'number') {
        const x = px(this.currentMove - 1);
        const v = cur;
        ctx.beginPath();
        ctx.moveTo(x, padT);
        ctx.lineTo(x, cssH - padB);
        ctx.strokeStyle = 'rgba(43,108,176,0.35)';
        ctx.setLineDash([3, 3]);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(x, py(v), 6, 0, Math.PI * 2);
        ctx.fillStyle = '#2b6cb0';
        ctx.fill();
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.fillStyle = '#235a92';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(String(this.currentMove), x, py(v) - 10);
      }

      // 底部手数刻度（手数多时抽稀）
      ctx.fillStyle = '#8a8375';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      if (maxMove <= 40) {
        for (let i = 1; i <= maxMove; i++) ctx.fillText(String(i), px(i - 1), cssH - 6);
      } else {
        const step = Math.ceil(maxMove / 20);
        for (let i = 1; i <= maxMove; i += step) ctx.fillText(String(i), px(i - 1), cssH - 6);
      }
    },

    drawPlaceholder(ctx, w, h, text) {
      ctx.fillStyle = '#8a8375';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(text, w / 2, h / 2);
    },

    // ---------------- M1 真实复盘数据流（submit → poll → detail） ----------------
    resetReview() {
      reviewToken += 1;
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
      this.busy = false;
      this.reviewStatus = 'idle';
      this.progress = 0;
      this.reviewError = '';
      this.reviewId = '';
      this.reviewDetail = null;
      this.curveData = [];
      this.closeExplain();
      this.updateBoardMarkers();
      document.body.dataset.reviewStatus = 'idle';
    },
    async runReview(sgfText) {
      const token = ++reviewToken;
      this.busy = true;
      this.reviewStatus = 'pending';
      this.progress = 0;
      this.reviewError = '';
      this.reviewId = '';
      this.reviewDetail = null;
      this.curveData = [];
      this.closeExplain();
      this.updateBoardMarkers();
      document.body.dataset.reviewStatus = 'pending';
      try {
        const submitted = await api.analyze(sgfText, this.reviewProfile);
        if (token !== reviewToken) return;
        this.reviewId = submitted.review_id;
        this.reviewStatus = 'analyzing';
        document.body.dataset.reviewStatus = 'analyzing';
        await this.pollStatus(submitted.review_id, token);
        if (token !== reviewToken) return;
        const detail = await api.reviewDetail(submitted.review_id);
        if (token !== reviewToken) return;
        this.reviewDetail = detail;
        this.curveData = (detail && detail.winrate_curve) || [];
        this.finalOwnership = (detail && detail.final_ownership) || [];
        this.reviewStatus = 'done';
        this.progress = 1;
        document.body.dataset.reviewStatus = 'done';
        this.syncUI();
        this.$nextTick(() => {
          this.drawCurve();
          this.drawComplexityCurve();
          this.drawScoreCurve();
          this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
        });
        this.toast('复盘完成：' + this.curveData.length + ' 手真实数据', 'success');
      } catch (e) {
        if (token !== reviewToken) return;
        this.reviewStatus = 'failed';
        this.reviewError = (e && e.message) || String(e);
        document.body.dataset.reviewStatus = 'failed';
      } finally {
        if (token === reviewToken) {
          this.busy = false;
          this.drawCurve();
        }
      }
    },
    pollStatus(reviewId, token) {
      // 1.5s 轮询 GET /api/v1/review/{id}/status（WS 不稳定，按任务书用轮询）
      return new Promise((resolve, reject) => {
        let polls = 0;
        const tick = async () => {
          if (token !== reviewToken) {
            resolve();
            return;
          }
          let st = null;
          try {
            st = await api.reviewStatus(reviewId);
          } catch (e) {
            /* 单次失败忽略，下个周期重试 */
          }
          if (token !== reviewToken) {
            resolve();
            return;
          }
          if (st) {
            if (typeof st.progress === 'number' && st.progress > this.progress) {
              this.progress = st.progress;
            }
            if (st.status === 'done') {
              resolve();
              return;
            }
            if (st.status === 'failed') {
              reject(new Error(st.error || '复盘分析失败'));
              return;
            }
            this.reviewStatus = st.status;
          }
          polls += 1;
          if (polls > 2000) {
            reject(new Error('分析超时（超过 50 分钟），请稍后重试'));
            return;
          }
          pollTimer = setTimeout(tick, 1500);
        };
        tick();
      });
    },
    moveDataAt(n) {
      if (this.source === 'real') {
        return this.curveData.find((m) => m.move === n) || null;
      }
      return null;
    },
    // 当前手的一选/落点标记：全部走 WGo.Board.addObject/removeObject，
    // 坐标来自后端字段（GTP→逻辑坐标映射见 gtpToBoardCoord），无任何像素换算。
    updateBoardMarkers() {
      if (!board) return;
      if (this._boardMarkers && this._boardMarkers.length) {
        try {
          board.removeObject(this._boardMarkers);
        } catch (e) {
          /* 忽略 */
        }
        this._boardMarkers = [];
      }
      const markers = [];
      if (this.source === 'real') {
        const m = this.moveDataAt(this.currentMove);
        if (m && m.coord) {
          const p = gtpToBoardCoord(m.coord, board.size);
          if (p) {
            markers.push({ type: 'MA', x: p.x, y: p.y, c: m.color === 'W' ? '#4a3f2c' : '#f5f1e6' });
          }
          if (m.best_coord) {
            const b = gtpToBoardCoord(m.best_coord, board.size);
            if (b && !(p && b.x === p.x && b.y === p.y)) {
              markers.push({ type: 'CR', x: b.x, y: b.y, c: '#2f855a', lineWidth: 2 });
            }
          }
        }
      }
      if (markers.length) {
        try {
          board.addObject(markers);
        } catch (e) {
          /* 忽略 */
        }
      }
      this._boardMarkers = markers;
      document.body.dataset.markers = String(markers.length);
    },
    // ---------------- M1 讲解面板（explain 单次调用、失败即降级，不重试） ----------------
    onKeyMoveClick(m) {
      this.goToMove(m.move);
      this.openExplainForMove(m.move);
    },
    openExplainForMove(n) {
      this.explainMoveNumber = n;
      this.explainOpen = true;
      this.explainLoading = false;
      this.explainData = null;
      this.explainFallback = '';
    },
    closeExplain() {
      this.explainOpen = false;
      this.explainMoveNumber = 0;
    },

    // ---------------- M3：变化播放（PV 步进） ----------------
    // 播放引擎变化：把当前棋盘状态导出为摆子，接 PV 序列构造临时棋谱，
    // 由 WGo KifuReader 步进（next/previous）驱动棋子增删——不自写变化逻辑。
    pvTargetBoard() {
      return pvTarget === 'practice' ? practiceBoard : board;
    },
    playExplainPv() {
      const m = this.explainMove;
      if (!m || !m.pv || !m.pv.length) return;
      this.pvStop(true);
      pvTarget = 'review';
      // 先保存当前路径（退出播放后恢复到此手），再退回一手导出该手之前的局面
      pvSavedPath = reader ? JSON.parse(JSON.stringify(reader.path)) : null;
      if (reader && this.explainMoveNumber > 1) {
        const path = { m: this.explainMoveNumber - 1 };
        reader.goTo(path);
        board.update(reader.change);
      }
      const firstColor = m.color === 'B' ? 'B' : 'W'; // PV 首手 = 该手行棋方
      this.pvStartCommon(board, m.pv, firstColor);
    },
    async playPenalty() {
      const m = this.explainMove;
      if (!m || !this.reviewId) return;
      this.penaltyLoading = true;
      try {
        const r = await api.penalty(this.reviewId, this.explainMoveNumber);
        const pv = r && r.content && r.content.penalty_pv;
        if (!pv || !pv.length) {
          this.toast('该手没有可演示的惩罚变化', 'error');
          return;
        }
        this.pvStop(true);
        pvTarget = 'review';
        pvSavedPath = reader ? JSON.parse(JSON.stringify(reader.path)) : null;
        // 确保棋盘定位在坏手落下后（惩罚变化的起点）
        if (reader && this.explainMoveNumber > 0) {
          const path = { m: this.explainMoveNumber };
          reader.goTo(path);
          board.update(reader.change);
        }
        const punisher = r.content.punisher === 'W' ? 'W' : 'B';
        this.pvStartCommon(board, pv, punisher);
        this.penaltyNotes = (r.content.steps || []).slice();
        this.penaltySummary = r.content.summary || '';
        this.refreshPenaltyNote();
        if (r.content.punisher_winrate != null) {
          this.toast(
            '惩罚方胜率 ' + (r.content.punisher_winrate * 100).toFixed(1) + '%',
            'success'
          );
        }
      } catch (e) {
        this.toast('惩罚变化分析失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.penaltyLoading = false;
      }
    },
    playPracticeVariation() {
      const v = this.attemptResult && this.attemptResult.variation;
      if (!v || !v.length || !practiceBoard) return;
      this.pvStop(true);
      // 播放正解变化：棋盘重置为题面，从变化第一步开始摆
      practiceBoard.removeAllObjects();
      if (practiceReader) practiceBoard.update(practiceReader.change);
      pvTarget = 'practice';
      const firstColor = this.practiceSolver;
      this.pvStartCommon(practiceBoard, v, firstColor);
    },
    pvStartCommon(targetBoard, pvGtpList, firstColor) {
      let built;
      try {
        built = buildPvSgf(targetBoard, pvGtpList, firstColor);
      } catch (e) {
        this.toast('变化播放失败：' + ((e && e.message) || e), 'error');
        this.pvStop(true);
        return;
      }
      if (!built.steps) {
        this.toast('变化为空，无法播放', 'error');
        return;
      }
      let k;
      try {
        k = WGo.SGF.parse(built.sgf);
      } catch (e) {
        this.toast('变化播放失败（棋谱构造）：' + ((e && e.message) || e), 'error');
        this.pvStop(true);
        return;
      }
      pvReader = new WGo.KifuReader(k, false, false);
      this.pvOpen = true;
      this.pvIndex = 0;
      this.pvTotal = built.steps;
      this.pvPlaying = false;
      targetBoard.removeAllObjects();
      targetBoard.update(pvReader.change); // 初始局面（与播放前一致）
      document.body.dataset.pvOpen = '1';
    },
    refreshPenaltyNote() {
      // 惩罚变化播放：显示当前步的解说（先手/好手/俗手/应对）
      if (!this.pvOpen || !this.penaltyNotes.length) {
        this.penaltyNoteText = '';
        return;
      }
      const n = this.penaltyNotes.find((x) => x.step === this.pvIndex);
      this.penaltyNoteText = n
        ? '【' + n.role + '】' + n.text
        : '';
    },
    pvStepNext() {
      if (!pvReader || !this.pvOpen) return;
      if (this.pvIndex >= this.pvTotal) return;
      try {
        if (!pvReader.next()) return;
        this.pvTargetBoard().update(pvReader.change);
        this.pvIndex += 1;
        this.refreshPenaltyNote();
      } catch (e) {
        this.pvStop(true);
        this.toast('变化播放中断：' + ((e && e.message) || '非法落子'), 'error');
      }
    },
    pvStepPrev() {
      if (!pvReader || !this.pvOpen || this.pvIndex <= 0) return;
      try {
        if (!pvReader.previous()) return;
        this.pvTargetBoard().update(pvReader.change);
        this.pvIndex -= 1;
        this.refreshPenaltyNote();
      } catch (e) {
        this.pvStop(true);
        this.toast('变化播放中断：' + ((e && e.message) || '非法落子'), 'error');
      }
    },
    pvToStart() {
      while (this.pvIndex > 0) this.pvStepPrev();
    },
    pvToEnd() {
      while (this.pvIndex < this.pvTotal) this.pvStepNext();
    },
    pvToggleAuto() {
      if (!this.pvOpen) return;
      if (pvTimer) {
        clearInterval(pvTimer);
        pvTimer = null;
        this.pvPlaying = false;
        return;
      }
      if (this.pvIndex >= this.pvTotal) this.pvToStart();
      this.pvPlaying = true;
      pvTimer = setInterval(() => {
        if (this.pvIndex >= this.pvTotal) {
          clearInterval(pvTimer);
          pvTimer = null;
          this.pvPlaying = false;
          return;
        }
        this.pvStepNext();
      }, 700);
    },
    pvStop(silent) {
      if (pvTimer) {
        clearInterval(pvTimer);
        pvTimer = null;
      }
      const wasOpen = this.pvOpen || pvReader;
      pvReader = null;
      this.pvOpen = false;
      this.pvPlaying = false;
      this.penaltyNotes = [];
      this.penaltySummary = '';
      this.penaltyNoteText = '';
      this.pvIndex = 0;
      this.pvTotal = 0;
      if (!wasOpen) return;
      const b = this.pvTargetBoard();
      if (pvTarget === 'review') {
        if (kifu && pvSavedPath) restoreReviewBoardTo(pvSavedPath);
        this.syncUI();
      } else {
        b.removeAllObjects();
        if (practiceReader) b.update(practiceReader.change); // 题面
        if (practiceTrial.length) b.addObject(practiceTrial); // 试下摆子
      }
      document.body.dataset.pvOpen = '0';
      if (!silent) this.toast('已退出变化播放', 'success');
    },
    // ================= 同步讲棋：讲解分段与棋盘变化联动 =================
    sanitizeVarForPlay(b, variation) {
      // 用 WGo.Game 当裁判过滤非法落子（LLM 偶发拼出非法序列，如提劫点重复）
      const g = gameFromBoard(b, 'B');
      const kept = [];
      for (const step of variation || []) {
        if (!Array.isArray(step) || step.length !== 2) continue;
        const c = step[0], cd = step[1];
        if ((c !== 'B' && c !== 'W') || !cd || cd === 'pass') continue;
        const xy = gtpToBoardCoord(cd, b.size);
        if (!xy) continue;
        const res = g.play(xy.x, xy.y, c === 'B' ? WGo.B : WGo.W);
        if (typeof res === 'number') continue; // 非法落子跳过
        kept.push([c, cd.toUpperCase()]);
      }
      return kept;
    },
    syncBoard() {
      return this.syncTarget === 'review' ? board : practiceBoard;
    },
    syncRestoreBase() {
      // 每段播放前恢复基础局面（复盘=第 N 手后；练习=题面+试下子）
      if (this.syncTarget === 'review') {
        // 同步讲棋变化基于「本手落下前」局面（首手=本手行棋方）
        const base = Math.max(0, this.explainMoveNumber - 1);
        if (kifu) {
          restoreReviewBoardTo({ m: base });
        }
      } else if (practiceBoard) {
        practiceBoard.removeAllObjects();
        if (practiceReader) practiceBoard.update(practiceReader.change);
        if (practiceTrial.length) practiceBoard.addObject(practiceTrial);
      }
    },
    syncClearTimer() {
      if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
      if (syncTimeout) { clearTimeout(syncTimeout); syncTimeout = null; }
    },
    syncBegin(target, segments) {
      const segs = (segments || []).filter(
        (s) => s && (s.text || (Array.isArray(s.variation) && s.variation.length))
      );
      if (!segs.length) { this.toast('没有可播放的讲解分段', 'error'); return; }
      this.syncClearTimer();
      this.pvStop(true);
      this.syncSegs = segs;
      this.syncTarget = target;
      if (target === 'review') {
        syncSavedPath = reader ? JSON.parse(JSON.stringify(reader.path)) : null;
      }
      this.syncOpen = true;
      this.syncIdx = -1;
      this.syncNext();
    },
    syncNext() {
      if (!this.syncOpen) return;
      const next = this.syncIdx + 1;
      if (next >= this.syncSegs.length) { this.syncStop(true); return; }
      this.syncPlaySeg(next, true);
    },
    syncPlaySeg(i, auto) {
      this.syncClearTimer();
      if (i < 0 || i >= this.syncSegs.length) return;
      this.syncRestoreBase();
      this.syncIdx = i;
      this.syncStep = 0;
      this.syncTotal = 0;
      const b = this.syncBoard();
      const seg = this.syncSegs[i];
      if (!seg) return;
      const varSeq = this.sanitizeVarForPlay(b, seg.variation);
      if (!varSeq.length) {
        // 无变化段：自动节奏下停 1.8s 后进下一段；手动点击则仅高亮本段
        if (auto) syncTimeout = setTimeout(() => this.syncNext(), 1800);
        return;
      }
      let built;
      try {
        built = buildPvSgf(b, varSeq.map((x) => x[1]), varSeq[0][0]);
      } catch (e) {
        if (auto) syncTimeout = setTimeout(() => this.syncNext(), 400);
        return;
      }
      if (!built.steps) {
        if (auto) syncTimeout = setTimeout(() => this.syncNext(), 400);
        return;
      }
      let k;
      try {
        k = WGo.SGF.parse(built.sgf);
      } catch (e) {
        if (auto) syncTimeout = setTimeout(() => this.syncNext(), 400);
        return;
      }
      syncReader = new WGo.KifuReader(k, false, false);
      b.removeAllObjects();
      b.update(syncReader.change);
      this.syncStep = 0;
      this.syncTotal = built.steps;
      this.syncPlaying = true;
      syncTimer = setInterval(() => {
        if (!this.syncOpen || !syncReader) return;
        if (this.syncStep >= this.syncTotal) {
          this.syncClearTimer();
          this.syncPlaying = false;
          if (auto) syncTimeout = setTimeout(() => this.syncNext(), 1000);
          return;
        }
        let ok = false;
        try { ok = !!syncReader.next(); } catch (e) { ok = false; }
        if (!ok) {
          this.syncClearTimer();
          this.syncPlaying = false;
          if (auto) syncTimeout = setTimeout(() => this.syncNext(), 1000);
          return;
        }
        b.update(syncReader.change);
        this.syncStep += 1;
      }, 900);
    },
    syncToggle() {
      if (!this.syncOpen) return;
      if (syncTimer) {
        this.syncClearTimer();
        this.syncPlaying = false;
        return;
      }
      if (this.syncIdx < 0 || this.syncIdx >= this.syncSegs.length) return;
      this.syncPlaySeg(this.syncIdx, false);
    },
    syncJump(i) {
      if (!this.syncOpen || i < 0 || i >= this.syncSegs.length) return;
      this.syncPlaySeg(i, false);
    },
    syncStop(silent) {
      this.syncClearTimer();
      const wasOpen = this.syncOpen;
      this.syncOpen = false;
      this.syncPlaying = false;
      syncReader = null;
      this.syncIdx = -1;
      this.syncStep = 0;
      this.syncTotal = 0;
      if (!wasOpen) return;
      if (this.syncTarget === 'review') {
        if (kifu && syncSavedPath) restoreReviewBoardTo(syncSavedPath);
        this.syncUI();
      } else if (practiceBoard) {
        practiceBoard.removeAllObjects();
        if (practiceReader) practiceBoard.update(practiceReader.change);
        if (practiceTrial.length) practiceBoard.addObject(practiceTrial);
      }
      if (!silent) this.toast('同步讲棋结束', 'success');
    },
    async loadPracticeExplain() {
      const pr = this.practiceProblem;
      if (!pr || this.practiceExplainLoading) return;
      this.practiceExplainLoading = true;
      this.practiceExplainError = '';
      this.practiceExplain = null;
      try {
        const r = await api.problemExplain(pr.id);
        this.practiceExplain = r;
        const segs = r && r.content && r.content.segments;
        if (segs && segs.length) {
          this.$nextTick(() => this.syncBegin('practice', segs));
        }
      } catch (e) {
        this.practiceExplainError = '深度讲解暂不可用：' + ((e && e.message) || e);
      } finally {
        this.practiceExplainLoading = false;
      }
    },
    async extractProblemToLibrary() {
      if (!this.reviewId || !this.explainMoveNumber || this.extracting) return;
      this.extracting = true;
      try {
        const r = await api.extractProblem(this.reviewId, this.explainMoveNumber);
        const id = (r.problem_id || '').slice(0, 8);
        this.toast(
          r.created ? ('已收录本题：' + id + '（' + r.theme + '）')
                    : ('本题已在题库中：' + id),
          'success'
        );
      } catch (e) {
        this.toast('收录失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.extracting = false;
      }
    },
    // ================= 成长视图：棋手档案 / 棋谱库 / 水平画像 =================
    async loadProfiles() {
      this.progressLoading = true;
      try {
        const r = await api.listProfiles();
        this.progressProfiles = r.profiles || [];
        if (this.progressProfileId) {
          if (!this.progressProfiles.some((x) => x.id === this.progressProfileId)) {
            this.progressProfileId = '';
          }
        }
        if (!this.progressProfileId && this.progressProfiles.length) {
          this.progressProfileId = this.progressProfiles[0].id;
        }
        if (this.progressProfileId) {
          await this.selectProgressProfile(this.progressProfileId, true);
        }
      } catch (e) {
        this.toast('档案加载失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.progressLoading = false;
      }
    },
    async selectProgressProfile(id, silent) {
      this.progressProfileId = id;
      this.progressInsight = null;
      this.progressAdvice = null;
      try {
        this.progressDetail = await api.profileDetail(id);
        const ins = this.progressDetail && this.progressDetail.insight;
        if (ins && ins.features) {
          this.progressInsight = {
            insight: {
              features: ins.features,
              rank_estimate: ins.rank_estimate || '',
              games_count: ins.games_count || 0,
              updated_at: ins.updated_at || '',
              advice: ins.advice || null,
            },
          };
          if (ins.advice) this.progressAdvice = ins.advice;
          this.$nextTick(() => this.drawPhaseChart());
        }
      } catch (e) {
        if (!silent) this.toast('档案详情加载失败：' + ((e && e.message) || e), 'error');
      }
    },
    async createProfile() {
      const name = (this.progressNewName || '').trim();
      if (!name) { this.toast('请填写档案名（如「小明」）', 'error'); return; }
      try {
        const r = await api.createProfile(name, '');
        this.progressNewName = '';
        this.toast('档案已创建：' + name, 'success');
        this.progressProfileId = (r.profile && r.profile.id) || '';
        await this.loadProfiles();
      } catch (e) {
        this.toast('创建失败：' + ((e && e.message) || e), 'error');
      }
    },
    async deleteCurrentProfile() {
      const id = this.progressProfileId;
      if (!id) return;
      const p = this.progressProfiles.find((x) => x.id === id);
      if (!window.confirm('删除档案「' + (p ? p.name : id) + '」？棋谱不会删除，只解除关联。')) return;
      try {
        await api.deleteProfile(id);
        this.progressProfileId = '';
        this.progressDetail = null;
        this.progressInsight = null;
        this.progressAdvice = null;
        this.toast('档案已删除', 'success');
        await this.loadProfiles();
      } catch (e) {
        this.toast('删除失败：' + ((e && e.message) || e), 'error');
      }
    },
    async refreshInsight() {
      const id = this.progressProfileId;
      if (!id || this.progressInsightLoading) return;
      this.progressInsightLoading = true;
      try {
        this.progressInsight = await api.profileInsight(id);
        this.$nextTick(() => this.drawPhaseChart());
        this.toast('画像已刷新', 'success');
      } catch (e) {
        this.toast('画像生成失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.progressInsightLoading = false;
      }
    },
    async generateAdvice() {
      const id = this.progressProfileId;
      if (!id || this.progressAdviceLoading) return;
      this.progressAdviceLoading = true;
      try {
        const r = await api.profileAdvice(id);
        this.progressInsight = r;
        this.progressAdvice = (r.insight && r.insight.advice) || null;
        this.$nextTick(() => this.drawPhaseChart());
        this.toast('提高建议已生成', 'success');
      } catch (e) {
        this.toast('建议生成失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.progressAdviceLoading = false;
      }
    },
    async importSgfToProfile() {
      const id = this.progressProfileId;
      const sgf = (this.progressImportText || '').trim();
      if (!id) { this.toast('请先选择档案', 'error'); return; }
      if (!sgf) { this.toast('请粘贴 SGF 棋谱', 'error'); return; }
      this.progressImporting = true;
      try {
        const r = await api.importSgf(id, sgf, 'fast');
        this.toast('棋谱已导入，正在后台分析…', 'success');
        this.progressImportText = '';
        this.pollImport(r.review_id, 0);
      } catch (e) {
        this.toast('导入失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.progressImporting = false;
      }
    },
    async pollImport(reviewId, n) {
      if (n > 90) { this.toast('分析超时，稍后刷新查看', 'error'); return; }
      try {
        const st = await api.reviewStatus(reviewId);
        if (st.status === 'done') {
          this.toast('棋谱分析完成', 'success');
          await this.selectProgressProfile(this.progressProfileId);
          await this.refreshInsight();
          return;
        }
        if (st.status === 'failed') {
          this.toast('棋谱分析失败：' + (st.error || '未知错误'), 'error');
          return;
        }
      } catch (e) { /* 忽略轮询错误，继续 */ }
      setTimeout(() => this.pollImport(reviewId, n + 1), 2500);
    },
    async attachCurrentReview() {
      const pid = this.attachProfileId || this.progressProfileId;
      if (!pid) { this.toast('请先选择档案', 'error'); return; }
      if (!this.reviewId) { this.toast('当前没有已分析的棋谱', 'error'); return; }
      try {
        await api.attachReview(pid, this.reviewId);
        this.toast('已收藏到档案', 'success');
        this.loadProfiles();
      } catch (e) {
        this.toast('收藏失败：' + ((e && e.message) || e), 'error');
      }
    },
    async openGameReview(reviewId) {
      if (!reviewId) return;
      this.switchView('review');
      this.$nextTick(async () => {
        try {
          const detail = await api.reviewDetail(reviewId, true);
          if (detail && detail.id) {
            this.reviewId = detail.id;
            this.reviewDetail = detail;
            this.curveData = detail.winrate_curve || [];
            this.totalMoves = this.curveData.length;
            if (detail.sgf_text) this.loadSgf(detail.sgf_text);
            this.toast('已载入该局复盘', 'success');
          }
        } catch (e) {
          this.toast('复盘载入失败：' + ((e && e.message) || e), 'error');
        }
      });
    },
    goPracticeFromAdvice(theme) {
      const map = {
        做活: 'life_death', 杀棋: 'life_death', 对杀: 'capturing_race',
        吃棋筋: 'middle', 逃棋筋: 'middle', 中盘要点: 'middle',
        收官最大: 'endgame',
      };
      this.practiceTheme = map[theme] || '';
      this.practiceList = [];
      this.switchView('practice');
      this.loadLibrary();
    },
    insightTrendCn() {
      const f = this.progressInsight && this.progressInsight.insight
        && this.progressInsight.insight.features;
      return { improving: '📈 进步中', declining: '📉 有所退步', flat: '➡ 平稳' }[f && f.trend] || '';
    },
    drawPhaseChart() {
      const f = this.progressInsight && this.progressInsight.insight
        && this.progressInsight.insight.features;
      const canvas = document.getElementById('progress-phase-canvas');
      if (!canvas || !f || !f.phases) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 420;
      const cssH = 150;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);
      const labels = [['layout', '布局'], ['middle', '中盘'], ['endgame', '官子']];
      const vals = labels.map((x) => (f.phases[x[0]] ? f.phases[x[0]].avg_loss : 0));
      const maxV = Math.max.apply(null, vals.concat([0.02]));
      const gap = (cssW - 70) / 3;
      const bw = Math.min(70, gap * 0.62);
      const baseY = cssH - 26;
      const colors = ['#d69e2e', '#2b6cb0', '#2f855a'];
      ctx.font = '12px sans-serif';
      labels.forEach((x, i) => {
        const h = Math.max(2, (vals[i] / maxV) * (cssH - 56));
        const bx = 40 + gap * i + (gap - bw) / 2;
        const by = baseY - h;
        ctx.fillStyle = colors[i];
        ctx.fillRect(bx, by, bw, h);
        ctx.fillStyle = '#6b5330';
        ctx.textAlign = 'center';
        ctx.fillText(vals[i].toFixed(3), bx + bw / 2, by - 6);
        ctx.fillText(x[1], bx + bw / 2, cssH - 8);
      });
      ctx.fillStyle = '#9a927f';
      ctx.textAlign = 'left';
      ctx.fillText('各阶段平均每手损失（越低越好）', 6, 14);
    },
    async requestExplain() {
      const n = this.explainMoveNumber;
      if (!n || this.explainLoading) return;
      this.explainLoading = true;
      this.explainData = null;
      this.explainFallback = '';
      if (this.source === 'mock') {
        // mock：本地 mock 讲解文本（M0 兼容）
        try {
          this.explainData = await api.explain('mock-review-9x9', n);
        } catch (e) {
          this.explainFallback = '讲解生成失败：' + ((e && e.message) || e);
        } finally {
          this.explainLoading = false;
        }
        return;
      }
      if (!this.reviewId) {
        this.explainFallback = '讲解暂不可用（M2 接入）：复盘数据未就绪';
        this.explainLoading = false;
        return;
      }
      try {
        // 预算纪律：单次调用，失败即降级，绝不重试
        this.explainData = await api.explain(this.reviewId, n);
      } catch (e) {
        this.explainFallback =
          '讲解暂不可用（M2 接入）：' + ((e && e.message) || '讲解服务未就绪');
      } finally {
        this.explainLoading = false;
      }
    },

    // ---------------- 对弈视图（新手板块：与 AI 对弈 + 下错实时提示） ----------------
    playBoardWidth() {
      const host = document.getElementById('play-board-host');
      const w = host && host.clientWidth ? host.clientWidth : 0;
      return w || Math.min(520, window.innerWidth - 80);
    },
    playInitBoard(size) {
      const host = document.getElementById('play-board-host');
      if (!host) return;
      host.innerHTML = '';
      playBoard = new WGo.Board(host, {
        size,
        width: this.playBoardWidth(),
        background: '',
        font: 'Microsoft YaHei, sans-serif',
        theme: { coordinatesColor: '#2c2417' },
        section: { top: 0.25, right: 0.25, bottom: 0.25, left: 0.25 },
      });
      playBoard.addCustomObject(BOARD_COORDS);
      this.$nextTick(() => this.syncBoardWidths());
      playBoard.addEventListener('click', (x, y) => this.onPlayBoardClick(x, y));
    },
    playNewGame() {
      const size = [9, 13, 19].includes(this.playBoardSize) ? this.playBoardSize : 9;
      this.playInitBoard(size);
      this.playSuggestion = [];
      playMoves = [];
      playGame = new WGo.Game(size);
      playBusy = false;
      this.playBoardSize = size;
      this.playBoardLabel = size + ' 路';
      this.playTurn = 'B';
      this.playMoveCount = 0;
      this.playAiThinking = false;
      this.playEnded = false;
      this.playUserWinrateText = '';
      this.playStatusText = '黑先：点棋盘落子（AI 执白）';
      this.playHint = null;
      this.playTipText = '';
      this.playTipLoading = false;
    },
    onPlayBoardClick(x, y) {
      if (playBusy || this.playAiThinking || this.playEnded || !playGame) return;
      if (this.playTurn !== 'B') return;
      const r = playGame.play(x, y, WGo.B);
      if (typeof r === 'number') {
        this.toast('这里不能下（' + ['出界', '已有棋子', '自杀'][r - 1] + '）', 'error');
        return;
      }
      for (const cap of r) playBoard.removeObject(cap);
      playBoard.addObject({ x, y, c: WGo.B });
      const coord = boardCoordToGtp(x, y, this.playBoardSize);
      playMoves.push(['B', coord]);
      this.playMoveCount += 1;
      this.playTurn = 'W';
      this.playAfterUserMove(coord);
    },
    playClearSuggestion() {
      if (playBoard && this.playSuggestion && this.playSuggestion.length) {
        for (const m of this.playSuggestion) {
          try { playBoard.removeObject(m.obj); } catch (e) { /* 标记已不在 */ }
        }
      }
      this.playSuggestion = [];
    },
    async playAfterUserMove(userCoord) {
      playBusy = true;
      this.playClearSuggestion();
      this.playAiThinking = true;
      this.playStatusText = 'AI 思考中…';
      try {
        const resp = await api.playMove(
          this.playBoardSize, playMoves.slice(), 6.5,
          this.playAiRank === '' ? null : this.playAiRank
        );
        if (!resp || !resp.ai_move || String(resp.ai_move).toLowerCase() === 'pass') {
          this.playEnded = true;
          this.playStatusText = 'AI 停一手，对局结束（点「新对局」再来一盘）';
          return;
        }
        const pt = gtpToBoardCoord(resp.ai_move, this.playBoardSize);
        if (!pt) {
          this.playEnded = true;
          this.playStatusText = '对局异常，请开新局';
          return;
        }
        const r = playGame.play(pt.x, pt.y, WGo.W);
        if (typeof r === 'number') {
          this.playEnded = true;
          this.playStatusText = '对局异常，请开新局';
          return;
        }
        for (const cap of r) playBoard.removeObject(cap);
        playBoard.addObject({ x: pt.x, y: pt.y, c: WGo.W });
        playMoves.push(['W', resp.ai_move]);
        this.playMoveCount += 1;
        this.playTurn = 'B';
        if (resp.user_winrate != null) {
          this.playUserWinrateText = '你的胜率：' + (resp.user_winrate * 100).toFixed(1) + '%';
        }
        this.playStatusText = '轮到黑棋（你）';
        if (resp.hint && (resp.level === 'bad' || resp.level === 'question')) {
          this.playHint = {
            move: resp.move_number - 1,
            color: 'B',
            coord: userCoord,
            level: resp.level,
            title: resp.hint.title,
            text: resp.hint.text,
            best: resp.best,
            delta: resp.delta,
          };
          // 棋盘直接标出：绿圈=AI 推荐的好手位置，红圈=你这手（坏手对照）
          const bestPt = gtpToBoardCoord(resp.best, this.playBoardSize);
          if (bestPt) {
            const g = { type: 'CR', x: bestPt.x, y: bestPt.y, c: '#2f855a', lineWidth: 3 };
            playBoard.addObject(g);
            this.playSuggestion.push({ obj: g, x: bestPt.x, y: bestPt.y });
          }
          if (resp.level === 'bad') {
            const up = gtpToBoardCoord(userCoord, this.playBoardSize);
            if (up) {
              const rd = { type: 'CR', x: up.x, y: up.y, c: '#c53030', lineWidth: 3 };
              playBoard.addObject(rd);
              this.playSuggestion.push({ obj: rd, x: up.x, y: up.y });
            }
          }
          this.playTipText = '';
          this.playBeep();
          this.toast(
            '第 ' + this.playHint.move + ' 手' +
            (resp.level === 'bad' ? '是坏手' : '是疑问手') + '，看右侧提示',
            resp.level === 'bad' ? 'error' : 'warning'
          );
        }
      } catch (e) {
        this.toast('对弈分析失败：' + ((e && e.message) || e), 'error');
      } finally {
        playBusy = false;
        this.playAiThinking = false;
      }
    },
    playRebuildBoard() {
      if (!playBoard) return;
      playBoard.removeAllObjects();
      this.playSuggestion = [];
      playGame = new WGo.Game(playBoard.size);
      for (const item of playMoves) {
        const c = item[0];
        const m = item[1];
        const pt = gtpToBoardCoord(m, this.playBoardSize);
        if (!pt) continue;
        const r = playGame.play(pt.x, pt.y, c === 'B' ? WGo.B : WGo.W);
        if (typeof r === 'number') break;
        for (const cap of r) playBoard.removeObject(cap);
        playBoard.addObject({ x: pt.x, y: pt.y, c: c === 'B' ? WGo.B : WGo.W });
      }
    },
    playUndo() {
      if (playBusy || this.playAiThinking || !playGame || this.playMoveCount < 1) return;
      this.playHint = null;
      this.playClearSuggestion();
      this.playTipText = '';
      let undo = 0;
      if (playMoves.length && playMoves[playMoves.length - 1][0] === 'W') undo = 2;
      else undo = 1;
      for (let i = 0; i < undo && playMoves.length; i += 1) playMoves.pop();
      this.playRebuildBoard();
      this.playMoveCount = playMoves.length;
      this.playTurn = playMoves.length % 2 === 0 ? 'B' : 'W';
      this.playEnded = false;
      this.playStatusText = '轮到黑棋（你）';
      this.toast('已退回 ' + undo + ' 手，重新下这一手吧');
    },
    async playTipExplain() {
      const h = this.playHint;
      if (!h || this.playTipLoading) return;
      this.playTipLoading = true;
      try {
        const r = await api.playTip(
          this.playBoardSize, playMoves.slice(), h.coord, h.best, h.delta, h.level);
        const c = (r && r.content) || {};
        this.playTipText =
          (c.problem ? '问题：' + c.problem + '\n' : '') +
          (c.reason ? '原因：' + c.reason + '\n' : '') +
          (c.recommendation ? '常规：' + c.recommendation + '\n' : '') +
          (c.proverb ? '口诀：' + c.proverb : '');
      } catch (e) {
        this.toast('讲解失败：' + ((e && e.message) || e), 'error');
      } finally {
        this.playTipLoading = false;
      }
    },
    playBeep() {
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        const ctx = new Ctx();
        for (let i = 0; i < 2; i += 1) {
          const o = ctx.createOscillator();
          const g = ctx.createGain();
          o.connect(g);
          g.connect(ctx.destination);
          o.frequency.value = 880;
          g.gain.value = 0.08;
          const t0 = ctx.currentTime + i * 0.18;
          o.start(t0);
          o.stop(t0 + 0.14);
        }
      } catch (e) { /* 无声环境忽略 */ }
    },
    onResize() {
      this.syncBoardWidths();
      this.drawCurve();
      this.drawNoteCurve();
      this.drawComplexityCurve();
      this.drawScoreCurve();
      if (this.showHeat) this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
      this.drawHeat('note-heat-canvas', 'note-board-host', this.noteOwnership);
    },
    // 棋盘宽度与容器同步：canvas 溢出会遮挡边缘坐标（用户反馈「边框被遮挡」根因）
    syncBoardWidths() {
      if (board) board.setWidth(boardWidth());
      if (practiceBoard) practiceBoard.setWidth(practiceBoardWidth());
      if (noteBoard) noteBoard.setWidth(noteBoardWidth());
      if (playBoard) playBoard.setWidth(this.playBoardWidth());
      if (this.showHeat) this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
      this.drawHeat('note-heat-canvas', 'note-board-host', this.noteOwnership);
    },

    // ---------------- 记谱视图（对弈记录 + 胜率 + 试下研究；复盘功能不动） ----------------
    noteInitBoard(size) {
      const host = document.getElementById('note-board-host');
      if (!host) return;
      host.innerHTML = '';
      noteBoard = new WGo.Board(host, {
        size,
        width: noteBoardWidth(),
        background: '',
        font: 'Microsoft YaHei, sans-serif',
        theme: { coordinatesColor: '#2c2417' },
        section: { top: 0.25, right: 0.25, bottom: 0.25, left: 0.25 },
      });
      noteBoard.addCustomObject(BOARD_COORDS);
      this.$nextTick(() => this.syncBoardWidths());
      noteBoard.addEventListener('click', (x, y) => this.onNoteBoardClick(x, y));
    },
    noteNewGame() {
      if (this.noteTrialMode) this.noteToggleTrial();
      const size = [9, 13, 19].includes(this.noteBoardSize) ? this.noteBoardSize : 19;
      this.noteInitBoard(size);
      noteSetup = [];
      noteMoves = [];
      noteTrialStones = [];
      noteCurve = [];
      noteBaseTurn = 'B';
      this.noteBoardLabel = size + ' 路';
      this.noteCandidates = [];
      this.noteSgfInput = '';
      this.noteReplayTo(0);
      this.noteWinrateText = '';
      this.noteCurveLabel = '';
      this.$nextTick(() => this.drawNoteCurve());
      this.toast(`新对局（${size} 路）：黑先，点击棋盘落子`);
    },
    // 全量重放主线到指定手数：清盘 → 摆 setup → 逐手 play（提子效果同步重现）
    noteReplayTo(pos) {
      if (!noteBoard) return;
      noteBoard.removeAllObjects();
      for (const s of noteSetup) noteBoard.addObject({ x: s.x, y: s.y, c: s.c });
      noteGame = new WGo.Game(noteBoard.size);
      for (const s of noteSetup) noteGame.addStone(s.x, s.y, s.c);
      noteGame.turn = noteBaseTurn === 'W' ? WGo.W : WGo.B;
      const target = Math.max(0, Math.min(pos, noteMoves.length));
      for (let i = 0; i < target; i++) {
        const m = noteMoves[i];
        const res = noteGame.play(m.x, m.y, m.c);
        if (typeof res === 'number') break;
        for (const cap of res) noteBoard.removeObject(cap);
        noteBoard.addObject({ x: m.x, y: m.y, c: m.c });
      }
      this.noteViewPos = target;
      this.noteCount = noteMoves.length;
      const base = noteBaseTurn === 'W' ? 'W' : 'B';
      this.noteTurn = target % 2 === 1 ? (base === 'B' ? 'W' : 'B') : base;
      document.body.dataset.noteCount = String(this.noteCount);
      document.body.dataset.noteViewPos = String(this.noteViewPos);
    },
    noteGoTo(pos) {
      if (this.noteTrialMode || !noteBoard) return;
      const p = Math.max(0, Math.min(pos, noteMoves.length));
      this.noteReplayTo(p);
      this.updateNoteWinrate();
    },
    onNoteBoardClick(x, y) {
      if (!noteBoard || x < 0 || y < 0) return;
      const st = noteBoard.getState();
      const objs = (st.objects[x] && st.objects[x][y]) || [];
      if (objs.some((o) => o.c === WGo.B || o.c === WGo.W)) {
        return; // 已有棋子
      }
      if (this.noteTrialMode) {
        // 试下研究：自由摆子（提子/自杀判定走 WGo.Game）
        if (!noteTrialGame) return;
        // 研究分支首手颜色 = 主线下一手行棋方
        const trialColor = this.noteTurn === 'W' ? WGo.W : WGo.B;
        const res = noteTrialGame.play(x, y, trialColor);
        if (typeof res === 'number') {
          this.toast('非法落子（自杀或禁着）', 'error');
          return;
        }
        for (const cap of res || []) {
          noteBoard.removeObject(cap);
          noteTrialStones = noteTrialStones.filter((o) => !(o.x === cap.x && o.y === cap.y));
        }
        const o = { x, y, c: trialColor };
        noteBoard.addObject(o);
        noteTrialStones.push(o);
        this.noteTrialCount = noteTrialStones.length;
        document.body.dataset.noteTrial = String(this.noteTrialCount);
        return;
      }
      // 主线落子：只能在末尾记录（浏览历史时需先到结尾）
      if (this.noteViewPos < noteMoves.length) {
        this.toast('正在浏览历史：请先「到结尾」再落子', 'error');
        return;
      }
      const c = this.noteTurn === 'W' ? WGo.W : WGo.B;
      const res = noteGame.play(x, y, c);
      if (typeof res === 'number') {
        this.toast('非法落子（自杀或禁着）', 'error');
        return;
      }
      for (const cap of res || []) noteBoard.removeObject(cap);
      noteBoard.addObject({ x, y, c });
      noteMoves.push({ x, y, c });
      this.noteReplayTo(noteMoves.length);
      this.noteAnalyze();
    },
    noteUndo() {
      if (this.noteTrialMode || !noteMoves.length) return;
      noteMoves.pop();
      noteCurve = noteCurve.filter((p) => p.move <= noteMoves.length);
      this.noteReplayTo(noteMoves.length);
      this.updateNoteWinrate();
      this.$nextTick(() => this.drawNoteCurve());
    },
    noteToggleTrial() {
      if (!noteBoard) return;
      if (this.noteTrialMode) {
        this.noteReplayTo(this.noteViewPos); // 恢复主线（研究子清除）
        noteTrialStones = [];
        noteTrialGame = null;
        this.noteTrialMode = false;
        this.noteTrialCount = 0;
        document.body.dataset.noteTrial = '0';
        this.toast('已退出试下，恢复记录局面');
      } else {
        noteTrialStones = [];
        noteTrialGame = gameFromBoard(noteBoard, this.noteTurn);
        this.noteTrialMode = true;
        this.noteTrialCount = 0;
        document.body.dataset.noteTrial = '0';
        this.toast('试下研究：自由摆子（含提子），退出后恢复记录');
      }
    },
    noteTrialUndo() {
      if (!noteBoard || !noteTrialStones.length) return;
      noteTrialStones.pop();
      this.noteReplayTo(this.noteViewPos); // 先恢复主线
      noteTrialGame = gameFromBoard(noteBoard, this.noteTurn);
      const stones = noteTrialStones.slice();
      noteTrialStones = [];
      for (const o of stones) {
        const res = noteTrialGame.play(o.x, o.y, o.c);
        if (typeof res === 'number') continue;
        for (const cap of res) noteBoard.removeObject(cap);
        noteBoard.addObject(o);
        noteTrialStones.push(o);
      }
      this.noteTrialCount = noteTrialStones.length;
      document.body.dataset.noteTrial = String(this.noteTrialCount);
    },
    noteBuildSgf() {
      const size = noteBoard ? noteBoard.size : 9;
      let sgf = `(;GM[1]FF[4]CA[UTF-8]SZ[${size}]KM[6.5]PB[黑方]PW[白方]`;
      const ab = noteSetup.filter((s) => s.c === WGo.B).map((s) => `[${sgfCoord(s.x, s.y)}]`).join('');
      const aw = noteSetup.filter((s) => s.c === WGo.W).map((s) => `[${sgfCoord(s.x, s.y)}]`).join('');
      if (ab) sgf += 'AB' + ab;
      if (aw) sgf += 'AW' + aw;
      let color = noteBaseTurn === 'W' ? 'W' : 'B';
      for (const m of noteMoves) {
        sgf += `;${color}[${sgfCoord(m.x, m.y)}]`;
        color = color === 'B' ? 'W' : 'B';
      }
      return sgf + ')';
    },
    noteSaveSgf() {
      if (!noteMoves.length) {
        this.toast('还没有落子，无可保存', 'error');
        return;
      }
      const sgf = this.noteBuildSgf();
      const d = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const name = `弈友对局-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.sgf`;
      const blob = new Blob([sgf], { type: 'application/x-go-sgf' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
      // 桌面 webview 可能没有下载对话框：剪贴板兜底
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(sgf);
          this.toast(`已下载 ${name}（SGF 同时复制到剪贴板）`, 'success');
        } else {
          this.toast(`已下载 ${name}`, 'success');
        }
      } catch (e) {
        this.toast(`已下载 ${name}`, 'success');
      }
    },
    noteCopySgf() {
      if (!noteMoves.length) {
        this.toast('还没有落子，无可复制', 'error');
        return;
      }
      const sgf = this.noteBuildSgf();
      try {
        navigator.clipboard.writeText(sgf).then(
          () => this.toast('SGF 已复制到剪贴板', 'success'),
          () => this.toast('复制失败（可手动下载保存）', 'error')
        );
      } catch (e) {
        this.toast('复制失败：' + ((e && e.message) || e), 'error');
      }
    },
    noteLoadSgf() {
      const text = (this.noteSgfInput || '').trim();
      if (!text) return;
      let k;
      try {
        k = WGo.SGF.parse(text);
      } catch (e) {
        this.toast('SGF 解析失败：' + ((e && e.message) || e), 'error');
        return;
      }
      if (!k || !k.size) {
        this.toast('SGF 缺少 SZ（棋盘大小）属性', 'error');
        return;
      }
      if (this.noteTrialMode) this.noteToggleTrial();
      this.noteInitBoard(k.size);
      noteSetup = [];
      if (k.root && k.root.setup) {
        for (const s of k.root.setup) {
          if (s.c) noteSetup.push({ x: s.x, y: s.y, c: s.c });
        }
      }
      noteMoves = [];
      let node = k.root;
      while (node && node.children && node.children.length) {
        node = node.children[0];
        if (node.move && !node.move.pass) noteMoves.push({ x: node.move.x, y: node.move.y, c: node.move.c });
      }
      noteBaseTurn = k.root && k.root.turn === WGo.W ? 'W' : 'B';
      noteCurve = [];
      noteTrialStones = [];
      this.noteBoardSize = k.size;
      this.noteBoardLabel = k.size + ' 路';
      this.noteCandidates = [];
      this.noteReplayTo(noteMoves.length);
      this.toast(`已载入棋谱（${noteMoves.length} 手），可继续记录`, 'success');
      this.noteAnalyze();
    },
    // 胜率：真实后端复用复盘同一 review 接口（复盘功能不动），mock 本地模拟
    async noteAnalyze() {
      if (this.noteAnalyzing) return;
      if (!noteMoves.length) return;
      const token = ++noteToken;
      this.noteAnalyzing = true;
      this.noteCurveLabel = this.source === 'real' ? 'KataGo 分析中…（黑方胜率）' : '模拟数据（黑方胜率）';
      const sgf = this.noteBuildSgf();
      try {
        if (this.source === 'real') {
          const r = await api.analyze(sgf, this.reviewProfile || 'fast');
          let detail = null;
          for (let i = 0; i < 240 && token === noteToken; i++) {
            await new Promise((res) => setTimeout(res, 1500));
            if (token !== noteToken) return;
            const st = await api.reviewStatus(r.review_id);
            if (st.status === 'done') {
              detail = await api.reviewDetail(r.review_id);
              break;
            }
            if (st.status === 'failed') {
              this.toast('分析失败：' + (st.error || '引擎不可用'), 'error');
              break;
            }
          }
          if (token !== noteToken) return;
          if (detail && detail.winrate_curve) {
            noteCurve = detail.winrate_curve.map((m) => ({
              move: m.move,
              winrate: m.color === 'B' ? m.winrate : 1 - m.winrate,
            }));
            // 选点推荐：最后一手的 KataGo 候选点（统一转黑方视角）
            const last = detail.winrate_curve[detail.winrate_curve.length - 1];
            this.noteCandidates = (last && last.candidates) ? last.candidates.map((c) => ({
              ...c,
              winrate: last.color === 'B' ? c.winrate : (c.winrate != null ? 1 - c.winrate : null),
            })) : [];
            this.noteOwnership = detail.final_ownership || [];
            this.$nextTick(() => this.drawHeat('note-heat-canvas', 'note-board-host', this.noteOwnership));
          }
        } else {
          await new Promise((res) => setTimeout(res, 300));
          noteCurve = buildMockNoteCurve(noteMoves.length);
          this.noteCandidates = [];
        }
      } catch (e) {
        this.toast('分析失败：' + ((e && e.message) || e), 'error');
      } finally {
        if (token === noteToken) {
          this.noteAnalyzing = false;
          this.noteCurveLabel = this.source === 'real'
            ? `真实 KataGo · 黑方胜率（${noteCurve.length} 手）`
            : '模拟数据 · 黑方胜率';
          this.updateNoteWinrate();
          this.$nextTick(() => this.drawNoteCurve());
        }
      }
    },
    updateNoteWinrate() {
      if (!noteCurve.length) {
        this.noteWinrateText = '';
        return;
      }
      const p = noteCurve[Math.min(this.noteViewPos, noteCurve.length) - 1];
      if (p) {
        this.noteWinrateText = `第 ${p.move} 手 · 黑方胜率 ${(p.winrate * 100).toFixed(1)}%`;
      } else {
        this.noteWinrateText = '初始局面 · 黑方胜率 50.0%';
      }
    },
    drawNoteCurve() {
      const canvas = document.getElementById('note-curve-canvas');
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 400;
      const cssH = 150;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);
      const raw = noteCurve;
      const pts = raw.map((p, i) => ({
        move: p.move,
        winrate: smoothWinrate(raw, i),
      }));
      if (!pts.length) {
        ctx.fillStyle = '#9a927f';
        ctx.font = '13px sans-serif';
        ctx.fillText('落子后自动分析胜率（真实数据源走 KataGo）', 20, cssH / 2);
        return;
      }
      const padL = 34, padR = 10, padT = 12, padB = 18;
      const maxMove = Math.max(pts[pts.length - 1].move, 1);
      const px = (i) => padL + (i * (cssW - padL - padR)) / maxMove;
      const py = (v) => padT + (1 - v) * (cssH - padT - padB);
      // 50% 参考线
      ctx.strokeStyle = '#e2dccd';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, py(0.5));
      ctx.lineTo(cssW - padR, py(0.5));
      ctx.stroke();
      ctx.fillStyle = '#9a927f';
      ctx.font = '11px sans-serif';
      ctx.fillText('50%', 6, py(0.5) + 4);
      // 曲线
      ctx.strokeStyle = '#2b6cb0';
      ctx.lineWidth = 2;
      ctx.beginPath();
      pts.forEach((p, i) => {
        const x = px(p.move - 1);
        const y = py(p.winrate);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      // 数据点
      ctx.fillStyle = '#2b6cb0';
      for (const p of pts) {
        ctx.beginPath();
        ctx.arc(px(p.move - 1), py(p.winrate), 3, 0, Math.PI * 2);
        ctx.fill();
      }
    },

    // ---------------- 复杂度曲线（目差不确定度，绝艺同款指标） ----------------
    drawComplexityCurve() {
      const canvas = document.getElementById('complexity-canvas');
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 400;
      const cssH = 80;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);
      const pts = this.curveData
        .map((m) => ({ move: m.move, v: m.score_stdev }))
        .filter((p) => typeof p.v === 'number');
      if (!pts.length) {
        ctx.fillStyle = '#9a927f';
        ctx.font = '12px sans-serif';
        ctx.fillText('分析完成后显示复杂度（局面目差不确定度）', 20, cssH / 2);
        return;
      }
      const padL = 34, padR = 10, padT = 10, padB = 14;
      const maxMove = Math.max(pts[pts.length - 1].move, 1);
      const maxV = Math.max(...pts.map((p) => p.v), 1);
      const px = (i) => padL + (i * (cssW - padL - padR)) / maxMove;
      const py = (v) => padT + (1 - v / maxV) * (cssH - padT - padB);
      ctx.strokeStyle = '#805ad5';
      ctx.lineWidth = 2;
      ctx.beginPath();
      pts.forEach((p, i) => {
        const x = px(p.move - 1), y = py(p.v);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.fillStyle = '#9a927f';
      ctx.font = '10px sans-serif';
      ctx.fillText(`峰值 ${maxV.toFixed(1)} 目`, padL, cssH - 2);
    },

    // ---------------- 目差曲线（黑方视角，正=黑好） ----------------
    drawScoreCurve() {
      const canvas = document.getElementById('score-curve-canvas');
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth || 400;
      const cssH = 80;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);
      const pts = this.curveData
        .map((m) => ({
          move: m.move,
          v: typeof m.score_lead === 'number'
            ? (m.color === 'B' ? m.score_lead : -m.score_lead)
            : null,
        }))
        .filter((p) => typeof p.v === 'number');
      if (!pts.length) {
        ctx.fillStyle = '#9a927f';
        ctx.font = '12px sans-serif';
        ctx.fillText('分析完成后显示目差曲线', 20, cssH / 2);
        return;
      }
      const padL = 34, padR = 10, padT = 10, padB = 14;
      const maxMove = Math.max(pts[pts.length - 1].move, 1);
      const maxAbs = Math.max(...pts.map((p) => Math.abs(p.v)), 1);
      const px = (i) => padL + (i * (cssW - padL - padR)) / maxMove;
      const py = (v) => padT + (1 - (v + maxAbs) / (2 * maxAbs)) * (cssH - padT - padB);
      // 0 参考线
      ctx.strokeStyle = '#e2dccd';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, py(0));
      ctx.lineTo(cssW - padR, py(0));
      ctx.stroke();
      ctx.strokeStyle = '#dd6b20';
      ctx.lineWidth = 2;
      ctx.beginPath();
      pts.forEach((p, i) => {
        const x = px(p.move - 1), y = py(p.v);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.fillStyle = '#9a927f';
      ctx.font = '10px sans-serif';
      ctx.fillText(`幅度 ±${maxAbs.toFixed(1)} 目`, padL, cssH - 2);
    },

    // ---------------- 目数热图（KataGo ownership 叠加层） ----------------
    heatOwnership() {
      // 当前手当时 KataGo 算出的目数归属；与上一手做平均，
      // 避免估值噪声导致同一块空的黑白颜色随手数来回跳。
      const m = this.curveData[this.currentMove - 1];
      if (m && m.ownership && m.ownership.length) {
        const prev = this.curveData[Math.max(0, this.currentMove - 2)];
        if (prev && prev.ownership && prev.ownership.length === m.ownership.length) {
          const a = m.ownership;
          const b = prev.ownership;
          const out = new Array(a.length);
          for (let i = 0; i < a.length; i += 1) {
            out[i] = 0.5 * a[i] + 0.5 * b[i];
          }
          return out;
        }
        return m.ownership;
      }
      if (this.source === 'mock' && this.isBuiltin()) {
        // mock：固定模式热图（不随手数变化）
        const size = this.kifuSize || 9;
        const total = size * size;
        const arr = [];
        for (let i = 0; i < total; i += 1) {
          arr.push(Math.sin(17 + i * 1.7) * 0.85);
        }
        return arr;
      }
      // 旧复盘数据（无逐手 ownership）：回退终局热图
      return this.finalOwnership;
    },
    toggleHeat() {
      this.showHeat = !this.showHeat;
      if (this.showHeat) {
        this.$nextTick(() => this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership()));
      }
    },
    drawHeat(canvasId, hostId, ownership) {
      const canvas = document.getElementById(canvasId);
      const host = document.getElementById(hostId);
      if (!canvas || !host || !ownership || !ownership.length) return;
      const boardCv = host.querySelector('canvas');
      if (!boardCv) return;
      const frame = canvas.parentElement;
      const W = boardCv.clientWidth;
      const dpr = window.devicePixelRatio || 1;
      const br = boardCv.getBoundingClientRect();
      const fr = frame.getBoundingClientRect();
      canvas.style.left = `${br.left - fr.left}px`;
      canvas.style.top = `${br.top - fr.top}px`;
      canvas.style.width = `${W}px`;
      canvas.style.height = `${W}px`;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(W * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, W);
      const size = Math.round(Math.sqrt(ownership.length)) || 19;
      // 与 WGo section=0.25 配套（section 再大会让 WGo 坐标 getX(-.75) 越界）
      const S = W / (size + 0.5);
      const L = 0.75 * S;
      // 目数模式：每格显示 KataGo 目数估计（黑正白负，1 位小数）；
      // 黑地数字深蓝、白地数字白色带深描边；中性点（|v|<0.25）不标注。
      const fs = Math.max(8, S * 0.46);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
          const v = ownership[y * size + x];
          if (typeof v !== 'number' || Math.abs(v) < 0.25) continue;
          const cx = L + x * S;
          const cy = L + y * S;
          const txt = v.toFixed(1);
          if (v > 0) {
            ctx.fillStyle = 'rgba(21, 63, 122, 0.92)';
            ctx.font = `bold ${fs}px "Segoe UI", sans-serif`;
            ctx.fillText(txt, cx, cy);
          } else {
            ctx.font = `bold ${fs}px "Segoe UI", sans-serif`;
            ctx.lineWidth = Math.max(1.5, fs / 7);
            ctx.strokeStyle = 'rgba(75, 62, 40, 0.92)';
            ctx.strokeText(txt, cx, cy);
            ctx.fillStyle = 'rgba(255, 252, 245, 0.96)';
            ctx.fillText(txt, cx, cy);
          }
        }
      }
    },
  },

  watch: {
    // 播放 / 滑条步进时，热图跟随当前手 KataGo 的目数归属
    currentMove() {
      if (this.showHeat) {
        this.drawHeat('review-heat-canvas', 'board-host', this.heatOwnership());
      }
    },
  },

  mounted() {
    window.__app = this; // 便于自动化验收/调试
    window.addEventListener('resize', this.onResize);
    if (window.ResizeObserver) {
      this._hostRO = new ResizeObserver(() => this.syncBoardWidths());
      ['board-host', 'practice-board-host', 'note-board-host', 'play-board-host']
        .forEach((id) => {
          const el = document.getElementById(id);
          if (el) this._hostRO.observe(el);
        });
    }
    if (this.prodMode) {
      this.source = 'real';
      setSource('real');
    }
    this.loadSgf(MOCK_SAMPLE_SGF);
  },

  beforeUnmount() {
    window.removeEventListener('resize', this.onResize);
    if (this._hostRO) this._hostRO.disconnect();
    this.stopPlay();
    if (pollTimer) clearTimeout(pollTimer);
  },
}).mount('#app');
