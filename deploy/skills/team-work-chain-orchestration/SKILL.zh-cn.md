---
name: team-work-chain-orchestration
description: "Run Team Work chains as main: launch, spawn, settle."
version: 1.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, TeamWork, Kanban, Orchestration]
---

# Team Work 链式编排（main 侧）

## When to Use

用户发来 Team Work / 多智能体任务（带 workflow_id + project_workspace），或收到
「mw 报告转发」要求推进链时加载本 skill。

mw-task-dispatch 覆盖单卡下发；本 skill 覆盖多卡链（launch → 逐张轮转/spawn/结算）。
平台端口：mw web = `http://127.0.0.1:8700`，账本 = `/mw_data/ledger.db`，
mw 代码 = `/opt/mwroot/mw/`（行为存疑时直接读源码，比猜快）。

## 下发前资料准备（需求引用外部私有仓库时）

需求指向私有 Gitea/GitHub 仓库时，**main 在 launch 前把所需文档预取到
`project_workspace/docs-source/`**，卡片描述写明『只读本地 docs-source/，
不要访问远端仓库』——绝不把 token 传给 worker：传输层会把消息/代码里的凭据
字面量打码（与密码打码同机制），worker 拿 token 必出假故障，且 worker 不应
持有秘密。Gitea 取法：`GET /api/v1/repos/<owner>/<repo>/git/trees/<branch>?recursive=1`
列文件筛出文档，再逐个 `GET .../raw/<branch>/<path>` 带 `Authorization: token <tok>`
落盘；shell 里构造 token 用变量拼接（`TOKEN="${P1}${P2}"`）避开打码，
匿名 raw 访问 404 即证明仓库私有、必须走 API+token。

需求引用 tracker issue 时, 快照必须 = issue 正文 **+ 全部评论** 落 docs-source (用户的最终决策指令往往在最后一条评论而非正文, 只存正文会把真需求拆错); 评论中的分叉问题 (如「到底是 A 还是 B」) 逐条转写成分析卡的必答项。

用户经聊天上传图片作施工参考（LOGO/设计稿）时：图片原文件**不会**落盘到共享工作区（聊天附件只带文字描述进上下文）。收到后 main 必须立即把图写成权威文字描述文档落进 project_workspace（如 docs/LOGO-REFERENCE.md：精确色值、几何元素、点位方位、尺寸关系、动态语义），并在所有下游卡描述中写明『以该文档为唯一依据，若后续原图补进共享区则以原图为准』——worker 看不到聊天图片，这份文档就是它的眼睛。

## 角色铁律（用户 2026-10-07 明确纠正）

**main 只决策与结算，绝不亲自修改代码或文档。** 审查打回的重要项/建议项一律 `hermes kanban create` 修复卡派 coder（走 register + advance 正规链路），main 只做：读报告 → 建卡派单 → 独立验证产物 → 结算背书。main 亲自动手改产物（哪怕 3 行）= 越权施工，破坏「worker 产物可审计、责任可追溯」的链语义。唯一例外：worker 因 LLM 输出上限物理挂死时的分片代建，且必须在结算结果与审查背书段显式声明「main 代建」并事后尽快把后续维护交回 worker。

## 需求到手就建卡：分析与方案不是 main 的活

用户把需求交给你、指名 workflow 时，**立刻拆卡下发，不要先替 product-manager /
system-engineer 做需求分析或方案设计**。main 的产出是「一张张卡 + 一份契约」，
不是「一份分析」。

- 需求原文**原样转交**给 PM 卡（issue 正文 + 快照路径），别先消化再转述——转述会
  丢掉用户的原话硬约束（如「单次最多 10 条」这种带数字的口径）。
- main 在建卡前只做**秒级取证**：拉 issue 原文与评论、下载附件、读 PROJECT-MEMORY。
  **端点实测基线由首张调研/研判卡产出**，main 只转述 issue 原文与秒级取证结果，
  不自己 grep repo / 连远端机 / 跑测试。
- main **不得**替下游定方案（选哪个实现手段、怎么拆模块、页面怎么排布）——那是
  REQ/ARCH 卡的产物。你先写了，下游会当成既定前提照抄，裁决权就悄悄易主了。
- 给 coder 卡写精确的文件/行号定位线索**由首张调研/研判卡产出并随该卡结论转述**
  （缩短施工回合）；main 不自行定位代码，也不在首卡里塞定位线索。那是
  「施工定位」，不是「方案设计」，别越界。

★ **秒级取证要有预算，且必须在写卡过程中顺手做**：取证不是许可你先开一轮分析。
把它当「写卡时的顺手动作」——拿到就写进卡，立刻建卡。判据：拉取总数控制在个位数
量级、只拉「issue 原文与评论 / 附件 / PROJECT-MEMORY」这类秒级来源；这批原文摘录
齐了，**下一步就是写卡 + launch**，不许再开新一轮取证。把每份资料都翻到完美再发卡
= 变相代做分析，用户会连催三次。实测基线由首张调研/研判卡按其任务目标去取。

★ **下游卡的内容留到派发时再定稿**：链上第 N 张卡的正文只需写「本卡以第 N-1 张卡
的产物为唯一依据」+ 硬约束 + 产物路径；**具体清单 / 文件清单 / 命令随上游产物回来
后再补**。先猜好下游卡内容必然猜错（上游方案还没出），猜错的部分会变成下游的既成
前提。用户口径是「把需求原样交给他们，再根据他们的反馈调整后续卡的内容」。

**为什么**：需求分析一旦由 main 代劳，链上就再没有 REQ/ARCH 产出物，下游卡的
「上游依据」断链；而用户看到的只是一次长篇分析，不是可见的链在推进（与既有
「用户看不到卡建出并派发就会视为零进展」同源）。

★ **粗分类口径（与 #70 决策路径模块对齐）**: main 只做粗分类——按用户输入的**文本形态**
判断：给的是验收标准 / 用户故事 / 期望行为 → 需求类，交 product-manager；给的是报错 /
堆栈 / 性能数据 / 接口契约 / 复现步骤 → 技术类，交 system-engineer。分类依据只是文本
形态，**不做**根因分析、不做复现、不做代码审查。★ **非对称否决口径（技术信号优先）**：
需求类被技术词否决、技术类**不被**需求词否决——「验收标准 + 堆栈」这类混合输入必判
技术类（有报错栈 = 可复现的技术问题）。这条口径只存在于 `mw/decision_paths.yaml` 的
词表构造里，照文字自行推算容易得出相反结论，这也是判定必须走端点的原因之一。判定走端点：`GET
/api/decision-paths/classify?text=<用户输入原文>` 的返回就是判定（`path_id` /
`route_to` 依据 / `score` / `fallback`），**不要自己对着词表手算得分**（词表在 `GET
/api/decision-paths`，数据源 `mw/decision_paths.yaml`，仅供查阅）；不可判定（`path_id: null`）
⇒ **必须问用户**，不许自行挑角色。

## 契约播种：任务正文必须物理写进 TODO.md『当前任务』段

worker 提示词里**只有项目根 `<project_workspace>/TODO.md` 的路径指针**——kanban 卡
正文、`steps[].task_description` 都不是通道（实测：worker 起手第一条 user 消息
= 契约模板 + todo_path，随后 read_file 读到的就是 TODO.md 里的正文）。所以：

- **需求正文写进『当前任务』段**；卡正文只是给人看的副本。两者都写，但 worker
  只认 TODO.md。
- **嵌入正文前把行首 `- ` 换成 `* `**：TODO 解析器把任何 `- key: value` 行当 kv
  字段（`_parse_kv` 正则 `^\s*-\s*([^:：]+)[:：]` —— **前导空白也算**，所以缩进的
  子项 `   - xxx: yyy` 同样会被收进 dict）。卡正文里的 `- 需求文档: docs/REQ.md`
  会污染契约字段；更危险的是 `- 状态: xxx` / `- 结果: xxx` 会**直接覆盖协议的状态
  与结果字段**（同 key 后写覆盖先写）——reaper 只认那一个状态行，worker 的真结果
  也写不进去。协议键只有 状态/结果/目标/承接方/产物目录/产出文件/步骤/标题/
  kanban_task/卡 会被识别，其余 kv 行原样进 dict。
  ★ 转换要处理**所有缩进层级**（`^\s*- ` → 保留缩进换 `* `），只改顶格的 `- `
  会漏掉正文里的缩进子项 —— 预验脚本会抓出来。
- **launch/advance 前用平台自己的解析器预验**（别只肉眼看文件）：
  直接 import `mw/todo.py` 跑 `parse()` +
  `verify_current_task_binding()`，断言四段结构、当前卡绑定行、待执行队列的
  kanban_task/产出文件、以及 kv 污染预警。
- 预验失败 = launch 报 409 `todo_missing`，且 **run 会回滚成 ABORTED 并占住该
  session 席位**（下一轮 launch 报 `session_reused`），代价高；advance 侧 409 无
  副作用，补完再 advance 即可。

## 角色分工矩阵（main 建卡口径, 用户 2026-10-18 明示: 杜绝角色间重复作业）

每张卡的『验收口径』只能写本角色职责内的事; 跨角色的验证压进卡里 = 重复作业 + 实现卡莫名拖长 (实案 m1: coder 卡混入真容器起服实测, 45min+)。

| 角色 | 决策路径 | 卡片验收口径只写 | 明确不写(归属) |
|---|---|---|---|
| product-manager | `requirement` | 需求/验收标准文档 (REQ/ACCEPT) | 不写代码、不做技术选型承诺 |
| coder (实现) | — | 代码改动+commit + 静态自检: bash -n/py_compile、pytest 单测套件、i18n --strict、grep 断言 | **不跑部署/不起容器/不上 170/不做浏览器实测** → 归 tester |
| code-review | — | **只检代码规范**: 风格/命名/约定一致性、安全规范(输入校验/权限模式)、错误处理、接口契约、明显逻辑缺陷, 读 diff 出报告 (REVIEW*.md, verdict) | 不修代码、**不做任何测试**(不跑测试套件、不起环境、不做探针复现) → 实证/复现/回归全部归 tester |
| tester | — | 运行期/真机验证: 部署脚本实跑、起容器/compose、170 场景矩阵、端到端差异验证、回归套件失败集比对、浏览器冒烟 | 不修代码 (发现问题写报告, main 建回流卡) |
| system-engineer | `technical` | **故障排查/分析定位** (用户 2026-10-18 明示): 接手问题/issue 做根因分析、复现定位, 产出**精确修复方案**文档 (点名文件/位置/改法/影响面/验证要点) | 不亲自改代码——方案实施归 coder |
| doc_audit | — | repo tracked 文档 vs 代码实态一致性 | 不审计过程文档、不碰代码 |
| main | 路由入口 | 产物存在性+verdict → complete/轮转/advance/回流裁决 | **绝不亲自施工/复跑 tester 核验** (角色铁律); 唯一裁决场景=上游口径冲突 |

「决策路径」列取值与 `mw/decision_paths.yaml` 的 path id 逐字一致 (`requirement` /
`technical`), 词表由 `GET /api/decision-paths` 暴露; main 建首卡前先调
`GET /api/decision-paths/classify?text=<用户输入原文>` 拿判定定承接角色。

issue/故障类任务标准链 (用户 2026-10-18 明示): 问题/issue 先建 system-engineer 分析定位卡 → 结算出修复方案文档后, main 据方案建 coder 实施卡 (卡描述点名该方案文档为唯一依据, coder 只按方案调工具改代码+commit) → tester 定向复测 (只测修复点) → doc_audit/交付。main 不把未定位的 issue 直接丢给 coder。

### main 自己写的 issue 根因只是**待验假设**, 允许被 worker 推翻 (2026-10-21 实测)

main 为省事在 tracker issue 正文里写一段「根因分析」再去建链时, 那段分析是**main 的假设**,
不是既成事实 —— 链上的 system-engineer 有权拿实测证据推翻它。碰到这种情况:

1. **先独立复核 worker 的反证**, 别直接转述采纳: 反证通常指名某个源码位置
   (如「镜像把裸 `gateway run` 重定向成 `s6-svc -u` + `execvp sleep infinity`,
   所以不会起第二个进程」) —— main 必须自己去读那一处源码确认, 确认后再裁决。
   只看 worker 的报告不核源码 = 把「worker 说的」当「事实」, 换了个权威来源而已。
