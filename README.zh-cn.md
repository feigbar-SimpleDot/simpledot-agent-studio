# SimpleDot Agent Studio

**[`中文`](README.zh-cn.md)** | [English](README.md)

> 基于 Hermes 原生看板系统构建的多智能体长时任务管理工具。

## 这是什么

任务总线上的多容器智能体矩阵：**Main** 通过看板驱动工作流，**Worker**
容器隔离运行，中间件（`mw`）以**确定性代码**（而非 LLM）完成校验、派发、
收割与中继，Web 控制台让整个过程可观测、可操作。

## 设计理念（1A 边界，2026-10-03 确立）

> 确定性来自「让 LLM 只做对的事，并让它做不了别的」——而不是「让 LLM
> 做对的事」。

- **main 驱动看板，`mw` 只是管道**：建卡 / 领卡 / 结卡全是 main 的事；
  `mw` 只做校验、记账、拉起 worker 与中继——不跑外部命令、不写项目文件。
- **worker 自述永不作为状态输入**：裁决只依据物理证据——TODO.md 协议状态
  字段 + 产物文件 + 会话记录里的真实工具调用。
- **收割收的是档案，不是活物**：会话档案独立于进程存活；worker 崩溃后从
  会话锚点恢复同一会话，上下文零丢失。
- **失败只 BLOCK，绝不自动重试**：重试是 main 的显式决策，防止重复劳动。
- **严格串行**：一次只派发首卡，main 结卡后再派发下一张。
- **工作流管理在这里，智能体管理在那里**：worker 的模型 / SOUL / 技能由
  用户用 hermes-webui 或 Hermes TUI 自管，本平台刻意不代理智能体管理，
  以守住「worker 无共享卷挂载、无看板感知」的隔离边界。

## 快速开始

```bash
cp .env.example .env && vi .env    # 填 API_SERVER_KEY / 模型端点
./deploy/install.sh                # 一键拉起 main（中间件 + 控制台）
./deploy/verify.sh                 # 全绿即就绪
open http://<host>:8700            # 控制台：聊天 / 看板 / 文件 / worker
```

## 延伸阅读

| 主题 | 文档 |
|---|---|
| 部署：三步部署 / 添加 worker / 特殊需求 / 排障 | [DEPLOY.md](DEPLOY.md) |
| 完整架构设计（1A 边界 / 收割流程 / 部署约束） | [design.md](design.md) |
| Team Work 流程：边界矩阵 / workflow 与 registry schema / 步骤跳过 / TODO.md 契约 | [design.md §11](design.md) |
| i18n v1.0：部署期 `LANG` / `GET /api/lang` / 运行时 Settings 切换 | [design.md §10](design.md) |
| Main 平台默认 SOUL | [docs/main-soul.zh-cn.md](docs/main-soul.zh-cn.md) / [docs/main-soul.en.md](docs/main-soul.en.md) |
| Worker SOUL 运行时模板 | `mw/soul_template.zh-cn.md` / `mw/soul_template.en.md` |
| 变更日志（按合入顺序, 最新在前） | [CHANGELOG.md](CHANGELOG.md) |
| English README | [README.md](README.md) |

## 视频教程

- [上手教程](https://youtu.be/2eWsBmwHzT0?si=NDQCfmLtZHU50LLt)
- [进阶教程](https://youtu.be/ymXLxXRYZWk?si=K2SMFhe5qWcQah89)

中文视频请搜索视频号 “SimpleClaw智能体工厂”。

## 致谢

本平台建立在以下开源项目的肩膀上：

| 项目 | 在本平台中的角色 |
|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | 本平台编排的智能体运行时——网关、原生看板、技能与会话模型。 |
| [gufo](https://github.com/gufo-org/gufo) | 开源 Strix Halo AI 引擎——为本套环境提供本地模型能力。 |
| [halogen-flash-server](https://github.com/peonist-ai/halogen-flash-server) | 面向智能体工作负载的高速本地 Qwen3.8-Flash-Next 推理——本地模型栈的另一半。 |
| [Hermes WebUI](https://github.com/nesquena/hermes-webui) | Hermes Agent 的网页界面；用户借此管理各 worker 的模型 / SOUL / 技能。 |
| [pixel-agents](https://github.com/pixel-agents-hq/pixel-agents) | 可选的像素办公室（pixel-office）看门狗，经同源 `/pixel` 代理接入控制台。 |
| [React](https://github.com/facebook/react) + [i18next](https://github.com/i18next/i18next) | 控制台 UI 与双语（英 / 简中）渲染。 |
| [marked](https://github.com/markedjs/marked) + [DOMPurify](https://github.com/cure53/DOMPurify) | 控制台 Markdown 渲染与 XSS 净化。 |
| [Vite](https://github.com/vitejs/vite) + [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | 控制台构建工具与样式。 |

感谢所有维护者。

## 支持

喜欢这个项目？欢迎 [请作者喝杯咖啡](https://buymeacoffee.com/feigbar) 支持它的持续开发。
