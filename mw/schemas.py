"""Pydantic 硬契约 — LLM 与流程之间唯一的表达空间。

1A 边界 (2026-10-03 减法重构):
- task_id: main 建好的 kanban 卡 id (dispatch 必填)。mw 不建卡, 只为卡起 worker。
- check_script 已删: mw 不执行任何外部命令, 需要跑脚本验证由 main 自己做。
- parent_task_id 已删: kanban link/parent 拓扑是 main 建卡时的事, 不经 mw。
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class WorkerTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str  # 必须命中注册表, 否则 422


class HarvestContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_files: List[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    # 故意不提供 auto_retry: 重派不在 LLM 可表达的空间里
    on_evidence_missing: Literal["block_for_human"] = "block_for_human"
    # required_passes 已废弃 (2026-10-08 边界减法): 原用于 pass_threshold
    # 门控 (PASS_BUMPED「通过 N 次才 COMPLETE」), 该门控已删 —— 重复执行由
    # main 建多张卡实现。字段保留仅为向后兼容旧 dispatch payload
    # (model_config extra="forbid" 会拒未知字段), mw 不再读取或计数。
    required_passes: Optional[int] = None
    # product_workspace (产物共享根) — required_files 按它解析而不是
    # worker 的临时 .tasks/<id>/。项目任务必须传, verify_physical 需要它。
    product_workspace: Optional[str] = None


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_description: str
    context_files: List[str] = Field(default_factory=list)
    # worker 提示词由中间件从本结构程序化生成, LLM 不能直接塞裸 prompt


class TaskManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 夹带私货直接拒
    title: str
    target: WorkerTarget
    input_spec: InputSpec
    harvest: HarvestContract
    idempotency_key: str
    # 项目工作区(可选): main 预先创建的共享项目目录(如代码仓库)。
    # 给了就不再建 tasks/<task_id> 临时目录, worker 直接在项目目录干活。
    # 校验: 必须在 MW_ALLOWED_WS_PREFIX 之下。
    project_workspace: Optional[str] = None
    # main 建好的 kanban 卡 id。dispatch 必填 —— mw 只为已存在的卡起
    # worker, 卡的生命周期 (create/claim/complete) 全归 main。
    task_id: Optional[str] = None
    # 任务工作区 (可选): worker 的 scratch 目录 + .clarify.json 落点。
    # 不传时 mw 按 project_workspace/.tasks/<task_id> 推导。
    workspace: Optional[str] = None