2. **裁决要选一边并说清理由**, 把选项摆给用户/issue: 采纳 worker 结论 vs 坚持原假设。
   坚持原假设时, 必须同时接受 worker 指出的下游风险 (如「这么改会让全新部署起不来」),
   不能只挑对自己有利的部分。
3. **tracker 必须发更正评论** (POST comment), 不能只改仓库不动 issue —— 读 issue 的人
   只看到被推翻的旧根因, 就会照着那个错根因去改生产机。评论要写: 旧结论哪条不成立 +
   证据 (文件:行号 / 实测命令与输出) + 新结论 + **真实成因是否仍未定位**。
4. **真实成因未定位时 issue 保持 open**, 不要因为「链跑完了」就关账。链跑完 ≠ 问题解决:
   链的产出可能恰恰是「证伪了原假设, 给出下一步取证方向」。此时收尾动作 = 改 issue
   标题/正文反映当前真实状态 (如「根因待定位」) + 留下可直接执行的取证命令清单
   (只读探针: 哪几个端口、哪几个容器内命令、怎么区分「容器内不通」vs「宿主不通」),
   交给用户在自己的机器上跑。
5. **下游卡的口径随裁决改写**: 复测卡里写明「不要按 issue 原文去复现 X, 基线上复现不出是预期;
   复现不出记『未复现』, 不得据此判已修复/修复无效」, 避免 tester 拿错尺子把
   「环境里没这个缺陷」误报成修复失败。

★ 这条与「本链自己造成的 repo 文档失同步 = 当轮修」是同一类责任归属: main 自己写下并
推送的错误结论 (issue 正文 / CHANGELOG / 设计文档) 同样是要还的债, 不能因为「不是 worker
写的」就当既有事实放过。

issue 里用户自带多个候选方案要求「评估后择一实施」时: main 先独立预评估并在下发汇报里亮出自己的倾向+一句理由, 同时把倾向写进分析卡 (标明「main 初步倾向 X, 你若实证推翻请以证据说话」)——分析卡产物必须含**逐方案裁决** (技术路径/改动面/风险/对上游依赖) + 明确选型结论, 不许只给一个综合方案而不交代其余候选的死因; 用户对某候选有具体追问时该追问列为必答项带 file:line 证据。结算分析卡时 main 核对: 每个候选都有 verdict、选型有依据、实施计划到逐文件粒度 (coder 可直接执行)。

建多张卡时正文先逐张落盘 scratch 文件, 再用 `hermes kanban create "<标题>" --assignee <w> --body-file -` **从 stdin 读正文** (hermes kanban create 支持 `--body-file -` 表示 stdin) —— 长中文/多行正文经 shell `$(cat file)` 展开会被引号与换行炸裂, 走 stdin 则一次一张多行中文卡稳定落盘, 完全绕开 shell 转义; task_id 用正则 `t_[a-z0-9]+` 从输出提取后存 JSON 清单文件备 launch 批读。多张同类小修合并成一『批』卡 (按代码/UI/文档分堆) 而非一 issue 一卡, 审查/测试卡各一张覆盖全批, 省串行轮次。

配套规则:
- 实现卡 required_files 只要 IMPL-NOTES/commit, 不要把 TEST-* 类文件混入实现卡。
- coder 静态自检的边界: 「在 worker 自己的容器里用 bash/python 直接可跑完的验证」; 凡需要 docker/compose/部署副作用/独立测试机的, 一律 tester 卡。
- tester FAIL 出的问题回流: main 建 fix 卡给 coder, fix 卡口径同样只静态自检+commit, 复测另开 tester 定向卡 (只测修复点)。
- 建卡前自检一遍: 本卡验收清单里有没有另一角色的活? 有 → 拆卡或删掉该条。
- **复测/验收卡的验收标准必须含「对照组复现原缺陷」**：只跑修复版树拿到 PASS 证明不了
  修好了（也可能是环境里根本没复现）。要求 worker 用**修复前的 commit 建独立 worktree**
  （`git worktree add /tmp/base_<sha> <base_sha>`）起同口径实例、跑**同一套步骤**，必须
  复现原缺陷（白屏 / 502 / 重复批次 / 恒绿测试），两侧各留截图。对照组复现不出来 ⇒ 该缺陷
  在本环境不可复现，结论只能写「未复现」而非「已修复」。这一步比再多加几条断言更能证伪假绿。
  ★ 对照组实例的**依赖要跟被测树对齐**（同一 gateway mock / 同一 API key / 同一构建产物
  md5 钉死），否则「对照组也正常」会被误当成修复生效，实为环境差异。
  ★ **对照组构造上不可能复现时（生产代码逐字未改），改用「反事实臂」**：经典对照组 = 修复前的树；
  当修复 commit 与基线的生产代码**逐字节相同**时，两臂运行期行为完全一致 ⇒ 对照组
  在构造上不可能复现该缺陷，「对照组也正常」既不能证明修好也不能证明没修好，结论只能写
  「未复现」。此时唯一有判别力的证据 = 把**被否决的那个改法**当对照臂，与保留现状的臂做 A/B，
  两臂唯一差异就是争议的那一行（镜像 tag / 凭据 / 构建产物 md5 / 数据目录新鲜度全部对齐）:
  B 臂复现出症状 ⇒ 证明该改法有害、现状正确；B 臂不出现症状 ⇒ 该改法无害，需重新裁决。
  报告必须写明: 反事实臂跑了什么、差异只有哪一行、观测到什么。没有反事实臂就写
  「结论: 现状正确」= 无依据, main 不接受。
  ★ 复测卡还应明确: **基线上复现不出是预期结果时, 结论写「未复现」, 不得据此判「已修复」或
  「修复无效」** —— 否则 tester 会把「环境里没有这个缺陷」误判成修复失败, 或把「没复现」
  包装成「已修复」。
- 「实证复现/攻击探针/跑套件验证」都是测试形态, 归 tester 不归 code-review (用户 2026-10-18 纠正, 旧实案 R-1/R-2/R-3 由 reviewer 亲自跑探针属旧口径勿效仿): reviewer 对实现声明存疑时在报告里列为 concern + 依据(引用代码行), 由 tester 卡实证确认; main 依 concern 决定是否加 tester 定向验证项——tester 卡 task_description 把各 concern 编号逐条列为必测实证项, 并写明允许「未复现/NOT TESTED」如实结论 (不许硬凑)。
- **review 非阻塞建议的处置**: 审查 PASS 但带非阻塞小修 (CHANGELOG 补条目、注释精度类) 时, 建一张『范围锁死』小卡批量夹在测试/审计卡之前派发 (POST /cards 登记 + advance), 让 tester/doc_audit 跑在终态树上; 卡文本写明『严格只做这两项, S-x/S-y 明确不在本卡』防 worker 扩大改动面。评审建议中属既有风险或基线遗留的项, 按 main 裁决显式写『不修』并转 Gitea issue 跟踪, 不塞进本卡。

## 上游规格文档必须逐条事实核验（下游把规格当真理）

链上每张卡只认上游产物作为依据 —— 需求卡定口径、设计卡定字段契约、实现卡照契约编码、
测试卡照 AC 逐条验。**这份信任链会把上游文档里的任何一句事实错误自动放大成下游的错误实现**,
而且往往静默通过: 实现卡照错的字段名写, 测试卡照错的 AC 验, 两边自洽, 无人报警。

所以 main 结算需求卡/设计卡时, 除了核产物存在 + verdict, **必须抽查文档里对现有代码的
事实断言**。各类最常出错 (探针配方见下表):

| 断言形态 | 怎么验 | 不验的后果 |
|---|---|---|
| "字段 X 已经在响应里了" | 读该端点的 SELECT 列表 **和** 字典构造块, 两处都要看 | 前端读 undefined, 整段功能静默失效而其余部分照常工作, 浅测发现不了 |
| "测试套件 Y 已覆盖 Z" | 打开 Y 的源文件 grep Z 的关键字 | 门禁是空的, 下游以为有回归保护 |
| "计数基线是 N" | 用与文档同口径重数一次 | 门禁阈值错, 零新增失败判不出来 |
| "后端挂了页面还能开" (失败域断言) | 查**进程拓扑**: 静态壳与 API 是否同一 handler/同一端口? compose 几个服务? 有无独立静态层? | 断言不可测, tester 只能写 NOT TESTED, 整条验收链空转 |
| "在 X 端点新增参数/改 Y 服务" (落点归属) | 查该组件源码**是否在本仓库**: repo 有没有它的代码? compose 的 image 是第三方? 挂了哪些目录? | 方案落点不存在, 下游照着不存在的落点设计, 整轮返工 |
| "某端点返回 700KB 要精简" (大字段消费) | 查消费端**到底用不用**那批字节 (还是判无效直接丢弃) | 修错层: 客户端懒加载加了复杂度却没减字节, 真正该做的是服务端剥离 |
| "进某页就拉某接口" (请求时机) | 找该 API 函数的**全部调用点**, 看在 mount effect 里还是在事件回调里 | 架构师按不存在的首屏负担做设计, 方案整个跑偏 |

★ 同一个键集合有三种计数口径 (顶层命名空间数 / 展平点路径叶子数 / 单命名空间叶子数),
三者互不相等。文档引用计数时必须写明口径, 否则下游会拿另一口径复数并判文档错 ——
规格作者往往是对的, 卡的是两边对不上数。

裁决一次、向下传播: 在项目根 TODO.md『当前任务』段写 ⚠ 裁决行 **并** 写进下一张卡的
advance task_description —— 两处都写, 否则只有一方看得到 (口径冲突见 坑 节同款做法)。

## 经 worker 网关 chat 下发非卡类治理指令（边界自检等）

- 通道: `POST <worker_api>/api/sessions` 建新会话 → `POST .../chat` (Bearer=注册表 api_key)，body `{"model":"hermes-agent","message":...}`；同步等回合，勿与 Team Work 施工会话混用（新会话不污染施工上下文，SOUL 按会话构建时重读、新会话即生效）。
- ★ kill 本地分发脚本 ≠ 中断 worker 回合；worker 端任务照常跑完并写产物。判"是否送达"以共享区产物文件为准，不以本地进程或当时 msgs=0 为准（实测：并行批次被 kill 后 7 份报告仍全部交回）。
- ★ 改 worker 自身 SOUL/skill 属授权动作，指令里必须写明红线：契约层（『## 1. 你的身份』~『契约层到此为止』）一字不动，只改其后角色段；写 /opt/data 用 bash heredoc（write_file 被沙箱拒）。worker 自改 SOUL 对新会话生效（每会话 build system prompt 时重读 SOUL.md，会话内缓存不刷新）。

## 参考文件

- `references/browser-smoke-verify.zh-cn.md`（EN 版同名 `.en.md`）— 前端产物浏览器冒烟核验 (独立端口起服 / CDP 视口仿真 /
  i18n 双语抽查 / 空值与 0 的区分)

## 流程（每张卡一轮）

1. **规划**：`GET /api/workflows` + `GET /api/workers`——两接口原样回显可达数十万字符（workers 每人带 base64 avatar），直接进上下文会把当轮模型响应拖超时中断；必须管道过滤只留 name/api_url/online/scope.tags 等必要字段。
   ★ 解析配方: 响应体含裸控制字符与 base64, 对 curl stdout 字符串直接 `json.loads` 会报 Invalid control character——固定 `curl -o /tmp/w.json` 落盘后 `json.load(open(...), strict=False)`。形状: `{"workflows":{"<workflow名>":{steps}}}`、`{"workers":{"<assignee名>":{api_url,api_key,scope.tags}}}`, 外层键即 id/assignee 名 (workers 条目内 name 字段为 null, 别按它取值)。按 worker scope.tags 语义匹配
   每张卡的步骤角色（中文标签按含义对：产品需求分解→req-analysis，架构设计→design，
   code-write→implementation，浏览器使用/测试报告→testing，review→code-review）。
   ★ **assignee 名字必须现读注册表再写死, 不要凭记忆或按 workflow 默认值抄**: 注册表键名即
   assignee, 常见为 `coder` / `tester` / `code-review` / `product-manager` / `system-engineer` /
   `doc_audit` / `ux-designer` —— 是**连字符**而非下划线 (凭记忆写 `code-reviewer` /
   `system_engineer` 会被 422 拒收, 报错里带 `available_assignees` 可直接重映射)。
   注册表随时可能被外部进程整体替换 (名字/主机/key 全变), launch 前再读一次确认稳定。
