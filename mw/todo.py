"""TODO.md — Team Work 任务契约文件 (1A 边界, 2026-10-03 减法重构)

设计动机: 任务内容落在共享卷的 TODO.md 文件里, worker 直接读, main 直接编辑。
两端确定, 中间代码层伤不到内容 (根治 "continue task" 占位符)。

所有权模型 (硬约定):
- main 拥有: 整份文件的结构 + 『当前任务』的目标/承接方/产物目录/产出文件
  + 『待执行』『已完成』『已放弃』三段。main 下发时写, 回收后轮转。
- 当前任务承接 worker 拥有: 『当前任务』→『状态』『结果』两行。仅此。
  worker 完成时把占位符替换成真实结果; 需澄清则不写 (保持占位)。
- 其他 worker / 非承接方: 只读, 不得改。

2026-10-03 减法: 删除契约校验和 / ContractTampered 防篡改机制。
理由: 该机制只制造 ContractTampered 死锁, 而 worker prompt 的硬约束
("只改状态/结果两行, 越权即判无效") 已足够; 篡改风险由所有权约定 +
main 回收时审计兜底, 不需要密码学校验。少一个机制 = 少一类静默失败。

文件结构 (放在 project_workspace 根):

    # Team Work: <title>

    ## 当前任务
    - 步骤: design
    - 目标: <task_description>
    - 承接方: system_engineer
    - 产物目录: /share_workspace/projects/xxx
    - 状态: 进行中
    - 结果: (worker 完成后填写)
    - kanban_task: t_xxxxxxxx

    ---

    ## 待执行
    - [code] 编码 → coder
      目标: ...

    ## 已完成
    - [req-analysis] 需求分析 → <结果摘要>

    ## 已放弃
    - [step_id] 目标 → 放弃原因
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 结果占位符: worker 没填之前是这个, 表示"未完成"
RESULT_PLACEHOLDER = "(worker 完成后填写)"

# 卡绑定键名别名 (REOPEN-DESIGN D1): 优先 kanban_task, 缺失回退中文 `卡`。
# 不改文件格式、不动 main 的书写习惯, 全链 (parse/reaper/rotate/校验) 统一生效。
# I18N-R3 (2026-10-16): 增 `kanban_task_id` 英文别名, 与 en 部署 main/worker 配套。
CARD_KEY_ALIASES = ("kanban_task", "kanban_task_id", "卡")

# I18N-R3 (2026-10-16, REQ §4.2 方案 A) — 双键别名解析 + 按部署语言渲染:
# 解析器放宽输入 (中英键各两种都收), 渲染侧 (render()) 按当前进程的
# 部署语言 (mw/i18n.resolve_lang()) 输出对应键名。保持历史中文 TODO.md
# 与新英文 TODO.md 全链通吃, 不动任何结算语义/超时/harvest 逻辑。
#
# 段头 aliases (解析宽松: 任一命中即视为对应段)。BILINGUAL_HEADER_ALIASES
# 反向查表 key 是「中文协议字面」(保持内部数据形态不变, 既有代码
# PENDING_HEADER in secs 之类零改动).
BILINGUAL_HEADER_ALIASES = {
    "当前任务": ("当前任务", "Current Task"),
    "待执行":   ("待执行",   "Pending"),
    "已完成":   ("已完成",   "Done"),
    "已放弃":   ("已放弃",   "Dropped"),
}
# 字段名 aliases (内部统一折算到「中文协议键」, 渲染侧按语言输出)
BILINGUAL_FIELD_ALIASES = {
    "状态":     ("状态",      "status"),
    "结果":     ("结果",      "result"),
    "目标":     ("目标",      "goal"),
    "承接方":   ("承接方",    "assignee"),
    "产物目录": ("产物目录",  "product_workspace"),       # § I18N-R3 M-4 别名
    "product workspace": ("产物目录", "product_workspace"),
    "workspace": ("产物目录", "product_workspace"),
    "产出文件": ("产出文件",  "required_files"),           # § I18N-R3 M-4 别名
    "required files": ("产出文件", "required_files"),
    "required_files": ("产出文件", "required_files"),
    "标题":     ("标题",      "title"),
    "步骤":     ("步骤",      "step"),
    "kanban_task": ("kanban_task", "kanban_task"),  # 已是英文键, 维持
    "kanban_task_id": ("kanban_task", "kanban_task_id"),
    "卡":       ("kanban_task", "card"),  # 中文 `卡` 同样认, 维持 D1
}
# 字段名反向表: 中文协议键 → [(中文键, 英文键)]。供 _parse_kv 反查
# 用, 也供渲染按语言挑选键名。
_FIELD_CN_KEYS = {cn: (cn, en) for cn, en in BILINGUAL_FIELD_ALIASES.values()}

# 状态值语种表 (I18N-R3, 2026-10-16): 解析端 (parse_status / reaper)
# 已兼容中英文 (`working`/`进行中`/`in-progress` 全接受), 渲染端不
# 翻译 —— main 写啥写啥, reaper.lower 后判 in_progress_set / terminal_set。
# 此处保留规范值引用 (测试 / 调用方用)。
_BILINGUAL_STATUS_NOTE = (
    "状态值不翻译; reaper 兼容 `working`/`进行中`/`in-progress`。"
)

# I18N-R3: 解析端专用常量 (向后兼容既有调用方)。CURRENT_HEADER 等仍是
# 中文协议字面量, parse() 端按 BILINGUAL_HEADER_ALIASES 反查, 但旧代码
# `if CURRENT_HEADER in secs:` 仍命中 (中文键照常是当前任务)。新增
# EN_* 别名常量供 en 部署 main/worker 直接用, 不必查表。
CURRENT_HEADER = "当前任务"
CURRENT_HEADER_EN = "Current Task"
PENDING_HEADER = "待执行"
PENDING_HEADER_EN = "Pending"
DONE_HEADER = "已完成"
DONE_HEADER_EN = "Done"
DROPPED_HEADER = "已放弃"
DROPPED_HEADER_EN = "Dropped"
SEP = "\n---\n"

# 任务状态枚举: worker 在 TODO.md『当前任务』→『状态』字段写这些值,
# reaper 读它做结算决策。fs 上的状态字段是物理证据 (HTTP exit_code 不可信)。
#
# 状态机:
#   进行中 (main 下发初始值) → working    (worker 起手 / 干到一半)
#   working                  → blocked    (worker 写 .clarify.json)
#   working                  → completed  (worker 写完产物)
#   working                  → failed     (worker 明确放弃)
#   any                      → 进行中     (main 接管时回退)
#
# 注意: '进行中' 是 main 下发时初始值, 'working' 是 worker 起手时写的——
# 它们都表示"在干活", reaper 不区分。语义上对齐就行。
# I18N-R3: STATUS_* 仍是规范值 (zh-cn main/worker 写入; en 部署仍写
# 同一规范值, 别名走 BILINGUAL_STATUS_ALIASES 解析)。
STATUS_INPROGRESS = "进行中"
STATUS_WORKING = "working"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
# I18N-R3: 别名规范值, en 部署 main/worker 写入此值等价于 STATUS_INPROGRESS
STATUS_INPROGRESS_EN = "in-progress"
# issue#13 (2026-10-15): worker 在「启动后台子任务后等待结果」期间写的
# TODO.md 状态, 性质 = 非终态 / 非求助态 / 不触发 harvest. reaper 读此状态
# 时启用独立超时阈值 (MW_REAPER_SUBAGENT_MIN) 与专属敲门文案, 杜绝被
# 当成「普通 working 卡挂死」误敲. 不入 TERMINAL_STATUSES /
# ATTENTION_STATUSES; _STATE_TRANSITIONS / _ACTOR_POLICY 零改动
# (派生态 token `WAITING_SUB` 走 ledger.derive_worker_state, AC-A7).
STATUS_WAITING_SUB = "waiting_sub"
# reaper 解析时接受的别名 (含中文 / kebab-case), 与规范值等价; reaper 端
# 一律 lower() 后判 in WAITING_SUB_ALIASES, worker 写规范值即可.
WAITING_SUB_ALIASES = frozenset({
    STATUS_WAITING_SUB, "waiting-sub", "等待子任务",
})
# reaper 关心的"任务已完结"集合
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})
# reaper 关心的"需要 main 介入"集合
ATTENTION_STATUSES = frozenset({STATUS_BLOCKED})

# issue #124: 状态值中英别名总表 (单一归一化入口)。
# 规范值 = 英文 (completed/failed/blocked/working/waiting_sub);
# 中文与常见变异仅作兼容别名, 一律 lower()+strip() 后查表。
# 未识别 → None (调用方按"未识别"处理, 与现状语义一致, 不新增猜测)。
STATUS_ALIASES: dict = {
    STATUS_COMPLETED: frozenset({
        "completed", "complete", "done", "已完成", "完成"}),
    STATUS_FAILED: frozenset({
        "failed", "fail", "已失败", "失败"}),
    STATUS_BLOCKED: frozenset({
        "blocked", "block", "已阻塞", "阻塞"}),
    STATUS_WORKING: frozenset({
        "working", STATUS_INPROGRESS, "in-progress", "inprogress",
        "in_progress"}),
    STATUS_WAITING_SUB: WAITING_SUB_ALIASES,   # waiting_sub/waiting-sub/等待子任务
}

# 反查索引: 别名 → 规范值 (模块加载时构建一次)
_STATUS_ALIAS_INDEX = {
    alias: canon
    for canon, aliases in STATUS_ALIASES.items()
    for alias in aliases
}

# "在干 (不结算)" 集合: working 与 waiting_sub 都属之, 语义对齐旧 in_progress_set
IN_PROGRESS_SET = frozenset({STATUS_WORKING, STATUS_WAITING_SUB})


def normalize_status(raw) -> "str | None":
    """把 TODO.md『状态』原始值归一化为英文规范值; 未识别 → None。

    大小写 / 首尾空白不敏感 (CJK 不受 lower 影响)。
    这是全仓唯一的状态语义入口: reaper / console 一律经此判状态,
    禁止再写本地字面量集合 (issue #124 R-124-A/B)。
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    return _STATUS_ALIAS_INDEX.get(s)


@dataclass
class TodoTask:
    step_id: str
    title: str
    goal: str
    assignee: str
    status: str = "进行中"
    result: str = RESULT_PLACEHOLDER
    # 产物目录 (worker 把交付物写这里, 供同 batch 其他卡读到)
    product_workspace: str = ""
    # 产出文件要求 (验收方物理检查)
    required_files: List[str] = field(default_factory=list)
    # 关联的 kanban task_id: main 建卡后回填。reaper 用它把『当前任务』
    # 精确对上正在 running 的那张卡, 确定性识别"这张卡的 worker 写完了"。
    kanban_task_id: str = ""

    @property
    def has_result(self) -> bool:
        """worker 是否已写回真实结果 (非占位符、非空)。"""
        r = (self.result or "").strip()
        return bool(r) and r != RESULT_PLACEHOLDER


@dataclass
class TodoDoc:
    title: str
    current: Optional[TodoTask] = None
    pending: List[TodoTask] = field(default_factory=list)  # 待执行步骤 (链的剩余)
    done: List[str] = field(default_factory=list)      # 已完成条目 (markdown 行)
    dropped: List[str] = field(default_factory=list)   # 已放弃条目

    def render(self) -> str:
        """渲染 TodoDoc 为 markdown 字符串。

        I18N-R3 (2026-10-16): 按部署语言 (mw/i18n.resolve_lang) 选段头 /
        字段键的语种 (中/英)。状态值不翻译 —— reaper.parse_status 返回字
        面, reaper 端 lower 后判 in_progress_set / terminal_set (兼容
        `working` / `进行中` / `in-progress`)。中英部署产出对应键名,
        解析端 (BILINGUAL_*_ALIASES) 双键通吃。
        """
        lang = _render_lang()
        no_cur = "(无当前任务)" if lang == "zh-cn" else "(No current task)"
        empty_marker = "(无)" if lang == "zh-cn" else "(None)"
        lines = [f"# Team Work: {self.title}", ""]
        lines.append(f"## {_pick_header(CURRENT_HEADER)}")
        if self.current:
            c = self.current
            lines.append(f"- {_pick_field_key('步骤')}: {c.step_id}")
            lines.append(f"- {_pick_field_key('标题')}: {c.title}")
            lines.append(f"- {_pick_field_key('目标')}: {c.goal}")
            lines.append(f"- {_pick_field_key('承接方')}: {c.assignee}")
            if c.product_workspace:
                lines.append(f"- {_pick_field_key('产物目录')}: {c.product_workspace}")
            if c.required_files:
                lines.append(f"- {_pick_field_key('产出文件')}: {', '.join(c.required_files)}")
            # 状态值: 不翻译 (兼容 reaper 既有的多语种识别)
            lines.append(f"- {_pick_field_key('状态')}: {c.status}")
            lines.append(f"- {_pick_field_key('结果')}: {c.result}")
            if c.kanban_task_id:
                lines.append(f"- kanban_task: {c.kanban_task_id}")
        else:
            lines.append(no_cur)
        lines.append("")
        lines.append(SEP.strip())
        lines.append("")
        lines.append(f"## {_pick_header(PENDING_HEADER)}")
        if self.pending:
            for t in self.pending:
                who = f" → {t.assignee}" if t.assignee else ""
                lines.append(f"- [{t.step_id}] {t.title}{who}")
                if t.goal:
                    lines.append(f"  {_pick_field_key('目标')}: {t.goal}")
                if t.product_workspace:
                    lines.append(f"  {_pick_field_key('产物目录')}: {t.product_workspace}")
                if t.required_files:
                    lines.append(f"  {_pick_field_key('产出文件')}: {', '.join(t.required_files)}")
                if t.kanban_task_id:
                    # reaper 据此把『当前任务』精确对上正在 running 的那张卡
                    lines.append(f"  kanban_task: {t.kanban_task_id}")
        else:
            lines.append(empty_marker)
        lines.append("")
        lines.append(f"## {_pick_header(DONE_HEADER)}")
        if self.done:
            lines.extend(self.done)
        else:
            lines.append(empty_marker)
        lines.append("")
        lines.append(f"## {_pick_header(DROPPED_HEADER)}")
        if self.dropped:
            lines.extend(self.dropped)
        else:
            lines.append(empty_marker)
        lines.append("")
        return "\n".join(lines)


# issue #139: 『当前任务』段字段行的**唯一识别点** (形态类容错集中于此)。
# 旧实现四入口各持一份字段行正则、各自演化 ⇒ indented 形态入口间不一致。
# 收敛为单一正则后, 缩进 / `*` bullet / 缺 `- ` 前缀 三种形态在同一处识别。
#
# 语义不放宽的护栏 (由调用方负责, 本函数无段概念):
#   - 键名仍由调用方过 _canonical_field_key 折算 (本函数不做别名归一化);
#   - 段限定由调用方的 in_current 把关 —— 不得在『当前任务』段外调用本
#     函数, 否则等于复活被刻意删除的「全文件扫一行式状态字段」fallback
#     (2026-10-03 减法: 该 fallback 掩盖过 worker 丢 `- ` 前缀的静默失败)。
#   - 缺 `- ` 前缀的容错**只在段内**发生: 段外无 `- ` 前缀的普通文本行
#     永不进入本函数 (调用方 continue 掉), 因此不违背上述减法决定。
#
# issue #139-F1 (2026-10-29): 代价特性 —— 本识别点必须有**线性上界**。
# 旧 pattern `^\s*[-*]?\s*([^:：]+?)\s*[:：]\s*(.*)$` 把三个相邻量词
# (`\s*` + `[-*]?` + `\s*` + 惰性 `([^:：]+?)` + `\s*`) 作用在同一段前导
# 空白 run 上 ⇒ 行内**无冒号**时, 引擎须对「空白 run 的每一种切分」逐一回溯,
# 成本实测呈超线性 (拟合幂次约 n^3.5~n^4.1, 随 n 区间而变; 400 空格单行
# ≈ 17.6 s; 属结算面 reaper 每 15 s 串行轮询的热路径 ⇒ 单行可把一轮结算
# 拖到分钟级)。这是本批 #139 引入的回归
# (基线 dc71a31 的 `^-\s*(?i:状态|status)[:：]\s*(.*)$` 强制行首字面 `-`,
# 无冒号行近零耗时失败)。
#
# 本次修复两处, 两者合起来把**整类**成本压到线性:
#   (1) 键名组 `([^:：]+?)` → `([^:：]+)` (惰性改贪婪): 键名组与冒号之间
#       不再留第二个相邻 `\s*` ⇒ 消除「键区内长空白 run」的二次回溯
#       (实测 'x'+3200 空格+'y: v': 156.7 ms → 0.032 ms);
#   (2) 键名组后**保留** `\s*` (不改写 pattern 语义): 键名与冒号之间的
#       空白仍被吃掉, 与旧实现逐字等价 (见 _match_field_line 内的说明)。
# 无冒号行的回溯由 _match_field_line 入口的**廉价短路**消除 (该 pattern
# 必含冒号才可能匹配 ⇒ 短路逐字等价), 无需在此处改 pattern。
#
# 成本守护: tests/test_issue139_todo_contract_robustness.py 的 T-H 组
# (深前导空白 + 无冒号行 / 耗时上界断言, 标准库 time.perf_counter 实测,
# 仓库无 pytest-timeout 依赖, 不引入新依赖)。
_FIELD_LINE = re.compile(r"^\s*[-*]?\s*([^:：]+)\s*[:：]\s*(.*)$")


def _match_field_line(ln: str) -> "tuple[str, str] | None":
    """识别『当前任务』段字段行, 返回 (键, 值) 或 None (issue #139)。

    容错形态: 行首缩进 / `-` 或 `*` bullet / 缺 `- ` 前缀 三种都识别。
    键、值各自 strip; 值可含冒号 (只切第一个)。段限定由调用方保证。

    issue #139-F1 (2026-10-29) 代价上界: 入口**廉价短路** —— 本 pattern
    必含冒号 (ASCII `:` 或全角 `：`) 才可能匹配, 故「行内无冒号」直接
    返回 None, 与旧实现**逐字等价**, 却彻底消除该形态下的超线性回溯
    (400 空格单行: 17.6 s → 0.002 ms)。短路是 O(n) 扫描, 不引入新依赖。
    """
    if ":" not in ln and "：" not in ln:
        return None                      # 无冒号 ⇒ pattern 必不匹配 (等价短路)
    m = _FIELD_LINE.match(ln)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _is_current_header(raw: str) -> bool:
    """段头字面 raw 是否『当前任务』段头 (中英别名都识别, issue #139)。

    收敛现散在 parse_status / current_binding / diagnose 三处的段头判定
    副本 (write_status_atomic 的副本属写面红线, 不动)。
    """
    zh, en = BILINGUAL_HEADER_ALIASES[CURRENT_HEADER]
    return raw == zh or raw == en


def _parse_kv(section_lines: List[str]) -> dict:
    """解析『当前任务』段字段行为 dict。value 可含冒号, 只切第一个。

    issue #139: 行识别收敛到 _match_field_line (缩进 / bullet / 缺 `- `
    前缀容错), 段限定由调用方保证 (全仓唯一调用点只喂当前任务段)。
    """
    out = {}
    for ln in section_lines:
        m = _match_field_line(ln)
        if m:
            out[m[0]] = m[1]
    return out


def _split_sections(text: str) -> dict:
    """按 '## ' 标题切段, 返回 {header: [lines]}。

    I18N-R3: 段头按 BILINGUAL_HEADER_ALIASES 反查回中文协议字面, 使
    旧调用方 `if CURRENT_HEADER in secs:` 仍命中 (中英段头都识别)。
    """
    sections = {}
    cur = None
    for ln in text.splitlines():
        if ln.startswith("## "):
            raw = ln[3:].strip()
            # 反查: 中文协议键 或 英文别名 → 中文协议键
            for cn_key, (zh, en) in BILINGUAL_HEADER_ALIASES.items():
                if raw == zh or raw == en:
                    cur = cn_key
                    break
            else:
                cur = raw  # 未知段头保留原样, 不误吃
            sections.setdefault(cur, [])
        if cur is not None and not ln.startswith("## "):
            sections[cur].append(ln)
    return sections


def _canonical_field_key(raw_key: str) -> str:
    """把任意语种键名折算到中文协议键 (BILINGUAL_FIELD_ALIASES 反查)。

    解析端唯一入口, 用途: 把 kv dict 键归一化到中文协议键, 后续 doc 构造
    与既有调用 `kv.get("状态", ...)` 完全无感 (中文键本身就是规范键)。

    大小写不敏感: `Status` / `STATUS` / `status` 都视为同一键。
    """
    # 大小写无关折算: 先按字面查, 再按 lower 查 (覆盖 `Status` 等大小写变体)
    if raw_key in BILINGUAL_FIELD_ALIASES:
        return BILINGUAL_FIELD_ALIASES[raw_key][0]
    lk = raw_key.lower()
    for k, (cn, en) in BILINGUAL_FIELD_ALIASES.items():
        if k.lower() == lk or en.lower() == lk:
            return cn
    return raw_key


# issue #152 + #139: 行号前缀容错 —— 剥 `^\s*\d+[|\t]` 两种形态 (竖线 / 制表符)。
#
# 为什么是「行级剥离」而不是「只在字段正则处剥」:
#   段头检测 (`## `) 与字段行检测是**两个独立环节**。只把前缀剥在字段
#   正则 (如 parse_status 的状态行正则) 上, 段头行 `3|## 当前任务` 仍然
#   匹配不上 `## ` ⇒ 循环体根本不进 ⇒ 状态行永远读不到。故必须**按行**
#   剥离, 让整份文件回到无前缀形态, 再交给既有解析规则。
#
# 为什么是「解析时容错」而不是「写时修复」 (issue #152 §建议修法 3):
#   写时修复会覆盖 worker 的原意 —— write_status_atomic() 是 worker 的
#   写入口, 在写入前把整份文件重写为无前缀形态, 等于平台替 worker 改写了
#   它交付的产物内容, 且写面是所有写入路径的共同入口, 影响面远超状态字段。
#   读侧容错 = 平台更宽容地理解 worker 写出的同义形态, 不改变磁盘上任何
#   字节 ⇒ TODO.md 写入面不变 (issue #152 红线第 1 条)。
#
# 为什么只影响读侧: 四个调用方 (parse / parse_status / current_binding /
#   verify_current_task_binding) 全是纯读函数, 写入口 write_status_atomic
#   不走本函数 ⇒ TODO.md 写入面不变。
#
# 容错范围**只**覆盖 `^[ \t]*\d+[|\t]` 两种行号前缀形态 (read_file /
#   nl -ba 的 `N|` 与 cat -n 的 `数字+制表符`)。issue #139 有意把制表符
#   形态纳入 (推翻 I 批 PLAN-I §10 Q-A 登记的遗留边界, 计划内变更);
#   段划分、字段名双键、pending/done 计数、状态值字面一律不放宽。
_LINE_NUM_PREFIX = re.compile(r"^[ \t]*\d+[|\t]")


def _strip_line_num_prefix(text: str) -> str:
    """剥掉每行的 `N|` / `N<TAB>` 行号前缀 (issue #152 + #139, 幂等)。

    认 `^[ \\t]*\\d+[|\\t]` 两种形态 —— read_file / nl -ba 的 `N|` 与
    `cat -n` 的 `数字+制表符`; 其它前缀形态 (如 `12:` / `12)`) 不在容错
    范围, 保持原样, 让既有解析规则照旧判定。

    快路径已移除 (issue #139): 旧实现 `if "|" not in text: return text`
    使 pattern 只对含竖线文本生效 (#153 登记的掩蔽坑) —— tab 形态输入
    无竖线, 保留快路径则 tab 容错静默无效。本实现**对每一行都应用**
    pattern, 不存在按字符短路的掩蔽路径。

    与 PLAN-K §1.2 字面「函数体只剩 splitlines+sub+join」的一处偏差:
    纯 join 会丢尾部换行, 与 152 守护文件 L194/L296 (钉「无竖线原文逐字
    返回原串」) 冲突, 而 §1.5 要求该文件除 tab 行外全部保持绿 ⇒ 取
    「无任何行被改动 ⇒ 逐字原样返回」的等价写法, 语义严格不宽于纯 join。
    """
    lines = text.splitlines()
    stripped = [_LINE_NUM_PREFIX.sub("", ln) for ln in lines]
    if stripped == lines:
        return text                      # 零改动: 逐字原样 (含尾部换行)
    return "\n".join(stripped)


def _normalize_kv(raw_kv: dict) -> dict:
    """kv dict 键名按 BILINGUAL_FIELD_ALIASES 归一化到中文协议键。

    解决: en 部署写 `status: working` / zh 部署写 `状态: 进行中` —— 两种
    形式经此函数后下游 `kv.get("状态", "")` 都命中。
    """
    out = {}
    for k, v in raw_kv.items():
        out[_canonical_field_key(k)] = v
    return out


def _render_lang() -> str:
    """渲染语言: 当前进程部署语言 (MW_LANG env), 未配置默认 en。

    I18N-R3: render() 按此选键名与状态值的语种。
    I18N-R3-FIX (2026-10-16, G3 守护回归): 直接读 env 而非 import 文案
    层 —— 协议字段不是文案, mw/todo.py 不得依赖 i18n 包 (守护测试
    test_todo_does_not_import_i18n)。语义与部署语言解析函数等价:
    非法值回退 "en" (en 是部署默认, conftest 钉 zh-cn 不受影响)。
    """
    lang = os.environ.get("MW_LANG", "en")
    return lang if lang in ("en", "zh-cn") else "en"


def _pick_field_key(cn_key: str) -> str:
    """按 _render_lang() 选 (中文键 / 英文键) 之一。cn_key = 中文协议键。"""
    lang = _render_lang()
    pair = _FIELD_CN_KEYS.get(cn_key)
    if not pair:
        return cn_key  # 未知字段保留原样
    return pair[1] if lang == "en" else pair[0]


def _no_result_marker() -> str:
    """『(无结果)』字面——按部署语言选 zh / en (§ I18N-R3 M-3)。"""
    lang = _render_lang()
    return "(无结果)" if lang == "zh-cn" else "(no result)"


def _pick_header(cn_key: str) -> str:
    """按 _render_lang() 选段头语种。"""
    lang = _render_lang()
    pair = BILINGUAL_HEADER_ALIASES.get(cn_key)
    if not pair:
        return cn_key
    return pair[1] if lang == "en" else pair[0]


def parse(path: Path) -> Optional[TodoDoc]:
    """读 TODO.md 解析成 TodoDoc。文件不存在返回 None。

    I18N-R3: 段头/字段名/状态值都按 BILINGUAL_*_ALIASES 双键识别, 解析
    端零代码变动 (键归一化到中文协议键后, 既有 kv.get("状态", ...) 仍命中)。
    """
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    # issue #152: 行级剥离行号前缀 (容错口径见 _strip_line_num_prefix 注释),
    # 段划分/字段名/pending 计数语义零放宽 —— 剥完交给既有规则。
    text = _strip_line_num_prefix(text)
    # title 从 '# Team Work: xxx' 取
    title = ""
    m = re.search(r"^#\s+Team Work:\s*(.+)$", text, re.MULTILINE)
    if m:
        title = m.group(1).strip()
    secs = _split_sections(text)
    doc = TodoDoc(title=title)
    if CURRENT_HEADER in secs:
        kv = _normalize_kv(_parse_kv(secs[CURRENT_HEADER]))
        if kv:
            req_files = [s.strip() for s in kv.get("产出文件", "").split(",")
                        if s.strip()]
            doc.current = TodoTask(
                step_id=kv.get("步骤", ""),
                title=kv.get("标题", ""),
                goal=kv.get("目标", ""),
                assignee=kv.get("承接方", ""),
                product_workspace=kv.get("产物目录", ""),
                required_files=req_files,
                status=kv.get("状态", "进行中"),
                result=kv.get("结果", RESULT_PLACEHOLDER),
                # D1 双键名回退: kanban_task 优先, 缺失认 `卡` (本链实际格式)
                kanban_task_id=(kv.get("kanban_task") or kv.get("卡", "")),
            )
    if PENDING_HEADER in secs:
        # 待执行段: 每项 "- [step_id] title → assignee" + 缩进子字段
        cur_pending: Optional[TodoTask] = None
        for ln in secs[PENDING_HEADER]:
            s = ln.strip()
            if not s or s == "(无)":
                continue
            m = re.match(r"^-\s*\[([^\]]+)\]\s*(.*)$", ln)
            if m:
                step_id = m.group(1).strip()
                rest = m.group(2).strip()
                title, assignee = rest, ""
                if "→" in rest:
                    title, assignee = [x.strip() for x in rest.split("→", 1)]
                cur_pending = TodoTask(step_id=step_id, title=title,
                                     goal="", assignee=assignee)
                doc.pending.append(cur_pending)
            elif cur_pending is not None:
                sm = re.match(r"^\s+([^:：]+)[:：]\s*(.*)$", ln)
                if sm:
                    k = sm.group(1).strip()
                    v = sm.group(2).strip()
                    # I18N-R3-FIX (2026-10-16, H-3): pending 子字段键先过
                    # _canonical_field_key 折算到中文协议键, 与 current 段
                    # 同样双键识别. 否则 en 部署 render → parse 回环时
                    # 全部丢失 (实测 rotate_next → 丢工作目录, 撞 REQ §8
                    # 风险表首行).
                    cn_key = _canonical_field_key(k)
                    if cn_key == "目标":
                        cur_pending.goal = v
                    elif cn_key == "产物目录":
                        cur_pending.product_workspace = v
                    elif cn_key == "产出文件":
                        cur_pending.required_files = [x.strip() for x in v.split(",")
                                                    if x.strip()]
                    elif cn_key == "kanban_task":
                        # pending 段保留 kanban_task_id, 升 current 时仍可用
                        # (D1: `卡` 键同样认, 但 kanban_task 已存在则不覆盖)
                        if k == "kanban_task" or not cur_pending.kanban_task_id:
                            cur_pending.kanban_task_id = v
    if DONE_HEADER in secs:
        doc.done = [ln for ln in secs[DONE_HEADER] if ln.strip().startswith("-")
                   and ln.strip() != "- (无)"]
    if DROPPED_HEADER in secs:
        doc.dropped = [ln for ln in secs[DROPPED_HEADER] if ln.strip().startswith("-")
                      and ln.strip() != "- (无)"]
    return doc


def write(path: Path, doc: TodoDoc) -> None:
    """把 TodoDoc 渲染写回文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc.render(), encoding="utf-8")


def parse_status(path: Path) -> str | None:
    """轻量读取 TODO.md『当前任务』→『状态』字段。

    reaper 每 15s 轮询 PENDING 卡时调用, 只看『状态』一行就知道要不要
    结算, 不解析整份文档。

    统一格式 (2026-10-03 减法): 只认『## 当前任务』段内的状态字段行。
    文件不存在 / 没有当前任务段 / 没有状态行 → 返回 None
    (reaper 视为未知, 走保守等待)。
    旧的"全文件扫一行式状态字段" fallback 已删 —— 它掩盖过 worker 写错
    格式 (丢 `- ` 前缀) 的静默失败, 统一格式后不再需要。
    ★ issue #139 的「缺 `- ` 前缀」容错**不违背**该决定: 容错严格限定
    『当前任务』段内 (段外行根本不进识别点), 不是全文件扫描; 段内识别
    与 parse / current_binding 的既有宽松度对齐, 消除入口间不一致。

    I18N-R3 (2026-10-16): 段头与键名都按双键别名识别 —— en 部署写
    `## Current Task` + `- status: working` 也正常读。返回值原样 (reaper
    端 lower 后判 in_progress_set / terminal_set)。

    兼容性: 枚举值大小写不敏感 (reaper 端 lower); 其他值原样返回
    (便于 main 写自定义状态)。

    issue #152 (2026-10-29): 行号前缀容错 —— worker 用带行号的读法抓全文
    原样写回时, 每行形如 `12|- 状态: completed`。解析前做**行级**剥离
    (见 _strip_line_num_prefix), 段头与状态行同时恢复 ⇒ 返回**正确状态值**
    而非 None。只影响读侧, 写入面不变。

    issue #139 (2026-10-29): 段内字段行识别收敛到 _match_field_line ——
    缩进 / `*` bullet / 缺 `- ` 前缀 三种形态与 parse / current_binding
    同口径识别 (四入口一致); 容错仍限段内, 状态值仍原样返回。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    text = _strip_line_num_prefix(text)      # issue #152 + #139
    in_current = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            in_current = _is_current_header(s[3:].strip())
            continue
        if not in_current:
            continue
        # 双键 + 大小写不敏感: `状态:` / `status:` / `STATUS:` / `sTaTuS:`
        # 都识别 (§ I18N-R3 M-1, 经 _canonical_field_key 折算)。
        m = _match_field_line(ln)
        if m and _canonical_field_key(m[0]) == "状态":
            v = m[1]
            return v if v else None
    return None


def write_status_atomic(path: Path, status: str) -> bool:
    """worker 用: 原子写入 TODO.md 的『状态』字段。

    实现: 读全文 → 替换『当前任务』段里第一处『- 状态: ...』→ 写 .tmp
    → rename 覆盖。避免 reaper 读到半截写入。

    返回 True = 成功, False = 没找到『当前任务』段 / 没找到状态行
    (后者说明 TODO.md 不是 mw 模板生成的, 让 worker 用 full rewrite)。

    I18N-R3 (2026-10-16): 段头与键名双键识别, `## Current Task` /
    `- status: ...` 也命中。写入保持输入字面 (worker 写啥写啥, 不翻译)。

    注意: 这是『只改一行的精简路径』, 只动状态字段不动其他。worker
    改『结果』字段走另一条路径 (parse→改 result→write 全文), 那是
    全量更新; 状态字段高频更新, 单行更安全。
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    in_current = False
    replaced = False
    out_lines = []
    for ln in lines:
        s = ln.rstrip("\n")
        stripped = s.strip()
        if stripped.startswith("## "):
            raw = stripped[3:].strip()
            in_current = any(raw == zh or raw == en
                             for zh, en in BILINGUAL_HEADER_ALIASES.values()
                             if zh == CURRENT_HEADER)
            out_lines.append(ln)
            continue
        if in_current and not replaced:
            m = re.match(r"^(\s*-\s*(?:状态|status|Status)[:：]\s*).*$", s)
            if m:
                out_lines.append(f"{m.group(1)}{status}\n")
                replaced = True
                continue
        out_lines.append(ln)
    if not replaced:
        return False
    new_text = "".join(out_lines)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, p)  # atomic rename on POSIX
    except OSError:
        return False
    return True


def init_todo(path: Path, title: str, first_task: TodoTask) -> TodoDoc:
    """初始化 TODO.md, 写入第一个当前任务。"""
    doc = TodoDoc(title=title, current=first_task)
    write(path, doc)
    return doc


def init_from_steps(path: Path, title: str, tasks: List[TodoTask]) -> Optional[TodoDoc]:
    """从整条链的 task 列表播种 TODO.md。

    第一个 task 成『当前任务』, 其余进『待执行』。这是 Team Work 的单一
    真相源: main 下发时一次性写全, worker 读当前任务, main 回收后推进。
    tasks 为空返回 None。
    """
    if not tasks:
        return None
    doc = TodoDoc(title=title, current=tasks[0], pending=list(tasks[1:]))
    write(path, doc)
    return doc


def bind_kanban_tasks(path: Path, step_to_task: dict) -> Optional[TodoDoc]:
    """建卡后回填: 把每个 step_id 对应的 kanban task_id 写进 TODO.md。

    覆盖『当前任务』和『待执行』里所有匹配 step_id 的 task。这样 reaper 读
    TODO.md 时, 能把『当前任务』精确对上正在 running 的那张 kanban 卡,
    确定性识别"这张卡的 worker 写完了", 不依赖 LLM 轮询。
    """
    doc = parse(path)
    if doc is None:
        return None
    if doc.current and doc.current.step_id in step_to_task:
        doc.current.kanban_task_id = step_to_task[doc.current.step_id]
    for t in doc.pending:
        if t.step_id in step_to_task:
            t.kanban_task_id = step_to_task[t.step_id]
    write(path, doc)
    return doc


def rotate_to(path: Path, task_id: str) -> Optional[TodoDoc]:
    """确定性推进: 仅当『当前任务』正是 task_id 这张卡时, 归档并提升下一张。

    main 回收上一步结果后调用 (worker 已退出, 无并发覆盖)。
    幂等: 若 current 的 kanban_task_id 已不是 task_id (已推进过 / 不是
    这张卡), 直接返回 None 不动文件。
    """
    doc = parse(path)
    if doc is None or doc.current is None:
        return None
    c = doc.current
    if (c.kanban_task_id or "") != task_id:
        return None  # 不是这张卡 / 已推进过, 幂等跳过
    result_txt = c.result if c.has_result else _no_result_marker()
    doc.done.insert(0, f"- [{c.step_id}] {c.title} → {result_txt}")
    doc.current = doc.pending.pop(0) if doc.pending else None
    write(path, doc)
    return doc


def rotate_next(path: Path) -> Optional[TodoDoc]:
    """把当前任务(含结果)归档到已完成, 从『待执行』取下一个当当前任务。

    main 回收上一步结果后调用。待执行为空则链结束 (当前任务清空)。
    """
    doc = parse(path)
    if doc is None:
        return None
    if doc.current is not None:
        c = doc.current
        result_txt = c.result if c.has_result else _no_result_marker()
        doc.done.insert(0, f"- [{c.step_id}] {c.title} → {result_txt}")
    if doc.pending:
        doc.current = doc.pending.pop(0)
    else:
        doc.current = None
    write(path, doc)
    return doc


def current_binding(path) -> Optional[dict]:
    """轻量读『当前任务』段绑定: {'step_id','kanban_task','status','title'}。

    文件不存在 / 无当前任务段 / 段内无任何 kv 行 / 读失败 → None。
    只扫当前段, 不解析 pending, 与 parse_status 同量级开销。
    (REOPEN-DESIGN D2: 供 advance/launch 派发前机器校验用, 纯读无副作用。)

    I18N-R3 (2026-10-16): 段头与字段名双键识别 (中英段头 / 中英键名都
    通吃), kv dict 归一化到中文协议键后, 既有 kv.get("状态", "") 仍命中。

    issue #152 (2026-10-29): 与 parse_status 同口径的行号前缀容错 (行级
    剥离, 见 _strip_line_num_prefix); verify_current_task_binding 经本函数
    继承容错, 三种 reason 语义不变 —— 四入口行为一致。

    issue #139 (2026-10-29): 字段行识别收敛到 _match_field_line, 与
    parse_status 同口径 (缩进 / bullet / 缺 `- ` 前缀 段内容错)。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # REOPEN-FIX P1-2: UnicodeDecodeError 是 ValueError 子类不是 OSError,
        # 非法 UTF-8 字节必须同样归入 todo_missing fail-closed —— 否则异常逃逸
        # 使 launch 跳过 ABORTED 回滚留 ACTIVE run、advance 返 500 非 409。
        return None
    text = _strip_line_num_prefix(text)      # issue #152 + #139
    in_current = False
    raw_kv: dict = {}
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            # 遇到下一个 '## ' 段即结束当前段扫描
            raw = s[3:].strip()
            in_current = _is_current_header(raw)
            if not in_current and raw_kv:
                break
            continue
        if not in_current:
            continue
        m = _match_field_line(ln)
        if m:
            raw_kv[m[0]] = m[1]
    if not raw_kv:
        return None
    # 键名归一化到中文协议键 (BILINGUAL_FIELD_ALIASES)
    kv = _normalize_kv(raw_kv)
    return {
        "step_id": kv.get("步骤", ""),
        # D1 加 kanban_task_id 英文别名: 三键同源, 优先级 kanban_task > kanban_task_id > 卡
        "kanban_task": (kv.get("kanban_task")
                        or kv.get("kanban_task_id")
                        or kv.get("卡", "")),
        "status": kv.get("状态", ""),
        "title": kv.get("标题", ""),
    }


