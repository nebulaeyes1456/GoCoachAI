"""Pydantic 数据模型（契约落地）。

与 ``docs/architecture.md`` §3 数据表、§4 各接口的入/出结构一一对应。
禁止引入额外重依赖；仅使用 pydantic。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# §4.1 复盘（窗口1 实现）
# ---------------------------------------------------------------------------


class ReviewAnalyzeRequest(BaseModel):
    """POST /api/v1/review/analyze 入参。"""

    sgf_text: Optional[str] = None
    sgf_base64: Optional[str] = None
    profile: str = "fast"  # fast/standard/fine


class ReviewAnalyzeResponse(BaseModel):
    review_id: str


class ReviewStatusResponse(BaseModel):
    status: str = "pending"  # pending/analyzing/done/failed
    progress: float = 0.0
    error: Optional[str] = None


class MoveInfo(BaseModel):
    """单手的分析信息（winrate_curve 与 key_moves 的元素）。"""

    move: int
    color: str  # B/W
    coord: str  # 如 "Q16"，pass 为 ""
    winrate: Optional[float] = None       # 当前方胜率 0~1
    score_lead: Optional[float] = None    # 目数领先
    category: str = "normal"              # blunder/question/good/normal
    delta: Optional[float] = None         # 胜率变化（当前方）
    best_coord: Optional[str] = None      # KataGo 推荐点
    pv: list[str] = Field(default_factory=list)  # 变化序列
    visits: Optional[int] = None
    # KataGo 选点推荐（每手前几个候选点）：
    # [{order, move, winrate, score_lead, visits, pv}]，winrate 为当前方视角
    candidates: list[dict] = Field(default_factory=list)
    score_stdev: Optional[float] = None  # 目差不确定度（复杂度指标）
    # 该手落下后局面 KataGo 计算的目数归属 [-1,1]（随播放逐手热图）
    ownership: list[float] = Field(default_factory=list)


class ReviewStats(BaseModel):
    blunders: int = 0
    questions: int = 0
    good: int = 0


class ReviewDetailResponse(BaseModel):
    id: str
    board_size: int
    black: Optional[str] = None
    white: Optional[str] = None
    profile: str = "fast"
    status: str = "pending"
    winrate_curve: list[MoveInfo] = Field(default_factory=list)
    key_moves: list[MoveInfo] = Field(default_factory=list)
    stats: ReviewStats = Field(default_factory=ReviewStats)
    final_ownership: list[float] = Field(default_factory=list)  # 终局目数热图 [-1,1]
    sgf_text: Optional[str] = None  # 仅 include_sgf=true 时返回


# ---------------------------------------------------------------------------
# §4.2 教练讲解（窗口2 实现）
# ---------------------------------------------------------------------------


class CoachExplainRequest(BaseModel):
    review_id: str
    move_number: int


class MoveExplanationContent(BaseModel):
    """§6 kind=move 的 content 字段。"""

    problem: str
    reason: str
    recommendation: str
    variation: list[str] = Field(default_factory=list)
    takeaway: str
    level_note: str = ""
    # 同步讲棋分段（每段绑定一个变化序列，前端边讲边摆棋）；
    # 旧缓存无此字段时前端降级纯文本展示
    segments: list[dict] = Field(default_factory=list)
    # 死活结果类型（净活/劫活/双活/净杀/劫杀）与脱先判断（能脱先/不能脱先/有条件）
    result_type: str = ""
    can_tenuki: str = ""


class CoachExplainResponse(BaseModel):
    move_number: int
    kind: str = "move"
    content: MoveExplanationContent
    model: str
    cost: float


class CoachSummaryRequest(BaseModel):
    review_id: str


class SummaryContent(BaseModel):
    """§6 kind=summary 的 content 字段。"""

    opening: str = ""
    middle: str = ""
    endgame: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CoachSummaryResponse(BaseModel):
    kind: str = "summary"
    content: SummaryContent
    model: str
    cost: float


class PenaltyRequest(BaseModel):
    """惩罚变化请求：坏手落子后对手的最强应对。"""
    review_id: str
    move_number: int


class PenaltyResponse(BaseModel):
    kind: str = "penalty"
    content: dict = Field(default_factory=dict)
    model: Optional[str] = None
    cost: float = 0.0


class TtsRequest(BaseModel):
    """语音合成请求。"""
    text: str
    persona: str = "gentle"  # gentle | tsundere | default


class CoachDeepRequest(BaseModel):
    """深度分析请求：整盘棋报告。"""
    review_id: str


class CoachDeepResponse(BaseModel):
    kind: str = "deep"
    content: dict = Field(default_factory=dict)
    model: Optional[str] = None
    cost: float = 0.0


class CoachAskRequest(BaseModel):
    sgf_text: str
    question: str
    level: str = "-5"


class AskAnswerContent(BaseModel):
    """§6 kind=answer 的 content 字段。"""

    conclusion: str
    reasoning: str
    variation: list[str] = Field(default_factory=list)
    kata_winrate: Optional[float] = None


class CoachAskResponse(BaseModel):
    answer: AskAnswerContent
    model: str
    cost: float


# ---------------------------------------------------------------------------
# §4.3 题目系统（窗口3 实现）
# ---------------------------------------------------------------------------


class ProblemGenerateRequest(BaseModel):
    review_id: str
    themes: list[str] = Field(default_factory=list)
    max_problems: int = 6
    target_rank: int = -5  # K 为负，D 为正：15K=-15, 3D=3


class GeneratedProblemBrief(BaseModel):
    id: str
    theme: str
    setup_sgf: str
    rank_min: Optional[int] = None
    rank_max: Optional[int] = None
    hint: Optional[str] = None


class ProblemGenerateResponse(BaseModel):
    problems: list[GeneratedProblemBrief] = Field(default_factory=list)
    failed: int = 0  # 验证未通过被丢弃的数量


# 局部死活提取（M3 新增，2026-08-31）
class ProblemExtractRequest(BaseModel):
    sgf_text: Optional[str] = None   # 完整对局 SGF；与 review_id 至少提供一个
    review_id: Optional[str] = None
    max_problems: int = 5
    target_rank: int = -5  # K 为负，D 为正：15K=-15, 3D=3


class ProblemExtractResponse(BaseModel):
    extracted: list[GeneratedProblemBrief] = Field(default_factory=list)
    failed: int = 0   # 验题未通过被丢弃的候选中心数
    skipped: int = 0  # 未验题跳过的信号数（超出前 N 中心/无效中心/重复题面）


class ProblemDetailResponse(BaseModel):
    """完整题目；explanation 字段始终存在，可为 null。"""

    id: str
    source: str  # generated/library/imported
    review_id: Optional[str] = None
    theme: str  # life_death/capturing_race/endgame/middle
    rank_min: Optional[int] = None
    rank_max: Optional[int] = None
    setup_sgf: str
    answer: str  # 正解第一手 coord
    branches: str  # 验证分支 JSON：变化树
    verdict: str  # 死活/对杀结论说明
    hint: Optional[str] = None
    explanation: Optional[str] = None
    goal: Optional[str] = None  # 目标分类：做活/杀棋/对杀/逃棋筋/吃棋筋/收官最大/中盘要点
    chain_id: Optional[str] = None    # 所属题链（v1.7.0；非空时前端显示来源徽标）
    chain_step: Optional[int] = None  # 链内步序（第 n 变）
    chain_name: Optional[str] = None  # 链名（路由层 join 填充）
    status: str = "active"


class ProblemAttemptRequest(BaseModel):
    coord: str  # 如 "D15"，或 "pass"


class ProblemAttemptResponse(BaseModel):
    correct: bool
    response: str = ""
    variation: list[str] = Field(default_factory=list)
    solved: bool = False  # true 时表示本题已过
    explanation: Optional[str] = None


class ProblemExplainResponse(BaseModel):
    """练习深度讲解（同步讲棋分段）。"""

    kind: str = "problem_explain"
    problem_id: str
    content: dict = Field(default_factory=dict)
    model: Optional[str] = None
    cost: float = 0.0


class ExtractFromReviewRequest(BaseModel):
    """从复盘局面局部截取入题库。"""

    review_id: str
    move_number: int


class ExtractFromReviewResponse(BaseModel):
    """截题结果。"""

    problem_id: str
    theme: str
    answer: str
    setup_sgf: str
    created: bool  # False=同题面已存在（幂等）


class LibraryProblemBrief(BaseModel):
    id: str
    theme: str
    rank_min: Optional[int] = None
    rank_max: Optional[int] = None
    setup_sgf: str
    hint: Optional[str] = None
    goal: Optional[str] = None
    chain_id: Optional[str] = None    # v1.7.0：题链来源（非空时列表项显示徽标）
    chain_step: Optional[int] = None


class ProblemLibraryResponse(BaseModel):
    problems: list[LibraryProblemBrief] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# §4.3 附：死活题生长链条（v1.7.0，Growth Chains）
# ---------------------------------------------------------------------------


class ChainBrief(BaseModel):
    """链列表条目（含题数与实际主题）。"""

    id: str
    name: str
    theme: Optional[str] = None          # life_death / capturing_race / mixed
    description: Optional[str] = None
    status: str = "draft"                # draft / active
    problems_count: int = 0
    themes: list[str] = Field(default_factory=list)  # 链上题实际涉及的主题
    created_at: Optional[str] = None


class ChainListResponse(BaseModel):
    chains: list[ChainBrief] = Field(default_factory=list)


class ChainProblemBrief(BaseModel):
    """链上的一道题（按 chain_step 排序即为学习顺序）。"""

    id: str
    theme: str
    goal: Optional[str] = None
    rank_min: Optional[int] = None
    rank_max: Optional[int] = None
    setup_sgf: str
    hint: Optional[str] = None
    answer: str = ""
    verdict: Optional[str] = None
    chain_id: str = ""
    chain_step: Optional[int] = None
    solved: bool = False                 # 是否已有答对记录


class ChainDetailResponse(BaseModel):
    chain: ChainBrief
    root_sgf: str = ""
    problems: list[ChainProblemBrief] = Field(default_factory=list)


class ChainGrowRequest(BaseModel):
    max_depth: int = 3                   # 从定式终局算起的层数
    max_per_level: int = 3               # 每层最多尝试的种子数
    profile: Optional[str] = None        # 验题档位；缺省按 config verify_profile
    verify_mode: Optional[str] = None    # 验收口径：local_board(默认)/local_death/winrate


class ChainGrowResponse(BaseModel):
    chain_id: str
    added: int = 0                       # 本次新入库题数（重复 grow 为 0）
    discarded: int = 0                   # 验题未通过/局部超框的丢弃次数
    steps: int = 0                       # 生长后链上末尾步序
    problems: list[ChainProblemBrief] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §4.4 系统（窗口0/4）
# ---------------------------------------------------------------------------


class SystemInfoResponse(BaseModel):
    version: str = "0.9.0"
    engine_ready: bool = False
    engine_backend: str = ""  # T1：当前所选后端 opencl/eigenavx2（探测失败为空）
    schema_version: int = 0   # T0：数据库 schema 版本（schema_migrations 最大值）
    model_ready: bool = False
    profile: str = "fast"
    token_usage_month: float = 0.0
    token_limit_month: float = 30.0


class SystemSettingsUpdate(BaseModel):
    """PUT /api/v1/system/settings 入参；允许部分更新。

    profile 对应 review.profile；其余已知键按顶层结构写回；
    未列出的键（如 katago/coach 子段）通过 extra 透传。
    """

    model_config = ConfigDict(extra="allow")

    profile: Optional[str] = None
    blunder_threshold: Optional[float] = None
    question_threshold: Optional[float] = None
    good_threshold: Optional[float] = None


# ---------------------------------------------------------------------------
# §4.4 系统运维/诊断（T0 新增：health/engine/logs/cache/backup/version）
# ---------------------------------------------------------------------------


class HealthCheckItem(BaseModel):
    ok: bool
    detail: str = ""


class HealthResponse(BaseModel):
    """GET /api/v1/system/health。checks 固定四项。"""

    status: str = "ok"  # ok | degraded
    checks: dict[str, HealthCheckItem] = Field(default_factory=dict)


class EngineStatusResponse(BaseModel):
    """GET /api/v1/system/engine/status。"""

    backend: str = ""         # 当前所选后端：opencl/eigenavx2（未选时为空）
    mode: str = "auto"        # 配置值：auto/cpu/opencl
    running: bool = False     # 引擎进程真实存活状态
    process_count: int = 0    # 存活的分析引擎进程数
    last_error: str = ""      # 最近一次引擎错误（无则空）


class EngineRestartRequest(BaseModel):
    """POST /api/v1/system/engine/restart 入参（可选）。"""

    backend: Optional[str] = None  # 临时切换：auto|cpu|opencl；缺省=回到配置值


class EngineRestartResponse(BaseModel):
    ok: bool
    backend: str
    mode: str = "auto"
    message: str = ""


class LogsResponse(BaseModel):
    """GET /api/v1/system/logs。"""

    path: str                 # 日志文件路径（相对项目根）
    lines: int                # 返回行数
    log: list[str] = Field(default_factory=list)  # 最近 N 行（含换行符）


class CacheClearRequest(BaseModel):
    """POST /api/v1/system/cache/clear 入参。"""

    kind: str = "all"  # coach | review | all


class CacheClearResponse(BaseModel):
    kind: str
    cleared: dict[str, int] = Field(default_factory=dict)  # 表名 -> 删除行数


class DbBackupResponse(BaseModel):
    """POST /api/v1/system/db/backup 出参。"""

    path: str            # 备份文件相对项目根的路径
    filename: str        # goapp-YYYYMMDD-HHMMSS.db
    size_bytes: int


class VersionResponse(BaseModel):
    """GET /api/v1/system/version。"""

    version: str            # 后端版本
    engine_backend: str     # 当前所选引擎后端
    schema_version: int     # 数据库 schema 版本
    api_prefix: str = "/api/v1"


# ---------------------------------------------------------------------------
# §6 讲解 JSON 结构（explanations.content 的通用别名）
# ---------------------------------------------------------------------------

# kind 与 content 类型的对应关系：
#   move    -> MoveExplanationContent
#   summary -> SummaryContent
#   answer  -> AskAnswerContent
ExplanationContent = MoveExplanationContent | SummaryContent | AskAnswerContent



# ---------------------------------------------------------------------------
# §4.6 棋手档案 / 棋谱库 / 水平画像（成长视图）
# ---------------------------------------------------------------------------

class ProfileCreateRequest(BaseModel):
    """创建棋手档案。"""

    name: str
    note: str = ""


class PlayerProfile(BaseModel):
    """档案摘要。"""

    id: str
    name: str
    note: str = ""
    games_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProfileListResponse(BaseModel):
    profiles: list[PlayerProfile] = Field(default_factory=list)


class ProfileGameBrief(BaseModel):
    """档案内一局棋的摘要。"""

    review_id: str
    black: str = ""
    white: str = ""
    board_size: int = 19
    moves_count: int = 0
    status: str = "done"
    created_at: Optional[str] = None
    blunders: int = 0
    questions: int = 0
    good: int = 0
    imported: bool = False


class ProfileDetailResponse(BaseModel):
    """档案详情：档案 + 棋谱列表 + 画像。"""

    profile: PlayerProfile
    games: list[ProfileGameBrief] = Field(default_factory=list)
    insight: Optional[dict] = None


class ProfileAttachRequest(BaseModel):
    """把已分析的复盘归档到档案。"""

    profile_id: str
    review_id: str


class ProfileImportRequest(BaseModel):
    """导入外部 SGF 到档案（自动复盘分析后归档）。"""

    profile_id: str
    sgf_text: str
    review_profile: str = "fast"


class ProfileImportResponse(BaseModel):
    review_id: str
    status: str = "pending"


class ProfileInsightResponse(BaseModel):
    """画像 + 建议。"""

    profile_id: str
    insight: dict = Field(default_factory=dict)
    model: Optional[str] = None
    cost: float = 0.0


class ProfileAdviceRequest(BaseModel):
    """生成/刷新 AI 提高建议。"""

    profile_id: str