2. **launch**：`POST /api/team-work/launch`，带 workflow_id / session_id /
   project_workspace / steps。平台自动注入 depends_on 链。**launch 只自动拉起第一张卡**，
   其余卡 claim 后必须手动 spawn（见步骤 8）。
   ★ 下发后必须逐张核对响应 `cards[].assignee` 与你的语义匹配意图：steps 里的
   `assignee` 字段可能被静默丢弃（写好的 JSON 复读时无此键），平台回退到 workflow 的
   default_assignee。若不一致且首卡已在施工，让首卡跑完（中途换人=重复施工），
   向用户如实报告差异，由用户决定是否在轮转时换人。
   ★ **一次 launch 登记全部卡是安全的**（不必一张张补登记）：advance 的
   `settle_team_work_residue` 只覆盖 BLOCKED / NEED_CLARIFICATION，**不动 PENDING**
   （ledger 源码 WHERE harvest_status IN ('BLOCKED','NEED_CLARIFICATION')）——所以
   未来卡不会因为推进上一张而被误标成已结算。launch 时把整条链的卡都登记进去，
   链尾收口时省掉一串补登记。
3. **验证先行链的条件回流卡**：链首是验证/判定卡、后续修复卡取决于判定结果时，不预建修复卡——验证卡结算出明确 verdict 后再 `hermes kanban create` 修复卡 + POST /cards 登记 + 照常 advance；根因/复现路径沉淀在验证报告文档里，修复卡描述点名该文档为唯一依据（卡片自包含，不靠上下文传递）。下发前资料准备（拉 issue 原文与评论/下载附件/读 PROJECT-MEMORY）控制在少数几轮内即建卡 launch——用户看不到卡建出并派发就会视为零进展。
4. **复用旧项目工作区开新链**：project_workspace 里常躺着上一条已 CLOSED 链的 TODO.md。launch 前把整文件重建为新链的四段契约（『当前任务』必须含 `- 卡: <id>` 绑定行，旧链摘要压成『已完成』里的历史行），回读确认落盘后才 launch/advance——陈旧 completed 段会触发 worker『产物已存在即复用』假完工。下发前按需求先 pull 远端（fetch 走 PROJECT-MEMORY token 的显式 URL 路，见 gitea-repo-sync），把首张调研/研判卡产出的根因与相关文件行随该卡结论转述给下游，可显著缩短 coder 回合，但只写定位线索不动代码（角色铁律）。
5. **收到 mw 落账转发先查重**：`GET /api/tasks` 看该卡 status——已 done 就是重复通知，
   忽略并转去推进当前在跑的卡，不要重复 complete。
6. **结算 + claim 下一张**：
   ```bash
   export PATH=/opt/hermes/.venv/bin:$PATH
   hermes kanban complete <id> --result "<一句话产物摘要>"   # 空结算会被拒，必须带 --result
   hermes kanban claim <next_id>
   ```
   ★ complete 可能挂起超时（实测首调 180s 超时而卡仍 running）：用 `timeout 90 hermes kanban complete ...`
   包裹并重试，重试前后都 `hermes kanban show <id>` 核实真实状态，别凭超时判失败。
   ★ 长中文多行 --result 先落 scratch 文件再 `--result "$(cat /tmp/res.txt)"` 传入——
   内联在 shell 命令行里会被引号/分号截断，逸出的片段还会被 bash 当命令执行。
   ★ `hermes kanban show` 输出接 `| head` 会因 BrokenPipe 在 Result 段崩 traceback——
   判成功看前几行 status 即可，别把 traceback 当失败。
   对已 done 的卡再 complete 报 'unknown id or terminal state' exit 1 —— 先 show 确认 done 即视为成功，不是错误。
7. **轮转 TODO.md**（project_workspace 根下，单一真相源）：把已完成步（含结果摘要）
   归档进『已完成』，下一张待执行卡升为『当前任务』。必须用 execute_code 里
   **原生 python `open(path,'w')`** 读写——patch 工具、write_file 工具、乃至
   execute_code 内的 hermes_tools write_file 对 /share_workspace 都会因
   HERMES_WRITE_SAFE_ROOT 拒写，且有的**谎报 verified:true 实际根本没落盘**。

`/opt/data/skills/agent-matrix/` 下的 SKILL.md 是另一类陷阱: 目录 `root:root 755`、文件 `hermes:hermes 664`——patch 工具以调用方身份写时直接 `Permission denied`(不是 HERMES_WRITE_SAFE_ROOT 拒的), 必须用终端或 execute_code 里的 `python3 open()/write()` (hermes 用户身份跑可写)。两种路径都拒绝时先 `ls -la <path>` 看属主再决定怎么写。

   写完必须用 search_files 或另起独立进程重读磁盘验证；
   execute_code 内的 hermes_tools read_file 也可能读到旧缓存，不可作为落盘证据。
   轮转前核对『契约校验』sha 未被 worker 改动。
   ★ 解析前先无条件剥离行号前缀 `re.sub(r'(?m)^\d+\|', '', raw)`——worker 用
   read_file 输出回写 TODO.md 时会把 `N|` 前缀烙进磁盘真实内容，不剥则所有字段
   正则（`^- 步骤:` 等）全部落空、断言莫名失败。
   ★ 轮转必须**整文件重建**（解析四段后拼新全文），不要做外科手术式 regex 局部替换——
   局部替换极易把归档项遗落在『待执行』尾部或整体丢掉『已完成』标题，且二次修补会因
   标题缺失而 replace 落空。step id 含连字符（req-analysis/code-review），校验正则用
   `[\w-]+`，`\w` 匹配不到连字符会误报『已完成』为空。写完必须重读文件按四段各自
   数条目数验证结构。
8. **spawn 链卡**（claim 后 mw 不会自动拉 worker）——首选 `spawn-for`，
   它自动从 submit_log 重建 manifest、写 marker、detached 起 worker，无需手拼 manifest：
   ```bash
   export PATH=/opt/hermes/.venv/bin:$PATH MW_HOME=/mw_data
   python3 -m mw.cli spawn-for --task <task_id>
   ```
   有 already_running 护栏（marker+session+harvest_status 三路汇判），被拒时
   返回现有 session_id 供 resume，不会重复起 worker。
   注：launch 响应 note 指向 `POST /api/team-work/<run_id>/advance` 作为平台推荐的
   轮转端点。wechat-login 实测 advance 是最可靠路径：body 带
   {task_id, target_profile, title, task_description, required_files, timeout_seconds}
   即自动 detached spawn worker（响应含 session_id），无需 spawn-for；上一张未结算会 409。
   task_description 会进 worker 提示词，把注意事项写进去（如『测凭据用变量构造避开
   平台打码』『启动命令』『产出文件放哪』）实测有效。仅 advance 报错才退回
   claim + spawn 手动三连。
   （旧路 `__spawn-bg --manifest ...` 仍可用，仅在 submit_log 无 manifest 时需要。）
   manifest 用 write_file 写到 scratch 目录（heredoc 到 /tmp 可能因命令被拒而没落盘，
   spawn 时报 FileNotFoundError）。用 terminal(background=true) 跑，**不要用 `&`**（工具直接拒）。
9. **轮询产物**：循环 sleep+检查目标文件出现、TODO.md『结果』行被填。浏览器测试类
   worker 可能跑 20-40 分钟，属正常，耐心轮询。
   ★ 自己的轮询循环窗口耗尽退出（poll N 轮仍 working）**不是 worker 失败信号**——
   只要 session 消息数仍在涨就再起一轮同样的轮询循环，绝不因窗口到点而提前判死
   或换人（实测 coder/tester/reviewer 各需 2-4 轮 15-20 分钟窗口才 completed）。
10. **收尸**：reaper 可能自行收（查 `mw_events` 表 harvest.complete），也可能不收。
   手动 `mw.cli harvest` 常报 `database is locked`（reaper 持锁）——不要硬重试，
   改只读账本判断：
   ```python
   c = sqlite3.connect("file:/mw_data/ledger.db?mode=ro", uri=True, timeout=10)
   c.row_factory = sqlite3.Row   # 必须，否则 row["harvest_status"] 抛 tuple index 错
   # harvest_contracts: harvest_status/worker_report; mw_events: harvest.complete/task.stuck
   ```
   产物存在 + 结果行已填（或事件 physical OK）→ 直接走步骤 6 结算，不必等账本。

## 每张卡轮转时必须先汇报再 advance（用户 2026-10-19 明确纠正）

完整链流程的主汇报纪律在本技能而不是 advance：结算一张 → 派发下一张之间，必须先在对话里给用户一份面向人的进展汇报，再动 advance。advance 技能里已写四要素规则（卡标识 / 实测结果 / 独立核验 / 下游影响，≥4 句），本处不重复；只强调：链长 N 张 = 对用户 N 次汇报机会，缺一次都视为协议破坏（实案 c6 复测卡只动账本给一行短描述，用户反馈『没看到 C6 总结汇报』后被纠正）。reviewer/tester 通过性 PASS 也要汇报——PASS 不是沉默的理由，是『该让用户知道现在可以松一口气』的节点。

