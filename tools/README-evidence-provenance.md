# 证据文件 provenance 头约定 (issue #107)

## 背景

170 验收 58 件证据文件中 25 件不含任何 170 路径/主机名/容器名标记,
42/43 不含 HEAD sha。下游独立复核被迫逐件回原机对撞原件 md5, 才能证明
「真机产出 vs 本机冒充」。本约定 + 采集脚本消除该复核成本。

## 团队约定

后续验收/测试产出的**每件证据文件首行**必须是一行 provenance 头, 格式固定:

```
# host=<主机名> head=<commit sha> cmd=<完整命令行> ts=<UTC ISO>
```

示例:

```
# host=dev-170 head=3f2a1b9c8d7e6f50413223140596a7b8c9d0e1f2 cmd=deploy/verify.sh --170 ts=2026-10-25T03:04:05Z
```

字段口径:

- `host` — `socket.gethostname()`: 真机主机名/容器名, 区分产出机器。
- `head` — `git rev-parse HEAD`: 产出时代码版本; 非 git 环境降级为 `-`。
- `cmd` — 产出该证据的完整命令行 (`--cmd` 显式传入; 缺省取脚本 argv)。
- `ts` — UTC ISO8601 秒级 (`YYYY-MM-DDTHH:MM:SSZ`)。

## 用法 (采集脚本 tools/evidence_provenance.py)

**约定由脚本自动注入, 不靠人工。**

```bash
# 1) 给一批证据文件原地加头 (幂等: 首行已是头的文件跳过, 重复跑不重复加)
python tools/evidence_provenance.py out/evidence-*.md out/run-*.log

# 2) 生成证据时直接带出头 (适合 verify 类脚本组合)
{ python tools/evidence_provenance.py --print --cmd "deploy/verify.sh --170"; \
  deploy/verify.sh --170 > /dev/stdout; } > evidence-verify.txt

# 3) 只生成头不落盘
python tools/evidence_provenance.py --print --cmd "pytest tests/ -q"
```

退出码: `0` 全部成功(含幂等跳过), `1` 有文件缺失/不可读写。

每件文件的处理结果打在 stdout: `added` (新加头) / `skipped` (已有头, 幂等)
/ `replaced` (近似头被替换)。首行是手写注释而跳过时, stderr 另有一条 `NOTE`
提示 (见下『幂等判定口径』)。

## 复核方怎么读

拿到证据文件先看首行: `host` 应为验收机(如 170)的主机名/容器名而非本机;
`head` 应能在原机 `git rev-parse HEAD` 对上; `ts` 与验收窗口一致。
三项任一不符即需人工介入, 无需逐件 md5 对撞。

## 纪律

- 头必须是**首行**且单行; 幂等判定只认首行是否匹配上述格式。
- 采集动作放进产出脚本/流程里 (如 verify 包装、测试报告生成),
  事后补头只作为存量证据的补救手段。
- 回归守护: `tests/test_issue107_provenance.py` (格式 / 幂等 / sha 一致性)。

## 幂等判定口径 (2026-10-25 D-2 修, Gitea #112)

首行分四类, **任何一类都不会产生双头** (旧判定只认严格正则, 首行是手写
注释时会被判「无头」⇒ 叠加第二行头):

| 首行形状 | 判定 | 动作 |
|---|---|---|
| 严格匹配 `# host=… head=… cmd=… ts=…` | 已有合规头 | `skipped` (幂等, 文件一字不动) |
| `#` 开头且含 `host=`/`head=`/`cmd=`/`ts=` 字段标记 | 近似头 (旧头/手抄头) | `replaced` —— 该行被**替换**为新采集的头 (旧 host/head/ts 已过期), 不留旧行 |
| `#` 开头的其它手写注释 (标题/说明) | 手写注释 | `skipped` —— 原行一字不动, stderr 打 `NOTE` 提示 (要加头用 `--force`) |
| 非注释首行 (命令输出/正文) | 无头 | `added` —— 头加在首行, 正文逐字节保留 |

CLI 状态词: `added` / `skipped` / `replaced`。
`--force`: 首行是手写注释时仍强制在首行之上加头 (默认不触发, 防双头)。