def verify_current_task_binding(path, task_id: str,
                               step_id: Optional[str] = None) -> tuple:
    """机器校验 TODO.md『当前任务』已绑定本卡 (REOPEN-DESIGN A面核心)。

    放行条件 (任一, 只查身份不查状态 — D5: 同卡 completed 重派合法):
      a) binding.kanban_task == task_id
      b) step_id 非空 且 binding.step_id == step_id
    拒绝 reason 码:
      todo_missing — 文件不存在 / 无当前任务段 / 读失败 (fail-closed)
      no_binding   — payload 无 step_id 且 TODO 无任何绑定键 (A-AC5)
      mismatch     — 绑定指向他卡/他步
    拒绝时 detail 必齐: {reason, expected_task_id, expected_step_id,
      actual_kanban_task, actual_step_id, actual_status, todo_path}
    纯函数: 不落任何标记, 校验被拒后可随时重试。
    """
    todo_path = str(path)
    binding = current_binding(path)
    if binding is None:
        return False, {
            "reason": "todo_missing",
            "expected_task_id": task_id,
            "expected_step_id": step_id or "",
            "actual_kanban_task": None,
            "actual_step_id": None,
            "actual_status": None,
            "todo_path": todo_path,
        }
    bound_task = binding["kanban_task"]
    bound_step = binding["step_id"]
    if bound_task and bound_task == task_id:
        return True, {}
    if step_id and bound_step and bound_step == step_id:
        return True, {}
    # 双方都拿不出可比对的身份 → fail-closed (A-AC5)
    if not bound_task and not (step_id and bound_step):
        return False, {
            "reason": "no_binding",
            "expected_task_id": task_id,
            "expected_step_id": step_id or "",
            "actual_kanban_task": bound_task or None,
            "actual_step_id": bound_step or None,
            "actual_status": binding["status"] or None,
            "todo_path": todo_path,
        }
    return False, {
        "reason": "mismatch",
        "expected_task_id": task_id,
        "expected_step_id": step_id or "",
        "actual_kanban_task": bound_task or None,
        "actual_step_id": bound_step or None,
        "actual_status": binding["status"] or None,
        "todo_path": todo_path,
    }