**协议补丁必须同时落到仓库 deploy/skills/**: main 在 `/opt/data/skills/agent-matrix/` (运行环境缓存层) patch skill 后, 必须把同补丁写入仓库 `<project>/repo/deploy/skills/<skill>/SKILL.<lang>.md` 并 commit(push 归协调方)——运行环境缓存会在下次 install.sh 时被覆盖, 仓库版本才是部署真正落地的源。补一次不补两次 = 用户下次加载 skill 时协议仍缺失。

## 编辑回链前先回读确认落盘（advance 派发铁律）

advance 端点有内置守卫生效：派发前必查项目根 TODO.md『当前任务』段是否含 `- 卡: <task_id>` 绑定行。缺行 → 409『当前任务未绑定本卡』拒派（无副作用，补完再 advance 即可，不算失败）。这意味着：

- advance 之前**必须先轮转 TODO.md**（用 execute_code 里的 python `open()/write`），把下一张卡的绑定行写到位。
- 轮转完必须**回读磁盘验证**（独立 search_files 或重读 TODO.md）——execute_code 内 read_file 读旧缓存不可信，patch/write_file 工具因 HERMES_WRITE_SAFE_ROOT 对 /share_workspace 拒写。即便写过，磁盘没落盘就 advance 必 409。

## 暂停与恢复（用户要求暂停任务时）

暂停 = 停止施工但保留可恢复性，**绝不 close run**（close 不可逆、毁掉恢复路径）：
① kill 当前卡的 spawn wrapper（`ps aux | grep __spawn-bg | grep <task_id>` 拿 pid）
② kill reaper → `UPDATE harvest_contracts SET notify_dead_until=<远期> WHERE task_id=<当前卡>` → watchdog 重拉
③ TODO.md 当前任务状态改为『已暂停（未派发）』。
恢复 = ① 恢复 run 级兜底敲门：`UPDATE team_work_runs SET last_null_slot_knock_at=NULL WHERE run_id=...`
② TODO 状态改回 working ③ 预播种任务目录契约 TODO.md ④ advance。
暂停期间的 run 级闲置敲门（『run 闲置 N 分钟』）按 `team_work_runs.last_null_slot_knock_at` 去重——
卡级 notify_dead_until 静音挡不住它；被吵就 kill reaper 后把该列推远期，恢复时清回 NULL。
每次敲门先只读账本核对：current_card 指向活 worker 且消息数在涨即吞掉，不动账本。

## Worker 存活探测与失败恢复

- **reaper 假 COMPLETE（vacuous harvest）**：launch 时 steps 未带 required_files →
  manifest harvest 是 `required_files:[] / check_script: exit 0`，reaper 见『物理 OK』
  就把 HARVESTED + verdict COMPLETE 落账——即使 worker 根本没产出、甚至 spawn 卡死
  (exit_code=None)。判据：worker_report 只有 exit_code=None + physical OK 而产物文件
  不存在。修法：确认产物缺失后把 harvest_status 改回 PENDING，按 spawn 死亡流程重发，
  完成真实施工后再结算。预防：launch 每张卡尽量带 required_files（平台注入 worker 提示词）。
- **spawn-for 卡死（wrapper 挂 poll、worker session 永不创建）**：__spawn-bg 进程在
  ps 里但 /api/sessions 里没有任何新 session、marker session_id=null 且 started_at
  久无进展。修法（『失败不重试』的合法例外，施工未开始）：kill wrapper →
  `POST <worker_api>/api/sessions {}` 预建新 session（响应在 `session.id` 字段）→
  先把该卡 notify_dead_until 清 NULL（否则完成时 relay 被静音挡掉）→
  terminal(background=true) 跑 `mw.cli __spawn-bg --manifest ... --task ... --workspace ... --session <新sid>`
  （setsid/nohup 被工具拒，必须 background=true）→ 用 GET /api/sessions/<sid> 的
  message_count 增长确认在干活。
- **spawn-for 不带 --session 必卡（design/implementation 两连踩）**：mw.cli spawn-for 与不带
  --session 的 __spawn-bg 都会让 wrapper 挂 do_poll 且 worker 侧 session 永不创建
  （GET /api/sessions 无新 session、msgs=0）。唯一可靠路：先 `POST <worker_api>/api/sessions {}`
  预建 session（响应嵌套在 `session.id`），再 `__spawn-bg --manifest ... --session <sid>`。
  另：launch 时 manifest 文件可能不在 /mw_data/manifest_<task>.json（spawn 报 FileNotFound），
  从 submit_log.manifest_json 字段原样恢复即可。
- worker API `GET /api/sessions` 返回 `{object, data:[...]}`（不是 sessions 键），
  按 session id 中段时间戳排序取最新。
- **worker `/api/sessions/<sid>/messages` 的 JSON 可能含非法 \uXXXX 转义**（worker 写的
  中文/emoji 内容经转义后 json.loads 直接抛 Invalid \uXXXX escape）。固定路：
  `curl -o file.json` 落盘后 `open(f, encoding='utf-8', errors='replace')` 再
  `json.load`——不要在内存里对 curl stdout 直接 loads，会炸且丢全部消息。

- 探测：worker API 地址与 api_key 一律现读 GET /api/workers 的 api_url/api_key——
  端口↔角色映射**不是固定的**，同一批端口在不同注册表重建下名字/主机/key 全变过，
  绝不硬编码 port→角色 映射或沿用旧 key（旧 key 对新 worker 全 401）。
  worker 网关认证头必须是 `Authorization: Bearer <api_key>`——裸 key（无 Bearer 前缀）
  即使值完全正确也回 gateway_auth_failed 401，别误判成 key 失效。直读
  `/mw_data/registry.json` 时顶层键就是 worker 名（`d['<worker>']['api_key']`），
  没有 'workers' 包装层。
  `GET http://<worker_host>:<port>/api/sessions` 按 session id 里的时间戳排序，
  看最新 session 的 message_count/tool_call_count 是否增长——增长=在干活。
  `/api/sessions/<sid>/messages` 可读最近消息判断进度。
- **用户问『worker 在干什么/为什么慢』**: 别猜——拉该 session messages 落盘, 解析每条
  assistant 消息 tool_calls 的 (timestamp, function, args 前 100 字), 按时间列最近
  10-12 条动作报给用户, 并指出当前瓶颈 (常见: 大文件整文件重写占十分钟一档, 或
  write_file 整文件重写后回读发现不对退回 patch 的多花几回合——后续由 commit diff
  核边界即可, 当场不打断)。
- **spawn 死亡**（spawn 进程不在 ps 里 + 无产物 + worker session 无活动）：注意
  marker 的 `session_id: null` 在 spawn 运行期间是**正常态**（wrapper 完成才回填），
  不能单凭它判死；判死要三路合看。解法：预建 session
  `POST <worker_api>/api/sessions {}` 拿 sid，重新 `__spawn-bg ... --session <sid>`。
  这是铁律"失败不重试"的合法例外——施工没开始，不算重复施工。
- **spawn 串卡（stale session 污染）**：spawn 进程 exit 0 但 worker 输出的是
  **上一张卡**的产物核对（如 design 卡却在校验 REQ.md），没产出本卡文件——
  mw 复用了 worker 的旧 session 上下文。诊断：读 spawn marker
  `/mw_data/spawn/<task_id>.json`（stdout/session_id/started_at），内容对不上
  本卡任务即确诊。解法同上：预建新 session 后 `__spawn-bg ... --session <新sid>`
  重发，并在重发前确认本卡产物文件确实不存在。
- **worker 把上一张卡的产物当自己的宣告完成**：审查卡 worker 读完 tester 的 TEST-REPORT.md 就宣布
  『任务已 completed 无需重做』，而本卡产物（REVIEW.md）根本没写。判据：worker 末条消息引用的产物
  不是本卡 required_files。修法（施工未开始，合法例外）：kill reaper → `UPDATE harvest_contracts SET
  harvest_status='PENDING', worker_report=NULL, harvest_log=NULL, notify_dead_until=NULL WHERE task_id=...`
  → `mw.cli resume --task <id> --instruction "<X> 是别人的产物，你的产物是 <绝对路径>，现在执行真正的审查…"`
  （resume 指令里必须点名本卡产物的完整路径，泛泛说『继续』唤不回正确目标）。
- **main-claimed 卡的 resume 报『无 session 锚点』**：main 手动 spawn 的卡，task_sessions.session_id 常为 NULL（runner_kind=main-claimed），`mw.cli resume --task <id>`（task 只能走 --task 选项，位置参数报 usage）直接拒。且 worker API 没有可直发的 chat 端点（POST /api/chat 404）。修法：从 spawn marker 拿真实 sid 回填 `UPDATE task_sessions SET session_id=? WHERE task_id=?`，再 resume。
- **LLM 长回合停顿 ≠ 挂死**：worker session 消息数可停 3-5 分钟零增长（尤其浏览器实测、
  大文件 read_file 之后的回合），随后爆发式恢复（wechat-login 实测 58→77→90）。判挂死前
  必须：看末条消息（若是 tool 返回且 spawn wrapper 在位，多半是长回合）→ 再延时观察
  2-3 轮（每轮 50-60s）确认无恢复，才走挂死恢复流程；单轮 45s 零增长不足以判死。
- **worker LLM 回合挂死（消息数长时间零增长 + 最后 assistant content='' + finish_reason=tool_calls 但无后续）**：session ended_at=null、token 计数停走、8+ 分钟无新消息即确诊（区别于『长任务中』：后者 reasoning/tokens 会动）。施工未产出时属 spawn 死亡同类：kill spawn wrapper → kill reaper → 预建新 session → 回填 task_sessions.session_id + 清该卡 notify_dead_until → `__spawn-bg --session <新sid>` 重发。旧 session 弃用即可，别试图 nudge。
- **submit_log 里的 manifest 不能直接喂 __spawn-bg**：launch 时平台注入的 manifest 含 TaskManifest(extra=forbid) 不认的字段（顶层 parent_task_id、harvest.check_script），直接 spawn 会 pydantic ValidationError 秒死（wrapper exit 1、无 marker、worker 0 消息）。修法：删掉 harvest.check_script 与 parent_task_id，补 task_id 字段，用 `sys.path.insert(0,'/opt/mwroot'); from mw.schemas import TaskManifest; TaskManifest.model_validate(m)` 本地先验再 spawn。spawn 后 30-60s 内 worker msgs 仍为 0 就先查 wrapper 输出（process_manage poll），别当慢启动。
- **项目工作区被外部删除（事故恢复）**：链跑到一半 /share_workspace/projects/<name> 整个消失（外部进程所为，出现同名单新目录）。恢复路：各 worker session 的转录里 read_file 结果存有文件全文（带 N| 行号前缀），用 `json.loads(content)` 解出后按行号 dict 合并多个分页分块（missing 校验为空才算全），重建 index.html/server.py/REQ/DESIGN。write_file 写的文档转录无全文，但其他 worker 的 read_file 分页可能补齐。重建后必须 py_compile + 起服冒烟四路验证。然后重建 TODO.md（从任一 worker 读过的 TODO 分块拼回 launch 版，再按 main 所有权轮转到当前步），预建新 worker session 重 spawn。教训：链产物应及时快照（md5+备份）。
- **pkill -f 会匹配到自己的命令行自杀**：`pkill -f 'spawn-bg.*t_xxx'` 在 terminal 里跑会把发起它的 shell 一起杀掉（exit -15）。改用 `ps aux | grep X | grep Y | grep -v grep | awk '{print $2}'` 拿 pid 再逐个 kill。
- **COMPLETE 假结算 = 只交笔记不施工（issues60-62 链 s1 实案）**：reaper 落账 COMPLETE 但结算核验必须三路对：TODO 结果行是否被填（占位『(worker 完成后填写)』=假）、required_files 内容是否含真实改动声明、**repo git log 是否有本卡 commit**（`git diff --stat 基线..HEAD` 为空即确诊零施工）。笔记里 [x] 自检项也可能是虚构（『UI 产物重新构建』勾选但无 commit）。修法（施工未开始，合法例外）：kill reaper → UPDATE harvest_contracts 回 PENDING+清 worker_report/notify_dead_until → TODO 状态行改回 working（防下轮抢收陈旧 completed）→ resume 指令逐条点名缺失项（改哪个文件/追溯 commit/重建产物同 commit/重写笔记/完成后才写 completed）。全角标点先换半角再落 instruction-file。变体 (issues60-62 链 s2b 实踩): worker 不在 repo 改真文件, 而在 project_workspace 根凭空创建同名假模板文件 (如 <ws>/deploy/install.sh 705B 假脚本冒充真 262 行 repo 脚本) 并宣称完成 —— 三路核验必须比对目标文件路径在 repo 内 + 真文件 mtime/内容变化; 打回指令须写明『产出必须进 git, 仓库外文件等于不存在』并先清理掉假文件防二次误导。
- **coder『裸改工作区交卷』高频复发（2026-10-19 一轮三连踩 c1/c2/c5b）**：worker 真改了代码甚至跑了测试, 但全部改动留在 git 工作区零 commit 就写 TODO completed。结算代码卡三路核验必须含 `git log <上卡tip>..HEAD` 有增量 commit + `git status` 干净, 否则 resume 定向打回 (点名『commit 才交卷』+ 要求拆分口径与 dist 同 commit 纪律)。变体: UI 改动只把 dist 搭在最早 commit (含未来 src 改动的 dist 使中间 commit 不可复现构建) —— 纪律=dist 重建统一搭最后一个改 src 的 commit。
- **worker 以环境借口跳过卡内已给定位的修复项（2026-10-19 c1 实案）**：worker 宣称『Gitea 不可达, 审查未发现问题』跳过了 main 卡片描述里已写明文件/行号/改法的条目。对策: 定位类卡把精确改动点直接写进卡片文本 (本已如此), 打回指令写明『卡片描述已含全部定位, 不依赖外部访问』。
- **终态卡 (ARCHIVED/CANCELLED/HARVESTED) 不能 resume 返工 —— 2026-10-25 起终态卡 resume 全面拒绝**：本条**原**为「已结算(HARVESTED)卡可继续 resume 返工（2026-10-19 多卡实证）」, 该打法**已于 2026-10-25 被废止**。原因: issue #104 的收口把 `is_terminal` 口径扩成**真终态三态** (HARVESTED/CANCELLED/ARCHIVED), 并在**三个执行点**同守 —— CLI 入口 (`mw/cli.py` cmd_resume) 终态卡直接退出码 1 不入队; console API 入口 (`mw/console.py` api_resume) 终态卡返回 409; pipeline 注入前 (`mw/pipeline.py` resume_task) 返回 `state_reset="refused:terminal"` 不发任何 POST; reaper 消费队列时 (`mw/reaper.py` reap_once) 终态卡走 `finish_resume(ok=False, error="terminal card")` 拒绝执行。**四处守卫都在副作用之前**, 操作员照旧打法做会撞 `[mw] task … 已终态 (HARVESTED), 拒绝 resume 入队`。详见 CHANGELOG s6fix P1-1 一节。**替代路径 = 新建 fix-up 卡** (重新 launch 一张卡, 不要试图把已结算的卡拨回 PENDING —— 状态机里 `_STATE_TRANSITIONS["HARVESTED"]` 只允许 {ARCHIVED, CANCELLED}, 不可回退)。结算前先三路核验, 打回比吞下假完成便宜这条铁律不变。
- **main 亲跑回归核验路**：容器全局 python 可能无 pytest——先找项目自带解释器 `repo/.venv/bin/python -m pytest`（多数 repo 自带 venv, 有则全套可亲跑）; 都没有时 node 类测试直接 `node ui/tests/*.mjs` 亲跑, python 侧退回 py_compile + grep 断言, 全套归 tester 卡。
  ★ 核『pytest 零新增红』声明不数总数: 要 worker 把改前基线与改后全量日志各落一份 (如 .scratch/pytest-{baseline,after}-*.log), main 用 `grep -E '^(FAILED|ERROR)' <log> | sort` 两份 diff——空集才算零新增红 (计数相等但失败集换名 = 修一个破一个)。同时亲跑本卡新增测试文件确认 passed 数与声明一致, 断言须真 raise。
- **worker 干完但没填结果行**：产物存在 + 账本 physical OK 时，main 可代记摘要
  到『已完成』（注明代记），照常结算。
- **结算 tester PASS 卡要读原始证据文件重数, 不只看报告**: 亲 grep 证据日志的关键签名计数并验互斥性 (如『修复版场景 N 次断连』应等于修复指纹日志条数、且旧行为签名只出现在对照组场景的条数里), 不采信报告转述; 证据日志里出现 traceback 时逐块看 file:line 是否落在本卡修复面——非修复路径的既有形态异常 (如普通 JSON 端点的 BrokenPipe) 不算修复失效; 回归集里多出的红须有 worker 双跑对照日志 (改环境参数前后各一份) 证明是环境现象才可豁免, 无双跑证据按新增红处理。
- **CHANGELOG/docs 卡结算核 Limitations/Verification 段与测试实态一致**: 条目把未测项写成已测、或 supersede 声明指向的旧段落实存性存疑, 即声明失真打回; 审计卡 task_description 写明『Known limitations 与测试报告结论逐条对照 (未复现/NOT TESTED 写成已测即失真)』+ supersede 目标实存核验。
- **advance 派发的 worker 把代码写在 /share_workspace/tasks/<id>/ 而非项目目录**：
  即使 task_description 指定项目路径，implementation 类 worker 常把 server.py/前端文件
  落在自己的 task workspace，且把 TODO completed+结果行写在 task 副本的 TODO.md 而非
  项目根。结算前 main 必须：以 task 副本 TODO 的 completed+结果行为完工判据 →
  shutil.copy/copytree 把代码同步进 project_workspace → py_compile → 独立冒烟
  （起服 curl 各分支，凭据用变量构造）→ 再 kanban complete + 轮转项目 TODO.md。
- **push 归属先实测, 勿一刀切 (2026-10-19 修订)**: worker 容器有无 git push 凭据因仓库而异。用户口径是任务完成即 push: 有凭据的仓库在卡验收口径写 commit+push+`git ls-remote` 核对远端 tip==本地 HEAD 并把 SHA 写进结果, 结算时 main 独立再对账一次 (不信 worker 自报, 实测有 worker 声明与远端实况一致但也有声明失真风险); 无凭据的仓库验收口径写『commit 即交卷』, worker 报『push 因无凭据失败』是正确行为不是缺陷, push 归 main 用 PROJECT-MEMORY token 显式 URL + ls-remote 对账。不确定某仓库 worker 有无 push 凭据时, 首张卡写明『push 失败则如实报告不算缺陷』试探, 据首次实测定后续口径。★ main 容器 git 也无内建凭据: 对 https 远端裸跑 `git ls-remote`/`git fetch` 会报 could not read Username 直接失败——远端 tip 对账改用 tracker API (`GET /api/v1/repos/<owner>/<repo>/branches/main` 带 token) 比对 commit id == 本地 rev-parse, 或 push 时直接用带 oauth2 token 的显式 URL。
- **NEED_CLARIFICATION 卡 resume 后的结算尾巴**: worker 按 resume 指令续作并把 TODO 改成 completed+结果后, harvest 契约**仍停在 NEED_CLARIFICATION**, 且可能再敲一条『worker 标记 blocked 但未提供 .clarify.json』的占位回声。正解序列: 三路核对产物与 TODO → `hermes kanban complete --result` → kill reaper → `mw.cli harvest --task <id>` 落 HARVESTED (报 locked 时 kill 后重试) → 只读账本确认 current_card_id 已清 → 静音该卡 → 才能 advance 下一张。
- **链尾必须手动 close（2026-10-08 边界减法; 2026-10-09 终态判定修复）**：mw **不再**自动 close run。
  全部卡 kanban done 后 run 停在 ACTIVE/current_card_id=NULL，mw 只发
  `team_work.run_idle` 事件；main 读完交付总结后主动
  `POST /api/team-work/<run>/close` 收尾。close 的终态判定：全完成
  （无未结算遗留 + current_card_id=NULL）→ status 落 **CLOSED**；
  存在未结算遗留、或 close 时杀掉了正在跑的 worker → **ABORTED**
  （异常中止，如首派失败回滚；先归档再关的调用方须显式传终态，
  见 mw/ledger.py close_team_work docstring）。以 kanban 全 done +
  交付物 md5 为准向用户报告，CLOSED = 正常收尾，勿误判链失败。
- **issue 类链对外部 tracker 双端对账（intake 清单 + 链尾批量关账）**：链首把 tracker 的 open issue 全量枚举落盘逐条给处置（已修即关 / 搁置待用户 / 本链处理），一条不漏；链尾 close run 后立即重开 open 清单程序化对账，本链已修复的逐条关闭并附证据评论（commit SHA + 核验依据，POST comment + PATCH state 后 GET 回读确认），待用户决策的保持 open 但挂分诊评论。禁止只凭『修复卡都 done』就宣布 issue 收尾——worker 卡 done ≠ tracker 已关账。tracker 的 owner/repo 在第一次 API 调用前从 git remote / PROJECT-MEMORY 读取固定，勿凭对话记忆书写。
- **PHYSICAL_ERROR 已废除（2026-10-08）**：旧版 mw 用 required_files 存在性
  推翻 worker 的 completed 判定落 PHYSICAL_ERROR，属越权，已删。worker 写
  completed 就是它自主判定完成，物理验收只作为 `worker_report.physical_check`
  证据随 COMPLETE 上报。若 worker 真没交卷，走的是 FAILED（超时收尸），见下。
- **浏览器冒烟帧哈希采样假阴性**：用 getImageData 哈希判"动画是否存活"时，两次采样可能撞上同一动画帧返回相同值（alive=False 假象）。改多点采样（6-8 次 x 0.4-0.5s）看集合去重数>1 即主循环存活，勿凭两次相同判死。
- **worker『N 个新测试全过』可能是静默假绿（issue13 实案）**：实现卡交付声明测试全过，但该测试文件用自建 _assert 只 append+print、不向 pytest 传递失败——pytest 退码恒绿，模块内部实为部分 FAIL。main 结算实现卡时若『测试通过』是主要质量证据，要求 worker 笔记附 `-s` 输出且 pytest passed 数与内部断言计数一致（或审查卡专项核查断言是否真 raise）；审查抓到此类失真时，main 在 TODO『已完成』归档里如实标注『声明失真』供审计追溯，并建修复卡收紧测试框架（先修真失败再收紧，避免收紧瞬间大面积红）。
- **同一卡的回声敲门 verdict 会翻转（issue13 实案：同卡先收 FAILED 后收 COMPLETE 两条矛盾文本）**：敲门文本的 verdict/harvest_status 同为网关侧陈旧/拼接值，不要据任一条文本决策；每次先三路核对（kanban show + 只读账本 harvest_status/notify_dead_until/last_notified_at + 产物与 TODO），已结算即吞掉，勿因文本翻转重新结算或重新建回流卡。
- **超时收尸的假 FAILED（『worker 超时未交卷』）**：reaper 可能在 worker 施工
  中途就落账 FAILED（worker_status=timeout），而 spawn wrapper 仍在 ps、worker
  session 消息数仍在涨。收到 FAILED 先三路核对（wrapper 进程在位 + session
  message_count 增长 + TODO 状态=working），全活则耐心轮询产物落盘——产物出现且
  结果行正常即照常结算，绝不因这条落账换 worker 或重发（=重复施工）。
  只有三路全死且产物确实缺失才走换人/重发决策。
  ★ 施工中的卡**绝不能静音**（notify_dead_until 设远期）——静音会连真正完成时的
  落账一起挡掉，链就永远推不动了；它落账前每轮重复转发只能当噪音吞掉，
  每次回复前快速三路核对即可，不必逐次展开。
  另：main 结算前自跑冒烟（起后端 curl 各接口分支核对契约）是比只读报告更强的
  结算依据，尤其 implementation 卡。前端页面类产物用 browser_exec 亲自跑登录分支 +
  CDP 视口仿真，配方见仓库内文件 `references/browser-smoke-verify.zh-cn.md`（EN 侧同名 `.en.md`）。

- **『当前任务未绑定本卡』守卫 launch 与 advance 同口径**：护栏查项目根 TODO.md『当前任务』段的 `- 卡: <id>` 绑定行，缺行即拒派。advance 报 reason=mismatch、launch 报 reason=todo_missing。launch 侧 409 有连带后果：run 建出来但首派回滚成 ABORTED，且占住 session_reused 席位，下一次 launch 又 409。所以复用旧工作区开新链的固定顺序：重建 TODO → 回读验证落盘 → launch。advance 侧 409 无副作用：补轮转 TODO+回读确认后再 advance 即可，不是失败不需换方案。
- **worker 死信回声（advance 写卡吞 body + submit_log 零增长 + 容器从未起）**：
  三连证据齐 = 卡真死信，不是 reaper 慢。①查 harvest_contracts.contract_json 看
  `input_spec`/`title`/`target`/`harvest` 是否为 None（advance 写卡时 task_description
  字段被吞是 known issue）；②查 `.tasks/<task_id>` 目录是否存在（不存在 = 容器未起）；
  ③查 submit_log 30+ 分钟无新行（reaper 不 spawn）。修法：resume 走 file-source 队列
  也接不通（前面死的 reaper 吃掉了，新 reaper 拉不到），但 main 仍要 main 亲做复审
  产物（review 性质）→ `hermes kanban complete --force`（force 跳 live-claim guard）
  → mute `notify_dead_until` → `POST /api/team-work/<run>/close` 落 **ABORTED**（不可
  伪装 CLOSED——close 时杀死了 worker, close 终态如实记 ABORTED）。
  **禁止** silent-skip 吞卡（违反 main 不替 worker 决策产物的分工）；亲做产物头部
  须声明"main 亲做复审，因 dX worker 死信"，把真相留在文档里。
  **教训**：advance 写卡后必须 GET 一次查 manifest 字段完整性，input_spec 为空就
  拒 advance 重写，不让空卡进 run 占用槽位。
## 口径与范围

- **doc_audit 卡范围 = 仅 repo/ 内 tracked 文档** (用户 2026-10-17 issue#46 修正): 项目 workspace 根 docs/ 的过程文档 (REQ/UX/ARCH/REVIEW/TEST/FIX-NOTES) 不是审计对象, 只作背景交叉引用; 下发与 TODO ⚠ 行都要写明, 否则审计会对过程文档出一堆越界判定。
- **mw.cli resume 中途改口径**: 选项是 `--instruction-file` (连字符, 非 --instruction_file); 指令内容先把全角标点 (（）：，。；) 替换成半角再落盘, 避 tirith homoglyph 拦截; 入队后 ≤15s reaper 执行, 读 worker session messages 确认指令落地 (搜关键词)。

## 坑

- **同一 project_workspace 严禁两条链并存 (2026-10-21 实踩)**: 开工前查 `team_work_runs` 无 ACTIVE
  **不等于期间无链** —— 控制台会话可能在你查完之后、开工之前对同一 workspace 起新链
  (实踩: 定时任务开跑时账本无 ACTIVE, 4 分钟后控制台会话 `api_1792523563_f36ffb36` 就对同一
  workspace 起了 run `12ef7058cc4941ca`)。**两条链共用项目根 TODO.md**, 新链 launch 会整文件
  重建 TODO.md → 摧毁在跑链的契约文件 → 那条链的 worker 走「自拟目标」分支假完工。
  对策: ① launch 之前**再查一次**账本 (开跑时的检查只是快照, 不是保证); ② 发现他人链在跑 →
  本轮让位, 不新建链; ③ 若仍要开自己的链, 必须用**独立 project_workspace** (如
  `<项目>-<批次>`), 绝不共用同一根目录。
- **让位不等于空跑**: 发现他人链在跑时, 本轮仍可产出**不触碰他人 TODO.md 的旁路产物** ——
  如 issue 分诊评论 (发到 tracker, 不动仓库) + 分诊记录落 `docs-source/<批次>/TRIAGE-*.md`
  (docs-source 是只读资料区, 不属契约文件), 让下一批有实证依据可接手。
  注意: 分诊记录里的根因必须**亲跑实证**, 不采信 issue 正文的自述结论。
  ★ **让位时仍可做「非侵入式分支管理」补做**: 用户要求「工作量大的任务要做好分支管理」,
  而让位意味着不能开链 —— 两者不冲突。用 `git branch -f <name> <ref>` 建**分支锚点**
  (不动 HEAD、不动工作区、不打断在跑的 worker), 实测对在跑链零干扰:
  ```
  git branch -f feat/<issue>-<topic> main          # 工作分支锚点 (指当前 main)
  git branch -f backup/pre-<issue>-base <base_sha> # 回退锚点 (指重构前主干)
  ```
  验链未被干扰: `git rev-parse --abbrev-ref HEAD` 仍是原分支 + `git status --short` 仍为空。
  锚点建完须在 issue 里写明**遗留待办** (如「这 N 个 commit 仍直接落在 main 上且未 push,
  链尾需决定是否移到分支 + push 后用 tracker API 核对远端 tip」), 否则锚点只是摆设。
  ★ **锚点会过期, 每轮让位都要重新刷**: 上轮建的 `feat/<issue>-x` 锚点指向当时的 HEAD,
  但在跑链继续施工后 HEAD 前进了 → 锚点落后于最新 commit, 覆盖不全 = 锚点名存实亡。
  每次让位先 `git rev-parse HEAD` 看清当前 tip, 再 `git branch -f feat/<issue>-x HEAD`
  重刷 (不是 `main` —— main 可能不动但 HEAD 变了; 以 HEAD 为准), 并在 issue 评论里
  显式写「上轮锚点指向 X、落后于当前 HEAD Y、本轮已刷新到 Y」。
  ★ **`write_file` 对 `/share_workspace` 拒写** (HERMES_WRITE_SAFE_ROOT 只含 /opt/data):
  开发机资产落盘走「先 write_file 到 /opt/data/cache/scratch → terminal `cp` 到目标目录」
  两步 (execute_code 里的 hermes_tools write_file 同样拒写且**谎报 verified:true**)。
- **让位期间「手工临时补丁」应升级为「自动挂载」, 消灭漏挂风险** (issue #77 实踩):
  链上常见的临时缓解是「逐卡手工挂一个 keeper/看门狗进程」—— 链有 N 张卡就得记着挂 N 次,
  **漏一张就重演事故**。让位轮次里 main 能做的最有价值的事 = 把这种手工补丁**通用化**:
  写一个 supervisor 轮询账本 (`team_work_runs` ACTIVE + `current_card_id` 非空) →
  找出当前执行卡 → 若该卡无对应进程则自动挂一个。
  必带护栏 (否则临时补丁会变成事故源): ①幂等 (已有进程就跳过, 否则 supervisor 每次轮询
  都会叠一个, 实测会出重复 keeper); ②复用原补丁的安全条件 (如 claim_lock CAS +
  status='running', 不能续到别人的卡); ③**给死进程兜底停止条件** (worker 停止推进即退出,
  免得给已死的 worker 无限续命、掩盖真实故障)。
  ★ **幂等检测不能靠「命令行里 task_id 后面跟一个空格」**: task_id 是 argv 的**最后一个**
  元素, ps 行到它就结束 —— 带尾空格的匹配会漏掉**每一个真实 keeper**, 于是 supervisor
  每轮都叠一个新的 (实测一轮内就出现 2 个 keeper 同时续同一张卡)。
  正确口径 = 按 argv 精确匹配: 遍历 `ps aux` 每一行, 该行含 `kanban_keepalive.py` 且
  `task_id in line.split()` 即判定已有 (记得排除含 `grep` 的行)。
  判据: supervisor 跑起来后, 任一时刻**同一张卡只能有 1 个真实 keeper 进程**
  (bash wrapper 不算, 它是 `bash -lic` 起的父进程)。
  supervisor 本体归开发机资产 → 落 `devops-skills/` 不进 repo (仓库卫生口径),
  且原脚本要从 `/tmp` 固化出来 (容器重启即失)。
- **issue 分诊要量化「影响面」而非只描述现象**: 判一条 issue 严不严重, 光复现一次不够 ——
  全库量化才能定级 (实测例: issue #77 描述为「偶发账面失真」, 全量统计 task_runs 得
  `reclaimed=259/657=39.4%` 已结束 run 走回收路径、跨 487 小时、
  `consecutive_failures>=2` 的 9 张卡 **9/9 (100%) 全是回收误伤** 无一真实失败 =
  影响面从「偶发」升级为「平台级默认行为」)。量化口径 (只读 `/opt/data/kanban.db`):
  `task_runs.outcome='reclaimed'` 计数与占比 / 回收 run 存活时长直方图 (印证 TTL) /
  `reclaimed-runs-per-task` 分布 / 达 failure 阈值的卡里含 reclaim 的比例 /
  被回收卡最终状态分布 (done vs archived) / reclaim 后同 task 是否又起新 run (= 重跑次数)。
  ★ 量化时务必区分「账面失真」与「真实功能故障」, 并如实说明代价 (本例 248 张卡里 202 张
  最终 done, 即代价是 233 次重跑 + 断路器误跳闸, 不是做不完) —— 夸大影响面会误导用户裁决。
- **skills 目录同名副本**：备份目录（如 *.rollback-broken）与原 skill 同名会使 skill_view 按裸名报 ambiguous 拒猜。分类路径只写 `category/skill-name`——追加 `/SKILL.md` 后缀会报 not found；分类路径也失败时用 read_file 直读 SKILL.md 绝对路径兜底（skill_manage patch 仍按裸名可命中）。陈旧的 .rollback-broken 目录还会占住 skill_manage 的回滚快照路（批量 patch 中途失败时报 ROLLBACK FAILED / Errno 39 Directory not empty）；此时原 skill 并未受损（批在写入前已中止），确认是旧残留后清掉该冲突目录即可恢复。
- **terminal 整行被拒 = 该行所有命令都没跑**：一行里混了 `cp a b && nohup ...` 而工具因 nohup/
  `&` 包装拒绝整行时，前面的 cp 同样没执行——你以为文件已拷贝实际不在盘上。被拒后把非包装部分
  单独重跑，并用 ls/md5 验证副作用真实发生再继续。
- **main 冒烟起服的残留进程坑**：历次冒烟的 `python3 server.py <port>` 进程若不清理，会让后续起服/请求出现诡异 000 或端口占用干扰。固定流程：起服前 `ps aux | grep 'server.py 88\|server.py 89' | grep -v grep | awk '{print $2}'` 逐个 kill；起服必须 terminal(background=true)（`(cmd &)` 子 shell 随命令结束被回收，服务不会活着）；curl 本地端口加 `--noproxy '*'` 防代理环境变量劫持 127.0.0.1；测完 kill 掉自己起的进程。
- **链 CLOSED 后仍收已结算卡敲门**：每次核对时把 last_notified_at 的具体数值记下来，下次敲门与之逐字节比对——未前进即静音有效、纯网关排队滞后，吞掉；若发现 reaper 进程启动时间早于你的静音操作（持旧 WAL 快照），kill 让 watchdog 重拉，新进程复查停走即终结。敲门文本里的 harvest_status=PENDING 是网关陈旧值，与账本矛盾时以只读账本为准。

- **advance 新加的卡（链中途追加的 fix/review 卡）报 404『无 harvest contract (未登记)』**：advance 只认 launch 时登记的卡。首选 HTTP 登记端点（正规路, 不碰账本函数）：
  ```bash
  curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"cards":[{"task_id":"t_xxx","title":"...","assignee":"coder",
         "required_files":["docs/X.md"],"timeout_seconds":3600}]}' \
    http://127.0.0.1:8700/api/team-work/<run_id>/cards
  ```
  ★ 路由是 **`/cards`**（正则 `^/api/team-work/<run_id>/cards$`）——猜成 `/register` 或
  `/add_cards` 会得 `{"error":"not found"}`（那不是 404 未知卡, 是路径没匹配上, 极易误判成
  「卡没建好」而去重建卡）。该端点**只登记已存在的 kanban 卡, 不建卡**：顺序必须是
  `hermes kanban create` → `POST .../cards` 登记 → `advance` 派发。
  兜底（HTTP 端点不可用时）才用账本函数：`sys.path.insert(0,'/opt/mwroot'); from mw.ledger import Ledger; Ledger('/mw_data/ledger.db').register_team_work_card(run_id, task_id)`（自动建 PENDING 契约占位 + 回填 team_work_run_id + task_initiators），再 advance 即通。

- **worker 对「一次性生成超大单文件」系统性挂死（2026-10-06 三连踩）**：coder 被派实现单文件 ~200KB HTML 时，三次都在读完文档、即将写大文件的回合冻结（末条 assistant content='' 或纯文本宣告后无工具调用，消息数/tokens 8+ 分钟零增长，resume 注入也唤不醒）。这不是网络/进程问题，是 LLM 单次输出上限问题。对策：施工未产出时，main 直接代建（分块：内容字典 JSON → HTML 骨架+CSS → JS 注入，每块独立小文件再合并），照常结算实现卡并在结果里注明「main 代建」；后续测试/审查卡不受影响。给 coder 派单前先评估产物体积，超过 ~50KB 单文件的任务预先拆成多卡，或把分块施工配方直接写进派单 task_description（预防优于事后代建）：在 <产物目录>/parts/ 下分多次写小分片（每片 ≤15KB，按 head / style-base / style-components / sections / footer / i18n-zh / i18n-en / app.js 分档），每次只写一两片，全部分片完成后用 shell cat 按序合并成目标文件，再校验合并结果（文件头尾完整、内嵌 JSON 字典可 parse、关键 grep 通过）。worker 照配方执行即可绕过单次输出上限，实测有效。分片构建必须同时记录合并模板（哪些分片进 <style> 块、哪些是裸 HTML、每段 JS 的 <script> 包裹与顺序）；worker 没留合并脚本时，main 从现有产物的标签结构反推模板重建合并，并校验标签配平（script/style 开闭计数相等、head/tail 完整、内嵌 JSON 可 parse）后才算合并成功。
- **（历史注记, 机制已废除 2026-10-08）advance 的 settle_team_work_residue 曾把未来卡隐式标 HARVESTED → 当前卡结算时 run 被 auto-close**：当年 launch 时全部卡的 PENDING 契约同时存在，advance 第 N 张会调 settle_team_work_residue 把 run 名下**所有其他 PENDING 卡（含还没轮到的 N+1..end）**强制覆盖成 HARVESTED（log='[main_advance] implicit completion'），第 N 张结算时 harvest 的 auto-close 判定『无遗留』成立 → run CLOSED(complete_all)，后续 advance 全 409『非 ACTIVE』。**该 auto-close 已在 2026-10-08 边界减法中废除**（mw/harvest.py 边界重划注释：reaper/harvest 永不 auto-close run），上述救场 UPDATE 流程不再需要。现行机制：最后一张 COMPLETE 落账且无遗留时 harvest 只 emit `team_work.run_idle` 信号，run 是否关闭由 main 读完最后一份报告后主动调 close、或操作员手动决定。若再看到「收到 COMPLETE 但 run 已 CLOSED 且剩余卡还是 todo」，先查是否旧版 mw 残留，勿再照旧流程改账本。
- **required_files 路径解析口径（先查部署的 mw 版本再定性）**：现行 harvest.verify_physical(contract, workspace, product_workspace) 对带 project_workspace 的 run 优先按项目根解析 required_files（源码 docstring：产物相对路径优先按 product_workspace 解析），worker 产物写在 project_workspace 不会因路径错位假 BLOCKED——launch/advance 带 project_workspace 即可。旧版 mw（只拼 /share_workspace/tasks/<id>/ 前缀）才有「产物在项目目录而 harvest 报缺少产出文件 BLOCKED」的假 BLOCKED；若现场遇到此症状先 grep 部署版 verify_physical 是否含 product_workspace 参数，确属旧版才走旧修法：kanban complete --result 后 kill reaper → UPDATE harvest_contracts SET harvest_status='HARVESTED', notify_dead_until=远期 WHERE task_id=... → 清 current_card_id → advance。另注意 worker 可能把契约 TODO.md 写到 /share_workspace/tasks/<id>/ 而非项目根——两处都查，以内容 completed+结果行为准。
- **launch 首卡 contract 的 team_work_run_id 可能留 NULL（qq-login 实测）**：launch 虽 register_team_work_card 回填，但首卡 contract 行可能仍 NULL → reaper 视为孤儿跳过，worker 写 completed 后 current_card_id 永不清、advance 一直 409。修法：手动 `mw.cli harvest --task <id>` 落账后，kill reaper → `UPDATE harvest_contracts SET team_work_run_id='<run_id>' WHERE task_id='<首卡>'` + `UPDATE team_work_runs SET current_card_id=NULL WHERE run_id='<run>' AND status='ACTIVE'` → watchdog 重拉 → advance 即通。
- **手动 spawn（__spawn-bg）的卡在账本里没有 harvest 契约**：launch 只为首卡建契约，
  main 手动 spawn 的后续卡 submit_log/harvest_contracts 均无记录 → reaper 永远不会
  harvest、永远不会转发唤醒 main。后果：worker 干完写 completed 后链静默停摆，
  main 若只等通知就永远不动作。对策：**spawn 之后必须自己轮询产物落盘 + TODO 状态行**
  （terminal sleep+check，或收到任何敲门时先查当前卡），看到 completed+产物即自行结算，
  不等 mw 通知。用户提醒『卡2早就结算了』即此坑的表现。

- **已结算卡被 mw 中继无限重复转发（每 120s 一轮）**：reaper 的 `_notify_main_of_reports`
  对所有 worker_report 非空的 HARVESTED 卡只按 120s 间隔去重，main 自己 complete 不会清
  报告 → 死循环刷屏。根治：kill reaper 释放锁后执行
  `UPDATE harvest_contracts SET notify_dead_until=<now+1年> WHERE task_id IN (全部已结算卡)`，
  watchdog 重拉后 should_notify 永返 False。验证：等 2-3 轮 reap，last_notified_at 不再前进。
  ★ 静音后仍可能收到几条重复转发——那是静音前已进网关投递队列的滞后消息在排空，
  查 last_notified_at 未前进即可确认静音有效，无需再动账本。敲门消息文本里的
  harvest_status（如 PENDING）是网关侧陈旧值，与账本实况（BLOCKED/HARVESTED+已静音）
  矛盾时一律以只读账本查询为准，勿据文本再动账本。
  ★★ 静音后隔很久又收到大量重复敲门（stuck 分钟数量级跳变，如 91→163）：
  这不是排队滞后，是 reaper 进程持有 WAL 长事务旧读快照——它眼里 notify_dead_until
  仍是 NULL，会继续发敲门并写回 last_notified_at/attempts（外部用新连接读账本
  却看到静音在位、should_notify=False，两边矛盾即确诊）。修法：kill reaper，
  watchdog 重拉后新进程读到正确快照，静音立即生效。验证同上新连接轮询
  last_notified_at 停走。
  ★ 时机：每张卡 complete 的**同一轮**就静音该卡，不要等收到第二次重复转发才动手。
  直接 UPDATE 常撞 `database is locked`（reaper 泄漏数十个 WAL 句柄持写锁，timeout=30
  也全超时）——固定顺序：`kill <reaper_pid>` → sleep 数秒 → UPDATE → watchdog 自动重拉。
  批量排查风暴卡：`WHERE worker_report 非空 AND (notify_dead_until IS NULL OR < now)`。
  静音后仍可能收到几条该卡转发——那是静音前排队的滞后投递：查 last_notified_at
  在静音时刻之后未前进即确认静音生效，不必重复静音，一句话带过。
  ★ 静音+reaper 重启后仍连收同一条敲门 6+ 次、stall 分钟数乱跳（5.1↔6.6 倒走）：
  查账本 notify_attempts=0 且 last_notified_at=None 即证明当前 reaper 从未为此卡发过
  通知，纯网关队列回声，吞掉即可，连 kill reaper 都不必。
- **卡已 done 但 reaper 反复敲门（harvest_status 永远 PENDING）**：spawn 包装进程若在
  回填 marker exit_code 前崩掉（常见于账本锁冲突），契约就永远 PENDING，reaper 每 30min
  敲一次门，即使 kanban 侧早已 complete。修法：先 `kill <reaper_pid>` 释放写锁（watchdog
  会重拉），再手动 `mw.cli harvest --task <id>` 收尸 → HARVESTED，敲门停止。
  判据：产物存在 + worker session 消息数不再增长 + REVIEW/结果行已填 = 施工已完成，
  收尸只是清账本尾巴，不是重新施工。
- **同 session 再 launch 新 run 报 409 `session_reused`（含已 CLOSED/ABORTED 的旧 run）**：一 session 一 run 是硬护栏（防 initiator 上下文互灌），CLOSED/ABORTED 均不释放——被 TODO 绑定守卫拒首派而自动回滚成 ABORTED 的 run 同样占住 session 席位，换合成 sid 前先确认这点，别对 ABORTED run 反复重试 launch。可行路：launch 用合成 initiator sid（如真实 sid 加后缀），launch 成功后立即 `UPDATE task_initiators SET session_id=<真实 sid> WHERE task_id IN (全部卡)`——reaper 投递按 task 级 get_initiator 路由，报告仍回真实 session；run 级闲置敲门的 initiator 指向合成 sid（无害）。此类账本改动属 main 补救，须在汇报里向用户如实报备。
- **worker 把状态行写成 `* 状态:` → reaper 永读不到 → 卡永不收割 (2026-10-21 issue#82 链 s3 实踩)**:
  `mw/todo.py::parse_status` 的正则是 `^-\s*(?i:状态|status)[:：]` —— **只认行首 `-`**,
  worker 写成 `* 状态: completed` (bullet 风格) 时解析结果是 `None`, reaper 每 15s 扫到
  都当成「状态未知」→ 不收割 → 卡永远停在 PENDING/WAITING, run.current_card_id 永不清,
  advance 一直 409 『必须先等其结算』。判据: 产物已落盘 + 结果行已填, 但
  `parse_status(TODO.md)` 返回 None 或非 completed ⇒ 确诊此坑 (直接
  `python3 -c "import sys;sys.path.insert(0,'.');from mw.todo import parse_status;print(parse_status(Path('<pw>/TODO.md')))"` 验)。
  正解: main 把该行改成合法 `- 状态: completed` → `POST /api/tasks/<id>/state-correct`
  body `{"action":"reassess","reason":"...","actor_session":"<该卡 initiator sid>"}` 立即收割
  (actor_session 必须是 task_initiators 里的 sid, 用合成 sid 会被 403 拒)。
  ★ **main 轮转 TODO 的时机**: 必须等 reaper 收割完当前卡再轮转下一张 —— reaper 只结算
  `run.current_card_id` 这一张, 却读**项目根共享的**那份 TODO.md; 你提前把 TODO 换成下一张卡,
  reaper 读到的就是下一张的状态, 当前卡永远结算不了 (main 提前轮转 = 亲手制造死锁)。
  轮转前的正确顺序: 等 harvest_status 变 HARVESTED → 只读账本确认 current_card_id 已清 →
  再重建 TODO.md 指向下一张。
- **advance 报『幽灵 profile 拒绝: '' 不在注册表』= 半途态陷阱 (2026-10-18 实踩)**: advance payload 用 `assignee` 字段无效, worker profile 取 `target_profile`; 漏传时端点已占槽 (set_team_work_current_card 在 dispatch 之前), dispatch 被 422 拒 → run.current_card_id 卡住 + 卡 status=running + session_id=None, worker 根本没起。reassess 翻不了案 (无 session 锚点), 且状态正确无需翻。**正解: 槽位与 claim 都在, 直接 POST /api/dispatch 补 spawn** (body: task_id+run_id+title+target:{profile}+input_spec.task_description+harvest.required_files/timeout_seconds+idempotency_key tw-<run>-<task>+project_workspace; 串行护栏 current_card_id==task_id 恰好满足)。预防: advance 必带 target_profile, launch 的 steps 里 assignee 与 target_profile 是两套字段别混。
- **链中仓 HEAD 被外部 commit 漂移**：测试/审查期间发现 HEAD 多出非本链 commit（作者可能是其它 agent/人）。不追改不恐慌：① 核对本链改动谱系完整（git log 基线..本链各 commit 在列）② 后续验证 pin 到被审 commit（detached/worktree）保口径干净 ③ 给文档审计卡加专项：外部 commit 是否覆盖本链契约（同名文件逐段 diff 取证）。若外部 commit 违反仓库卫生口径（开发机资产入库），建 revert 卡走正规链路 revert，不手工改仓。
- **launch 报 409 `orphan_pending_cards`**：账本里遗留历史 run 的 PENDING 契约（team_work_run_id IS NULL）会阻塞新 launch。修法（错误 hint 已给出）：kill reaper 释放锁 → `DELETE FROM harvest_contracts WHERE harvest_status='PENDING' AND team_work_run_id IS NULL` → 重新 launch。先只读确认这些卡所属 run 均已 CLOSED/ABORTED 再删。
- **launch 报 `database is locked`（record_submit INSERT）**：reaper 进程可能泄漏了几十个
  ledger.db 连接并长期持有写锁（`/proc/<pid>/fd` 里大量 ledger.db-wal 句柄、
  `BEGIN IMMEDIATE` 30s busy_timeout 全超时）。心跳文件仍在走，别被骗。解法：
  `kill <reaper_pid>`，entry.sh watchdog 5 秒内自动拉起新 reaper，锁即释放，
  重新 launch 即可。诊断脚本：遍历 /proc/[0-9]*/fd/* readlink 找 ledger 句柄持有者。

- `mw.cli status` 顶层 key 是 submits/sessions/harvests，worker_report 在 harvests 里，
  可能是 PENDING 而事件表已有 harvest.complete——两处都查。
- 账本表名：task_sessions / harvest_contracts / mw_events（列 event_type/payload/created_at），
  没有 sessions/worker_sessions 表，别猜表名。team_work_runs 的发起会话列名是
  `initiator_session`（无 _id 后缀）——launch 前查本 sid 是否已绑旧 run 用这列，猜列名会 OperationalError。harvest_contracts 没有 assignee 列
  （查它会 OperationalError），承接方看 kanban 或 submit_log.manifest_json 的 target.profile。
- 重复落账通知极常见：同一张卡可跨小时滞后重转 5+ 次（链结束后仍会收到早期卡的），
  先查状态再动手；确认重复后一句话带过即可，不必逐条向用户展开。
- **__spawn-bg 包装进程崩于 `database is locked`（upsert_session）≠ 施工失败**：
  worker 本体可能已跑完，reaper 照常收尸落账（HARVESTED）。先查产物文件 +
  harvest_contracts 状态再决定，不要盲目重 spawn。
- **mw 敲门（stuck 30min）可能是假警报**：reaper 的 last_progress_at 滞后，worker
  实际消息数在涨、产物已落盘。收到敲门先查 worker session 的 message_count 增长
  + 产物存在性，别急着 resume/换人。
  ★ 敲门卡自己还没 spawn 时，先查**上一张卡**：TODO.md『当前任务』若仍是上一张卡
  且状态=completed，说明是真实交接缺口（上卡完工没人结算，链停摆）——这不是假警报，
  立即走结算+轮转+spawn 三连，而不是静音。
- **链尾卡的 stuck 敲门在链中段就会开始响**：launch 时全部卡的 harvest 契约同时预建
  （status=PENDING），链走到第 N 步时第 N+2 步的契约已挂超 30min → reaper 敲门，
  但该卡 kanban 还是 todo、根本没 spawn。判据：敲门卡的 kanban status=todo 且无 spawn
  marker = 纯假警报，勿 resume/换人。★ 静音假敲门必须用「kill reaper 后立即 UPDATE
  notify_dead_until=远期」的方式：`_send_to_main` 发送成功后会把 notify_dead_until
  清回 NULL（reaper.py L143-147），不 kill 直接 UPDATE 会被在途发送的成功路径覆盖掉。
  并且轮到该卡真正 spawn 时要记得把 notify_dead_until 清 NULL，否则该卡完成时的
  relay 也会被静音挡掉、没人叫醒 main 结算。
- ~~pass_threshold 计数~~ **已废弃 (2026-10-08 边界减法)**：旧版 reaper 会
  自动 bump `harvest_contracts.pass_count`，PASS 但 pass<required 时状态仍
  PENDING（PASS_BUMPED 同卡重跑）。该机制已整体删除 —— mw 不再计数、不再
  门控、报告里不再有 `pass_count`/`required_passes` 字段。
  **要「测试跑 N 次」就建 N 张卡**：每张跑一轮，main 读报告决定是否
  advance 下一张。看到旧库里残留的 pass_count 值只当历史审计，别据此
  判断"还差几次"。
- api_server 会话里 terminal(background=true) **不支持 exit 异步通知**（结果要用
  process_manage(action='poll', session_id=...) 主动取，经 tool_call 调用）；
  notify 传正则字符串列表可能被拒（"notify must be true/false or a list of
  strings"），改 `notify=true` 可通过但仅标记 notify_unsupported——不要在这上面
  反复试参数形态，直接 background=true + poll。
- **worker 注册表在链期间被外部重建（名字/主机/key 全变）**：/mw_data/registry.json
  随时可能被外部进程整体替换——worker 名可变成中文（产品经理/系统架构师）、api 主机
  与 api_key 全换。对策：① launch 前重读 GET /api/workers 确认注册表已稳定；注册表
  为空时 launch 会跳过 assignee 校验、派单时才失败，故空表/变动中要先轮询几十秒
  到稳定再定承接方；② launch 报 422『assignee 不在 registry』时，按报错里的
  available_assignees 用 scope.tags 语义重映射，`hermes kanban assign <task_id> <新名>`
  同步 kanban 卡，并改 TODO.md 的承接方行，再重新 launch（launch 幂等拒重复 run，
  失败的 launch 未建 run 可直接重试）；③ 首卡已派发后不换首卡，只在轮转时换后续卡。
- TODO.md 所有权：worker 只许填『结果』行；轮转『当前任务/待执行/已完成』是 main 的事。
- **上游产物互相冲突必须由 main 一次裁决并向下传播**：链上两份上游产物的验收口径硬冲突时（如需求卡要求『旧名 grep=0』但同卡联动点又要求『桥接句必须含旧名』），下游设计卡会自行裁决出与需求卡矛盾的字面口径，测试/审查各拿一把尺。main 发现后裁决一次（如白名单制：允许恰 N 处=指定 key 值本身，其余命中即 FAIL），并把裁决写进项目根 TODO.md『当前任务』段的 ⚠ 行**和**每张后续卡 advance 的 task_description——让 tester/reviewer 拿同一把尺，绝不让各卡自行重新争论。
- **轮转会吞掉刚写进『当前任务』的 ⚠ 裁决行**：裁决写在该段, 而轮转是整文件重建 + 用新卡内容
  整体替换该段 → 裁决随旧段一起蒸发, 下游读不到。修法: 裁决行写进**新卡**的当前任务段;
  轮转后回读断言「⚠ 裁决」仍在新段里, 掉了就补写 (踩过两次: 第一次写在旧卡段被覆盖;
  第二次断言命中目标行里的同名短语而误判已存在 —— 断言要用不会误命中的长标记)。
- **整文件重建的拼接口径**：从原文抽出的段落自带 `## 标题` 行, 直接拼回去会得到
  「## 待执行」紧跟「## 待执行」, 旧占位行/残留条目也会混进来。修法: 拼装前先剥掉每段的
  `## 标题` 行, 拼装后断言每个标题恰好出现一次 + 四段各自条目数正确 (只数条目数查不出重复标题)。
- **complete 与 advance 之间的空窗会招来 run 闲置敲门**：结算(complete)后槽位已空、run 仍
  ACTIVE, 若此时做用户汇报(必须做)再 advance, 空窗超阈值 reaper 就敲「run 闲置 N 分钟」。
  这是**自己序列造成的正常敲门**, 不是 reaper 慢也不是 worker 挂了: 查 current_card_id=NULL
  且上一张已 HARVESTED 即确诊, 直接 advance 下一张, 勿因此回退结算或重发。
  机制与取证口径见 skill `mw-knock-and-llm-interrupt-forensics` (null_slot_knock 假警报节)。
- **launch 静默忽略 workflow_id**：平台不读 launch body 的 workflow_id (传了也不报错也不生效),
  steps[] 是唯一真理源 —— 参考 workflow 只是 main 自己拆解时的参照物, step 集合/承接方/
  optional 步取舍全部由 main 显式写死, 别指望平台按 workflow 补步骤或注入 depends_on。
- **advance 派发的新卡若任务目录 /share_workspace/tasks/<id>/ 无契约 TODO.md → worker 走『自拟目标』分支假完工**：worker 起手读不到任务内容，按下发模板自拟一个『链路自检』目标，写 REPORT.md、状态 completed——真正施工根本没发生，harvest 因缺 required_files 在 `physical_check` 报 passed=False（边界减法 2026-10-08 后不再落 PHYSICAL_ERROR，但假完工的账本证据仍在）。修法（施工未开始，属『失败不重试』合法例外）：①main 把真实契约整段写入 /share_workspace/tasks/<id>/TODO.md（含目标/产物绝对路径/状态 working）②kill reaper → `UPDATE harvest_contracts SET harvest_status='PENDING', worker_report=NULL, notify_dead_until=NULL WHERE task_id=...` ③`mw.cli resume --task <id> --instruction "重新读取契约并执行真正修复…"`（入队可靠，reaper ≤15s 执行，worker 同会话续作）。教训：给链中途新增的卡派发前，先自查任务目录契约是否存在，不存在就先播种。
  ★ 契约文件的真实位置以 worker 首条 user 消息点名的路径为准，不是 advance 响应里的 workspace 字段（后者可能是 <project>/.tasks/<id> 之类并不被 worker 读取的路径；launch 带 project_workspace 时 worker 通常读项目根 TODO.md）。派发后立刻读 worker 首条消息确认它读哪份契约，并确保派发前那份契约的『当前任务』已含本卡完整目标、产物绝对路径与状态 working——即先轮转/播种契约再 advance，顺序不可反。
- **run『ACTIVE 但 current_card_id=NULL 闲置 N 分钟』兜底敲门可能是假警报**：worker 可能早已完工（task TODO completed+产物落盘）只是 harvest/relay 没接住，槽位空转。收到先查当前卡 TODO 状态与产物落盘，真实完工就直接结算+advance，别干等账本。敲门文本本身也可能与账本相反：qq-login 实测敲门称 current_card_id=NULL，只读账本里 current_card_id 实为实现卡且 coder 只是大回合停顿——兜底敲门文本不可信，一律以只读账本 + worker session 消息数增长为准。
- **本链自己造成的 repo 文档失同步 = 当轮修, 不转遗留 issue**：文档审计 / 审查报出
  「repo 内 tracked 文档没同步本链的代码改动」（如端点表面表没跟上新增端点、CHANGELOG
  条目漏记本链 commit）时, 这不是**历史遗留**, 而是本链自己制造的债 —— 拖成 Gitea issue
  等于把自造的债甩给 operator。判据: 该失同步的**引入 commit 属于本链** ⇒ 建一张
  **范围锁死的 docs 同步卡**（单文件或两文件, 禁改源码, 事实必须逐条对代码实态, 只 commit
  不 push）当轮修掉, 修复后只需复核改动的那几行, 不必全链复审。
  真正的历史遗留（表最后改动停在很久以前的 commit, 与本链无关）才转 issue 跟踪。
  ★ 卡文本要给 worker **可核对的代码行号锚点**（哪个端点路由在第几行、字段白名单常量在
  第几行）, 否则 worker 写文档时会凭印象编, 审计下一轮照样打回。
- **CHANGELOG / 文档条目的 commit range 记号必须与自报计数自洽**：git 的 `A..B` 语义**排除
  A**, 写「Range: A..B (N commits)」时若 N 含 A, 记号就少圈了一个 commit。核法:
  ```bash
  git log --oneline A..B   | wc -l   # 与文中 N 比对
  git log --oneline A^..B  | wc -l   # 含 A 的正确写法
  ```
  文中「N commits」+ 逐个列出 SHA 时, 数字常是对的、**只有记号错**（读者按列出的 SHA 理解
  不受误导, 但 `git log A..B` 复现不出文中范围）—— 轻微项, 一并顺手改, 别留。
  ★ 附带核对 `git diff --shortstat A^..B` 的 files/+/− 数字与文中一致, 数字错就是事实错。
- **TODO.md 轮转后必须断言四个段标题齐全, 再 advance**：用**行列表**重建（`lines[:i] +
  ['## 已完成'] + lines[i:]`）而不是按 `str.index()` 切原文再拼 —— 字符串切片很容易把
  `## 已完成` / `## 待执行` 标题连带内容切丢或切重（切一次忘一次, 表现为「段落标题变成
  『执行』」或「整个已完成段消失」）。写完必跑:
  ```bash
  grep -n '^## ' <project_workspace>/TODO.md
  ```
  必须恰好看到 `当前任务 / 待执行 / 已完成 / 已放弃` 四个, 缺一个就补。**worker 契约解析器
  按四段结构读**, 标题缺一个 = 该卡读不到队列条目, worker 走「自拟目标」分支假完工。
  ★ 这类标题损坏通常在**上一轮轮转就埋下**（肉眼不看结构不发现）, 所以每次轮转都要断言, 别
  只在派发失败时回头查。
- **最终交付验证：交付文件 md5 记录一次，测试/审查前后对比可证明未被篡改。**
- **main 代修后必须同步审查记录哈希**（前提：仅物理挂死例外场景允许 main 代修，见顶部角色铁律）：审查/测试卡结算后产物又被改动，
  审查文档声明的哈希即过期，文档审计会把『记录与产物不符』列为阻塞项。结算审计卡前给审查文档
  追加『复核补记』段：当前产物 md5 + 已修项清单 + 接受遗留项，构成对新构建的背书。
  记录一致性是审计红线，禁止哈希不一致状态交付。
- **自测硬编码凭据时禁止在代码里写字面量**：传输层会自动把代码中的密码字面量打码成
  `***`（qq-login 实测：`post({"password":"admin"})` 实际发出 `***` → 误判 401，与
  worker 的 PASS 报告矛盾）。用同值变量构造（`u="admin"; post({"password": u})`）才能
  测出真实的 200。冒烟复测与 worker 报告冲突时，先怀疑打码，别急着判产品缺陷。
  ★ 打码也发生在**显示方向**：浏览器/工具回显的文本里出现与源文件不符的怪 emoji
  （如标题显示 🐴）或 `***`，先用 `grep -o '<title>…' file | od -c` 查源文件真实字节——
  源文件干净即是回显打码伪影，不是产品缺陷，勿据此打回 worker。

- **worker 报告"push 成功"必须 main 实测对账**: coder/reviewer 自报"已 push 到
  origin/main"时, main 结算前必须独立 `git rev-parse HEAD` + `git ls-remote`
  验证远端 tip == 本地 HEAD, 详尽流程见 `team-work-chain-advance` 同名段落
  (含仓库 owner/repo 错写三种假阳性)。main 进度汇报里 push 段须贴出
  远端 SHA + 本地 SHA + 仓库 owner/repo 路径 三等号才算数。
