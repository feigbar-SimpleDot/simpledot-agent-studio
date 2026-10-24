"""mw — 确定性任务编排中间件。

入口: mw submit / mw harvest / mw reaper / mw status
设计文档: ../design.md
铁律: worker 自述不作为状态输入; harvest 失败不自动重派; LLM 不碰 kanban.db。
"""
__version__ = "0.1.0"