def diagnose(path) -> dict:
    """issue #139 可观测入口: 读状态并说明读不出的原因。纯读零副作用。

    背景: 状态读不出时 reaper 走保守等待 / fail-open 收尸, 全程零诊断
    信息。本函数给调用方 (测试 / main 排障) 一个可区分的 reason, 不接入
    reaper 结算路径、不加运行时告警 ⇒ 零噪音 (PLAN-K §1.2 改动 6 裁决)。

    返回 {"status": <原样状态值或 None>, "reason": <以下之一>}
      file_missing       — 文件不存在 / OSError / 非法 UTF-8 (与 current_binding
                           的 todo_missing fail-closed 口径对齐)
      no_current_section — 无『当前任务』段 (含中英别名)
      no_status_line     — 段内无状态字段行
      unrecognized_value — 有状态行但值为空或 normalize_status 归一化不了
      ok                 — 正常读出
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"status": None, "reason": "file_missing"}
    text = _strip_line_num_prefix(text)
    in_current = False
    seen_current = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            in_current = _is_current_header(s[3:].strip())
            seen_current = seen_current or in_current
            continue
        if not in_current:
            continue
        m = _match_field_line(ln)
        if m and _canonical_field_key(m[0]) == "状态":
            v = m[1]
            if not v or normalize_status(v) is None:
                return {"status": v or None, "reason": "unrecognized_value"}
            return {"status": v, "reason": "ok"}
    if not seen_current:
        return {"status": None, "reason": "no_current_section"}
    return {"status": None, "reason": "no_status_line"}


def rotate(path: Path, next_task: Optional[TodoTask]) -> Optional[TodoDoc]:
    """把当前任务(含结果)归档到已完成, 写入下一个当前任务。

    main 回收上一步结果后调用: 上一步进"已完成", next_task 成"当前任务"。
    next_task=None 表示链结束 (当前任务清空)。
    """
    doc = parse(path)
    if doc is None:
        return None
    if doc.current is not None:
        c = doc.current
        result_txt = c.result if c.has_result else _no_result_marker()
        doc.done.insert(0, f"- [{c.step_id}] {c.title} → {result_txt}")
    doc.current = next_task
    write(path, doc)
    return doc
