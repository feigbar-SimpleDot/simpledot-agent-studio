"""mw console — Web 控制台后端(stdlib, 零新依赖)。

与中间件同容器部署(main), 只读数据源:
  kanban.db(/opt/data/kanban.db) + mw ledger + registry.json
写操作仅限 registry.json(worker 增删改, 原子写)。

启动: python3 -m mw.console  (端口 MW_CONSOLE_PORT, 默认 8700)

# [en] mw.console — Web console backend (stdlib, zero new deps).
# Deployed in the same container as the middleware (main); read-only data
# sources: kanban.db (/opt/data/kanban.db) + mw ledger + registry.json
# Write is limited to registry.json (worker CRUD, atomic write).
# Startup: python3 -m mw.console  (port MW_CONSOLE_PORT, default 8700)
#
# i18n v1.0 (2026-10-14): SOUL_TEMPLATE default is now language-aware
# (architecture §4.3); MW_SOUL_TEMPLATE env override still wins. Added
# GET /api/lang (design.md §10) for the deployment-language endpoint
# the interaction-design decision canon in §5.3 chose (Option A).
"""
from __future__ import annotations

import binascii
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from contextlib import suppress  # issue #63: 排空模式吞读异常
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

KANBAN_DB = os.environ.get("MW_KANBAN_DB", "/opt/data/kanban.db")
LEDGER_DB = os.environ.get("MW_LEDGER_DB",
                          os.path.join(os.environ.get("MW_HOME", "/mw_data"),
                                      "ledger.db"))
REGISTRY = os.environ.get("MW_REGISTRY",
                         os.path.join(os.path.dirname(__file__), "registry.json"))
# i18n v1.0 (架构 §4.3): worker SOUL 模板默认按部署语言选(语言切换入口在
# install.sh LANG;运行时由 resolve_lang() 决定)。MW_SOUL_TEMPLATE env
# 仍可显式覆盖(env override 优先,行为兼容)。
from .i18n import resolve_lang as _i18n_resolve_lang, t as _t  # i18n (架构 §5.2): note 写时 t(), 历史数据不动
SOUL_TEMPLATE = os.environ.get(
    "MW_SOUL_TEMPLATE",
    os.path.join(os.path.dirname(__file__),
                 f"soul_template.{_i18n_resolve_lang()}.md"))
SETTINGS = os.environ.get("MW_SETTINGS",
                         os.path.join(os.environ.get("MW_HOME", "/mw_data"),
                                     "settings.json"))
# Issue #51 (2026-10-17): worker 头像文件化,registry 只存裸文件名;
# 实际头像字节落在注册表同级目录的 avatars/ 子目录里。容器内 /mw_data,
# 宿主 data/mw_data —— 与 registry.json 同 bind mount, 写宿主即写容器。
AVATAR_DIR = os.environ.get(
    "MW_AVATAR_DIR",
    os.path.join(os.path.dirname(REGISTRY), "avatars"))
# 头像文件魔数嗅探白名单: 与前端校验集合一致 (png/jpg/webp/gif)。
# 拒绝任何以扩展名/MIME 头自报的伪造, 以解码后前 12 字节为准。
_AVATAR_MAGIC = (
    # (prefix_bytes, mime, ext, secondary_offset_or_None, secondary_bytes)
    # secondary 字段: RIFF 容器需校验字节 8:12 == b"WEBP", 否则 WAV/AVI 冒充 webp
    # 通过嗅探; 其他格式 None 即只判前缀。Issue #51 R-2 (2026-10-17)。
    (b"\x89PNG\r\n\x1a\n", "image/png",  "png",  None, None),
    (b"\xff\xd8\xff",       "image/jpeg", "jpg",  None, None),
    (b"RIFF",               "image/webp", "webp", 8,    b"WEBP"),
    (b"GIF87a",             "image/gif",  "gif",  None, None),
    (b"GIF89a",             "image/gif",  "gif",  None, None),
)
# 头像最大解码后字节数 (200KB, 与前端校验对齐); 拒收的阈值是字节数。
AVATAR_MAX_BYTES = 200 * 1024
# registry 中 avatar 字段允许的最大长度(裸文件名形态, 远短于 128)。
AVATAR_NAME_MAX_LEN = 128
# 文件名格式正则: <worker>-<8 hex>.<png|jpg|jpeg|webp|gif>。
_AVATAR_NAME_RE = re.compile(
    r"^[A-Za-z0-9_.\-]+-[0-9a-f]{8}\.(png|jpg|jpeg|webp|gif)$")
# worker key 白名单 (Issue #51 R-1, 2026-10-17): api_registry_save 写入侧未校验
# worker 名字符集, _save_avatar_data_url 直接 f"{worker_name}-{suffix}.{ext}"
# 拼接 → '../evilworker' / '/abs/path' 可逃出 AVATAR_DIR。前端 WorkersView.jsx L395
# 与 api_compose_generate 已采用同纪律, 此处统一为单点真源。
# s3d-fix N-1 加固 (2026-10-17): 显式排除 '.'/'..' — 正则集 [A-Za-z0-9_.-]+ 本身就
# 接受它们, 单凭正则无法挡。realpath 双保险对 '../' 类穿透有效, 但对纯字面 '.'
# (落盘成 <.>-<8hex>.<ext>) 与 '..' 也只是经 symlink 解析, 在容器内路径语义里
# 仍有歧义。最稳: 在正则前就拒。同步在测试 test_r1_rejects_dot_dot 里加断言。
_WORKER_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_RESERVED_WORKER_KEYS = frozenset({".", ".."})
# _AVATAR_MAGIC 第 3 项 webp 容器魔数 (RIFF 头 + 字节 8:12 == 'WEBP'), 见上方 tuple 注释。
PORT = int(os.environ.get("MW_CONSOLE_PORT", "8700"))

# 公共配置默认值(设置页管理; 不含自动重试——失败只 BLOCK 是硬原则)
# 注意区分 [宿主机] 与 [容器内] 路径; 容器内路径由 compose 固定, 不在此配置。
# (shared_workspace/task_dir_base 已移除: 实际生效值是 compose env
#  MW_TASK_DIR_BASE/MW_ALLOWED_WS_PREFIX, 设置页再放一份是第二真源)
SETTINGS_DEFAULTS = {
    "shared_workspace_host": "",                # [宿主机] 共享卷源路径, 生成 worker compose 的 volume 用; install.sh 按本机生成
    "docker_compose_dir": "",                   # [宿主机] 生成的 worker compose 建议存放目录; install.sh 按本机生成
    "default_timeout_seconds": 1800,            # 任务超时(dead-man's switch)
    "reaper_interval_seconds": 15,              # reaper 轮询间隔
    "default_image": "nousresearch/hermes-agent:v2026.9.24",
    "hermes_webui_url": "",   # hermes-webui 跳转地址(外部完整 WebUI/Agent 管理); 留空则顶栏不显示入口
    "pixel_office_enabled": False,  # 像素办公室(pixel-agents)开关: 控制控制台 /pixel 代理 + iframe tab
}

from .ledger import Ledger
from .ledger import SessionAlreadyBoundError
from . import pixel_proxy
from . import todo as _todo


def pixel_enabled() -> bool:
    """像素办公室开关(委托 pixel_proxy, 设置页优先, env 兜底)。"""
    return pixel_proxy.pixel_enabled()


def api_gwconfig(host_header: str = "") -> dict:
    """给前端对话页提供 gateway 连接参数, 免用户手填。

    部署形态: 前端走同源代理(本 console 转发到 gateway), 密钥由服务端注入,
    永不下发到浏览器。proxy=true 时前端 base 用 ''(相对路径)即可。
    """
    key = os.environ.get("API_SERVER_KEY", "")
    return {
        "proxy": True,                      # 前端据此走同源相对路径
        "proxy_prefixes": ["/api/sessions", "/api/chat/"],
        "enabled": bool(os.environ.get("API_SERVER_ENABLED")),
        "has_key": bool(key),
    }


UPLOAD_DIR = os.environ.get("MW_UPLOAD_DIR", "/share_workspace/uploads")
UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20MB


def api_upload(payload: dict) -> dict:
    """接收 base64 附件, 落盘到共享卷, 返回 main agent 可读的绝对路径。

    前端把文件读成 base64 POST 过来; 存到 UPLOAD_DIR(main 容器内可读),
    消息正文里带路径, main agent 用 read_file 直接读——不依赖 gateway
    的 dashboard 上传端点(那个不在 :8642 上)。
    """
    import base64
    import secrets
    import time as _t
    name = str(payload.get("name") or "file")
    data_url = str(payload.get("data") or "")
    if not data_url:
        raise ValueError("缺少附件数据")
    # 剥 data URL 前缀: data:<mime>;base64,xxxx
    if "," in data_url[:128] and data_url.startswith("data:"):
        b64 = data_url.split(",", 1)[1]
    else:
        b64 = data_url
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise ValueError("附件 base64 解码失败")
    if len(raw) > UPLOAD_MAX_BYTES:
        raise ValueError(f"附件超过 {UPLOAD_MAX_BYTES // (1024 * 1024)}MB 上限")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-") or "file"
    ts = _t.strftime("%Y%m%d_%H%M%S")
    target = Path(UPLOAD_DIR) / f"{ts}_{secrets.token_hex(3)}_{safe}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {"ok": True, "path": str(target), "name": name, "bytes": len(raw)}


def _ui_stamp() -> dict:
    """2026-10-10: 前端构建 stamp.

    install.sh 在 §4 把 build_md5/build_size/installed_at 写到 mw/ui_dist/.stamp.json;
    console.py 暴露给 /api/settings 让前端 detect 版本变化并提示 hard-reload.

    找不到 stamp 文件时回退 index.html 的 md5 + mtime (保证 deploy stamp 永远有值,
    避免前端因 stamp 缺失误判为陈旧)。
    """
    candidates = [
        os.environ.get("MW_CONSOLE_HTML", "") + ".stamp.json",
        "/share_workspace/ui_dist/.stamp.json",
        os.path.join(os.path.dirname(__file__), "ui_dist", ".stamp.json"),
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            try:
                return {"source": cand,
                        **json.loads(Path(cand).read_text())}
            except (OSError, json.JSONDecodeError):
                pass
    # 回退: 用 index.html 的 md5 + mtime
    fallback = [
        "/share_workspace/ui_dist/index.html",
        os.path.join(os.path.dirname(__file__), "ui_dist", "index.html"),
    ]
    for fb in fallback:
        if os.path.exists(fb):
            st = os.stat(fb)
            return {"source": "fallback:" + fb,
                    "build_md5": _md5_file(fb),
                    "build_size": st.st_size,
                    "mtime": int(st.st_mtime)}
    return {"source": "none"}


def _md5_file(p: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def api_settings_get() -> dict:
    try:
        cur = json.loads(Path(SETTINGS).read_text())
    except (OSError, json.JSONDecodeError):
        cur = {}
    merged = dict(SETTINGS_DEFAULTS)
    merged.update({k: v for k, v in cur.items() if k in SETTINGS_DEFAULTS})
    return {"settings": merged,
            "note": _t("console.note.settings_get"),
            "ui_stamp": _ui_stamp(),  # 2026-10-10: 暴露前端 stamp,前端发现 stamp 变可自动刷新
           }


def _clean_webui_url(v) -> str:
    """hermes_webui_url 只允许 http(s) 绝对地址, 防 javascript:/data: 注入。"""
    v = (v or "").strip()
    if not v:
        return ""
    low = v.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise ValueError("hermes_webui_url 必须以 http:// 或 https:// 开头")
    return v


def api_settings_save(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("settings 必须是对象")
    unknown = set(payload) - set(SETTINGS_DEFAULTS)
    if unknown:
        raise ValueError(f"未知配置项: {sorted(unknown)}")
    cur = dict(SETTINGS_DEFAULTS)
    try:
        cur.update(json.loads(Path(SETTINGS).read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    if "hermes_webui_url" in payload:
        payload = dict(payload)
        payload["hermes_webui_url"] = _clean_webui_url(payload["hermes_webui_url"])
    cur.update(payload)
    tmp = Path(str(SETTINGS) + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2))
    os.replace(tmp, SETTINGS)
    return {"ok": True, "settings": cur}


COMPOSE_TEMPLATE = """services:
  # 2026-10-22 issue #82: the service name doubles as the container's DNS alias
  # on the shared network, so it must be UNIQUE. A shared alias ("hermes")
  # resolves inside every container's own netns, so a cross-container call to
  # it silently reaches the wrong service. Keep it equal to container_name.
  hermes_{name}:
    image: {image}
    container_name: hermes_{name}
    restart: unless-stopped
    command: gateway run
    # issue #109 (2026-10-26): 模版不再含 ports: 宿主映射。worker 的 gateway
    # 只经容器内网别名 http://hermes_<name>:8642 被访问 (simple-dot 网络),
    # 宿主发布端口自 issue #82 起即为冗余, 且经 docker-proxy 反而更易掉线。
    networks:
      # 2026-10-22 issue #82: every worker joins the shared "simple-dot" network
      # and gets its OWN unique alias. main and the workers then talk over
      # container-internal addresses (http://hermes_<name>:8642) instead of
      # published host ports, which are proxied by docker-proxy inside the VM
      # and drop connections under memory pressure.
      simple-dot:
        aliases:
          - hermes_{name}
    volumes:
      # 2026-10-22 issue #82: /opt/data is a NAMED VOLUME, never a host bind mount.
      # On a virtiofs host mount (lima/Colima on macOS) a socket inode can become
      # unreachable *inside the container* (stat/unlink -> E95) while the host
      # still sees a normal socket, which kills the gateway on every restart.
      # A named volume lives on the VM's ext4 disk and has none of these semantics.
      - hermes-{name}:/opt/data
      - {shared_host}:/share_workspace
    environment:
      - API_SERVER_ENABLED=true
      - API_SERVER_HOST=0.0.0.0
      - API_SERVER_KEY={api_key}
      - API_SERVER_CORS_ORIGINS=*
      - HERMES_WRITE_SAFE_ROOT=/share_workspace
      - HERMES_YOLO_MODE=1

volumes:
  hermes-{name}:

networks:
  simple-dot:
    external: true
"""


def api_compose_generate(payload: dict) -> dict:
    """按模版生成 worker 的 docker-compose.yaml(只生成, 不执行)。"""
    name = (payload.get("name") or "").strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("worker 名称只允许字母数字和 -_")
    # issue #109 (2026-10-26): port 不再必填 —— 模版已删宿主端口映射,
    # worker 走容器内网别名 http://hermes_<name>:8642。传入的 port 一律忽略,
    # 保持对旧前端调用的向后兼容 (不报错)。
    api_key = (payload.get("api_key") or "").strip()
    if len(api_key) < 16:
        raise ValueError("api_key 至少 16 位")
    try:
        s = json.loads(Path(SETTINGS).read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    cfg = dict(SETTINGS_DEFAULTS)
    cfg.update({k: v for k, v in s.items() if k in SETTINGS_DEFAULTS})
    yaml_text = COMPOSE_TEMPLATE.format(
        name=name, api_key=api_key,
        image=payload.get("image") or cfg["default_image"],
        shared_host=payload.get("shared_host") or cfg["shared_workspace_host"])
    return {"yaml": yaml_text,
            "compose_dir_hint": f"{cfg['docker_compose_dir']}/{name}/docker-compose.yaml",
            "next_steps": [
                f"mkdir -p {cfg['docker_compose_dir']}/{name} && 保存为 docker-compose.yaml",
                f"cd {cfg['docker_compose_dir']}/{name} && docker compose up -d",
                f"回到本控制台『添加 Worker』注册: api_url=http://hermes_{name}:8642 (容器内网地址, 走 simple-dot 网络)"]}


def api_workflows() -> dict:
    """返回 workflows.yaml 的完整内容(JSON)。

    平台托管的 workflow 模板(跟 registry.json 平级)。
    LLM 通过此端点查询可用 workflow 列表,前端 Team Work 弹窗
    填充下拉选项也调这个端点。

    加载策略:
    - mtime-based reload(避免每次请求读盘,但支持热更新)
    - yaml 解析失败时返回 fallback {auto: ...}, 不阻塞前端
    """
    src_path = os.environ.get(
        "MW_WORKFLOWS_PATH",
        os.path.join(os.path.dirname(__file__) or ".", "workflows.yaml"))
    cache_path = os.environ.get(
        "MW_WORKFLOWS_CACHE",
        os.path.join(os.path.dirname(__file__) or ".", "_workflows_cache.json"))

    src_mtime = os.path.getmtime(src_path) if os.path.exists(src_path) else 0
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get("_mtime", 0) >= src_mtime and cached.get("_mtime", 0) > 0:
                cached.pop("_mtime", None)
                return cached
        except Exception:
            pass

    if not os.path.exists(src_path):
        return {"workflows": {"auto": {"name": "Auto-select",
                                        "description": "workflows.yaml 未找到,只有 auto"}}}

    try:
        import yaml
        with open(src_path) as f:
            d = yaml.safe_load(f) or {}
        out = {"workflows": d.get("workflows", {})}
        try:
            with open(cache_path, "w") as f:
                json.dump({**out, "_mtime": src_mtime}, f)
        except Exception:
            pass
        return out
    except Exception as e:
        return {"workflows": {"auto": {"name": "Auto-select",
                                        "description": f"workflows.yaml 解析失败: {e}"}}}


def api_decision_paths() -> dict:
    """返回决策路径路由表 (issue #70)。

    main 拆解首卡前 GET 本端点做粗分类: 每条 path 带 match.any_of /
    none_of / route_to / role_tags / forbidden_for_main。
    route_to 是**语义角色名**而非 profile 名 —— 选人由 main 运行时现读
    /api/workers 按 scope.tags 语义匹配, mw 只做确定性校验不做语义选人。
    yaml 缺失/损坏时降级 {'paths': []}, 不抛 (与 api_workflows 同纪律)。
    """
    from .decision_paths import load_paths
    return {"paths": load_paths()}


def api_decision_paths_classify(qs: dict) -> tuple[int, dict]:
    """GET /api/decision-paths/classify?text=<输入原文> → 粗分类判定 (issue #95)。

    把 mw/decision_paths.classify() 端点化 (REVIEW-78-70 §2.2 建议 (a)):
    响应直接是判定 dict — path_id / label / matched_signals /
    vetoed_signals / score / fallback。SOUL/skill 由此同步为「调端点拿
    判定」, 判定口径只剩 mw/decision_paths.py 一份。

    - text 参数缺席 ⇒ 400 (端点契约要显式); 空串是合法输入 ⇒
      path_id null + fallback ask_user, 不是 400。
    - 判定是纯函数, 零 IO; yaml 缺失/损坏 ⇒ load_paths 降级空表 ⇒
      path_id null, 永不 500 (与 api_decision_paths 同降级纪律)。
    """
    from .decision_paths import classify
    text = (qs.get("text") or [None])[0]
    if text is None:
        return 400, {"error": "missing required query parameter: text "
                              "(GET /api/decision-paths/classify?text=...)"}
    return 200, classify(text)


# workflow id 白名单: 小写字母/数字/连字符, 防路径穿越与非法 key
WF_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# split_policy 合法枚举
WF_SPLIT_POLICIES = {"must_split", "prefer_split", "allow_same"}


def _workflows_paths():
    src_path = os.environ.get(
        "MW_WORKFLOWS_PATH",
        os.path.join(os.path.dirname(__file__) or ".", "workflows.yaml"))
    cache_path = os.environ.get(
        "MW_WORKFLOWS_CACHE",
        os.path.join(os.path.dirname(__file__) or ".", "_workflows_cache.json"))
    return src_path, cache_path


def _invalidate_workflows_cache(cache_path: str) -> None:
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except OSError:
        pass


def _validate_workflow_body(body: dict) -> tuple[bool, str]:
    """校验 workflow 定义。返回 (ok, error_msg)。"""
    if not isinstance(body, dict):
        return False, "body 必须是对象"
    name = (body.get("name") or "").strip()
    if not name:
        return False, "name 必填"
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        return False, "steps 至少一个"
    seen_ids = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return False, f"steps[{i}] 必须是对象"
        sid = (s.get("id") or "").strip()
        if not sid:
            return False, f"steps[{i}].id 必填"
        if sid in seen_ids:
            return False, f"steps[{i}].id 重复: {sid}"
        seen_ids.add(sid)
        if not (s.get("name") or "").strip():
            return False, f"steps[{i}].name 必填"
        # description: 本步骤主要任务, 500 字以内 (可选但强烈建议)
        desc = s.get("description")
        if desc is not None:
            if not isinstance(desc, str):
                return False, f"steps[{i}].description 必须是字符串"
            if len(desc.strip()) > 500:
                return False, (f"steps[{i}].description 超长: "
                              f"{len(desc.strip())} 字符 > 500")
        # input / output: 输入/输出文档, 可选, 字符串数组
        for field in ("input", "output"):
            v = s.get(field)
            if v is not None:
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    return False, f"steps[{i}].{field} 必须是字符串数组"
        # depends_on: 可选, 字符串数组 (元素在第二轮校验是否为已定义 step)
        dep = s.get("depends_on")
        if dep is not None:
            if not isinstance(dep, list) or not all(isinstance(x, str) for x in dep):
                return False, f"steps[{i}].depends_on 必须是字符串数组"
        tags = s.get("required_role_tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return False, f"steps[{i}].required_role_tags 必须是字符串数组"
        sp = s.get("split_policy")
        if sp is not None and sp not in WF_SPLIT_POLICIES:
            return False, (f"steps[{i}].split_policy 非法: {sp} "
                          f"(合法: {sorted(WF_SPLIT_POLICIES)})")
        # loops 可选, 旧 workflow 不写默认 []
        loops = s.get("loops")
        if loops is not None:
            if not isinstance(loops, list) or not all(isinstance(x, str) for x in loops):
                return False, f"steps[{i}].loops 必须是字符串数组"
        # pass_threshold 已废弃 (2026-10-08): 不再校验, 也不再拷贝/计数/门控。
        # 重复执行由 main 建多张卡实现。旧 workflow 里残留的 pass_threshold
        # 字段被静默忽略 (向后兼容, 不报错)。
        # optional: 布尔 (main 可否跳过); skip_when: 任务类型提示数组
        opt = s.get("optional")
        if opt is not None and not isinstance(opt, bool):
            return False, f"steps[{i}].optional 必须是布尔"
        sw = s.get("skip_when")
        if sw is not None:
            if not isinstance(sw, list) or not all(isinstance(x, str) for x in sw):
                return False, f"steps[{i}].skip_when 必须是字符串数组"
    # 第二轮: 校验 loops / depends_on 元素必须是已定义的 step_id
    for i, s in enumerate(steps):
        loops = s.get("loops") or []
        for ref in loops:
            if ref == s["id"]:
                return False, f"steps[{i}].loops[{ref}] 不能自指"
            if ref not in seen_ids:
                return False, f"steps[{i}].loops[{ref}] 引用了未定义的 step"
        for ref in (s.get("depends_on") or []):
            if ref == s["id"]:
                return False, f"steps[{i}].depends_on[{ref}] 不能自指"
            if ref not in seen_ids:
                return False, f"steps[{i}].depends_on[{ref}] 引用了未定义的 step"
    return True, ""


def api_workflow_save(wf_id: str, body: dict) -> dict:
    """新建或更新一个 workflow, 写回 workflows.yaml。

    body: {name, description, steps: [{id, name, required_role_tags, split_policy?}]}
    """
    if not WF_ID_RE.match(wf_id):
        return {"error": f"workflow id 非法(仅小写字母/数字/连字符): {wf_id}",
                "code": 422}
    ok, err = _validate_workflow_body(body)
    if not ok:
        return {"error": err, "code": 422}

    src_path, cache_path = _workflows_paths()
    try:
        import yaml
    except ImportError:
        return {"error": "后端缺 PyYAML", "code": 500}

    # 读现有全部 workflow
    data = {}
    if os.path.exists(src_path):
        try:
            with open(src_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            return {"error": f"读 workflows.yaml 失败: {e}", "code": 500}
    if not isinstance(data, dict):
        data = {}
    wfs = data.get("workflows", {}) or {}

    is_new = wf_id not in wfs
    wf = {
        "name": (body.get("name") or "").strip(),
        "description": (body.get("description") or "").strip(),
        "steps": [
            {k: v for k, v in {
                "id": s["id"].strip(),
                "name": s["name"].strip(),
                "description": (s.get("description") or "").strip() or None,
                "input": s.get("input") or None,
                "output": s.get("output") or None,
                "default_assignee": (s.get("default_assignee") or "").strip() or None,
                "required_role_tags": s.get("required_role_tags", []),
                "split_policy": s.get("split_policy"),
                # 跳过语义 (2026-10-06): optional=true 表示 main 可决策跳过;
                # skip_when 是给 main 的任务类型提示。false/None 不落盘。
                "optional": s.get("optional") if s.get("optional") is True else None,
                "skip_when": s.get("skip_when") or None,
                "depends_on": s.get("depends_on") or None,
                "loops": s.get("loops"),
            }.items() if v is not None}
            for s in body["steps"]
        ],
    }
    wfs[wf_id] = wf
    data["workflows"] = wfs

    # 原子写: tmp + rename
    tmp = src_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, src_path)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"error": f"写 workflows.yaml 失败: {e}", "code": 500}

    _invalidate_workflows_cache(cache_path)
    return {"ok": True, "workflow_id": wf_id, "created": is_new}


def api_workflow_delete(wf_id: str) -> dict:
    """删除一个 workflow。"""
    if not WF_ID_RE.match(wf_id):
        return {"error": f"workflow id 非法: {wf_id}", "code": 422}
    src_path, cache_path = _workflows_paths()
    try:
        import yaml
    except ImportError:
        return {"error": "后端缺 PyYAML", "code": 500}
    if not os.path.exists(src_path):
        return {"error": "workflows.yaml 不存在", "code": 404}
    try:
        with open(src_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return {"error": f"读失败: {e}", "code": 500}
    wfs = data.get("workflows", {}) or {}
    if wf_id not in wfs:
        return {"error": f"workflow 不存在: {wf_id}", "code": 404}
    del wfs[wf_id]
    data["workflows"] = wfs
    tmp = src_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, src_path)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"error": f"写失败: {e}", "code": 500}
    _invalidate_workflows_cache(cache_path)
    return {"ok": True, "deleted": wf_id}


def _reconcile_archived_orphans(led) -> list:
    """issue #106 方案 (a): kanban 已归档卡的孤儿契约行联动推终态。

    滞留链路: main 用 `hermes kanban archive` 直建/直归档的卡不经过 mw
    任何端点 (reaper T3 只结算 ACTIVE run 的 current_card), 其契约行
    永远滞留 PENDING —— launch 护栏因此 409 (orphan_pending_cards),
    「无 PENDING 孤儿」巡检口径也被污染 (170 实测: s4 两张测试卡)。

    本函数在「孤儿面被消费的时刻」(launch 护栏 / 任务列表巡检) 做一次
    对账: 孤儿 task_id ∩ kanban status='archived' → 走正规状态机入口
    Ledger.sync_archived_contract_states 推到 ARCHIVED (带事件与审计,
    非裸 UPDATE)。kanban.db 不可读 / 无孤儿 / 单卡异常都安全降级为
    no-op —— 联动是增益路径, 绝不允许把消费方拖崩。
    返回实际被推入 ARCHIVED 的 task_id 列表。
    """
    try:
        orphans = led.list_pending_orphan_cards(limit=200)
    except Exception:
        return []
    if not orphans:
        return []
    ids = [o["task_id"] for o in orphans]
    placeholders = ",".join("?" * len(ids))
    try:
        rows = _ro_query(
            KANBAN_DB,
            "SELECT id FROM tasks WHERE status='archived' "
            f"AND id IN ({placeholders})", tuple(ids))
    except Exception:
        return []
    archived_ids = [r["id"] for r in rows]
    if not archived_ids:
        return []
    try:
        return led.sync_archived_contract_states(archived_ids)
    except Exception:
        return []


def api_team_work_launch(payload: dict) -> dict:
    """Team Work 启动器 (1A 边界, 2026-10-03 减法重构 + T2 顶层对象化)。

    **mw 不建卡、不播种 TODO.md** —— kanban 卡由 main 自己
    `hermes kanban create --parent ...` 建好, TODO.md 由 main 自己写。
    本端点只做四件事:
      1. 校验 steps (assignee 在 registry / 不踩 excludes / must_split)
      2. 创建一条 team_work_runs (run_id 在 launch 时生成, 不从 manifest 派生)
      3. 登记发起 session (task_initiators, 报告中继路由表)
      4. 把当前卡槽 (current_card_id) 推到第一张, 然后派发第一张起 worker
         (严格串行, 后续卡由 main 在 advance 端点推进)

    入参:
      {
        "initial_task": str,           # 原始需求(回显用)
        "session_id":   str,           # main 所在 hermes session_id
        "project_workspace": str,      # 可选; 若有, 透传给 dispatch + 落 run 行
        "steps": [                     # main 已拆解 + 已建卡
          {
            "step_id":   str,
            "title":     str,
            "task_id":   str,          # ★ main 建好的 kanban 卡 id (必填)
            "assignee":  str,          # worker profile, 必须在 registry
            "required_files": [str],   # 可选
            "timeout_seconds": int,    # 可选, 默认 1800
          }, ...
        ]
      }
    返回:
      {"ok": true, "run_id": "<16 hex>", "cards": [...],
       "dispatched": <第一张 task_id>, ...}

    护栏:
      - 已有 ACTIVE run → 409 拒(UI 必须先 close 老的)
      - 内部异常(IntegrityError race)→ catch 转 409,DB partial unique 兜底
    """
    initial_task = (payload.get("initial_task") or "").strip()
    session_id   = (payload.get("session_id") or "").strip()
    steps_in     = payload.get("steps") or []

    if not initial_task:
        return {"error": "initial_task 必填", "code": 400}
    if not session_id:
        return {"error": "session_id 必填(由 main 填,标识发起 session)", "code": 400}
    if not isinstance(steps_in, list) or not steps_in:
        return {"error": "steps 必须是非空数组", "code": 400}

    # ── 步骤跳过 (路径 A 最小实现, dryrun/workflow-step-skip) ──
    # main / 产品经理可在每步带 include=false 标记该步跳过;被跳过的步
    # 完全不进 steps、ledger 不知道它存在。语义:跳过 = 物理不存在这张卡。
    # 审计: 被跳过的 step_id + skip_reason 收集到 skipped, 落到 run 行
    # 的 notes JSON, 关闭 run 时仍在产物包可查。
    skipped: list[dict] = []
    active_steps: list[dict] = []
    for s in steps_in:
        if not isinstance(s, dict):
            return {"error": "steps 项必须是对象", "code": 400}
        include = s.get("include", True)
        if include is False:
            skipped.append({
                "step_id": s.get("step_id"),
                "skip_reason": s.get("skip_reason") or "no reason given",
            })
            continue
        active_steps.append(s)
    if not active_steps:
        return {"error": "steps 全被跳过 (include=false), 至少保留 1 步",
                "code": 400, "skipped": skipped}
    steps_in = active_steps  # 后续逻辑全部基于裁剪后的数组

    try:
        registry = api_workers().get("workers", {})
    except Exception:
        registry = {}

    used_assignees: set[str] = set()
    for i, s in enumerate(steps_in):
        if not isinstance(s, dict):
            return {"error": f"steps[{i}] 不是对象", "code": 400}
        for f in ("step_id", "title", "task_id", "assignee"):
            v = (s.get(f) or "").strip() if isinstance(s.get(f), str) else s.get(f)
            if not v:
                return {"error": f"steps[{i}].{f} 必填 "
                                f"(task_id = main 建好的 kanban 卡 id; "
                                f"mw 不建卡, 卡由 main 自己 create)", "code": 400}
        assignee = s["assignee"].strip()
        if registry and assignee not in registry:
            return {"error": f"steps[{i}].assignee '{assignee}' 不在 registry,可选: "
                            f"{sorted(registry)}", "code": 422,
                    "available_assignees": sorted(registry)}
        required_tags = s.get("required_role_tags") or []
        if registry and required_tags:
            scope = (registry[assignee].get("scope") or {})
            scope_excludes = set(scope.get("excludes") or [])
            if set(required_tags) & scope_excludes:
                return {"error": f"steps[{i}].assignee '{assignee}' 拒绝该任务: "
                                f"required_tags {required_tags} 与 scope.excludes "
                                f"{sorted(scope_excludes)} 冲突", "code": 422}
        if s.get("split_policy") == "must_split" and assignee in used_assignees:
            return {"error": f"steps[{i}] split_policy=must_split 要求换 worker, "
                            f"但 '{assignee}' 已被同 batch 之前 step 使用",
                    "code": 422, "must_split_violation": True,
                    "used_assignees": sorted(used_assignees)}
        used_assignees.add(assignee)

    # === 2026-10-03 T2: 顶层对象化 — 建 run 行 + 登记卡 + 占槽 ===
    import uuid as _uuid
    led = Ledger(LEDGER_DB)
    try:
        # 应用层守卫 1: 已有 ACTIVE run → 409 拒
        # 应用层守卫 2: ledger 里还有孤儿 PENDING 卡(team_work_run_id
        # IS NULL)→ 409 拒。这些卡要么是前一轮 launch 半途崩了,要么
        # 是人工遗留,直接开新 run 会同时派同 task 制造混乱。
        # 操作员走 /api/team-work/<run_id>/close 关闭活跃 run 后,
        # 还要手工清孤儿(目前没有批量清端点 — 故意保留,免误删)。
        # issue #106: 先做一次「kanban 已归档」联动对账 —— 直建直归档的
        # 测试卡 (main `hermes kanban archive`, 不经 mw) 契约行滞留
        # PENDING, 正是本护栏最常见的假阳性阻塞源; 对账后仍留下的
        # 才是真孤儿, 继续拦。
        _reconcile_archived_orphans(led)
        blockers = []
        existing = led.get_active_team_work()
        if existing:
            blockers.append({
                "kind": "active_team_work",
                "run_id": existing["run_id"],
                "initial_task": existing["initial_task"],
                "current_card_id": existing["current_card_id"],
                "card_count": existing["card_count"],
                "created_at": existing["created_at"],
            })
        orphans = led.list_pending_orphan_cards(limit=50)
        if orphans:
            blockers.append({
                "kind": "orphan_pending_cards",
                "count": len(orphans),
                "in_more": False,  # 50 已封顶,真实数可能更大;UI 提示让操作员去看
                "sample": [{"task_id": o["task_id"],
                            "updated_at": o["updated_at"]}
                           for o in orphans[:10]],
            })
        if blockers:
            led.close()
            return {"error": "ledger 还有未结束任务, 禁止创建新 Team Work",
                    "code": 409,
                    "blockers": blockers,
                    "hint": ("先关闭 ACTIVE Team Work (POST "
                             "/api/team-work/<run_id>/close); 孤儿 "
                             "PENDING 卡目前需手工清理 (delete from "
                             "harvest_contracts where harvest_status="
                             "'PENDING' and team_work_run_id is null)"
                             "或完成它们后才会消失")}
        # 生成 run_id 并建行 — run_id 永不从 manifest 派生 (T1 决策)
        run_id = _uuid.uuid4().hex[:16]
        led.create_team_work_run(
            run_id, session_id, initial_task,
            project_workspace=payload.get("project_workspace"))
    except Exception as e:
        # DB partial UNIQUE INDEX 兜底 race: 两个并发都过 app guard 时,
        # 第二个被 SQLite 拒绝 → 409
        try:
            led.close()
        except Exception:
            pass
        # issue#12 R2 (2026-10-13): 改用 isinstance 判别专属异常
        # SessionAlreadyBoundError,不再嗅探 err 文案。ledger 改文案
        # 即静默退化为通用 409 的隐患已堵。
        # 窄化到 SessionAlreadyBoundError 而非 sqlite3.IntegrityError:
        # create_team_work_run 还有「已有 ACTIVE run」分支也抛
        # sqlite3.IntegrityError,前端拿到 session_reused=True 会盲目
        # 自动重拉 sid,但 ACTIVE run 还在——必须让用户先 close 老 run。
        # 那种 409 应走 blockers 路径而非 session_reused,所以这里必须
        # 用窄类型。DB 层 race 兜底若冒泡 IntegrityError,我们兜底退到
        # 通用 409,让上层发现 + 走老的『race/冲突』路径 (issue#8 行为不变)。
        if isinstance(e, SessionAlreadyBoundError):
            led2 = Ledger(LEDGER_DB)
            try:
                conflicts = led2.find_runs_by_initiator_session(session_id)
            finally:
                led2.close()
            return {
                "error": "session_id 已绑到既有 run, 请先新建对话再发起 "
                         "(避免多 run 共享 initiator 上下文污染)",
                "code": 409,
                "session_reused": True,
                "session_id": session_id,
                "conflicting_runs": [
                    {"run_id": c["run_id"],
                     "status": c["status"],
                     "created_at": c["created_at"],
                     "initial_task": (c["initial_task"] or "")[:80]}
                    for c in conflicts[:5]
                ],
                "hint": ("UI 应当先 g.createSession() 拿到新 sid 再 launch;"
                         "若已建, 直接 reload ChatView 重新发起。"),
            }
        return {"error": f"创建 run 失败 (race/冲突): {e}", "code": 409}

    # 先给每张卡写一份占位 contract (PENDING, 空 contract), 让
    # register_team_work_card 能 backfill team_work_run_id — 没有 contract
    # 行, advance/get 等端点的反查全部 404。
    # 若该 task 已有 contract (legacy 数据), put_contract 保留 pass_count
    # 只覆盖 contract_json + status, 不破坏既有契约。
    for s in steps_in:
        try:
            led.put_contract(s["task_id"], {"required_files":
                                          s.get("required_files") or []})
        except Exception:
            pass  # put 失败也走下面 register, register 对无 contract 行 no-op

    # 把全部卡登记到本 run (同步回写 harvest_contracts.team_work_run_id)
    for s in steps_in:
        led.register_team_work_card(run_id, s["task_id"])

    # 登记发起 session (报告中继路由表) — 全部卡先登记, 后续每张结算后
    # reaper 都能把报告送回 main。中继写失败不阻断 launch。
    for s in steps_in:
        try:
            led.record_initiator(s["task_id"], session_id)
        except Exception:
            pass

    # 把当前卡槽推到第一张 (advance 端点的前置: dispatch 前必须先 set)
    first = steps_in[0]
    # ★ REOPEN-DESIGN A-AC6 (P1, D4 门控): launch 首卡同检 TODO 绑定。
    # launch 未带 project_workspace → 跳过校验但响应带 warning (现有
    # 「先 launch 后绑目录」用法不打断, P1 不扩大爆炸半径)。
    launch_warning = None
    if payload.get("project_workspace"):
        ok_bind, bind_detail = _todo.verify_current_task_binding(
            f"{payload['project_workspace']}/TODO.md",
            first["task_id"], first.get("step_id"))
        if not ok_bind:
            # 首卡绑定失败 = 异常中止, 复用首派失败同款回滚路径
            try:
                led.archive_team_work_cards(run_id)
                led.close_team_work(run_id, "todo_binding_failed",
                                     final_status="ABORTED")
            finally:
                led.close()
            return {**_todo_binding_409(bind_detail), "run_id": run_id}
    else:
        launch_warning = "launch 未带 project_workspace, 跳过首卡 TODO 绑定校验"
    led.set_team_work_current_card(run_id, first["task_id"])
    led.close()

    # 只派第一张 (严格串行)。后续卡由 main 在 complete 当前卡后调
    # /api/team-work/<run_id>/advance 推进。
    dispatch_body = {
        "task_id": first["task_id"],
        "run_id": run_id,                # ★ T2 串行铁律: dispatch 必须带 run_id
        "title": first["title"],
        "target": {"profile": first["assignee"]},
        "input_spec": {"task_description": first.get("task_description")
                      or first.get("description")
                      or first["title"],
                      "context_files": first.get("input") or []},
        "harvest": {
            "required_files": first.get("required_files") or [],
            "timeout_seconds": first.get("timeout_seconds", 1800),
        },
        "idempotency_key": f"tw-{run_id}-{first['step_id']}",
    }
    if payload.get("project_workspace"):
        dispatch_body["project_workspace"] = payload["project_workspace"]

    r = api_dispatch(dispatch_body)
    if not r.get("ok") and not r.get("accepted"):
        # 派发失败回滚: 关 run + 归档卡, 避免留半截 ACTIVE run 阻塞后续
        led_rollback = Ledger(LEDGER_DB)
        try:
            led_rollback.archive_team_work_cards(run_id)
            # A-3: 回滚时卡已被 archive, 自动判定 residue=0 会误得 CLOSED;
            # 首派失败是异常中止, 必须显式 ABORTED (AC3-2)
            led_rollback.close_team_work(run_id, "first_dispatch_failed",
                                         final_status="ABORTED")
        finally:
            led_rollback.close()
        return {**r, "code": r.get("code", 500),
                "run_id": run_id,
                "note": _t("console.note.first_dispatch_failed")}

    return {
        "ok": True,
        "run_id": run_id,
        "initial_task": initial_task,
        "session_id": session_id,
        "cards": [{"task_id": s["task_id"], "step_id": s["step_id"],
                   "title": s["title"], "assignee": s["assignee"]}
                  for s in steps_in],
        "card_count": len(steps_in),
        "dispatched": first["task_id"],
        "skipped_steps": skipped,
        **({"warning": launch_warning} if launch_warning else {}),
        "note": (_t("console.note.launch_ok_base")
                 + (f" {_t('console.note.launch_skipped_suffix', n=len(skipped))}"
                    if skipped else "")),
        "created_at": time.time(),
    }


def _todo_binding_409(detail: dict) -> dict:
    """把 verify_current_task_binding 的 detail 包装成统一 409 响应。

    R1: 必须让 main 能自纠 —— error 一句话 + expected/actual 并列 + hint 指路。
    (REOPEN-DESIGN 1.4)
    """
    reason = detail.get("reason", "")
    hints = {
        "mismatch": ("先完成 TODO 轮转 (todo.rotate_to(上一卡) 或 rotate_next) "
                     "使『当前任务』指向本卡, 再 advance"),
        "no_binding": "在『当前任务』段补写 `- 卡: <task_id>` 或 `- 步骤: <step_id>`",
        "todo_missing": ("先在该 project_workspace 写 TODO.md 并把『当前任务』"
                        "指向本卡 (main 下发职责)"),
    }
    return {
        "ok": False, "code": 409,
        "error": "TODO.md『当前任务』未绑定本卡, 拒绝派发 (疑似轮转缺失)",
        "reason": reason,
        "expected": {"task_id": detail.get("expected_task_id"),
                     "step_id": detail.get("expected_step_id")},
        "actual": {"kanban_task": detail.get("actual_kanban_task"),
                   "step_id": detail.get("actual_step_id"),
                   "status": detail.get("actual_status")},
        "todo_path": detail.get("todo_path"),
        "hint": hints.get(reason, "检查 TODO.md『当前任务』绑定"),
    }


def api_team_work_advance(run_id: str, payload: dict) -> dict:
    """推进一个 team work 到下一张卡 (T2 串行铁律的关键端点)。

    调用时机: main 把当前卡 settle (COMPLETE / NEED_CLARIFICATION /
    FAILED 等) 后, 想要推进到下一张卡时调。 本端点:
      1) 校验 run 是 ACTIVE, 且 current_card_id 为 NULL (上一张已清)
      2) 校验 task_id 在 run 名下 (有 harvest_contracts 行 + team_work_run_id
         反查匹配)
      3) 把 current_card_id 设到 task_id (占槽)
      4) 透传一份 dispatch body 给 api_dispatch, 由 api_dispatch 的
         run_id 护栏再次校验 (防御深度)

    payload:
      {
        "task_id": str,              # 必填, 要推进到的下一张卡
        "target_profile": str,       # worker profile
        "title": str,                # 可选, 默认 task_id
        "required_files": [str],     # 可选
        "timeout_seconds": int,      # 可选, 默认 1800
        "task_description": str,     # 可选, 默认 title
        "project_workspace": str,    # 可选, 透传 dispatch
      }

    返回: api_dispatch 的结果 (ok=true 表示派发受理, false 表示被拒)
    """
    led = Ledger(LEDGER_DB)
    try:
        run = led.get_team_work(run_id)
        if not run or run["status"] != "ACTIVE":
            return {"error": f"team work {run_id} 非 ACTIVE",
                    "code": 409, "ok": False}

        # 串行铁律: 上一张必须已清 current_card_id 才能 advance
        # (harvest 落 COMPLETE 等终态时由 harvest 端调 clear_team_work_current_card)
        if run["current_card_id"] is not None:
            return {"error": f"team work 还有 current_card_id="
                            f"{run['current_card_id']!r}, 必须先等其结算",
                    "code": 409, "ok": False,
                    "current_card_id": run["current_card_id"]}

        task_id = (payload.get("task_id") or "").strip()
        if not task_id:
            return {"error": "task_id 必填", "code": 400, "ok": False}

        # 校验 task 属于本 run (反查 harvest_contracts.team_work_run_id)
        # get_contract 返回 sqlite3.Row, 走 dict-style access (Row[key])
        hc = led.get_contract(task_id)
        if not hc:
            return {"error": f"task_id={task_id} 无 harvest contract (未登记)",
                    "code": 404, "ok": False}
        # Row 直接 [key] 访问, 与 isinstance 检查 hc["x"] 的语义一致
        if hc["team_work_run_id"] != run_id:
            return {"error": f"task_id={task_id} 不属于 run {run_id} "
                            f"(team_work_run_id={hc['team_work_run_id']!r})",
                    "code": 409, "ok": False,
                    "task_id": task_id, "expected_run_id": run_id}

        # ★ REOPEN-DESIGN A面 (issue #5): 派发前机器校验 TODO.md『当前任务』
        # 绑定本卡。插在归属校验之后、settle_residue/占槽/派发之前 —— 拒绝
        # 必须零副作用 (A-AC3): 不覆盖 residue、不占槽、不起 worker。
        pw = payload.get("project_workspace") or run.get("project_workspace")
        if not pw:
            return {"ok": False, "code": 409,
                    "error": "advance 无法定位 project_workspace, 拒绝派发 "
                             "(fail-closed, 无法校验 TODO 绑定)",
                    "reason": "no_workspace",
                    "expected": {"task_id": task_id,
                                 "step_id": payload.get("step_id") or ""},
                    "hint": "advance payload 带 project_workspace 或确保 "
                           "launch 时已落 run 行"}
        ok_bind, bind_detail = _todo.verify_current_task_binding(
            f"{pw}/TODO.md", task_id, payload.get("step_id"))
        if not ok_bind:
            return _todo_binding_409(bind_detail)

        # 串行铁律 (2026-10-05 用户明示): main 推进下一张 = 隐式判定
        # run 名下所有非当前卡已完成。reaper 兜底写的 BLOCKED / PENDING /
        # NEED_CLARIFICATION 残留必须在此刻覆盖成 HARVESTED, 否则
        # main 回溯 ledger 时会看到与事实不符的状态, 违反「一次只允许
        # 一个 ACTIVE run + 一个 ACTIVE task」的串行契约。
        settled = led.settle_team_work_residue(run_id, exclude_task_id=task_id)
        for old_tid in settled:
            print(f"[advance] run {run_id[:12]}: implicit-settled reaper "
                  f"residue {old_tid[:14]} -> HARVESTED")

        # 1) 占槽 (dispatch 前置)
        led.set_team_work_current_card(run_id, task_id)
    finally:
        led.close()

    # 2) 透传 dispatch (api_dispatch 会再次校验 current_card_id == task_id)
    dispatch_body = {
        "task_id": task_id,
        "run_id": run_id,           # ★ 串行铁律: dispatch 必须带 run_id
        "title": payload.get("title") or task_id,
        "target": {"profile": (payload.get("target_profile") or "").strip()},
        "input_spec": {
            "task_description":
                payload.get("task_description") or payload.get("description")
                or payload.get("title") or task_id,
            "context_files": payload.get("input") or [],
        },
        "harvest": {
            "required_files": payload.get("required_files") or [],
            "timeout_seconds": payload.get("timeout_seconds", 1800),
        },
        "idempotency_key": f"tw-{run_id}-{task_id}",
    }
    # F1 (2026-10-04): 补充卡回流时 main 常忘带 project_workspace, 产物根
    # 会错落到 task 目录 → worker 正确交付但契约在错目录找不到产物。
    # (201 实测 t_321617ab: FIX_NOTES.md 写在项目目录, 契约却去
    # /share_workspace/tasks/<id>/ 找 → 假「产物缺失」→ 清槽 → 死锁)。
    # 边界减法 (2026-10-08) 后该缺失不再落 PHYSICAL_ERROR, 而是随 COMPLETE
    # 上报 physical_check.passed=False 证据; 但继承 run.project_workspace 仍
    # 必要 —— 它从源头避免产物落错地方, 省掉 main 一次误判排查。
    # run 行是 project_workspace 的权威源, advance 未显式指定时继承 run 的。
    pw = payload.get("project_workspace") or run.get("project_workspace")
    if pw:
        dispatch_body["project_workspace"] = pw
    return api_dispatch(dispatch_body)


def api_team_work_add_cards(run_id: str, payload: dict) -> dict:
    """往一个已存在的 ACTIVE run 追加卡片 (2026-10-04 补根)。

    背景: 卡片登记进 run 的唯一入口原本只有 launch 的 steps —— run 建好
    之后 HTTP 面上没有「追加卡」的端点, 而 advance 又强制要求卡已登记
    (反查 harvest_contracts.team_work_run_id)。回流场景 (code-review 打回
    要建修复卡 / 复审卡) 因此无路可走, main 只能在容器里徒手调
    register_team_work_card 塞卡 —— 绕过契约保护。本端点把这条路正式化。

    职责边界 (与 advance 严格分工):
      - 本端点 **只登记, 不占槽, 不派发**。登记完卡是「待派」状态。
      - 派活仍归 advance (current_card_id 单槽 + 串行铁律不变)。
      - 这样「加卡」与「派卡」两个动作解耦, 一次加多张、按任意顺序
        advance 其中一张都成立。

    payload:
      {
        "cards": [                       # 必填, 一张或多张
          {
            "task_id": str,              # 必填, main 已建好的 kanban 卡 id
            "title": str,                # 可选
            "task_description": str,     # 可选 (worker 零上下文, 必须自包含)
            "assignee": str,             # 可选, worker profile
            "required_files": [str],     # 可选
            "timeout_seconds": int,      # 可选, 默认 1800
            "position": float,           # 可选, 2026-10-17 issue#11 s2-fix-11:
            # 排队预览中的『计划位』 (REAL, 支持 2.5 等小数位)。
            # 写 plan_seq 列, api_team_work_get 未执行段排序键
            # COALESCE(plan_seq, seq) ASC。不传 → plan_seq=NULL → 沿
            # 用 seq (老行为零回归)。
          }, ...
        ]
      }

    返回: {ok, run_id, registered: [...], already_registered: [...], skipped: [...]}
    幂等: 同 task_id 重复登记不重复递增 card_count, 归入 already_registered。
    """
    cards = payload.get("cards")
    if not isinstance(cards, list) or not cards:
        return {"error": "cards 必填 (非空数组)", "code": 400, "ok": False}

    led = Ledger(LEDGER_DB)
    try:
        run = led.get_team_work(run_id)
        if not run or run["status"] != "ACTIVE":
            return {"error": f"team work {run_id} 非 ACTIVE",
                    "code": 409, "ok": False}

        registered, already, skipped = [], [], []
        for c in cards:
            if not isinstance(c, dict):
                skipped.append({"card": str(c)[:80], "why": "非 object"})
                continue
            tid = (c.get("task_id") or "").strip()
            if not tid:
                skipped.append({"card": str(c)[:80], "why": "task_id 必填"})
                continue

            # position (2026-10-17 issue#11 s2-fix-11): 可选 float, 写 plan_seq。
            # 不传或传非数字 → plan_seq=None (NULL) → 排序时退化为 seq,
            # 与 add_cards 老行为完全一致。传数字 → 写 plan_seq, 排队预览
            # 按 COALESCE(plan_seq, seq) 排, 允许『插到中间』(小数位插入)。
            # 边界: 负数 / 极端大值不挡 —— 不变量由 caller (main) 自负,
            # 因为 plan_seq 是展示层键, 不参与 dispatch 逻辑。
            pos = c.get("position")
            plan_seq = None
            if pos is not None:
                try:
                    plan_seq = float(pos)
                except (TypeError, ValueError):
                    skipped.append({"card": tid,
                                    "why": f"position 必须是数字, got {pos!r}"})
                    continue

            # 幂等: 已属本 run 的卡不再登记 (防 card_count 虚增)
            hc = led.get_contract(tid)
            if hc and hc["team_work_run_id"] == run_id:
                already.append(tid)
                continue
            # 已属别的 run → 拒 (一张卡不能挂两个 run)
            if hc and hc["team_work_run_id"] not in (None, run_id):
                skipped.append({"card": tid, "why": f"已属 run "
                             f"{hc['team_work_run_id']}"})
                continue

            contract = {
                "required_files": c.get("required_files") or [],
                "timeout_seconds": c.get("timeout_seconds", 1800),
                "on_evidence_missing": "block_for_human",
                # F1 同源: 追加卡同样继承 run 的产物根, 否则 required_files
                # 会错落到 task 目录 → 契约在错目录找不到产物 (physical_check
                # 报 passed=False 证据, 见边界减法 2026-10-08)。
                "product_workspace": run.get("project_workspace"),
            }
            led.put_contract(tid, contract)
            # 2026-10-17 issue#11 s2-fix-11: 把 plan_seq 透传到 register,
            # 让 add_cards 中途插入的卡可指定『计划位』, 排队预览按
            # COALESCE(plan_seq, seq) 排, 不再恒排队尾。
            led.register_team_work_card(run_id, tid, plan_seq=plan_seq)
            # 报告中继路由表: 这张卡结算后 reaper 要把报告送回 main
            led.record_initiator(tid, run["initiator_session"])
            registered.append(tid)

        return {"ok": True, "run_id": run_id,
                "registered": registered,
                "already_registered": already,
                "skipped": skipped,
                "card_count": led.get_team_work(run_id)["card_count"],
                "note": _t("console.note.advance_registered").replace("{run_id}", run_id)}
    finally:
        led.close()


def api_team_work_close(run_id: str, payload: dict) -> dict:
    """操作员手动关闭一个 team work (T2)。

    操作:
      1) 杀 current_card_id 对应的 spawn-bg 进程 (若有, 立刻停 mw 侧驱动)
      2) 把 run 名下所有未结算卡 (PENDING/ERROR/BLOCKED/NEED_CLARIFICATION)
         标 ARCHIVED — 避免 reaper 误激活遗留 PENDING
      3) run.status: 视当前是否有遗留判断 (已全清空 → CLOSED, 否则 → ABORTED)
      4) emit 'team_work.closed' 事件供 SSE / webhook

    payload: {"reason": str (default 'user_close'), "operator": str}

    护栏: 只能关 ACTIVE run, 已关的幂等返 409 (前端不该重复 close)。

    2026-10-08 边界: 这是 **唯一** close run 的合法入口。
    - mw 内部 (harvest/reaper) 永不调此 — 实测 b9a9445419ab489e run 被
      auto-close 强行关闭 4 次,均因 harvest 的 chain-tail auto-close 撞
      settle_team_work_residue 副作用提前触发 count==0 而误关。已删。
    - main agent 主动调 (在读完最后一张 COMPLETE 报告, 确认无遗留后调)
      也通过这里,actor="main"。
    - 操作员点控制台关闭按钮, 必须走 UI 二次确认后调这里,actor="console"。
    """
    led = Ledger(LEDGER_DB)
    try:
        run = led.get_team_work(run_id)
        if not run:
            return {"error": f"team work {run_id} 不存在",
                    "code": 404, "ok": False}
        if run["status"] != "ACTIVE":
            return {"error": f"team work {run_id} 已是 {run['status']}, 无需再关",
                    "code": 409, "ok": False,
                    "current_status": run["status"]}

        # 1) 杀当前卡 worker (mw 侧驱动)
        killed = 0
        if run["current_card_id"]:
            killed = _kill_spawn_bg(run["current_card_id"])

        # 2) 归档未结算卡 — 必须在 close_team_work 之前, 让 reaper 看到终态
        archived = led.archive_team_work_cards(run_id)

        # 3) 关 run + 清 current_card_id —— 收编进 ledger.close_team_work
        # (A-1: 本端点先归档再关, 必须显式传 final_status; 自动判定
        #  会因 ARCHIVED 不算遗留而误得 CLOSED, 见 ledger docstring 警告)
        reason = (payload.get("reason") or "user_close").strip()
        operator = (payload.get("operator") or "console").strip()
        # 判定: 没有遗留未结算卡 且 没有正在跑的卡 → CLOSED, 否则 ABORTED
        final_status = "CLOSED" if (not archived and not run["current_card_id"]) \
                                  else "ABORTED"
        led.close_team_work(run_id, f"{reason} (by {operator})",
                           final_status=final_status)
    finally:
        led.close()

    # 4) 事件 emit — 给 SSE / webhook 一个明确信号 (与 mw_events 表对齐)
    try:
        led_emit = Ledger(LEDGER_DB)
        led_emit.emit_event("team_work.closed", "", {
            "run_id": run_id,
            "status": final_status,
            "reason": reason,
            "archived_cards": archived,
            "killed_spawn_bg": killed,
            "operator": operator,
        })
        led_emit.close()
    except Exception:
        pass  # 事件失败不阻断响应

    return {
        "ok": True,
        "run_id": run_id,
        "status": final_status,
        "reason": reason,
        "operator": operator,
        "archived_cards": archived,
        "killed_spawn_bg": killed,
        "note": (f"{_t('console.note.close_done') if final_status == 'CLOSED' else _t('console.note.close_aborted')}"
                f"team work {run_id}"),
    }


def api_team_work_archive(run_id: str, payload: dict) -> dict:
    """操作员归档一条非 ACTIVE run (2026-10-06 新增)。

    区别于 close:close 终结 ACTIVE(清孤儿+杀 worker 进程);
    archive 隐藏已结束的 (ABORTED/CLOSED),避免列表过长。
    不允许归档 ACTIVE —— 必须先 close 再 archive,防止误归档正在跑的
    task chain (UI 也不显示归档按钮,后端再兜底一道)。

    payload 可选 {'operator': str}。返回 {ok, run_id, archived_cards,
    was_already, note}。已 ARCHIVED 再调 → ok,was_already=True(幂等)。
    """
    operator = str(payload.get("operator") or "console-ui")
    led = Ledger(LEDGER_DB)
    try:
        try:
            res = led.archive_team_work(run_id)
        except ValueError as e:
            # ACTIVE 拒绝 / run 不存在 → 409,UI 提示明确
            msg = str(e)
            code = 409 if "不允许归档" in msg else 404
            return {"error": msg, "code": code, "ok": False}
        res["note"] = (
            "已归档(幂等)" if res["was_already"]
            else f"已归档 team work {run_id}, 旗下 {len(res['archived_cards'])} 张未结算卡一并清理")
        res["operator"] = operator
        return res
    finally:
        led.close()


def _parse_wr(blob) -> dict | None:
    """worker_report TEXT 列 → dict。坏 JSON / None → None (按缺失卡处理)。"""
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _aggregate_run_tokens(cards: list[dict]) -> dict:
    """issue #58 (D3): run 级 token 读取时聚合 — 两出口共用同一实现 (AC-A3)。

    输入: cards 列表 (worker_report 已解析为 dict 或缺失/为 None)。
    算法 (ARCHITECTURE-58 §5.1):
      - 有 tokens 且 source=="api" 的卡: 五桶与 cost 计入求和, captured+1
      - 有 tokens 但 source=="unavailable" / 完全无 tokens 键 (历史卡):
        missing+1, 不贡献数值
      - source 派生: captured==len 且>0 → "api"; 0<captured<len → "partial";
        captured==0 (含空 run) → "unavailable"
    返回 5 键 {total_tokens, estimated_cost_usd, captured, missing, source}。
    """
    total = 0
    cost = 0.0
    captured = 0
    missing = 0
    for c in cards:
        wr = c.get("worker_report")
        t = wr.get("tokens") if isinstance(wr, dict) else None
        if isinstance(t, dict) and t.get("source") == "api":
            # 五桶求和只信 total_tokens 的语义源: 直接加五桶 (落库快照
            # 已保证 total=Σ五桶, 这里逐桶加防御脏 total 字段)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "reasoning_tokens"):
                v = t.get(k)
                total += v if isinstance(v, int) and not isinstance(v, bool) else 0
            ec = t.get("estimated_cost_usd")
            cost += ec if isinstance(ec, (int, float)) and not isinstance(ec, bool) else 0.0
            captured += 1
        else:
            missing += 1
    n = len(cards)
    if captured == 0 or n == 0:
        source = "unavailable"
    elif captured == n:
        source = "api"
    else:
        source = "partial"
    return {"total_tokens": total, "estimated_cost_usd": cost,
            "captured": captured, "missing": missing, "source": source}


def api_team_works_list(payload: dict) -> dict:
    """列出 team work 列表 (UI 用, T2)。

    payload 可选 {'include_closed': bool, 'include_archived': bool}。
    每项附 current_card 信息 (assignee + session_id + workspace + harvest_status)。

    默认只看 ACTIVE,include_closed=true 含 CLOSED/ABORTED,
    include_archived=true 含 ARCHIVED(归档管理面板)。
    2026-10-06: 新增 include_archived,UI 默认不传 → ARCHIVED 自动隐藏。
    """
    include_closed = bool(payload.get("include_closed", False))
    include_archived = bool(payload.get("include_archived", False))
    led = Ledger(LEDGER_DB)
    try:
        runs = led.list_team_works(include_closed=include_closed,
                                   include_archived=include_archived)
        # issue #58 (D3): 一次批量拉全部 run 名下卡的 worker_report 文本,
        # 循环内按 run 分组解析聚合 (单次 ledger read, 不逐 run 逐卡查询)。
        _wr_rows = led.conn.execute(
            "SELECT team_work_run_id, worker_report FROM harvest_contracts"
        ).fetchall()
        _wr_by_run: dict = {}
        for _r in _wr_rows:
            _wr_by_run.setdefault(_r["team_work_run_id"], []).append(
                _r["worker_report"])
        out = []
        for r in runs:
            info = dict(r)
            # 当前卡 (若有): assignee + session + harvest_status
            cur_id = info.get("current_card_id")
            if cur_id:
                hc = led.get_contract(cur_id)
                sess = led.get_session(cur_id)
                info["current_card"] = {
                    "task_id": cur_id,
                    "harvest_status": hc["harvest_status"] if hc else None,
                    "assignee": sess["profile"] if sess else None,
                    "session_id": sess["session_id"] if sess else None,
                    "workspace": sess["workspace"] if sess else None,
                }
            # issue #58: run 级 token 汇总 (两出口同源 helper, AC-A2/A3)
            info["token_total"] = _aggregate_run_tokens([
                {"worker_report": _parse_wr(w)} for w in
                _wr_by_run.get(info.get("run_id"), [])])
            out.append(info)
    finally:
        led.close()
    return {"team_works": out, "generated_at": time.time()}


def api_team_work_get(run_id: str) -> dict:
    """读单个 run 详情 + 旗下所有 kanban 卡的完整字段 (UI 树状展开用)。

    返回 {team_work: {...}, cards: [{task_id, ...}, ...]} — 字段命名与
    /api/tasks 对齐 (id/title/assignee/status/block_kind/result/harvest_status/
    exit_code/harvest_log/question/options/session_id/runner_kind/workspace/
    created_at/started_at/completed_at), 让 UI 在树状列表里直接渲染和操作
    (回答/取消/归档/续作) 而不必再二次查 /api/tasks。

    卡按 updated_at 升序 (注册顺序的近似);未在 kanban 表里的孤儿
    (只有 ledger harvest_contracts 记录) 也一并返回, 但仅带 task_id +
    harvest_status, UI 标"未登记在 kanban"。
    """
    led = Ledger(LEDGER_DB)
    cards = []
    try:
        run = led.get_team_work(run_id)
        if not run:
            return {"error": f"team work {run_id} 不存在",
                    "code": 404, "ok": False}

        # ── 1) ledger 侧登记 ── 拉所有属于本 run 的 task_id
        # 2026-10-09 Issue#2: 排序键从 updated_at (可变, 被 put_contract/
        # set_harvest/settle 刷新) 改为不可变 seq (register 时一次性写入);
        # 无 seq 的 (理论仅迁移缝隙中的新行) 排尾、按 updated_at。
        # 2026-10-17 issue#11 s2-fix-11: 增加 plan_seq 列, 用于 add_cards
        # 中途插入的『计划位』展示 (NULL 时退化为 seq, 兼容 launch 老数据)。
        # 连续两次 GET 顺序逐位相同 (AC2-3): seq 不可变 + plan_seq 一经写入
        # 不再变 (add_cards 不二次覆盖) + 排序键确定。
        rows = led.conn.execute(
            "SELECT task_id, harvest_status, updated_at, seq, plan_seq "
            "FROM harvest_contracts WHERE team_work_run_id=? "
            "ORDER BY (seq IS NULL), seq, updated_at", (run_id,)).fetchall()
        task_ids = [r["task_id"] for r in rows]

        # ── 2) kanban 主表 + ledger 补字段 ── 复用 api_tasks 那套字典构造
        led.close()
        led = None
        # 打开只读连接,与 api_tasks 风格一致
        led_ro = {}
        for r in _ro_query(LEDGER_DB,
                           "SELECT task_id,harvest_status,exit_code,harvest_log,"
                           "team_work_run_id,worker_report FROM harvest_contracts"):
            led_ro[r["task_id"]] = r
        sess_ro = {}
        for r in _ro_query(LEDGER_DB,
                           "SELECT task_id,session_id,runner_kind,workspace "
                           "FROM task_sessions"):
            sess_ro[r["task_id"]] = r

        # 读 kanban 这些 task_id 的整行 (不 LIMIT 200, 精确按 run 拉)
        if not task_ids:
            cards = []
        else:
            placeholders = ",".join("?" * len(task_ids))
            kanban_rows = _ro_query(KANBAN_DB,
                                    "SELECT id,title,assignee,status,block_kind,"
                                    "result,created_at,started_at,completed_at "
                                    f"FROM tasks WHERE id IN ({placeholders})",
                                    tuple(task_ids))
            # ledger → kanban 字段补全
            cards = []
            for t in kanban_rows:
                h = led_ro.get(t["id"], {})
                s = sess_ro.get(t["id"], {})
                t["harvest_status"] = h.get("harvest_status")
                t["exit_code"] = h.get("exit_code")
                t["harvest_log"] = (h.get("harvest_log") or "")[:400]
                q = _extract_question(t["harvest_status"], h.get("harvest_log") or "")
                t["question"] = q["question"]
                t["options"] = q["options"]
                t["session_id"] = s.get("session_id")
                t["runner_kind"] = s.get("runner_kind")
                t["workspace"] = s.get("workspace")
                t["team_work_run_id"] = run_id
                # issue #58 (main 裁决 1): 注入解析后的 worker_report, 步骤行
                # 直接读 card.worker_report.tokens。只增不改 (AC-A4);
                # 老卡无 tokens 键 → 该键缺失, 前端按缺失渲染 "—"。
                t["worker_report"] = _parse_wr(h.get("worker_report"))
                cards.append(t)
            # 孤儿 (有 ledger 但 kanban 找不到):保底返回 task_id + harvest_status
            found_ids = {c["id"] for c in cards}
            orphans = [r for r in rows if r["task_id"] not in found_ids]
            for r in orphans:
                cards.append({"id": r["task_id"],
                              "harvest_status": r["harvest_status"],
                              "orphan": True,
                              "team_work_run_id": run_id,
                              # issue #58 (AC-A5): 孤儿卡快照同样计入 run 求和
                              "worker_report": _parse_wr(
                                  led_ro.get(r["task_id"], {}).get("worker_report"))})

            # ── 排序 (s5 fix #11) — 已被 s2-fix-11 (2026-10-17) 升级:
            # 增加 plan_seq (可变『计划位』) 排序键; 未执行段按
            # COALESCE(plan_seq, seq) 排, 既兼容 launch 老数据 (NULL=seq)
            # 又支持 add_cards(position=N) 中途插入到中间。
            # ──────────────────────────────────────────────
            # 之前按 ledger 注册序 (seq, 2026-10-09 Issue#2) 稳定排序,但
            # add_cards 追加的卡 seq 大于 launch 全量卡,在列表尾部展示却
            # 被先执行 → 审查时顺序与真实执行顺序矛盾 (201 真实数据: f30ad9
            # 6 张 launch 卡 + 6 张 add_cards 卡,后建先执行)。
            #
            # s5 fix (16bce74, 2026-10-13): 排序键 = 已执行段按
            # started_at/completed_at 升序,未执行段按 seq 升序 (triage §6
            # 推荐方案 A — 纯展示层稳定排序)。seq 与 started_at 都不可变,
            # 「连续两次 GET 顺序逐位相同」(AC2-3, AC11-2) 仍然成立。
            #
            # s2-fix-11 (2026-10-17, issue#11 reopen): 用户复测发现
            # add_cards 追加卡『排在队尾, 但被优先执行』的矛盾在「未执行
            # 段预览」窗口依然存在 (visual seq ASC ≠ 真实派发序); 引入
            # plan_seq REAL 列, add_cards 接受可选 position 写 plan_seq,
            # 排序键改成 COALESCE(plan_seq, seq) —— 兼顾 launch 老行为与
            # 中途插入意图位。
            #
            #   - 已执行段 (段 0):  started_at ASC;无 started_at 则 completed_at ASC。
            #   - 未执行段 (段 1):  COALESCE(plan_seq, seq) ASC; NULL 落 seq,
            #                       老数据零回归 (Issue#2 派发幂等仍成立)。
            def _exec_key(c):
                started = c.get("started_at")
                completed = c.get("completed_at")
                row = next((r for r in rows if r["task_id"] == c.get("id")), None)
                seq_val = row["seq"] if row and row["seq"] is not None else (1 << 30)
                plan_seq_val = (row["plan_seq"] if row and row["plan_seq"] is not None
                                else seq_val)
                updated = row["updated_at"] if row else None
                # 已执行段: started_at ASC;无 started_at 则 completed_at ASC
                if started is not None or completed is not None:
                    return (0,
                            float(started) if started is not None else float(completed),
                            plan_seq_val, seq_val, updated or 0)
                # 未执行段: plan_seq 优先 (NULL 落 seq); seq 兜底保 Issue#2 不变式
                return (1, 0.0, plan_seq_val, seq_val, updated or 0)

            cards.sort(key=_exec_key)
            # ── 排序完毕, 给每张卡注 exec_index (s5 fix #11) ──
            # exec_index 是 UI 序号显示的唯一权威源 (避免前后端各排一遍
            # 抖动)。所有返回的 card(含孤儿)都要参与序号。
            for idx, c in enumerate(cards):
                c["exec_index"] = idx + 1
            # ── D3 (2026-10-13, VERIFY-20261013 §8): 「补」角标权威判定 ──
            # 旧前端判定 i+1 !== stepNo 恒假 (exec_index 就是按位置注入的),
            # 属死代码。正确语义 = 中途插入 run 的卡: launch 基线「先建的
            # 先执行」, 若一张卡 seq 比某张未执行卡大 (计划上本该更晚) 却
            # 已执行 (排在执行段), 只能是 mid-run add_cards 插入并被优先
            # 派发 —— 201 事故数据 (f30ad9: add_cards 卡先于 launch 卡执行)
            # 的精确特征。不用 kanban created_at:「中途插入」≠「中途建卡」,
            # 已存在的卡也可被插进 run。纯展示层派生, server 单一源。
            _seq_of = {r["task_id"]: r["seq"] for r in rows}
            _pending_seqs = [_seq_of[c["id"]] for c in cards
                             if c.get("started_at") is None
                             and _seq_of.get(c["id"]) is not None]
            for c in cards:
                _sq = _seq_of.get(c["id"])
                c["appended"] = bool(
                    c.get("started_at") is not None and _sq is not None
                    and any(_sq > ps for ps in _pending_seqs))
    finally:
        if led is not None:
            led.close()

    # issue #116: 卡片行「已被 X 于 HH:MM 答复」—— 批量注入答复事实。
    # 与 api_task_detail 的 answered_by 同源 (task_resume_requests 每卡
    # 最近一条含 actor), 但走 run 级单条批量查询 (IN 子句), 避免逐卡
    # 开 Ledger 的 N 次连接。fail-open: 注入失败不阻断详情返回。
    if task_ids:
        try:
            _ans_led = Ledger(LEDGER_DB)
            try:
                _ph = ",".join("?" * len(task_ids))
                _arows = _ans_led.conn.execute(
                    f"SELECT task_id, actor, requested_at "
                    f"FROM task_resume_requests WHERE task_id IN ({_ph}) "
                    f"ORDER BY requested_at DESC", tuple(task_ids)).fetchall()
                _latest = {}
                for _ar in _arows:  # DESC: 首见即该卡最新一条
                    _latest.setdefault(_ar["task_id"], _ar)
                for c in cards:
                    _a = _latest.get(c.get("id"))
                    if _a and _a["actor"]:
                        c["answered_by"] = _a["actor"]
                        c["answered_at"] = _a["requested_at"]
                        c["answered_at_hhmm"] = time.strftime(
                            "%H:%M", time.localtime(_a["requested_at"]))
            finally:
                _ans_led.close()
        except Exception:
            pass

    # F5 (2026-10-13, issue #5 状态机统一): 给 run 与每张 card 注入
    # `state` 字段, 单一派生态的真理来源。 三处 UI (TasksBoardView /
    # WorkersView / ChatView) 只需消费这里的 `state`, 不再各自拍脑
    # 袋用 harvest_status / sse_live / current_card_id 三套不同输入
    # 派生。 `state` 与 `state_source` 两字段返回 —— 后者含派生真
    # 因(便于审计与调试; UI 主视图不需展示)。
    try:
        _state_led = Ledger(LEDGER_DB)
        try:
            overview = _state_led.derive_run_overview(run_id)
            run["state"] = overview.get("state", "IDLE")
            run["state_source"] = {
                "holder_task_id": overview.get("holder_task_id"),
                "holder_state": overview.get("holder_state"),
                "handoff_active": overview.get("handoff_active"),
                "waiting_active": overview.get("waiting_active"),
            }
            # 给每张 card 写 `state` (派生视角)
            _card_states = {}
            for c in cards:
                _tid = c.get("id")
                if _tid:
                    _st, _src = _state_led.derive_worker_state(
                        _tid, run_id=run_id)
                    c["state"] = _st
                    c["state_source"] = _src.get("tier", "")
                    _card_states[_tid] = _st
            run["state_card_states"] = _card_states
        finally:
            _state_led.close()
    except Exception:
        # F5 注入失败不阻断既有数据返回 (best-effort, 老客户端兼容)
        run.setdefault("state", None)
        for c in cards:
            c.setdefault("state", None)

    # issue #58 (D3, API-SPEC-58 §3.4): run 级 token 汇总注入。
    # 与 /api/team-works 同一 helper (AC-A3 两出口同字段同语义);
    # 空 run 也总是写 (0 卡 → source=unavailable, UI 显示 "—")。
    run["token_total"] = _aggregate_run_tokens(cards)

    return {"team_work": run, "cards": cards}


# ── F5 (2026-10-13, issue #5 状态机统一) 派生端点 ──────────────────
# 提供单一派生端给三处 UI 消费, 不强求; api_team_work_get 已带
# state 字段, 这里是「仅要派生状态」场景的轻量端点 (适合 SSE / 高频
# 轮询 / pixel_bridge)。
def api_team_work_state(run_id: str) -> dict:
    """返回 run 级 + 每张 card 的派生状态。

    返回:
      run: dict  { state, run_status, holder_task_id, holder_state,
                  has_active_run, waiting_active, handoff_active }
      cards: [ { id, state, state_source, harvest_status }, ... ]
    """
    led = Ledger(LEDGER_DB)
    try:
        run = led.get_team_work(run_id)
        if not run:
            return {"error": f"team work {run_id} 不存在",
                    "code": 404, "ok": False}
        overview = led.derive_run_overview(run_id)
        # 拉 run 名下全部 card 派生
        rows = led.conn.execute(
            "SELECT task_id, harvest_status FROM harvest_contracts "
            "WHERE team_work_run_id=? ORDER BY seq, updated_at",
            (run_id,)).fetchall()
        cards_out = []
        for r in rows:
            st, src = led.derive_worker_state(r["task_id"], run_id=run_id)
            cards_out.append({
                "id": r["task_id"],
                "state": st,
                "state_source": src.get("tier", ""),
                "harvest_status": r["harvest_status"],
            })
        return {
            "ok": True,
            "run": {
                "run_id": run_id,
                "state": overview["state"],
                "run_status": overview["run_status"],
                "holder_task_id": overview["holder_task_id"],
                "holder_state": overview["holder_state"],
                "has_active_run": overview["has_active_run"],
                "waiting_active": overview["waiting_active"],
                "handoff_active": overview["handoff_active"],
            },
            "cards": cards_out,
        }
    finally:
        led.close()


# _TODO_FIELD_HELP: 字段中文标签 (UI 渲染『当前任务』细节面板用, 与 todo.py 解析的对齐)
_TODO_FIELD_HELP = {
    "步骤": "step_id",
    "标题": "title",
    "目标": "goal",
    "承接方": "assignee",
    "产物目录": "product_workspace",
    "产出文件": "required_files",
    "状态": "status",
    "结果": "result",
    "kanban_task": "kanban_task_id",
}
_TODO_STATUS_SEM = {
    "进行中": ("pending", "排队中", "badge-slate"),
    "working": ("running", "执行中", "badge-blue"),
    "blocked": ("attention", "等你回复", "badge-yellow"),
    "completed": ("finished", "已完成", "badge-green"),
    "failed": ("blocked", "已失败", "badge-red"),
}


# 单行 '- [step_id] title → result' markdown 行无损拆出 (s5 fix #10 UI 卡片化):
# 多数现有 TODO.md 行严格匹配;旧链 / 手工维护若未匹配则回退到单字段文本视图,
# 不可拆的 step / title / result 至少三字段独立给出,前端 HistoryList 不再裸渲染文本。
_DONE_LINE_RE = re.compile(
    r"^-\s*\[(?P<step>[^\]]+)\]\s*(?P<title>.*?)\s*(?:→|->)\s*(?P<result>.*?)\s*$"
)


def _parse_history_line(line: str) -> dict:
    """把单条 done/dropped markdown 行解析成结构化 dict (s5 fix #10)。

    返回字典必有 raw(原行)字段,匹配失败时 step_id/title/result 字段为空串,
    由前端决定降级渲染。保证新增任何 TODO 行格式都不会让 UI 渲染失败。
    """
    s = (line or "").strip()
    raw = s
    if s.startswith("-"):
        s = s[1:].strip()
    m = _DONE_LINE_RE.match(raw)
    if m:
        return {
            "step_id": m.group("step").strip(),
            "title": m.group("title").strip(),
            "result": m.group("result").strip(),
            "raw": raw,
        }
    # 兜底: 不匹配时不谎报 step_id(空串),留给 UI 做单文本行降级展示
    return {"step_id": "", "title": "", "result": "", "raw": raw}


def _serialize_todo_doc(doc, raw_text: str) -> dict:
    """把 parse() 出来的 TodoDoc 转成 UI 直渲的 dict (主次分明, 字段对齐 todo.py)。

    UI 主视图只需要结构化数据, 不需要原始 markdown 行, 但 raw 一并带回供
    "复制整文/导出"用途。

    s5 fix #10: done / dropped 由裸 markdown 行升级为结构化 items
    ({step_id, title, result, raw}) — UI HistoryList 据此渲染卡片 (序号 + 标题
    + 结果 + step_id chip),与 PendingList / CurrentBlock 视觉体系一致。
    """
    def _task_dict(t):
        return {
            "step_id": t.step_id,
            "title": t.title,
            "goal": t.goal,
            "assignee": t.assignee,
            "product_workspace": t.product_workspace,
            "required_files": list(t.required_files or []),
            "status": t.status,
            "status_sem": _TODO_STATUS_SEM.get(t.status.lower() if isinstance(t.status, str) else "",
                                               ("pending", t.status, "badge-slate")),
            "result": t.result,
            "has_result": t.has_result,
            "kanban_task_id": t.kanban_task_id,
        }

    cur = _task_dict(doc.current) if doc.current else None
    pending = [_task_dict(t) for t in doc.pending]
    return {
        "title": doc.title,
        "current": cur,
        "pending": pending,
        # done / dropped: 单行 markdown → 结构化 (s5 fix #10)
        "done_lines": [_parse_history_line(ln) for ln in doc.done],
        "dropped_lines": [_parse_history_line(ln) for ln in doc.dropped],
        "raw": raw_text,
    }


def api_team_work_todo(run_id: str) -> dict:
    """只读读 Team Work 的 TODO.md (2026-10-11 新增, UI 看板只读面板用)。

    返回 {ok, exists, run_id, project_workspace, todo: <serialize_todo_doc>,
          error?, code?}。

    设计原则 (与 todo.py 所有权模型一致, UI 不解析不写入):
      - **完全只读**: 不开文件写权限、不解析修改。
      - **fail-soft**: 文件不存在 / project_workspace 缺失 / 解析异常
        都返回 ok=True + exists=False, UI 显示空态而不是 500。
      - **project_workspace 权威源是 run 行**, 不接受 payload 覆盖, 防前端
        传假路径读他人工位。
      - **raw 必带**: 整 markdown 原文带回去, 方便 UI 一键复制/导出; 不再做
        解释, 仅渲染层用。
    """
    led = Ledger(LEDGER_DB)
    try:
        run = led.get_team_work(run_id)
        if not run:
            return {"ok": False, "error": f"team work {run_id} 不存在",
                    "code": 404, "run_id": run_id}
        pw = (run.get("project_workspace") or "").strip()
        if not pw:
            return {"ok": True, "exists": False, "run_id": run_id,
                    "project_workspace": None,
                    "todo": None,
                    "reason": "run 没有 project_workspace"}
        todo_path = Path(pw) / "TODO.md"
        if not todo_path.exists():
            return {"ok": True, "exists": False, "run_id": run_id,
                    "project_workspace": pw,
                    "todo": None,
                    "reason": "TODO.md 不存在"}
        # 容错解析: parse 自身已有保护, 这层再兜一次 IO 异常
        try:
            raw_text = todo_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"ok": True, "exists": False, "run_id": run_id,
                    "project_workspace": pw,
                    "todo": None,
                    "reason": f"TODO.md 读取失败: {e}"}
        try:
            doc = _todo.parse(todo_path)
        except Exception as e:
            return {"ok": True, "exists": False, "run_id": run_id,
                    "project_workspace": pw,
                    "todo": None,
                    "reason": f"TODO.md 解析失败: {e}"}
        if doc is None:
            return {"ok": True, "exists": False, "run_id": run_id,
                    "project_workspace": pw,
                    "todo": None,
                    "reason": "TODO.md 内容为空"}
        return {"ok": True, "exists": True, "run_id": run_id,
                "project_workspace": pw,
                "todo": _serialize_todo_doc(doc, raw_text)}
    finally:
        led.close()


def api_dispatch(payload: dict) -> dict:
    """派发一张 main 已 claim 的卡起 worker (1A: mw 唯一的派活入口)。

    前置 (main 的责任): kanban 卡已建 + 已 claim; 项目任务 TODO.md 已播种。

    2026-10-03 T2: 单任务串行铁律 — 凡是属于某 ACTIVE team work 的卡,
    dispatch 必须带 run_id;同时 run.current_card_id 必须等于本卡 task_id
    (即 advance 已 set 过)。这条护栏确保 main 不能跳过 advance 直接
    派多张卡,把串行结构变成并行。
    """
    from .pipeline import dispatch, SubmitError

    # === T2 串行护栏 (先做,避免下游 pipeline 拿到脏 payload) ===
    task_id = (payload.get("task_id") or "").strip()
    run_id = (payload.get("run_id") or "").strip()
    if run_id:
        # 有 run_id → 必须校验 ACTIVE + current_card_id 匹配
        led = Ledger(LEDGER_DB)
        try:
            run = led.get_team_work(run_id)
            if not run:
                return {"error": f"team work {run_id} 不存在",
                        "code": 404, "ok": False}
            if run["status"] != "ACTIVE":
                return {"error": f"team work {run_id} 状态 {run['status']} "
                                f"非 ACTIVE, 不能再派卡",
                        "code": 409, "ok": False, "run_status": run["status"]}
            if run["current_card_id"] != task_id:
                return {"error": f"team work {run_id}.current_card_id="
                                f"{run['current_card_id']!r} != task_id={task_id!r}。"
                                f"必须先调 POST /api/team-work/<run_id>/advance "
                                f"设置 current_card_id, 再 dispatch",
                        "code": 409, "ok": False,
                        "current_card_id": run["current_card_id"],
                        "task_id": task_id}
        finally:
            led.close()
    # 若 run_id 为空 → 自由派 (legacy 单任务路径, 不走 team work 串行铁律)
    # pipeline.dispatch 不识 run_id 字段 (TaskManifest schema 严格),
    # 校验通过后从 payload 里删掉, 避免 schema 拒绝
    payload_for_pipeline = dict(payload)
    payload_for_pipeline.pop("run_id", None)

    try:
        # issue #61 顺带审: 旧写法 Ledger(LEDGER_DB) inline 传给 dispatch,
        # 永不 close —— 每次派活泄漏一个 sqlite fd。
        led = Ledger(LEDGER_DB)
        try:
            return dispatch(payload_for_pipeline,
                            registry_path=REGISTRY,
                            ledger=led,
                            async_mode=True)
        finally:
            led.close()
    except SubmitError as e:
        return {"error": e.msg, "code": e.code, "ok": False}


def api_worker_detail(name: str) -> dict:
    """worker『查看』: registry 元数据 + 实时 API 元数据。

    刻意不读 SOUL.md 等文件——gateway API 无文件端点, 读文件需挂载,
    与『worker hermes_home 免挂载』设计冲突。toolsets/models 经 API
    实时获取, 反映 worker 真实运行配置。
    """
    try:
        reg = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        reg = {}
    entry = reg.get(name)
    if not entry:
        raise ValueError(f"worker '{name}' 不在注册表")
    import urllib.request
    out = {"name": name, "registry": {k: ("***" if "key" in k.lower() else v)
                                     for k, v in entry.items()}}
    base = (entry.get("api_url") or "").rstrip("/")
    key = entry.get("api_key", "")
    # 真实生效模型: /v1/models 只返回请求别名(hermes-agent), 真实模型在
    # /api/model/options 顶层 model 字段(worker config.yaml 的 model.default)
    try:
        req = urllib.request.Request(
            base + "/api/model/options",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            mo = json.loads(r.read())
        out["actual_model"] = mo.get("model")
        out["actual_provider"] = mo.get("provider")
    except Exception as e:
        out["actual_model"] = None
        out["actual_model_error"] = str(e)[:150]
    for field, path in (("models", "/v1/models"), ("toolsets", "/v1/toolsets")):
        try:
            req = urllib.request.Request(
                base + path, headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=6) as r:
                out[field] = json.loads(r.read())
            out[field + "_ok"] = True
        except Exception as e:
            out[field] = None
            out[field + "_ok"] = False
            out[field + "_error"] = str(e)[:150]
    out["health"] = None
    try:
        with urllib.request.urlopen(base + "/health", timeout=6) as r:
            out["health"] = json.loads(r.read() or b"{}")
        out["online"] = True
    except Exception:
        out["online"] = False
    return out


def api_soul_template() -> dict:
    """返回 worker SOUL 通用模板内容, 供 UI 打开 SOUL 弹窗时预填。

    I18N-R3 (2026-10-16, REQ §3.2a): en 部署用 mw/soul_template.en.md
    (全英文 + 协议 marker 注释), zh-cn 部署用 soul_template.zh-cn.md。
    解析仍由 mw/i18n.resolve_lang() 单源决定 (其他文案一致)。
    """
    try:
        content = Path(SOUL_TEMPLATE).read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "content": "",
                "error": _t("console.soul_template_error", err=str(e))}
    return {"ok": True, "content": content}


def api_worker_soul_init(name: str, payload: dict) -> dict:
    """初始化 worker SOUL: 把 SOUL 内容直接塞进 chat 消息发给 worker,
    worker 的 LLM 自己写到 /opt/data/SOUL.md。重启 worker 后生效。

    I18N-R3 (2026-10-16, REQ §3.2b/§4.2 B2): 错误串走 mw/i18n t(), 正文
    走 mw/i18n t() (en/zh 双语), marker `【平台 SOUL 初始化指令】` 协议
    字面保持不变 (§7 注释强调)。
    """
    content = payload.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(_t("console.soul_init_content_required"))
    # 找到 worker registry 项
    try:
        reg = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        reg = {}
    entry = reg.get(name)
    if not entry:
        raise ValueError(_t("console.soul_init_worker_not_in_registry", name=name))
    base = (entry.get("api_url") or "").rstrip("/")
    key = entry.get("api_key", "")
    if not base or not key:
        raise ValueError(_t("console.soul_init_worker_missing_endpoint", name=name))
    import urllib.request

    # 1. 在 worker 侧创建新 session(避免污染用户已有对话)
    try:
        req = urllib.request.Request(
            base + "/api/sessions", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sess = json.loads(r.read())
        sid = (sess.get("session") or {}).get("id") or sess.get("id")
        if not sid:
            raise ValueError(_t("console.soul_init_session_failed"))
    except Exception as e:
        raise ValueError(_t("console.soul_init_sessions_endpoint_failed", err=str(e)))

    # 2. 把 SOUL 内容直接放进 chat 消息, 让 worker 用 bash 写文件
    #    (不能用 write_file 工具: 它被 HERMES_WRITE_SAFE_ROOT 沙箱限制,
    #     写不了 /opt/data, worker 会即兴绕道经 /share_workspace 暂存)
    # I18N-R3: 指令正文走 mw/i18n t(), marker 协议字面保持不变。
    instruction = (
        _t("console.soul_init_instruction_marker") + "\n"
        + _t("console.soul_init_instruction_body", content=content) + "\n"
        + _t("console.soul_init_instruction_suffix") + "\n"
    )
    try:
        body = json.dumps({"model": "hermes-agent", "message": instruction}).encode()
        req = urllib.request.Request(
            base + f"/api/sessions/{sid}/chat", data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        # worker LLM 写 SOUL.md 可能较慢(大文件 + 慢模型), 给足 180s
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read())
        reply = ((resp.get("message") or {}).get("content", ""))[:500]
    except Exception as e:
        return {"ok": False,
                "error": _t("console.soul_init_worker_chat_failed", err=str(e)),
                "session_id": sid}
    return {"ok": True, "session_id": sid, "worker_reply": reply}


def _ro_query(db: str, sql: str, args=()) -> list[dict]:
    # issue #61 顺带审: close 挪进 finally —— 旧写法 execute/fetch 若抛
    # sqlite3.Error 之外的异常 (如参数 TypeError), c.close() 被跳过,
    # 连接 fd 泄漏。finally 保证任何路径都关连接。
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]
        except sqlite3.Error:
            return []
        finally:
            c.close()
    except sqlite3.Error:
        return []


def _extract_question(harvest_status: str, harvest_log: str) -> dict:
    """从 harvest_log 提取"需要人知道的核心信息", 前端一等展示。

    返回 {question, options}。
    NEED_CLARIFICATION → worker 的问题原文与可选项(.clarify.json 的
                        options 字段, 若 worker 提供了选择项)
    BLOCKED           → check_script 失败原因
    """
    out = {"question": "", "options": []}
    if not harvest_log:
        return out
    if harvest_status == "NEED_CLARIFICATION":
        for line in harvest_log.splitlines():
            if not line.startswith("clarify:"):
                continue
            raw = line[len("clarify:"):].strip()
            try:  # 新格式: clarify: {json}
                d = json.loads(raw)
                if isinstance(d, dict):
                    out["question"] = str(d.get("question", "")).strip()
                    opts = d.get("options")
                    if isinstance(opts, list):
                        out["options"] = [str(o) for o in opts][:8]
                    return out
            except (json.JSONDecodeError, TypeError):
                pass
            out["question"] = raw  # 旧格式: 纯文本
            return out
        out["question"] = harvest_log.strip()
        return out
    if harvest_status == "BLOCKED":
        lines = [l for l in harvest_log.splitlines() if l.strip()]
        for l in lines:
            if "check_script" in l:
                out["question"] = l.strip()
                return out
        out["question"] = lines[-1].strip() if lines else ""
        return out
    return out


# ── issue #75 R1 (main 裁决 2026-10-20): /api/tasks 列表瘦身投影 ─────
# 实测 219630 B/200 条的大头 = result(29%) + harvest_log(12.3%) +
# workspace(6.6%)。列表响应只下发前端消费的白名单字段; result 截断
# 200 字符 (完成区 ResultChips 用截断值, TasksBoardView ResultChips),
# 全量 result/harvest_log 走新详情端点 GET /api/tasks/<id>。
# 白名单之外的键(含 harvest_log/workspace/exit_code/question/options/
# started_at/state_source/block_kind)一律不出列表 —— 兼容性红线: 前端
# api.tasks( 调用点 (App.jsx pollStatus counts / TasksBoardView) 只消费
# status/harvest_status/has_result/result 截断值, 已逐点核对。
#
# issue #75 s6 缺陷 2 (P1, AC-03 首屏预算超标):
#   main 实测 tasks 列表 122185 B —— 其中 result 截断值 (200 字符 × 184 条)
#   ≈ 33930 B (占 33%), 而【前端根本不消费 tasks[].result】:
#     - App.jsx pollCounts 只读 status/harvest_status/block_kind/length;
#     - TasksBoardView loadActive 只读顶层 d.team_work_active (不用 d.tasks);
#     - 详情字段走 api.teamWorkGet(run_id) 的 cards, 与 tasks 列表无关。
#   ⇒ 列表投影去掉 result 字段, 只留 has_result 布尔 (详情端点仍给全量
#   result, 语义不变)。实测 122185 B → 69431 B。
#
# s6 附带修 (IMPL-NOTES 遗留①): block_kind 加回白名单 —— App.jsx 的
# needs_input 判定原本因 block_kind 不在白名单而退化为"全部 blocked"
# (注意力计数虚高)。block_kind 是短字符串, 体积代价可忽略, 加回后恢复
# 精确计数。
_TASK_LIST_FIELDS = ("id", "title", "status", "assignee", "step_id",
                     "priority", "created_at", "updated_at", "completed_at",
                     "runner_kind", "liveness", "harvest_status",
                     "team_work_run_id", "session_id", "block_kind")
# (s6: 原 _TASK_RESULT_TRUNC=200 随列表 result 字段一并移除, 全量走详情端点)
# 说明: 列表 SELECT 保持原样 (kanban 主表 9 列) + ledger/session 注入;
# _project_task_row 白名单里 step_id/priority/updated_at 等列若上游
# schema 不存在则自然跳过 (投影只减不增, 不 SELECT 不存在的列)。
# s6: result 已从列表投影移除 —— result 截断值 (200 B × 184 条 ≈ 34KB)
# 前端零消费, 却占首屏预算 33%; 全量 result 由 GET /api/tasks/<id> 提供。


def _project_task_row(t: dict) -> dict:
    """api_tasks 列表条目 → 白名单投影 (issue #75 R1 + s6 缺陷 2)。"""
    out = {k: t.get(k) for k in _TASK_LIST_FIELDS if k in t}
    full = t.get("result") or ""
    # 只留布尔标记 (前端零消费的 result 文本已移出列表; 全量走详情端点)
    out["has_result"] = bool(full.strip())
    return out


def led_resume_actor(db_path: str, task_id: str) -> dict | None:
    """读该卡最近一次答复的事实 (issue #104 期望 2)。

    返回 {actor, requested_at, answered_at_hhmm} 或 None。只读连接,
    详情端点用; 失败一律 None (fail-open, 不阻断详情渲染)。
    """
    try:
        from .ledger import Ledger
        led = Ledger(db_path)
        try:
            r = led.last_resume_actor(task_id)
        finally:
            led.close()
        if not r:
            return None
        out = dict(r)
        out["answered_at_hhmm"] = time.strftime(
            "%H:%M", time.localtime(r["requested_at"]))
        return out
    except Exception:
        return None


def api_task_detail(task_id: str) -> dict:
    """issue #75 R1: 单任务全字段详情 (列表投影后的补全通道)。

    与 api_tasks 同一套 enrichment, 但 result/harvest_log 全量下发
    (列表端点截断值不用于详情抽屉/harvest_log 场景)。
    找不到 → {"error", "code":404}, 路由层翻 HTTP 404。
    """
    rows = _ro_query(KANBAN_DB,
                     "SELECT id,title,assignee,status,block_kind,result,"
                     "created_at,started_at,completed_at "
                     "FROM tasks WHERE id=?", (task_id,))
    if not rows:
        return {"error": f"task '{task_id}' 不存在", "code": 404}
    t = rows[0]
    h = {}
    for r in _ro_query(LEDGER_DB,
                       "SELECT task_id,harvest_status,exit_code,harvest_log,"
                       "team_work_run_id FROM harvest_contracts "
                       "WHERE task_id=?", (task_id,)):
        h = r
    s = {}
    for r in _ro_query(LEDGER_DB,
                       "SELECT task_id,session_id,runner_kind,workspace "
                       "FROM task_sessions WHERE task_id=?", (task_id,)):
        s = r
    t["harvest_status"] = h.get("harvest_status")
    t["exit_code"] = h.get("exit_code")
    # 详情端点: harvest_log 全量 (列表端点不再下发该字段)
    t["harvest_log"] = h.get("harvest_log") or ""
    q = _extract_question(t["harvest_status"], h.get("harvest_log") or "")
    t["question"] = q["question"]
    t["options"] = q["options"]
    t["session_id"] = s.get("session_id")
    t["runner_kind"] = s.get("runner_kind")
    t["workspace"] = s.get("workspace")
    t["team_work_run_id"] = h.get("team_work_run_id")
    # issue #104 期望 2: 卡片上要能看出「已被 X 于 HH:MM 答复」。
    # 从账本的 resume 请求历史读最近一条 (含 actor), 双通道 (main 走 CLI /
    # operator 走 console) 共用这张表 ⇒ 两侧都能从详情端点读到对方已答复。
    try:
        _last = led_resume_actor(LEDGER_DB, task_id)
        if _last:
            t["answered_by"] = _last["actor"]
            t["answered_at"] = _last["requested_at"]
            t["answered_at_hhmm"] = _last["answered_at_hhmm"]
    except Exception:
        pass
    t["liveness"] = None
    try:
        from . import sse_bridge
        _stale_s = float(os.environ.get("MW_LIVE_STALE_S", "180"))
        _now = time.time()
        for (_prof, _sid), v in sse_bridge.live_snapshot().items():
            if _sid == t.get("session_id"):
                _ts = v.get("last_event_ts") or 0
                t["liveness"] = {
                    "thinking": v.get("thinking"),
                    "last_event_ts": _ts,
                    "last_event_type": v.get("last_event_type"),
                    "stale": (_ts <= 0) or (_now - _ts) > _stale_s,
                }
                break
    except Exception:
        pass
    t["has_result"] = bool((t.get("result") or "").strip())
    return {"task": t}


def _api_tasks_active() -> dict:
    """issue #75 s6 缺陷 2 (P1): 只回顶层 team_work_active 段 (轻量模式)。

    TasksBoardView 的 5s 轮询 loadActive 只消费 d.team_work_active, 但走的是
    完整 /api/tasks (实测 122KB/次 → 约 1.4MB/min 持续流量)。本函数只查
    ledger 的 ACTIVE run + 当前卡摘要, 不查 kanban 任务列表, 响应体
    约 1KB。

    字段与 api_tasks() 的 team_work_active 逐字一致 (前端零改即可切换)。
    """
    led_obj = Ledger(LEDGER_DB)
    active_info = None
    try:
        try:
            active_run = led_obj.get_active_team_work()
        except Exception:
            active_run = None
        if active_run:
            active_info = {
                "run_id": active_run["run_id"],
                "initiator_session": active_run["initiator_session"],
                "initial_task": active_run["initial_task"],
                "project_workspace": active_run["project_workspace"],
                "current_card_id": active_run["current_card_id"],
                "card_count": active_run["card_count"],
                "created_at": active_run["created_at"],
                "status": active_run["status"],
            }
            # 补当前卡的 assignee / session / workspace / title / status
            cur_id = active_run["current_card_id"]
            if cur_id:
                for r in _ro_query(KANBAN_DB,
                                   "SELECT title,status,assignee FROM tasks "
                                   "WHERE id=?", (cur_id,)):
                    active_info["current_card_title"] = r.get("title")
                    active_info["current_card_status"] = r.get("status")
                    active_info["current_card_assignee"] = r.get("assignee")
                    break
                for r in _ro_query(LEDGER_DB,
                                   "SELECT profile,session_id,workspace "
                                   "FROM task_sessions WHERE task_id=?",
                                   (cur_id,)):
                    active_info["current_card_assignee"] = (
                        active_info.get("current_card_assignee")
                        or r.get("profile"))
                    active_info["current_card_session_id"] = r.get("session_id")
                    active_info["current_card_workspace"] = r.get("workspace")
                    break
                for r in _ro_query(LEDGER_DB,
                                   "SELECT harvest_status "
                                   "FROM harvest_contracts WHERE task_id=?",
                                   (cur_id,)):
                    active_info["current_card_harvest_status"] = r.get(
                        "harvest_status")
                    break
    finally:
        try:
            led_obj.close()
        except Exception:
            pass
    return {"generated_at": time.time(),
            "team_work_active": active_info,
            "team_work_active_count": (1 if active_info else 0)}


def api_tasks(fields: str = "") -> dict:
    """任务列表: kanban 为主, ledger 补 harvest 细节 + team_work_run_id 维度。

    T4 (2026-10-03): 暴露 team_work 顶层对象信息 + 每条 task 的 run_id。
      - 顶层字段: team_work_active (dict | null), team_work_active_count (int)
      - 每条 task 增加: team_work_run_id (str | null)
    UI 用 team_work_active 显示顶部状态卡 (CURRENT WORK: 需求描述,
    承接 worker, 工作目录, 当前卡), 防止 reaper 误扫非当前 run 的目录。

    issue #75 s6 缺陷 2 (P1): fields="active" → 只回顶层 team_work_active
    段, 不带 tasks 列表 (TasksBoardView 的 5s 轮询只消费 team_work_active,
    却每次拖走整个列表; 实测 122KB/次 → 约 1KB/次)。
    """
    if (fields or "").strip().lower() == "active":
        return _api_tasks_active()
    # issue #106 巡检联动: 任务列表是「无 PENDING 孤儿」巡检的读面 ——
    # 读之前先把「kanban 已归档但契约行滞留 PENDING」的卡对账掉
    # (走正规状态机, 见 _reconcile_archived_orphans), 巡检信号不再被
    # 直建直归档的测试卡污染。任何异常降级 no-op, 列表照常返回。
    try:
        _led_rec = Ledger(LEDGER_DB)
        try:
            _reconcile_archived_orphans(_led_rec)
        finally:
            _led_rec.close()
    except Exception:
        pass
    tasks = _ro_query(KANBAN_DB,
                      "SELECT id,title,assignee,status,block_kind,result,"
                      "created_at,started_at,completed_at "
                      "FROM tasks ORDER BY created_at DESC LIMIT 200")
    led = {}
    for r in _ro_query(LEDGER_DB,
                       "SELECT task_id,harvest_status,exit_code,harvest_log,"
                       "team_work_run_id FROM harvest_contracts"):
        led[r["task_id"]] = r
    sess = {}
    for r in _ro_query(LEDGER_DB,
                       "SELECT task_id,session_id,runner_kind,workspace "
                       "FROM task_sessions"):
        sess[r["task_id"]] = r
    for t in tasks:
        h = led.get(t["id"], {})
        s = sess.get(t["id"], {})
        t["harvest_status"] = h.get("harvest_status")
        # issue #75 R1: exit_code/harvest_log/workspace 移出列表 (走
        # GET /api/tasks/<id> 详情端点); question/options 同源 harvest_log,
        # 一并不再下发 (回复弹窗的 question 消费点在 run 详情 cards, 不受影响)。
        # M6 (BATCH1-DESIGN, #5 展示单源): 状态徽章只从 S1 (ledger
        # harvest_status + worker_report) 派生, 显式标注来源。活动性
        # (S3, sse_bridge 内存) 是**独立字段** liveness, 前端渲染为
        # 与状态分离的"活动指示灯", 禁止与 harvest_status 合成单一徽章
        # —— 两者更新时机不同 (S3 内存 / S1 账本, 无同步事务), 混排
        # 必然打架。liveness 超过 MW_LIVE_STALE_S (默认 180) 视为陈旧,
        # 前端应显示"最后已知活动 @ ts"而非"进行中"。
        t["session_id"] = s.get("session_id")
        t["runner_kind"] = s.get("runner_kind")
        # ★ T4: 每条 task 暴露所属 team work run (供 TasksView 分组显示)
        t["team_work_run_id"] = h.get("team_work_run_id")
    # M6: 活动性独立注入 (只读 sse_bridge 内存, 零 health 探测成本)。
    # 按 session_id 匹配; 拿不到 bridge 时全 None, 前端按"无活动证据"渲染。
    for t in tasks:
        t["liveness"] = None
    try:
        from . import sse_bridge
        _live_by_sid = {}
        for (_prof, _sid), v in sse_bridge.live_snapshot().items():
            _live_by_sid[_sid] = v
        _stale_s = float(os.environ.get("MW_LIVE_STALE_S", "180"))
        _now = time.time()
        for t in tasks:
            v = _live_by_sid.get(t.get("session_id"))
            if v is None:
                continue
            _ts = v.get("last_event_ts") or 0
            t["liveness"] = {
                "thinking": v.get("thinking"),
                "last_event_ts": _ts,
                "last_event_type": v.get("last_event_type"),
                "stale": (_ts <= 0) or (_now - _ts) > _stale_s,
            }
    except Exception:
        pass  # bridge 不可用: liveness 保持 None, 状态展示不受影响
    # ★ T4: 暴露顶层 ACTIVE team work 信息 (供 UI 顶部状态卡)
    # 用独立连接 + 用后即关, 避免 api_tasks 调用者持有未关闭 connection
    # (测试场景连续 fresh db 时, 上一次创建的连接持有旧 fd 会让 unlink 报 EBUSY)
    led_obj = Ledger(LEDGER_DB)
    active_info = None
    try:
        try:
            active_run = led_obj.get_active_team_work()
        except Exception:
            active_run = None
        if active_run:
            active_info = {
                "run_id": active_run["run_id"],
                "initiator_session": active_run["initiator_session"],
                "initial_task": active_run["initial_task"],
                "project_workspace": active_run["project_workspace"],
                "current_card_id": active_run["current_card_id"],
                "card_count": active_run["card_count"],
                "created_at": active_run["created_at"],
                "status": active_run["status"],
            }
            # 补当前卡的 assignee / session / workspace (worker + 工作目录)
            if active_run["current_card_id"]:
                cur_t = next((t for t in tasks
                             if t["id"] == active_run["current_card_id"]), {})
                cur_sess = sess.get(active_run["current_card_id"], {})
                active_info["current_card_assignee"] = (
                    cur_t.get("assignee") or cur_sess.get("profile"))
                active_info["current_card_session_id"] = cur_sess.get("session_id")
                active_info["current_card_workspace"] = cur_sess.get("workspace")
                active_info["current_card_title"] = cur_t.get("title")
                active_info["current_card_status"] = cur_t.get("status")
                cur_hc = led.get(active_run["current_card_id"], {})
                active_info["current_card_harvest_status"] = cur_hc.get(
                    "harvest_status")
    finally:
        try:
            led_obj.close()
        except Exception:
            pass
    return {"tasks": [_project_task_row(t) for t in tasks],
            "generated_at": time.time(),
            "team_work_active": active_info,
            "team_work_active_count": (1 if active_info else 0)}


def _worker_entry(name: str) -> dict:
    """按名查 worker 注册条目(需含 api_url)。"""
    try:
        reg = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        reg = {}
    e = reg.get(name)
    if not e or not e.get("api_url"):
        raise ValueError(f"worker '{name}' 未注册或缺少 api_url")
    return e


# ════════════════════════════════════════════════════════════════════
# issue #75 B1 (ARCH-75 §5, main 裁决 R2/R3): /messages 代理层窗口化
# ────────────────────────────────────────────────────────────────────
# 口径 (§5.1): 「对话内容」= user(非 command) + assistant 带非空白文本。
# 排除: role=tool、assistant 纯 tool_calls、thinking/reasoning 字段。
# R2 (a2 变体): 顶层条目只含 user/assistant 文本; tool_calls/tool 结果
#   收进所属条目的 tool_group 字段 (非独立条目, 满足 AC-11 且保历史工具详情)。
# R3 (main 实测修正): gateway /messages 忽略一切游标参数, 只认
#   limit/offset/order(仅 'oldest')。游标以 offset 语义编码 "o:<n>",
#   n = 已展示窗口头部之前的原始记录数; 尾部锚定 offset = max(0,
#   message_count - W) (message_count 来自 GET /api/sessions/<sid>)。
# 回退 (§9): 不带窗口参数 = 前端不调本函数, 走旧 _proxy_stream 全量。
# ════════════════════════════════════════════════════════════════════
CONV_RATIO_EST = 0.178   # 对话内容占比基线 (101/567, §5.2)
WANT_DEFAULT = 10        # 每批对话内容条数 (FR-08「最多10条」)
WINDOW_SAFETY = 1.5      # 期望回溯系数安全垫 (§5.2 步骤1)
MAX_WINDOW = 200         # 单窗口原始记录上限保护 (§5.2)
MAX_WINDOW_EXPAND = 3    # 自适应倍增重试次数上限 (§5.2 步骤3)
_WINDOW_MIN = 20


def _is_conversation_entry(m: dict) -> bool:
    """§5.1 口径: 该原始记录是否算 1 条「对话内容」。

    user(非 command) 恒计; assistant 需带非空白文本 —— 但**同时带
    tool_calls 的带文本 assistant** 也计 (前端 historyToMessages 把它
    同时渲染为工具组+气泡; 若窗口按纯 tool_calls 丢弃它会整回合消失)。
    """
    role = m.get("role")
    if role == "user":
        return m.get("display_kind") != "command"
    if role == "assistant":
        c = m.get("content")
        return isinstance(c, str) and c.strip() != ""
    return False


def _tool_group_items(entry: dict) -> list[dict]:
    """assistant tool_calls 记录 → tool_group 条目列表。"""
    out = []
    for c in entry.get("tool_calls") or []:
        fn = c.get("function") if isinstance(c, dict) else None
        out.append({
            "type": "tool_call",
            "id": c.get("id") if isinstance(c, dict) else None,
            "name": (fn or {}).get("name") or "tool",
            "arguments": (fn or {}).get("arguments") or "",
        })
    return out


def group_conversation_turns(raw: list[dict]) -> tuple[list[dict], list[int]]:
    """原始记录 → (对话条目, 每条目对应的 raw 下标)。

    回合重组 (R2): user/assistant 文本为顶层条目; 其前面 (且晚于上一
    对话条目) 的 tool_calls / role=tool 记录收进该条目的 tool_group。
    thinking/reasoning/reasoning_content 字段一律不下发 (§5.1)。
    尾部悬空 pending 工具组 (其归属文本在更新批次, 前端已展示) 丢弃。
    """
    entries: list[dict] = []
    idxs: list[int] = []
    pending: list[dict] = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if _is_conversation_entry(m):
            own = role == "assistant" and (m.get("tool_calls") or [])
            if own:
                # 带文本又带 tool_calls 的 assistant: 文本是顶层条目,
                # 其自身 tool_calls 并入 tool_group 尾部 (pending 里是
                # 本回合更早的工具调用, 顺序保持 raw 到达序)。
                entry_items = pending + _tool_group_items(m)
                pending = []
            else:
                entry_items = pending
                pending = []
            entries.append({
                "id": m.get("id"),
                "role": role,
                "content": m.get("content") or "",
                "display_kind": m.get("display_kind"),
                "timestamp": m.get("timestamp"),
                "tool_group": entry_items,
            })
            idxs.append(i)
        elif role == "assistant" and (m.get("tool_calls") or []):
            pending.extend(_tool_group_items(m))
        elif role == "tool":
            pending.append({
                "type": "tool_result",
                "tool_call_id": m.get("tool_call_id"),
                "content": m.get("content") or "",
                "is_error": bool(m.get("is_error")),
            })
        # 其余 (空文本 assistant / 未知 role / thinking 载体) 直接丢弃
    return entries, idxs


def _parse_before_cursor(before) -> int | None:
    """游标 "o:<n>" / 纯数字 → n (已展示窗口头部之前的原始记录数)。

    非法形态抛 ValueError (路由层翻 400)。None = 未提供 (首屏尾部锚定)。
    """
    if before is None:
        return None
    s = str(before).strip()
    if s.startswith("o:"):
        s = s[2:]
    if not s.isdigit():
        raise ValueError(f"非法 before 游标: {before!r}")
    return int(s)


def build_messages_window(fetch_raw, get_message_count,
                          want: int = WANT_DEFAULT, before=None,
                          conv_ratio: float = CONV_RATIO_EST,
                          safety: float = WINDOW_SAFETY,
                          max_window: int = MAX_WINDOW,
                          max_expand: int = MAX_WINDOW_EXPAND) -> dict:
    """§5.2 自适应窗口算法 (纯逻辑, 网络经两个回调注入, 便于单测)。

    参数:
      fetch_raw(offset, limit) -> list[dict]  (order=oldest 语义)
      get_message_count() -> int | None       (会话原始记录总数)
      want  : 每批对话内容条数
      before: "o:<n>" | None (None = 首屏取最近一批)
    返回:
      {"data": [对话条目...], "history": {has_more, before_cursor,
       conversation_count}}
      或 {"error": str, "code": int}
    """
    try:
        boundary = _parse_before_cursor(before)
    except ValueError as e:
        return {"error": str(e), "code": 400}
    want = max(1, min(int(want or WANT_DEFAULT), 100))
    if boundary is None:
        mc = get_message_count()
        if not isinstance(mc, int) or mc < 0:
            # 尾部锚定的唯一依据缺失 (R3: 无 message_count 无法定位尾部,
            # 猜 offset=0 会把最旧记录当最近, 语义更错) → 显式报错,
            # 前端按错误态渲染并可重试 (§3 归因), 不静默给错数据。
            return {"error": "session message_count unavailable", "code": 502}
        boundary = mc
    W = int(min(max_window, max(_WINDOW_MIN,
                                math.ceil(want / conv_ratio * safety))))
    expanded = 0
    while True:
        used_offset = max(0, boundary - W)
        # issue #75 s9 B-1 (AC-12 P0 阻塞): 游标续翻批 (before 给了游标) 的
        # 窗口不得越过边界 boundary —— 越界会把【已展示过】的记录原样拉回,
        # 而裁头逻辑裁的是窗口前部、保留的是窗口尾部 ⇒ 整批与上一批重复。
        # 首批 (无游标, 尾部锚定到 message_count) 语义不变: limit 仍为 W,
        # 越过边界只会撞到会话末尾 (不存在记录), 拉不回已展示内容。
        limit = W if before is None else min(W, boundary - used_offset)
        # limit<=0 = 边界已取尽 (游标批且无更早记录) → 空批, 不回退到
        # 越界取数 (那正是重复的来源)。
        raw = (fetch_raw(used_offset, limit) or []) if limit > 0 else []
        entries, idxs = group_conversation_turns(raw)
        trimmed_boundary = None
        if len(entries) > want:
            cut = len(entries) - want
            # 裁掉的更早条目留在原始流里: 下一批边界 = 最后一条被裁条目
            # 的绝对 raw 下标 (保证 AC-12 衔接无缺口, 不丢更早对话)。
            trimmed_boundary = used_offset + idxs[cut]
            entries = entries[cut:]
        if (len(entries) < want and len(raw) == limit and W < max_window
                and expanded < max_expand
                # 游标批不得再扩窗: 扩窗只会把窗口往【已展示区】方向拉
                # (used_offset 归零后 limit = boundary, 拿回的是同一段)。
                and (before is None or W < boundary)):
            W = min(W * 2, max_window)
            expanded += 1
            continue
        if trimmed_boundary is not None:
            has_more, cursor = True, f"o:{trimmed_boundary}"
        elif used_offset > 0:
            has_more, cursor = True, f"o:{used_offset}"
        else:
            has_more, cursor = False, None
        total_est = (round(boundary * conv_ratio)
                     if boundary is not None else len(entries))
        return {
            "data": entries,
            "history": {
                "has_more": has_more,
                "before_cursor": cursor,
                # §5.5 (i): 会话对话内容总数为估算值 (message_count × d),
                # 仅供"是否还有更多"兜底; UI 不直接展示。
                "conversation_count": total_est,
            },
        }


# ── issue #75 (ARCH-75 §6.3 / OQ-3): reaper 心跳单一读取源 ──────────
# 原逻辑内联在 api_workers(); 抽成 helper 供 api_workers 与新端点
# /api/heartbeat 共用, 判定式 hb_age < 90 保持逐字不变 (INV/R8:
# 只改暴露面, 不改语义)。
def _reaper_hb_age() -> float | None:
    """读 reaper 心跳文件年龄(秒); 文件缺失/损坏返回 None。"""
    hb_path = Path(os.environ.get("MW_HOME", "/mw_data")) / "reaper_heartbeat"
    if hb_path.exists():
        try:
            return time.time() - float(hb_path.read_text())
        except (ValueError, OSError):
            pass
    return None


class _GwError(Exception):
    """issue #81-4: gateway 错误的元组载体 (code, body_dict)。

    原实现把 (code, body) 拼成字符串, 再用 partition + json.loads 重解析,
    body 超过 [:200] 即抛 JSONDecodeError 并逃到 do_GET 兜底 500 ——
    真实状态码与上游错误串双丢 (issue #81-4 实测)。改为直接携带元组,
    截断只留在 __str__ 的日志摘要里, 不再参与控制流。
    """
    def __init__(self, code: int, body: dict):
        super().__init__(
            f"gateway {code}: {json.dumps(body, ensure_ascii=False)[:200]}")
        self.code = code
        self.body = body


def api_heartbeat() -> dict:
    """issue #75 B1: 顶栏存活心跳轻量端点 (数百字节, 纯本地文件读)。

    OQ-3 不可用 /api/workers/live (无 reaper 字段, 见 ARCH-75 §0.3);
    字段名与 api_workers 逐字一致, 前端顶栏灯直接换数据源零改语义。
    """
    hb_age = _reaper_hb_age()
    return {"reaper_alive": (hb_age is not None and hb_age < 90),
            "reaper_heartbeat_age_s": hb_age}


def _health_probe(entry: dict) -> bool:
    """对 worker gateway /health 做 2s 超时 GET 探测, 任何异常/超时视为离线。"""
    base = (entry.get("api_url") or "").rstrip("/")
    if not base:
        return False
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as r:
            return bool(r.status and r.status < 400)
    except Exception:
        return False


def api_workers() -> dict:
    """worker 列表 = registry.json; 附 reaper 心跳与容器健康占位。

    WCARD §5.3: 每条目附 online 布尔 —— 并发 /health 探测(2s 超时),
    N 个 worker 总耗时 ≈ 单次探测耗时, 不随数量线性放大。
    """
    try:
        reg = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        reg = {}
    # issue #81-5: 复用 _reaper_hb_age() 单一读取源 (原先此处内联重读心跳
    # 文件, 与 api_heartbeat 的 helper 逻辑重复)。判定式 hb_age < 90
    # 逐字不变, 零语义差异 (路径/缺失/损坏三态与 helper 完全一致)。
    hb_age = _reaper_hb_age()
    # v9: 把 SSE bridge 活跃订阅一并返回, dashboard 可视化 "现在谁在跑"
    try:
        from . import sse_bridge
        live = sse_bridge.live_snapshot()
        active = sse_bridge.active_subscriptions()
    except Exception:
        live, active = {}, []
    # online 并发探测: 线程池并发打 /health, 总耗时 ≈ 最慢一次探测(≤2s)
    names = list(reg.keys())
    online_map = {}
    if names:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
            online_map = dict(zip(names, ex.map(lambda n: _health_probe(reg[n]), names)))
    workers_view = {
        name: {
            # issue #75 B3 (ARCH-75 §6.1 OQ-1): 剥离 avatar 死重 —— 只剥
            # 未迁移的 data: base64 残留 (732KB 成因); 裸文件名保留, 前端
            # avatarSrc 据此判定有无, 图片字节走 /api/workers/<name>/avatar
            # (INV-03 头像不回归)。启动期 migrate_avatars() 兜底迁移。
            **{k: v for k, v in entry.items()
               if not (k == "avatar" and isinstance(v, str)
                       and v.startswith("data:image/"))},
            "online": online_map.get(name, False),
            "live": [
                {"session_id": k[1], "thinking": v.get("thinking"),
                 "last_event_ts": v.get("last_event_ts"),
                 "last_event_type": v.get("last_event_type"),
                 "event_count": v.get("event_count")}
                for k, v in live.items() if k[0] == name
            ],
        }
        for name, entry in reg.items()
    }
    # D2 (2026-10-13, issue #5 / VERIFY-20261013 §4): 给每个 worker 注入
    # derived_state —— 与 run 页 (/api/team-work/<id>) 同一派生源
    # (ledger.derive_worker_state / derive_run_overview), worker 面板不再
    # 只靠 SSE 活体独立拍脑袋, 消除「run 页等介入 / 面板空闲」双源失真。
    # fail-open: ledger 不可读时不注入该字段, 前端回落旧派生。
    # FIX-15 (2026-10-17, issue #15 worker 误激活): 暴露
    # active_team_work_run_id (单一字符串 | null), 让前端知道『
    # 当前派生为空是因为无 ACTIVE run, 还是 active run 名下确实没
    # 这张 worker 的卡』。派生层 derived_state 不注入 === worker
    # 不在当前 run 派生态, 前端不再依赖 sse_bridge.live 驱动
    # 「工作中」徽章, 杜绝跨 run SSE 残留导致的误激活。
    active_run_id = None
    try:
        _dled = Ledger(LEDGER_DB)
        try:
            derived, active_run_id = _workers_derived_states(_dled)
        finally:
            _dled.close()
        for name, ds in derived.items():
            if name in workers_view:
                workers_view[name]["derived_state"] = ds
    except Exception:
        pass
    return {"workers": workers_view,
            "active_run_ids": active,
            "active_team_work_run_id": active_run_id,
            "reaper_alive": (hb_age is not None and hb_age < 90),
            "reaper_heartbeat_age_s": hb_age}


# D2 worker 派生态显著度优先级 (同一 worker 名下多卡取最显著一张;
# 词表与 ledger.derive_worker_state 返回值逐字一致, UI 直接消费)
# issue#13 (2026-10-15): WAITING_SUB 插在 WORKING/HANDOFF 之后,
# NEED_CLARIFICATION/BLOCKED/ERROR/WAITING 之前 —— worker 等待子任务
# 时球仍在 worker 不在 main (与 WAITING「等介入」/ NEED_CLARIFICATION
# 「举手求答」不同), 应排 worker 忙碌族内, 不能与「举手求助」同档.
_DERIVED_SALIENCE = ["WORKING", "HANDOFF", "WAITING_SUB",
                     "NEED_CLARIFICATION",
                     "BLOCKED", "ERROR", "WAITING", "HARVESTED",
                     "ARCHIVED", "CANCELLED", "PENDING"]


def _workers_derived_states(led) -> tuple[dict, str | None]:
    """ACTIVE run 名下 worker → 派生态 (derive_worker_state 单一源)。

    卡 → worker 映射读 kanban (harvest_contracts 无 assignee 列, 与
    pixel_bridge._active_card 的 kanban 兜底同源)。

    返回 (out, active_run_id):
      - out: worker_name → 派生态 (WORKING / HANDOFF / WAITING_SUB /
        NEED_CLARIFICATION / BLOCKED / ERROR / WAITING / HARVESTED /
        ARCHIVED / CANCELLED / PENDING); 仅当 worker 名下有 ACTIVE
        run 的卡时才有键。空 dict 表示 worker 不在派生视野。
      - active_run_id: 当前 ACTIVE run_id (string), 无 ACTIVE run 时 None。
        FIX-15: 把这个上下文显式返回, 让 api_workers 把它塞到顶层
        active_team_work_run_id 字段, 前端据此判断『派生为空是因为
        没有 ACTIVE run 还是 worker 不在 run 里』, 不再依赖 SSE 残留
        决定 worker 状态 (issue #15 worker 误激活 根因)。

    行为兼容: 老调用方只解 out, 不解元组会爆 —— 故列 5 处调用全部
    在本 PR 内同步升到解元组; 单元测试 r2_2/r2_3 也同步断言形状。
    """
    out: dict[str, str] = {}
    active = led.get_active_team_work()
    if not active:
        return out, None
    run_id = active["run_id"]
    rows = led.conn.execute(
        "SELECT task_id FROM harvest_contracts WHERE team_work_run_id=?",
        (run_id,)).fetchall()
    if not rows:
        # ACTIVE run 存在但还没有卡: 仍返回 run_id 让前端知道『run 在』,
        # 此时全部 worker 都不在派生态 (空 dict)。
        return out, run_id
    task_ids = [r["task_id"] for r in rows]
    assignees = {}
    placeholders = ",".join("?" * len(task_ids))
    for kr in _ro_query(KANBAN_DB,
                        f"SELECT id, assignee FROM tasks "
                        f"WHERE id IN ({placeholders})", tuple(task_ids)):
        if kr.get("assignee"):
            assignees[kr["id"]] = kr["assignee"]
    for tid in task_ids:
        name = assignees.get(tid)
        if not name:
            continue
        st, _src = led.derive_worker_state(tid, run_id=run_id)
        cur = out.get(name)
        if cur is None or (_DERIVED_SALIENCE.index(st)
                           < _DERIVED_SALIENCE.index(cur)):
            out[name] = st
    return out, run_id


def api_workers_live() -> dict:
    """SSE-FIX-F2: 轻量活体端点 —— 只读 sse_bridge 内存态, 零 health 探测。

    刻意不复用 /api/workers: 后者每次并发探 N 个 worker 的 /health,
    reaper 敲门路径(FIX-3)不背这个成本。返回:
      live: [{profile, session_id, thinking, last_event_ts,
             last_event_type, event_count}]  ← sse_bridge.live_snapshot()
      active_run_ids: [run_id]              ← sse_bridge.active_subscriptions()
    """
    from . import sse_bridge
    return {
        "live": [
            {"profile": k[0], "session_id": k[1],
             "thinking": v.get("thinking"),
             "last_event_ts": v.get("last_event_ts"),
             "last_event_type": v.get("last_event_type"),
             "event_count": v.get("event_count")}
            for k, v in sse_bridge.live_snapshot().items()
        ],
        "active_run_ids": sse_bridge.active_subscriptions(),
    }


def api_workers_subscribe_sse(payload: dict) -> dict:
    """v9: SSE bridge subscribe 端点 — 通知 console 进程起 SSE daemon thread.

    caller 是 mw.cli __spawn-bg 子进程 (Popen 后立即退出), console 进程
    持有 daemon thread, 与子进程生命周期解耦.

    payload:
      {
        "run_id": str,           # 必填
        "profile": str,          # worker profile (coder/tester/...)
        "session_id": str,       # ledger session 锚点
        "task_id": str,          # 可选, 仅日志
        "api_url": str,          # 可选, worker gateway base URL (默认 registry 取)
        "api_key": str,          # 可选, worker gateway API key (默认 registry 取)
      }
    """
    from . import sse_bridge
    run_id = (payload.get("run_id") or "").strip()
    profile = (payload.get("profile") or "").strip()
    session_id = (payload.get("session_id") or "").strip()
    if not (run_id and profile and session_id):
        return {"ok": False, "error": "run_id / profile / session_id 都必填"}

    api_url = (payload.get("api_url") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    if not api_url or not api_key:
        try:
            reg = json.loads(Path(REGISTRY).read_text())
            entry = reg.get(profile, {})
            api_url = api_url or entry.get("api_url", "")
            api_key = api_key or entry.get("api_key", "")
        except (OSError, json.JSONDecodeError):
            pass

    try:
        sub = sse_bridge.open_bridge(
            run_id=run_id,
            profile=profile,
            session_id=session_id,
            base_url=api_url,
            api_key=api_key,
        )
        return {"ok": True, "run_id": run_id, "profile": profile,
                "session_id": session_id,
                "task_id": payload.get("task_id", "")}
    except Exception as e:
        return {"ok": False, "error": f"open_bridge failed: {e}"}


def _build_dependency_links(steps_in: list[dict],
                            workflow_depends_on: dict[str, list[str]]) -> list[tuple[str, str]]:
    """问题2核心: 把 steps_in + workflow.depends_on 转为 (child_task_placeholder, parent_step_id) 列表。

    返回 [(child_step_id, parent_step_id), ...] — 调用方拿到 step_id 映射成 task_id 后调 kanban link。

    优先级: step.depends_on (LLM 显式) > workflow_depends_on[step_id] (workflow 默认) > 链式 prev。
    """
    if not steps_in:
        return []
    step_ids = [s.get("step_id", "") for s in steps_in]
    links: list[tuple[str, str]] = []
    prev_step_id = step_ids[0]
    for i, step in enumerate(steps_in):
        if i == 0:
            continue
        sid = step_ids[i]
        # 1) LLM 显式
        deps = step.get("depends_on")
        # 2) workflow 默认
        if not deps:
            deps = workflow_depends_on.get(sid)
        parent = prev_step_id
        if deps and isinstance(deps, list):
            for d in deps:
                if d in step_ids:
                    parent = d
                    break
        links.append((sid, parent))
        prev_step_id = sid
    return links


# 工作目录根: 与闸三(MW_ALLOWED_WS_PREFIX)同源, 只暴露 projects/ 层,
# 不暴露 tasks/ui/uploads 等系统目录。
WS_ROOT = os.environ.get("MW_ALLOWED_WS_PREFIX", "/share_workspace")
WS_PROJECTS = os.path.join(WS_ROOT, "projects")
# 新建 workspace 名的严格白名单: 仅小写字母/数字/连字符, 防路径穿越
WS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def api_workspaces() -> dict:
    """列出 /share_workspace/projects/ 下所有工作目录(供用户选当前对话工作目录)。

    只列 projects/ 一层子目录; 每项附可写性与修改时间。
    根目录不存在时返回空列表(不报错), 前端据此提示。
    """
    out = []
    try:
        entries = sorted(Path(WS_PROJECTS).iterdir())
    except OSError:
        return {"workspaces": [], "root": WS_PROJECTS, "exists": False}
    for d in entries:
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            writable = os.access(str(d), os.W_OK)
            mtime = d.stat().st_mtime
        except OSError:
            writable, mtime = False, 0
        out.append({"name": d.name, "path": str(d),
                   "writable": writable, "mtime": mtime})
    return {"workspaces": out, "root": WS_PROJECTS, "exists": True}


def api_workspace_create(name: str) -> dict:
    """在 projects/ 下新建工作目录。严格白名单校验, 防路径穿越。

    只接受 [a-z0-9-] 且不以 - 开头的名字; 已存在则幂等返回。
    """
    if not isinstance(name, str) or not WS_NAME_RE.match(name):
        raise ValueError(
            f"非法工作目录名: {name!r} (仅允许小写字母/数字/连字符, "
            f"须以字母或数字开头, ≤63 字符)")
    target = os.path.join(WS_PROJECTS, name)
    # 双保险: normpath 后必须仍在 projects/ 之下
    norm = os.path.normpath(target)
    if not norm.startswith(os.path.normpath(WS_PROJECTS) + os.sep):
        raise ValueError(f"路径越权拒绝: {name!r}")
    try:
        os.makedirs(norm, exist_ok=True)
    except OSError as e:
        raise ValueError(f"创建失败: {e}")
    return {"name": name, "path": norm, "created": True}


def api_registry_save(workers: dict) -> dict:
    """registry.json 原子写(临时文件 + rename)。

    只校验新增/变更的条目必须有 api_url; 与现存注册表完全一致的旧条目
    原样放行——否则注册表里历史遗留的旧格式条目(无 api_url)会让
    任何一次保存/注册都被整体拒绝。

    Issue #51 (2026-10-17): avatar 字段以 data:image/...;base64, 开头时,
    服务端解码 → 魔数嗅探 → 落盘到 <REGISTRY_DIR>/avatars/<name>-<8hex>.<ext>
    → 把 entry.avatar 替换为裸文件名再写表。失败/超限 → ValueError 抛错,
    与其他字段校验口径一致。裸文件名形态(已迁移或新上传)按字面透传。
    """
    if not isinstance(workers, dict):
        raise ValueError("workers 必须是 {name: {...}} 对象")
    try:
        cur = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    for name, entry in workers.items():
        # Issue #51 R-1 (2026-10-17): worker key 白名单校验。单点真源 ——
        # 前端 WorkersView.jsx L395 + api_compose_generate 已有相同纪律, 此处
        # 合并为后端硬约束。否则 '_save_avatar_data_url' 的
        # f"{worker_name}-{suffix}.{ext}" 拼接会把含 '/' 或绝对路径的 key
        # 落出 AVATAR_DIR 外 (写侧路径逃逸, 实证复现 R-1 探针)。
        # s3d-fix N-1 加固 (2026-10-17): 显式排除 '.'/'..' — 正则集 [A-Za-z0-9_.-]+
        # 本身就接受它们, 必须在正则前就拒 (realpath 双保险对纯字面 '.'/'..'
        # 仍有 symlink 解析歧义, 拒在最前是最稳)。
        if not isinstance(name, str) or name in _RESERVED_WORKER_KEYS \
                or not _WORKER_KEY_RE.match(name):
            if not isinstance(name, str):
                msg = f"worker key {name!r} 非法 — 必须是非空字符串"
            elif name in _RESERVED_WORKER_KEYS:
                msg = (f"worker key {name!r} 非法 — 保留名 ('.'/'..') "
                       "拒绝接受, 防止路径解析歧义")
            else:
                msg = f"worker key {name!r} 非法 — 仅允许 [A-Za-z0-9_.-]+"
            raise ValueError(msg)
        if not isinstance(entry, dict):
            raise ValueError(f"worker '{name}' 必须是对象")
        if cur.get(name) == entry:
            continue  # 未变更的存量条目不校验
        if "api_url" not in entry:
            raise ValueError(f"worker '{name}' 缺少 api_url")
        # avatar 字段类型 + 形态校验:
        #  - data URL 形态 (待解码落盘): 留给第二轮处理
        #  - 裸文件名形态: 必须严格匹配 _AVATAR_NAME_RE (防路径穿越/超长脏值)
        av = entry.get("avatar")
        if av is not None:
            if not isinstance(av, str):
                raise ValueError(f"worker '{name}'.avatar 必须是字符串")
            if av.startswith("data:image/"):
                # data URL 形态: 字节大小由 _detect_avatar_format (200KB) 验,
                # 长度前置卡死仅在解前能拒绝超长裸字符串, 解后长度才是真指标。
                pass
            else:
                # 裸文件名形态: Issue #51 R-3 (2026-10-17) — AC-51-08 128 上限强制。
                # 与 _AVATAR_NAME_RE 的匹配长度上界一致, 防 300 字符脏值入库。
                if len(av) > AVATAR_NAME_MAX_LEN:
                    raise ValueError(
                        f"worker '{name}'.avatar 超长 — 长度 {len(av)} 超过 "
                        f"{AVATAR_NAME_MAX_LEN} 上限")
                if not _AVATAR_NAME_RE.match(av):
                    # 不符合裸文件名形态 = 路径穿越/脏值风险 → 拒
                    raise ValueError(
                        f"worker '{name}'.avatar 形态非法: 必须是 data URL 或 "
                        f"匹配 '^<name>-<8hex>.<png|jpg|jpeg|webp|gif>$'")
    # 第二遍: 把含 data URL 的 entry 替换成文件名;旧 avatar 文件同步清理。
    new_workers: dict = {}
    for name, entry in workers.items():
        entry = dict(entry)
        av = entry.get("avatar")
        if isinstance(av, str) and av.startswith("data:image/"):
            new_name = _save_avatar_data_url(name, av)
            entry["avatar"] = new_name
            # 旧文件 (上一轮同名 worker) 在 _save_avatar_data_url 内已删,
            # 此处仅记录到 entry;外层 put 不再动文件。
        new_workers[name] = entry
    tmp = Path(str(REGISTRY) + ".tmp")
    tmp.write_text(json.dumps(new_workers, ensure_ascii=False, indent=2))
    os.replace(tmp, REGISTRY)
    return {"ok": True, "count": len(new_workers)}


def _avatar_root() -> Path:
    """avatars/ 根目录的 Path, 失败时惰性创建 (含 parents=True)。"""
    root = Path(AVATAR_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _detect_avatar_format(raw: bytes) -> tuple[str, str]:
    """按 _AVATAR_MAGIC 嗅探白名单, 命中返回 (mime, ext); 否则 ValueError。

    头 12 字节逐前缀对比, 严格不依赖扩展名/MIME 自报。
    部分容器 (webp) 还需校验 secondary 字段 (字节偏移), 防 RIFF 容器冒充。
    """
    if not raw:
        raise ValueError("avatar 文件为空")
    if len(raw) > AVATAR_MAX_BYTES:
        raise ValueError(
            f"avatar 解码后 {len(raw)} 字节 > {AVATAR_MAX_BYTES} 字节上限")
    for prefix, mime, ext, sec_off, sec_bytes in _AVATAR_MAGIC:
        if not raw.startswith(prefix):
            continue
        if sec_off is not None and sec_bytes is not None:
            # 容器类型: 二次魔数必须严格匹配 (webp RIFF + 'WEBP')
            end = sec_off + len(sec_bytes)
            if end > len(raw) or raw[sec_off:end] != sec_bytes:
                continue
        return mime, ext
    raise ValueError(
        f"avatar 魔数嗅探失败: 解码后前 12 字节 = "
        f"{raw[:12].hex()} (不在 png/jpg/webp/gif 白名单)")


def _decode_avatar_data_url(value: str) -> bytes:
    """解 data:image/<mime>;base64,<payload> → 原始字节。

    不接受 url-encoded (前端 FileReader.readAsDataURL 永远是 base64);
    解码失败或格式错 → ValueError。
    """
    if "," not in value:
        raise ValueError("avatar data URL 缺少 ',' 分隔符")
    head, payload = value.split(",", 1)
    if not head.startswith("data:image/"):
        raise ValueError("avatar data URL 必须以 'data:image/' 开头")
    if ";base64" not in head:
        raise ValueError("avatar data URL 必须含 ';base64'")
    import base64
    try:
        return base64.b64decode(payload, validate=True)
    except (ValueError, TypeError, binascii.Error) as e:
        raise ValueError(f"avatar base64 解码失败: {e}")


def _save_avatar_data_url(worker_name: str, data_url: str) -> str:
    """解码 data URL → 魔数嗅探 → 落盘到 <AVATAR_DIR>/<name>-<8hex>.<ext>。

    返回裸文件名 (不含路径)。落盘前删除 worker 已有头像文件 (atomic 替换)。

    Issue #51 R-1 (2026-10-17): worker_name 已由 api_registry_save 入口白名单
    校验过 (^[A-Za-z0-9_.-]+$), 但此处仍做 realpath/commonpath 双保险, 防止
    AVATAR_DIR 被运维误指向根目录等情形下越界写出。读侧/删侧 _send_avatar_file
    / _delete_avatar_file_silent 已采用同 realpath 断言纪律, 写侧补齐。
    """
    raw = _decode_avatar_data_url(data_url)
    mime, ext = _detect_avatar_format(raw)
    # hex 8 位随机: 用 secrets 防猜测; 失败回退 os.urandom。
    try:
        import secrets
        suffix = secrets.token_hex(4)
    except Exception:
        import os as _os
        suffix = _os.urandom(4).hex()
    fname = f"{worker_name}-{suffix}.{ext}"
    root = _avatar_root()
    target = root / fname
    # 写侧防御: realpath 断言 target 落在 AVATAR_DIR 内 (防运维误改
    # AVATAR_DIR 指向根目录等情形下越界)。worker_name 已白名单, 此为双保险。
    target_resolved = target.resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(target_resolved)]) \
            != str(root_resolved):
        raise ValueError(
            f"avatar 落盘路径 {target_resolved} 不在 AVATAR_DIR {root_resolved} 内"
            " — worker_name 越界或 AVATAR_DIR 配置异常")
    # 删旧文件 (若有) —— 即使本次最终失败, 旧文件仍可恢复。
    old = workers_avatar_filename(_load_registry(), worker_name)
    if old and old != fname:
        _delete_avatar_file_silent(old)
    target.write_bytes(raw)
    return fname


def _load_registry() -> dict:
    """读 registry, 容错性等价 api_registry_save 顶部那段。"""
    try:
        d = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return d if isinstance(d, dict) else {}


def workers_avatar_filename(reg: dict, name: str) -> str | None:
    """返回 worker 注册项里的 avatar 字段, 经 _AVATAR_NAME_RE 校验后透传。

    与裸文件名形态 (data URL 已迁移) 一致即视为合法; 否则 None。
    """
    if not isinstance(reg, dict):
        return None
    e = reg.get(name)
    if not isinstance(e, dict):
        return None
    av = e.get("avatar")
    if not isinstance(av, str):
        return None
    if not _AVATAR_NAME_RE.match(av):
        return None
    return av


def _delete_avatar_file_silent(filename: str | None) -> None:
    """best-effort 删 avatar 文件; 文件不存在/路径异常时不抛错。

    realpath 前缀校验 (defense in depth): 只允许落在 AVATAR_DIR 根内的
    目标文件被删除, 防运维误改 AVATAR_DIR 指向根目录时越界。
    """
    if not filename:
        return
    if not _AVATAR_NAME_RE.match(filename):
        return  # 路径形态非法直接忽略
    try:
        root = _avatar_root().resolve()
        target = (root / filename).resolve()
        # 必须在 root 之内 (防越界); 用 os.path.commonpath 比 .is_relative_to 更通用。
        if os.path.commonpath([str(root), str(target)]) != str(root):
            return
        if target.is_file():
            target.unlink()
    except OSError:
        pass


def migrate_avatars() -> dict:
    """启动期幂等迁移: 把 registry 里残留的 data:image base64 avatar 一次性
    转成文件名 + 落盘到 avatars/。

    失败单条置空 + 计数告警, 不阻塞其他条目; 整体不影响启动 (fail-open)。
    返回统计 {scanned, migrated, failed_clear, written} 供日志; 写入只在
    至少有一处变更时发生 (写完 os.replace 原子落盘)。
    """
    reg = _load_registry()
    if not reg:
        return {"scanned": 0, "migrated": 0,
                "failed_clear": 0, "written": False}
    scanned = migrated = failed_clear = 0
    changed = False
    new_reg: dict = {}
    for name, entry in reg.items():
        if not isinstance(entry, dict):
            new_reg[name] = entry
            continue
        av = entry.get("avatar")
        new_entry = dict(entry)
        if isinstance(av, str) and av.startswith("data:image/"):
            scanned += 1
            try:
                new_name = _save_avatar_data_url(name, av)
                new_entry["avatar"] = new_name
                migrated += 1
                changed = True
            except ValueError as e:
                # base64 损坏/超限/魔数嗅探失败: 置空并告警。
                new_entry.pop("avatar", None)
                failed_clear += 1
                changed = True
                print(
                    f"[console] avatar migration skipped for '{name}': {e}")
        new_reg[name] = new_entry
    if changed:
        tmp = Path(str(REGISTRY) + ".tmp")
        tmp.write_text(json.dumps(new_reg, ensure_ascii=False, indent=2))
        os.replace(tmp, REGISTRY)
    return {"scanned": scanned, "migrated": migrated,
            "failed_clear": failed_clear, "written": changed}


def api_worker_avatar(name: str) -> dict:
    """解析 worker 头像文件路径, 供测试 / 内部复用。

    返回 {"ok": bool, "path": <abs path>|None, "error": str}。
    文件读取与 Content-Type 决策委托 _send_avatar_file (handler 形态)。
    """
    if not name:
        return {"ok": False, "path": None, "error": "name 必填"}
    fname = workers_avatar_filename(_load_registry(), name)
    if not fname:
        return {"ok": False, "path": None,
                "error": f"worker '{name}' 无头像或文件名非法"}
    try:
        root = _avatar_root().resolve()
        target = (root / fname).resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            return {"ok": False, "path": None, "error": "路径非法"}
        if not target.is_file():
            return {"ok": False, "path": None, "error": "文件缺失"}
    except OSError as e:
        return {"ok": False, "path": None, "error": str(e)}
    return {"ok": True, "path": str(target), "error": ""}


def _send_avatar_file(handler, name: str) -> None:
    """GET /api/workers/<name>/avatar 真路由: 读 registry → 校验文件 →
    realpath 断言在 avatars/ 根内 → 返回 200/404/422。

    worker 名先精确查表 (reg.get(name) → 真有则用其 avatar 字段),
    绝不接受请求路径里的额外文件名片段 (也没有)。
    """
    reg = _load_registry()
    fname = workers_avatar_filename(reg, name)
    if not fname:
        handler.send_response(404)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(json.dumps(
            {"error": f"worker '{name}' 无头像或文件名非法"}).encode("utf-8"))
        return
    try:
        root = _avatar_root().resolve()
        target = (root / fname).resolve()
        # 路径穿越双保险: 必须落在 AVATAR_DIR 根内。
        if os.path.commonpath([str(root), str(target)]) != str(root):
            handler.send_response(422)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(json.dumps(
                {"error": "avatar 路径非法"}).encode("utf-8"))
            return
        if not target.is_file():
            handler.send_response(404)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(json.dumps(
                {"error": f"avatar 文件缺失: {fname}"}).encode("utf-8"))
            return
        data = target.read_bytes()
    except OSError as e:
        handler.send_response(500)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(json.dumps(
            {"error": f"avatar 读取失败: {e}"}).encode("utf-8"))
        return
    # 用 _AVATAR_MAGIC 决定 Content-Type (与落盘时一致)。
    mime, _ext = _detect_avatar_format(data)
    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.end_headers()
    # issue #62 A-2: 头像 >128KB 即险, 循环写全 (经 Handler 类引用,
    # 不依赖 handler 实例方法 —— 调用方可能是鸭子类型 handler)。
    Handler._write_all(handler.wfile, data)


def api_registry_delete(name: str) -> dict:
    """删除单个 worker: 只摘除目标 key 后原子回写。

    刻意不走 api_registry_save 的全表校验——注册表里可能存在历史遗留的
    旧格式条目(无 api_url), 全表校验会让这些条目永远删不掉。

    Issue #51: 删除 worker 时同步清理其头像文件 (best-effort,
    失败不阻塞删除本身 —— 文件已经不在也是一种合法状态)。
    """
    if not name:
        raise ValueError("worker 名称必填")
    try:
        reg = json.loads(Path(REGISTRY).read_text())
    except (OSError, json.JSONDecodeError):
        reg = {}
    if not isinstance(reg, dict) or name not in reg:
        raise ValueError(f"worker '{name}' 不存在")
    # 同步删 avatar 文件 (best-effort)
    old_avatar = workers_avatar_filename(reg, name)
    reg.pop(name)
    tmp = Path(str(REGISTRY) + ".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2))
    os.replace(tmp, REGISTRY)
    _delete_avatar_file_silent(old_avatar)
    return {"ok": True, "removed": name, "count": len(reg)}


# ===== 问题7D: SSE + webhook =====

def api_webhook_register(payload: dict) -> dict:
    """POST /api/webhooks 注册回调。 payload={url, events:[], secret:''}"""
    url = (payload.get("url") or "").strip()
    if not url.startswith("http"):
        raise ValueError("url 必须以 http:// 或 https:// 开头")
    events = payload.get("events") or []
    if not isinstance(events, list):
        raise ValueError("events 必须是数组")
    secret = (payload.get("secret") or "").strip()
    # issue #61 顺带审: ledger 用完必须 close, 堵 fd 泄漏 (同款纪律同其他端点)
    ledger = Ledger(LEDGER_DB)
    try:
        wid = ledger.register_webhook(url, events, secret)
    finally:
        ledger.close()
    return {"id": wid, "url": url, "events": events, "enabled": True}


def api_webhook_list() -> dict:
    """GET /api/webhooks 列出所有 webhook。"""
    ledger = Ledger(LEDGER_DB)
    try:
        rows = ledger.list_webhooks()
    finally:
        ledger.close()
    return {"webhooks": [{
        "id": r["id"], "url": r["url"], "events": r["events"],
        "has_secret": bool(r["secret"]), "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
    } for r in rows]}


def delete_webhook_by_id(wid: str) -> bool:
    """DELETE /api/webhooks/<id>"""
    ledger = Ledger(LEDGER_DB)
    try:
        return ledger.delete_webhook(wid)
    finally:
        ledger.close()


def dispatch_webhook(event: dict) -> None:
    """问题7D: 同步触发所有匹配的 webhook 投递。 用 daemon thread 不阻塞主线程。

    失败/超时只 log, 不抛(投递失败不应影响主流程)。"""
    # issue #61 顺带审: 本函数每次事件都新建 Ledger, 旧写法不 close →
    # 事件越多泄漏越多 fd。
    ledger = Ledger(LEDGER_DB)
    try:
        rows = ledger.list_webhooks(enabled_only=True)
    finally:
        ledger.close()
    if not rows:
        return
    for r in rows:
        ev_filter = (r["events"] or "").split(",")
        if ev_filter and ev_filter != [""]:
            if event.get("event_type") not in ev_filter:
                continue
        url = r["url"]
        secret = r["secret"] or ""
        wid = r["id"]
        threading.Thread(target=_post_webhook,
                         args=(url, secret, event, wid),
                         daemon=True).start()


def _post_webhook(url: str, secret: str, event: dict, wid: str):
    """后台线程: POST event JSON 到 url, 3s 超时。 secret 非空加 X-MW-Signature (HMAC-SHA256)。"""
    body = json.dumps(event, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "mw-webhook/1.0"}
    if secret:
        import hashlib, hmac
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-MW-Signature"] = f"sha256={sig}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            _ = resp.read()  # drain
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[webhook] {wid} {url} 失败: {e!r}")
    except Exception as e:
        print(f"[webhook] {wid} {url} 异常: {e!r}")


def api_resume(task_id: str, instruction: str,
               actor: str = "operator",
               actor_claimed: str | None = None) -> dict:
    """控制台直接回答/续作: **入队** resume 请求, 立即返回受理。

    2026-10-25 issue #104 根因 1 (console resume 不进账本、零簿记):
      旧版直接起后台线程调 pipeline.resume_task, **不写 task_resume_requests**;
      只有 CLI 路径 (mw/cli.py) 才入队, reaper 只消费该队列 ⇒ console 的
      人工答复对 reaper 完全不可见 (实案 t_a8052713: 账本上该卡 0 行)。
      修法: console 路径改为入队同一张表 (ledger.enqueue_resume), 由
      reaper 消费 —— 两条答复通道共用一条队列、一份账本。

    2026-10-25 issue #104 期望 2 (双答复通道互相不可见):
      入队前先读 ledger.last_resume_actor —— 若另一方已答过, 返回
      conflict 提示 (卡片上可见「已被 X 于 HH:MM 答复」), 不是盲注。

    2026-10-25 issue #104 期望 4 (api_resume 不校验卡片终态):
      入口加 is_terminal() 守卫 —— 已 ARCHIVED/CANCELLED/HARVESTED 的卡
      直接拒 (409), 不再往终态卡注入新指令。

    2026-10-25 issue #115 (actor 由客户端自称 ⇒ 答复归属可伪造):
      actor 是**服务端决定的**, 不是入参可信面。HTTP 路由层 (/api/resume)
      不再把请求体的 actor 传进来, 而是固定传 "operator" + 把自称值放
      actor_claimed。actor_claimed 只落审计痕迹 (harvest_log / 事件),
      绝不参与归属判定, 也绝不参与冲突比较 —— 否则「冒充成对方」仍能
      让两条答复看起来同 actor, 把 #104 期望 2 的冲突提示绕掉。
      main 的答复走 CLI 路径 (mw/cli.py cmd_resume → actor="main"),
      不经本端点, 归属不受影响。

    resume 可能耗时数分钟(worker 干活), 不能阻塞 HTTP 请求 —— 入队后
    由 reaper 消费, 语义与 CLI 路径完全一致。
    """
    from .ledger import Ledger
    if not task_id or not instruction.strip():
        raise ValueError("task_id 与 instruction 必填")
    led = Ledger(LEDGER_DB)
    try:
        sess = led.get_session(task_id)
        if not sess or not sess["session_id"]:
            raise ValueError(f"task {task_id} 无 session 锚点, 无法 resume")
        # issue #104 期望 4: 终态守卫 —— 终态卡不接受新指令。
        if led.is_terminal(task_id):
            return {"ok": False, "code": 409,
                    "error": f"task {task_id} 已终态, 不可 resume",
                    "harvest_status": led.get_contract(task_id)["harvest_status"]
                    if led.get_contract(task_id) else None}
        # issue #104 期望 2: 冲突检测 —— 另一方已答过就提示, 不盲注。
        prior = led.last_resume_actor(task_id)
        conflict = None
        if prior and prior.get("actor") and prior["actor"] != actor:
            hhmm = time.strftime("%H:%M", time.localtime(prior["requested_at"]))
            conflict = (f"已被 {prior['actor']} 于 {hhmm} 答复 "
                        f"(status={prior['status']})")
        # 入队 —— 同一张 task_resume_requests 表, reaper 消费。
        req_id = led.enqueue_resume(task_id, instruction, actor=actor)
        # 答复事实落痕: harvest_log + worker.state 事件 (双通道互读)。
        # actor_claimed 一并传入, 仅在 ≠ actor 时留「自称值被规范化」痕迹。
        stamp = led.record_resume_answer(task_id, actor, instruction,
                                        actor_claimed=actor_claimed)
    finally:
        led.close()
    out = {"ok": True, "task_id": task_id, "request_id": req_id,
           "actor": actor,
           "answered_at_hhmm": stamp["answered_at_hhmm"],
           "note": _t("console.note.resume_accepted")}
    if conflict:
        out["conflict"] = conflict
    return out


def _kill_spawn_bg(task_id: str) -> int:
    """杀掉该 task 的 detached spawn-bg 进程(取消 mw 侧对 worker 的驱动)。

    worker 容器内的 LLM turn 无 abort API, 无法从这里强杀; 但杀掉 mw 侧
    spawn-bg 后, mw 不再 harvest/续作, 配合 ledger 终态, 该 task 彻底停摆。
    返回杀掉的进程数。
    """
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", f"__spawn-bg.*{task_id}"],
                          capture_output=True, text=True, timeout=5)
        n = 0
        for pid in r.stdout.split():
            try:
                os.kill(int(pid), 15)  # SIGTERM
                n += 1
            except (ProcessLookupError, ValueError):
                pass
        return n
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0


def api_task_state_correct(task_id: str, payload: dict) -> dict:
    """M5 main 纠正端点 (BATCH1-DESIGN §M5, #5: main 负责纠正状态)。

    payload:
      action: "reassess" | "set"
        reassess = 立即按当前 TODO.md 状态重跑一次 harvest (等价 M3, 不等 15s)
        set      = 显式改写 (main 人工判定), 走 update_worker_state
      target_status: set 时必填 (HARVESTED/BLOCKED/PENDING/...)
      reason: 必填, 纠正理由 (审计轨迹)
      actor_session: 发起纠正的 main session id (鉴权用)
    守卫:
      - actor_session 必须等于该卡的 initiator session (task_initiators),
        否则 403 (对齐既有「仅 run initiator 可调」模式)
      - HARVESTED/ARCHIVED/CANCELLED 真终态保护由 update_worker_state
        迁移表强制 (set 回退直接 raise → 422); 翻案走 reassess, 由
        harvest B 白名单决定可否重写
    """
    from .ledger import Ledger
    if not task_id:
        raise ValueError("task_id 必填")
    action = (payload.get("action") or "").strip()
    reason = (payload.get("reason") or "").strip()
    actor_session = (payload.get("actor_session") or "").strip()
    if action not in ("reassess", "set"):
        raise ValueError("action 必须为 'reassess' 或 'set'")
    if not reason:
        raise ValueError("reason 必填 (纠正轨迹可审计)")
    led = Ledger(LEDGER_DB)
    try:
        # 鉴权: 仅该卡 initiator (发起这张卡的 main session) 可纠正
        initiator = led.get_initiator(task_id)
        if not initiator or actor_session != initiator:
            return {"ok": False, "code": 403,
                    "error": "仅该卡 initiator session 可纠正状态"}
        row = led.get_contract(task_id)
        if not row:
            return {"ok": False, "code": 404, "error": "契约不存在"}
        if action == "set":
            target = (payload.get("target_status") or "").strip()
            if not target:
                raise ValueError("action=set 时 target_status 必填")
            old = row["harvest_status"]
            # 真终态保护: set 不许从 HARVESTED/ARCHIVED/CANCELLED 回退
            if old in ("HARVESTED", "ARCHIVED", "CANCELLED"):
                raise ValueError(
                    f"{old} 是真终态, 不允许 set 回退 — 翻案请用 reassess")
            led.update_worker_state(task_id, target, actor="main_correct",
                                   reason=reason)
            return {"ok": True, "task_id": task_id, "action": "set",
                    "from": old, "to": target, "reason": reason}
        # reassess: 按当前 TODO.md 状态立即重 harvest (复用 M3 语义)
        from .reaper import _read_todo_status
        from .todo import STATUS_COMPLETED, STATUS_FAILED
        from .harvest import harvest
        sess = led.get_session(task_id)
        ws = _row_get(sess, "workspace")
        if not ws:
            return {"ok": False, "code": 404,
                    "error": "无 workspace 锚点, 无法 reassess"}
        contract = json.loads(row["contract_json"] or "{}")
        todo_status = _read_todo_status(ws, ledger_row=row,
                                      contract=contract)
        norm = (todo_status or "").strip().lower() if todo_status else None
        # P1-1 修复 (BATCH1-REVIEW): 镜像 M3 (_reap_late_completions) 的
        # 终态门槛 —— 仅 completed/failed 才结算。working/blocked/缺失等
        # 非终态一律不结算, 杜绝 `or "completed"` 兜底把「没交卷」误报
        # 成 COMPLETE 中继给 main。
        if norm not in (STATUS_COMPLETED, STATUS_FAILED,
                       "complete", "fail"):
            return {"ok": True, "task_id": task_id, "action": "reassess",
                    "todo_status": todo_status, "settled": False,
                    "note": _t("console.note.reassess_non_terminal"), "reason": reason}
        r = harvest(task_id, ledger=led,
                    hermes_home=os.environ.get("MW_HERMES_HOME", "/opt/data"),
                    workspace=ws, spawn_result=None,
                    worker_status=norm)
        # 纠正轨迹: reassess 也留审计痕
        try:
            led.emit_event("worker.state_correct", task_id, {
                "action": "reassess", "todo_status": todo_status,
                "verdict": r.get("verdict"), "reason": reason,
                "actor_session": actor_session,
            })
        except Exception:
            pass
        return {"ok": True, "task_id": task_id, "action": "reassess",
                "todo_status": todo_status,
                "verdict": r.get("verdict"),
                "harvest_status": r.get("harvest_status"),
                "settled": r.get("settled"), "reason": reason}
    finally:
        try:
            led.close()
        except Exception:
            pass


def _row_get(row, key, default=None):
    """sqlite3.Row 无 .get() —— 安全取列 (与 reaper._row_get 同语义)。"""
    if row is None:
        return default
    try:
        if key in row.keys():
            v = row[key]
            return default if v is None else v
    except Exception:
        pass
    return default


def api_task_cancel(task_id: str) -> dict:
    """取消任务: 杀 mw 侧 spawn-bg 进程 + 标 ledger CANCELLED。

    1A 边界 (2026-10-03): mw 不再归档 kanban 卡 —— 卡的生命周期归 main。
    mw 只停自己这一侧的驱动 (杀 spawn-bg + 标终态, reaper 不再结算/敲门)。
    kanban 卡的 archive 由 main 自己调 `hermes kanban archive` 完成。
    """
    from .ledger import Ledger
    if not task_id:
        raise ValueError("task_id 必填")
    led = Ledger(LEDGER_DB)
    killed = _kill_spawn_bg(task_id)
    led.set_terminal(task_id, "CANCELLED", _t("console.cancel_by_user"))
    led.close()
    return {"ok": True, "task_id": task_id, "killed_procs": killed,
            "note": _t("console.note.task_cancel")}


def api_task_archive(task_id: str) -> dict:
    """归档残留/历史任务: 标 ledger ARCHIVED + 联动停掉 spawn wrapper。

    1A 边界: mw 不碰 kanban。kanban 卡归档由 main 自己完成。

    2026-10-25 issue #104 根因 4 (归档不停 worker):
      ledger.set_terminal 只标终态让 reaper 跳过, **不调用** _kill_spawn_bg
      (有杀进程能力的只有 task_cancel 路径) ⇒ 卡归档后 worker 仍继续施工
      产出 commit (实案 t_a8052713: 归档后 3+ 分钟 worker 仍在改文件)。
      修法: archive 路径至少联动 _kill_spawn_bg 停掉 spawn wrapper,
      与 api_task_cancel 对齐。
    """
    from .ledger import Ledger
    if not task_id:
        raise ValueError("task_id 必填")
    led = Ledger(LEDGER_DB)
    # issue #104: 归档前先杀 mw 侧驱动 (spawn wrapper), 与 cancel 对齐。
    killed = _kill_spawn_bg(task_id)
    led.set_terminal(task_id, "ARCHIVED", _t("console.archive_by_user"))
    led.close()
    return {"ok": True, "task_id": task_id, "killed_procs": killed,
            "note": _t("console.note.task_archive")}



class Handler(BaseHTTPRequestHandler):
    # issue #62 (2026-10-19): BaseHTTPRequestHandler.wbufsize=0 → self.wfile
    # 是无缓冲 SocketIO, write() = 单次 sock.send(): 短写时返回已写字节数、
    # 剩余字节静默丢弃且**不抛异常** (macOS send buffer ~128KB, GET / 642KB
    # 实测 40/40 截断)。所有 body 写出必须经本助手循环写全; 返回 0 视为
    # 断连, 抛 OSError 交调用方既有兜底处理。
    @staticmethod
    def _write_all(wfile, body: bytes) -> None:
        """循环写直到全部落线; wfile.write 允许 short-write 且不抛异常。"""
        view = memoryview(body)
        while view:
            n = wfile.write(view)
            if n is None:
                # io 流惯例: 返回 None = 已全量吞下 (真实 SocketIO 恒返回 int,
                # 仅测试桩/鸭子类型 wfile 会返回 None)。
                break
            if not n:
                raise OSError("wfile.write returned 0")
            view = view[n:]

    # Apple container 端口代理适配参数 (docs/ANALYSIS-PROXY-TRUNC.md):
    PACE_CHUNK = int(os.environ.get("MW_PACE_CHUNK", "65536"))  # 64 KB
    PACE_DELAY = float(os.environ.get("MW_PACE_DELAY", "0.025"))  # 25 ms
    PACE_DISABLED = os.environ.get("MW_PACE_DISABLED", "") == "1"
    # issue #63 (PLAN-63 §5 步骤1): 下游断连后排空上游的时长上限。
    # 0 = 不排空(行为回滚开关, 近似恢复 issue #63 之前的旧语义)。
    DRAIN_MAX_S = float(os.environ.get("MW_PROXY_DRAIN_MAX_S", "3600"))

    @classmethod
    def _write_paced(cls, wfile, body: bytes) -> None:
        """Apple container 端口代理适配 (docs/ANALYSIS-PROXY-TRUNC.md):
        >~128KB 的突发写在代理处被提前 FIN 截断 (190 实测: 642KB 92/100,
        176KB 10/10 截断; ≤90KB 全过; SSE 涓流全过)。>PACE_CHUNK 的 body
        逐块写 + 块间 sleep, 给代理用户态留出排空时间。≤PACE_CHUNK 语义
        与 _write_all 完全一致 (SSE/小 JSON 零新增延迟)。"""
        if cls.PACE_DISABLED or len(body) <= cls.PACE_CHUNK:
            cls._write_all(wfile, body)
            return
        view = memoryview(body)
        first = True
        while view:
            if not first:
                time.sleep(cls.PACE_DELAY)
            cls._write_all(wfile, bytes(view[:cls.PACE_CHUNK]))
            view = view[cls.PACE_CHUNK:]
            first = False

    def _send(self, code: int, obj, ctype="application/json"):
        if isinstance(obj, bytes):
            body = obj
        elif isinstance(obj, str):
            body = obj.encode()
        else:
            body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if "charset" not in ctype:
            ctype += "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # issue #62/64 A-2 → P-2: team-work 详情/state 可 >128KB, Apple container 代理截断适配
        self._write_paced(self.wfile, body)

    def _proxy_stream(self, url: str, key: str):
        """把当前请求转发到 url 并流式回传(SSE 逐行 flush)。

        通用转发体: main gateway 与 worker gateway 共用。
        """
        import urllib.request
        import urllib.error
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(url, data=body, method=self.command)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        ctype = self.headers.get("Content-Type")
        if body is not None and ctype:
            req.add_header("Content-Type", ctype)
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as e:
            data = e.read()
            self._send(e.code, data,
                       e.headers.get("Content-Type", "application/json"))
            return
        except Exception as e:
            self._send(502, {"error": f"gateway unreachable: {e}"})
            return
        with resp:
            ct = resp.headers.get("Content-Type", "application/json")
            self.send_response(resp.status)
            self.send_header(
                "Content-Type",
                ct + ("; charset=utf-8" if "json" in ct and "charset" not in ct else ""))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # SSE 必须逐行转发: read(4096) 会阻塞攒满 4KB 才返回,
            # SSE 事件每帧才几百字节 → 事件全卡在代理缓冲区,
            # 前端长时间收不到 run.started(表现为状态条卡在"发送请求")。
            try:
                for line in resp:
                    # issue #62/64 A-3 → P-2: gateway chunked JSON 整包无换行 → 单行
                    # 可到 MB 级, Apple container 代理截断适配。≤64 KB 走直通,
                    # SSE 实时性零回退; flush 语义保留。
                    self._write_paced(self.wfile, line)
                    self.wfile.flush()  # SSE 实时性关键
            except (BrokenPipeError, ConnectionResetError, OSError):
                # issue #63: 浏览器(下游)断了 ≠ 回合该死。异常必须在本函数内
                # 吞掉(不再穿到 do_GET/do_POST 外层走 _send(500) 死 socket),
                # 转排空模式继续读尽 gateway 流到 EOF/上限, gateway 全程不见
                # 断连 → 不再触发 api_server.py 的 SSE-disconnect interrupt,
                # 回合跑完并正常持久化到会话库 (PLAN-63 §2.2/§5 步骤1)。
                # 日志带 gateway url path 摘要 = A 型断连的 console 侧指纹
                # (PLAN-63 §1.4/§5 步骤3; 只打 path, 不打任何请求头)。
                print("[proxy] downstream gone; draining upstream to completion "
                      f"({self.command} {self.path.split('?')[0]} "
                      f"-> gateway {urlparse(url).path})")
                # 下游已死: 排空后直接断本侧连接, 不再等下一个 keep-alive 请求。
                self.close_connection = True
                if self.DRAIN_MAX_S > 0:
                    deadline = time.monotonic() + self.DRAIN_MAX_S
                    with suppress(Exception):
                        try:
                            for _line in resp:
                                if time.monotonic() > deadline:
                                    # 排空超时 = 上游迟迟不终态, 疑似 B 型断连或
                                    # gateway 异常; 弃排空(回退旧行为, 交 sweep 兜底)。
                                    print("[proxy] drain timeout, abandoning "
                                          f"({self.command} {self.path.split('?')[0]} "
                                          f"-> gateway {urlparse(url).path})")
                                    break
                        except Exception as e:
                            # issue #72: B 型断连 (上游 gateway 侧断) 的 console 侧指纹。
                            # 排空期读上游抛错 = 上游没了; 此前此处被 suppress(Exception)
                            # 静默吞掉, 现场既无 drain timeout 也无任何指纹 → 会被误判
                            # 成 A 型 (下游写失败)。此处补一条独立日志, A/B 靠该行有无
                            # 一眼分型; A 型 (排空读到 EOF) 不打这行。
                            # 只打 method + console path + gateway path + 异常类型名,
                            # 不打请求头/凭据/查询串 (与 A 型日志同一口径)。
                            print("[proxy] upstream gone during drain "
                                  f"({self.command} {self.path.split('?')[0]} "
                                  f"-> gateway {urlparse(url).path}): "
                                  f"{type(e).__name__}")

    def _proxy_to_gateway(self, p: str):
        """把 /api/sessions* 等请求透传到同容器 gateway(:8642)。

        同源代理: 浏览器只见 console 一个源, 彻底绕开 gateway SSE 响应
        缺 CORS 头的问题(api_server.py StreamResponse.prepare 早于
        cors_middleware 加头)。密钥由本进程环境变量注入, 不下发浏览器。
        """
        base = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
        return self._proxy_stream(base + self.path,
                                 os.environ.get("API_SERVER_KEY", ""))

    def _proxy_to_worker(self, p: str):
        """把 /api/w/<worker>/<rest> 透传到该 worker 的 gateway。

        与 main 同源代理同理: 浏览器只见 console 一个源(绕开 worker
        gateway 的 SSE CORS 缺陷), 且 worker 的 api_key 由服务端从
        注册表读取后注入, 同样不下发浏览器。
        """
        rest = p[len("/api/w/"):]
        name, _, tail = rest.partition("/")
        # BaseHTTPRequestHandler 不解码百分号,中文名会被原样送到这里。
        # 不解码就查不到注册表项,前端走英文名时本句是 no-op。
        name = unquote(name)
        try:
            entry = _worker_entry(name)
        except ValueError as e:
            return self._send(404, {"error": str(e)})
        base = entry["api_url"].rstrip("/")
        return self._proxy_stream(f"{base}/{tail}", entry.get("api_key") or "")

    def _gateway_base_key_for(self, p: str):
        """issue #75: 解析请求属于 main gateway 还是 worker gateway。

        返回 (base, key, tail_path) —— tail_path 是 gateway 侧路径
        (/api/sessions/... 形态)。worker 形态 (/api/w/<name>/...) 同样
        支持窗口化 (§9 R3: B1 单测覆盖 main + worker base 两形态)。
        """
        if p.startswith("/api/w/"):
            rest = p[len("/api/w/"):]
            name, _, tail = rest.partition("/")
            name = unquote(name)
            entry = _worker_entry(name)
            return (entry["api_url"].rstrip("/"),
                    entry.get("api_key") or "", "/" + tail)
        return (os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642"),
                os.environ.get("API_SERVER_KEY", ""), p)

    def _gw_fetch_json(self, base: str, key: str, path: str):
        """issue #75: 代理层自 gateway 拉 JSON (非流式, 超时 30s)。

        返回 (data, None) 或 (None, (http_status, err_body_dict))。
        网络不可达 → (None, (502, {...})) 与 _proxy_stream 归因口径一致。
        """
        req = urllib.request.Request(base + path, method="GET")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {"error": f"gateway http {e.code}"}
            return None, (e.code, body)
        except Exception as e:
            return None, (502, {"error": f"gateway unreachable: {e}"})

    def _gateway_messages_windowed(self, p: str, qs: dict) -> bool:
        """issue #75 B1: /messages 带窗口参数时走过滤代理 (§5.2)。

        返回 True = 本函数已应答; False = 无窗口参数, 交回旧透传路径
        (§9 回退方案的前提: 参数可选, 缺参数即旧行为)。
        支持 main (/api/sessions/<sid>/messages) 与 worker
        (/api/w/<name>/api/sessions/<sid>/messages) 两形态。
        """
        m = re.search(r"api/sessions/([^/]+)/messages$", p)
        if not m:
            return False
        if not (qs.get("want") or qs.get("before")):
            return False  # 无窗口参数 → 旧全量透传 (向后兼容红线)
        sid = m.group(1)
        try:
            base, key, tail = self._gateway_base_key_for(p)
        except ValueError as e:
            self._send(404, {"error": str(e)})
            return True
        # issue #81-3: want 前置解析, 非法值与 before 同口径回 400。
        # 原实现把 int() 放在 build_messages_window 调用参数里, ValueError
        # 逃出本函数 → do_GET 兜底 500 + "invalid literal for int()"。
        # 这里在 try 之前解析, 失败即 400 (文案沿用 before 的既有形态)。
        raw_want = (qs.get("want") or [str(WANT_DEFAULT)])[0]
        try:
            want_val = int(raw_want)
        except (TypeError, ValueError):
            self._send(400, {"error": f"非法 want 参数: {raw_want!r}"})
            return True
        # 窗口参数只作用于本代理层; 转发给 gateway 时重新拼 query,
        # 不透传 want/before (gateway 反正忽略, 拼干净避免歧义)。
        def fetch_raw(offset: int, limit: int):
            d, err = self._gw_fetch_json(
                base, key,
                f"{tail}?limit={limit}&offset={offset}&order=oldest")
            if err:
                # issue #81-4: 直接携带 (code, body) 元组, 不再拼字符串后
                # partition + json.loads 重解析 (body >200B 会被截断炸成
                # JSONDecodeError → 状态码与上游错误串双丢)。
                raise _GwError(err[0], err[1])
            data = d.get("data") if isinstance(d, dict) else d
            return data if isinstance(data, list) else []

        def get_message_count():
            """会话原始记录总数 (尾部锚定的唯一依据)。

            issue #75 s6 缺陷 1 (P0): gateway GET /api/sessions/<sid> 的
            真实返回形状是 {"object": ..., "session": {..., "message_count": N}}
            —— message_count 嵌在 session 里, 顶层没有。原实现只从顶层读,
            恒得 None ⇒ 窗口化端点恒 502 (无参调用走旧透传路径, 掩盖了此 bug)。

            两种形态都兼容 (嵌套优先, 顶层兜底): 取到任一合法非负 int 即返回;
            都取不到才返回 None (维持 502 显式报错语义, 不猜 offset=0)。

            issue #80-3: gateway 传输层错误 (不可达/认证失败/5xx) 原来被
            吞成 None, 于是首批恒报 "session message_count unavailable",
            掩盖真实原因。改为抛出 _GwError, 由路由层原样透传上游 code
            与 body; 只有 gateway 可达却真的缺 message_count 时才返回 None
            (该形态仍是 502 显式报错, 语义不变)。
            """
            d, err = self._gw_fetch_json(
                base, key, f"/api/sessions/{quote(sid, safe='')}")
            if err:
                raise _GwError(err[0], err[1])
            if not isinstance(d, dict):
                return None
            # 嵌套形态 (真实形状) 优先; 顶层形态兜底 (旧版/扁平响应)。
            for cand in (d.get("session"), d):
                if not isinstance(cand, dict):
                    continue
                mc = cand.get("message_count")
                # bool 是 int 的子类, 显式排除 (True 不可能是记录数)。
                if isinstance(mc, int) and not isinstance(mc, bool) and mc >= 0:
                    return mc
            return None

        try:
            out = build_messages_window(
                fetch_raw, get_message_count,
                want=want_val,
                before=(qs.get("before") or [None])[0])
        except _GwError as e:
            # issue #81-4: 元组直传, 上游 code 与 body 原样透传。
            self._send(e.code, e.body)
            return True
        if out.get("code"):
            self._send(out["code"], {"error": out.get("error")})
            return True
        self._send(200, out)
        return True

    def do_GET(self):
        p = self.path.split("?")[0]
        try:
            if p == "/" or p == "/index.html":
                # 优先 serve React 构建产物(ui/dist/index.html, 单文件内联),
                # 不存在则回退旧版 console_ui.py(保证任何时刻控制台可用)
                # 优先级: /share_workspace/ui_dist (worker 夜间构建, 热更新)
                #       → mw 包内 ui_dist → 旧版 console_ui.py (兜底)
                # 2026-10-10 反例诊断: /share_workspace/ui_dist/ 任何时候都不该覆盖
                #   mw 包内 (bind-mount host git pull 的 UI)。 该路径用于 worker
                #   夜间构建 (发布到 share_workspace 广播给所有 worker), 但一旦
                #   那份是陈的, install.sh 后 host 拉新 + 容器看 baked-in, console
                #   仍 serve 陈的 share_workspace — 用户报 "UI 没更新"。 修: 如果
                #   /share_workspace 与 mw 包内 stamp 不一致, 跳过 share_workspace
                #   链 (mw 包内是 source of truth)。 这保证 install.sh git pull 始终
                #   反映到 console 反响。
                candidates = [
                    os.environ.get("MW_CONSOLE_HTML", ""),
                    "/share_workspace/ui_dist/index.html",
                    os.path.join(os.path.dirname(__file__), "ui_dist", "index.html"),
                ]
                # 2026-10-10: 加 ETag 头让浏览器基于 stamp 做条件 GET,
                # 服务端发现 stamp 未变即可跳过 470KB 响应体直返 304,
                # 这解决 install.sh 后"前端没更新"误判: 浏览器磁盘缓存
                # 命中旧 index.html 后, console.py 此时用 304 否决决策才是
                # 真正的 "前端升级了". 没 ETag 时浏览器用 if-none-match 不带值,
                # 直接 cache stale 用旧版, 用户体感就是 "UI 没换"。
                stamp = _ui_stamp()
                etag = f'W/"{stamp.get("build_md5", "none")}"'
                # 2026-10-10 bug fix: /share_workspace/ui_dist/ 是 worker 夜间构建产物,
                # 优先级高于 mw 包内, 但 stamp 不一致就跳过 — 否则 host git pull 新版
                # 被老 share_workspace 覆盖, install.sh 看似成功 console 仍 serve 老 dist.
                # 验证每个候选文件的 md5 是否与 stamp 一致, 不一致的被剔除。
                # issue #62 (两处修):
                #   1) 旧 _passes 整读算 md5, 命中后又二次 open 读 body ——
                #      两次读之间 install.sh/夜间构建可改写文件 (TOCTOU),
                #      导致 Content-Length (新尺寸) 与响应体 (旧尺寸) 错位。
                #      改单次读入同一 bytes 同时用于 md5 校验与 body。
                #   2) wfile.write 抛 BrokenPipeError/ConnectionResetError
                #      (客户端提前断开) 会冒泡到 do_GET 兜底 except 再叠发
                #      一个 500 —— 半截 200 响应体与已声明的 Content-Length
                #      不符, macOS 浏览器报 ERR_CONTENT_LENGTH_MISMATCH。
                #      写异常就地吞掉并置 close_connection 断连。
                def _read_if_pass(c: str) -> bytes | None:
                    """单次读: 同一 bytes 既算 md5 校验也作响应 body。

                    None = 不存在 / 读失败 / md5 与 stamp 不符 (剔除该候选)。
                    """
                    if not c or not os.path.exists(c):
                        return None
                    try:
                        with open(c, "rb") as f:
                            data = f.read()
                    except OSError:
                        return None
                    import hashlib
                    if hashlib.md5(data).hexdigest() != stamp.get("build_md5", ""):
                        return None
                    return data
                body = next((b for b in (_read_if_pass(c) for c in candidates)
                             if b is not None), None)
                if body is not None:
                    not_modified = self.headers.get("If-None-Match") == etag
                    self.send_response(304 if not_modified else 200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    # 304 按 RFC 不带 body, Content-Length 声明 0; body 已
                    # 在单次读中固定, 200 分支写出的字节数与之一字不差。
                    self.send_header("Content-Length", str(0 if not_modified else len(body)))
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control",
                                     "no-cache" if not_modified else "no-store")
                    self.end_headers()
                    if not not_modified:
                        try:
                            # issue #62/64 A-2 → P-2: Apple container 端口代理截断适配
                            self._write_paced(self.wfile, body)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            # 客户端提前断开: 已写出的字节 < Content-Length,
                            # 不能再走外层 500 兜底 (会往同一连接叠半截响应),
                            # 直接断连让浏览器按连接中断处理。
                            self.close_connection = True
                    return
                from .console_ui import HTML_TEMPLATE, _LANG_TABLES
                # i18n (架构 §5.3 item 4): 兜底页按部署语言整页渲染。哨兵
                # ⟪KEY⟫ = key 点换下划线大写; zh-cn 值=基线逐字 (零回归锚)。
                _tb = _LANG_TABLES.get(_i18n_resolve_lang(), _LANG_TABLES["en"])
                _fb_body = HTML_TEMPLATE
                for _sk, _sv in _tb.items():
                    _fb_body = _fb_body.replace(
                        "\u27ea" + _sk.replace(".", "_").upper() + "\u27eb", _sv)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_fb_body.encode())))
                self.send_header("ETag", "fallback/console_ui")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                # issue #62: 兜底页同款防护 —— 客户端提前断开时不再冒泡
                # 到外层 500 兜底叠发半截响应, 直接断连。
                try:
                    # issue #62/64 A-2 → P-2: 兜底页同款代理截断适配
                    self._write_paced(self.wfile, _fb_body.encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True
                return
            if p == "/api/tasks":
                # issue #75 s6 缺陷 2: ?fields=active → 只回 team_work_active
                # 段 (前端 5s 轮询用; 不带该参数时行为与此前完全一致)。
                _tq = parse_qs(urlparse(self.path).query,
                               keep_blank_values=True)
                return self._send(
                    200, api_tasks((_tq.get("fields") or [""])[0]))
            # issue #75 R1: 单任务全字段详情 (列表投影瘦身后的补全通道)。
            # 必须精确正则, 不与 /api/tasks 列表冲突。
            m = re.match(r"^/api/tasks/([A-Za-z0-9_.-]+)$", p)
            if m:
                d = api_task_detail(m.group(1))
                if d.get("code") == 404:
                    return self._send(404, {"error": d.get("error")})
                return self._send(200, d)
            # issue #75 B2: 顶栏心跳 (OQ-3 正解, 数百字节, 纯本地读)
            if p == "/api/heartbeat":
                return self._send(200, api_heartbeat())
            if p == "/api/workers":
                return self._send(200, api_workers())
            # SSE-FIX-F2: 轻量活体端点, 必须先于 /api/workers/ 前缀匹配,
            # 否则 "live" 会被当成 worker 名误路由到 api_worker_detail。
            if p == "/api/workers/live":
                return self._send(200, api_workers_live())
            # Issue #51 (2026-10-17): GET /api/workers/<name>/avatar 必须
            # 先于 /api/workers/ 前缀兜底, 否则 "<name>/avatar" 会被当成
            # worker 名 "<name>/avatar" 误路由到 api_worker_detail (与
            # /api/workers/live 同纪律)。文件名解析走 registry 精确查表,
            # 不接受请求路径里的额外文件名片段。
            m = re.match(r"^/api/workers/([^/]+)/avatar$", p)
            if m:
                return _send_avatar_file(self, m.group(1))
            if p == "/api/workflows":
                return self._send(200, api_workflows())
            # issue #70: 决策路径只读端点 (新增路由, 不改任何既有路由行为)
            if p == "/api/decision-paths":
                return self._send(200, api_decision_paths())
            # issue #95: 判定入口 (s5 §2.2 建议 a) —— 必须先于前缀兜底。
            # 精确匹配, 与 /api/decision-paths 词表端点互不冲突。
            if p == "/api/decision-paths/classify":
                _cq = parse_qs(urlparse(self.path).query,
                               keep_blank_values=True)
                _code, _body = api_decision_paths_classify(_cq)
                return self._send(_code, _body)
            if p == "/api/soul-template":
                return self._send(200, api_soul_template())
            if p == "/api/workspaces":
                return self._send(200, api_workspaces())
            # Issue #46 / docs/ARCH-FILEBROWSER.md §3.5: fs 只读路由
            # (POST/PUT/DELETE 不注册 → 落入既有 404 等效拒绝; AC-F4)
            if p.startswith("/api/fs/"):
                from .console_files import handle_fs_get, FsPathError
                qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                try:
                    return handle_fs_get(self, p, qs)
                except FsPathError as e:
                    return self._send(e.code, {"error": e.msg})
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception:
                    return self._send(500, {"error": "internal error"})
            if p == "/api/settings":
                return self._send(200, api_settings_get())
            if p == "/api/gwconfig":
                return self._send(200, api_gwconfig(
                    self.headers.get("Host", "")))
            # i18n v1.0 (架构 §2 / design.md §10): 部署语言只读端点
            # /api/lang → {"lang":"<en|zh-cn>"}。无鉴权,Cache-Control: no-store,
            # 永远不 404。值来自 mw.i18n.resolve_lang()(MW_LANG env,缺省/非法=en)。
            if p == "/api/lang":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # AC A4 + api-spec §2.1: 新增端点是加法,不破坏既有契约
                self.send_header("Cache-Control", "no-store")
                from .i18n import resolve_lang as _api_lang_resolve
                lang_val = _api_lang_resolve()
                self.end_headers()
                self.wfile.write(json.dumps({"lang": lang_val}).encode("utf-8"))
                return
            # issue #75 B1: /messages 带窗口参数(want/before) 时走过滤代理;
            # 不带参数 → 返回 False 落入下方旧透传分支 (§9 回退依赖此)。
            # 必须先于 /api/w/ 与 /api/sessions 两个透传分支。
            if (p.endswith("/messages") and "/api/sessions/" in p):
                _qs = parse_qs(urlparse(self.path).query,
                               keep_blank_values=True)
                if self._gateway_messages_windowed(p, _qs):
                    return
            if p.startswith("/api/w/"):
                return self._proxy_to_worker(p)
            if p.startswith("/api/sessions") or p.startswith("/v1/runs"):
                return self._proxy_to_gateway(p)
            if p.startswith("/api/workers/"):
                return self._send(200, api_worker_detail(
                    p[len("/api/workers/"):]))
            if p == "/health":
                return self._send(200, {"status": "ok"})
            # 像素办公室状态: 前端据此决定是否显示 tab + iframe 是否可用。
            if p == "/api/pixel/status":
                return self._send(200, {
                    "enabled": pixel_enabled(),
                    "running": pixel_proxy.pixel_office_running(),
                })
            # 像素办公室同源代理: /pixel/* → pixel-agents(保留 Host 头)。
            # 浏览器只见控制台一个源; pixel-agents 只绑容器内 127.0.0.1。
            if p == "/pixel" or p.startswith("/pixel/"):
                if not pixel_enabled():
                    return self._send(404, {"error": "pixel office disabled"})
                return pixel_proxy.proxy_http(
                    self, self.path[len("/pixel"):])
            # pixel-agents SPA 的 WS 硬编码连 location.host/ws(根路径,
            # 不在 /pixel 前缀下), 控制台自身不用 /ws, 直接让给它做隧道。
            if p == "/ws":
                if not pixel_enabled():
                    return self._send(404, {"error": "pixel office disabled"})
                return pixel_proxy.tunnel_ws(self)
            # 问题7D: SSE 事件流
            if p == "/api/events":
                qs = parse_qs(urlparse(self.path).query)
                since = int(qs.get("after", ["0"])[0])
                return self._stream_events(since)
            # 问题7D: webhook 列表
            if p == "/api/webhooks":
                return self._send(200, api_webhook_list())
            # 2026-10-03 T2: team work 列表 (UI TeamWorksView 拉列表用)
            if p == "/api/team-works":
                qs = parse_qs(urlparse(self.path).query)
                incl = qs.get("include_closed", ["false"])[0].lower() in ("true", "1")
                incl_arch = qs.get("include_archived", ["false"])[0].lower() in ("true", "1")
                return self._send(200, api_team_works_list({
                    "include_closed": incl,
                    "include_archived": incl_arch,
                }))
            # 2026-10-03 T2: 单个 team work 详情 (只读 GET, 无 body)
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)$", p)
            if m:
                return self._send(200, api_team_work_get(m.group(1)))
            # 2026-10-11: 只读 TODO.md 视图 (UI 看板『查看任务计划』)。
            # 必须在通用 `<run_id>` 之前匹配, 否则被吃掉。
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)/todo$", p)
            if m:
                return self._send(200, api_team_work_todo(m.group(1)))
            # F5 (2026-10-13, issue #5 状态机统一): 单一派生端点 ——
            # 返回 run + 每张卡的 derived state。三处 UI 可专用此端点
            # 减少载荷 (不必拉全 run 详情), 也可与 api_team_work_get
            # 内的 `state` 字段共用同一组派生函数。 在 `/todo` 之后,
            # `/launch`/`/advance` 等 POST 路径之前, 不冲突。
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)/state$", p)
            if m:
                return self._send(200, api_team_work_state(m.group(1)))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            # ★ 代理路由必须先于 body 预读: _proxy_to_gateway 自己读 rfile,
            #   若此处先消费掉请求体, 代理二次 read 会永久阻塞。
            if p.startswith("/api/w/"):
                return self._proxy_to_worker(p)
            if p.startswith("/api/sessions") or p.startswith("/v1/runs"):
                return self._proxy_to_gateway(p)
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            if p == "/api/resume":
                # issue #104: 答复来源 (operator = 控制台人工答复; main 走
                # CLI 路径不入这个端点)。终态卡返回 409, 路由层透传。
                # issue #115: actor **不再取自请求体** —— 客户端自称的 actor
                # 原样进账本三处审计面, 操作员可把答复伪造成 main, 且
                # 「两条答复同 actor ⇒ 冲突提示互不可见」。本端点是操作员通道,
                # 生效 actor 由服务端钉死 "operator"; 自称值只作审计留痕
                # (actor_claimed), 不参与归属判定, 也不参与冲突比较。
                _res = api_resume(payload.get("task_id", ""),
                                  payload.get("instruction", ""),
                                  actor="operator",
                                  actor_claimed=payload.get("actor"))
                _code = _res.pop("code", 200)
                return self._send(_code, _res)
            if p == "/api/compose/generate":
                return self._send(200, api_compose_generate(payload))
            if p == "/api/upload":
                return self._send(200, api_upload(payload))
            if p == "/api/team-work/launch":
                # 错误码按 payload 里 code 字段透传,不是 HTTP 状态码
                return self._send(200, api_team_work_launch(payload))
            # v9 (2026-10-07): SSE bridge subscribe 端点 — 通知 console 进程
            # 启 SSE daemon thread 订阅 worker gateway 的 /v1/runs/{run_id}/events.
            # caller 是 mw.cli __spawn-bg 子进程 (Popen 后立即退出), console
            # 进程持有 daemon thread, 与子进程生命周期解耦.
            if p == "/api/workers/subscribe_sse":
                return self._send(200, api_workers_subscribe_sse(payload))
            # 2026-10-03 T2: /advance /close 是 POST (有 body), regex
            # 子路径必须在 /api/team-work/launch 之后, 同时子路径
            # 必须先于通用路径匹配 — 否则会被 launch 或 regex 吃错。
            # /api/team-work/<run_id>/advance 优于 /api/team-work/<run_id>
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)/advance$", p)
            if m:
                return self._send(200, api_team_work_advance(
                    m.group(1), payload))
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)/close$", p)
            if m:
                return self._send(200, api_team_work_close(
                    m.group(1), payload))
            # 2026-10-06 新增:归档非 ACTIVE run, 列表收缩用
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)/archive$", p)
            if m:
                return self._send(200, api_team_work_archive(
                    m.group(1), payload))
            # 2026-10-04 补根: run 中途追加卡 (回流场景: 审查打回建修复卡)。
            # 只登记不派发, 派活仍走 /advance。
            m = re.match(r"^/api/team-work/([A-Za-z0-9_-]+)/cards$", p)
            if m:
                return self._send(200, api_team_work_add_cards(
                    m.group(1), payload))
            if p == "/api/dispatch":
                # 1A: 为一张 main 已 claim 的卡起 worker (mw 唯一派活入口)
                return self._send(200, api_dispatch(payload))

            if p == "/api/task/cancel":
                return self._send(200, api_task_cancel(payload.get("task_id", "")))
            if p == "/api/task/archive":
                return self._send(200, api_task_archive(payload.get("task_id", "")))
            # M5 (BATCH1-DESIGN): main 纠正误报/错误终结端点
            m = re.match(r"^/api/tasks/([A-Za-z0-9_-]+)/state-correct$", p)
            if m:
                res = api_task_state_correct(m.group(1), payload)
                code = res.pop("code", 200)
                return self._send(code, res)
            if p == "/api/team-work/rotate":
                return self._send(410, {"error": "api_team_work_rotate 已删除 (2026-10-02 回退); "
                                       "TODO.md 推进由 main agent 直接编辑"})

            # 2026-10-02: reaper 远程触发端点。当 ssh 不通时 (e.g. 防火墙 / 鉴权失败),
            # 提供手动驱动 reaper 立即结算的口子, 让运维可从 web 控制台触发 reap_once,
            # 不必登录 host 跑 pkill + 等 watchdog。专门解决"reaper 看起来没跑"的诊断
            # 场景: 先 call /api/reaper/status 看进程/最后 reap 时间, 决定要不要 trigger。
            if p == "/api/reaper/status":
                return self._send(200, _api_reaper_status())
            if p == "/api/reaper/reap-once":
                return self._send(200, _api_reaper_reap_once(payload))
            # 2026-10-02: 深度诊断 — 显示每个 stuck 卡的 TODO.md 扫描真实状态
            if p == "/api/reaper/debug-todo-scan":
                return self._send(200, _api_reaper_debug_todo_scan(payload))
            if p == "/api/workspaces":
                return self._send(201, api_workspace_create(
                    payload.get("name", "")))
            # 问题7D: webhook 注册
            if p == "/api/webhooks":
                return self._send(201, api_webhook_register(payload))
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(422, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_PUT(self):
        p = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            if p == "/api/workers":
                return self._send(200, api_registry_save(payload))
            if p == "/api/settings":
                return self._send(200, api_settings_save(payload))
            if p.startswith("/api/workflows/"):
                return self._send(200, api_workflow_save(
                    p[len("/api/workflows/"):], payload))
            if p.startswith("/api/workers/") and p.endswith("/soul"):
                return self._send(200, api_worker_soul_init(
                    p[len("/api/workers/"):-len("/soul")], payload))
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(422, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_DELETE(self):
        p = self.path.split("?")[0]
        try:
            if p.startswith("/api/w/"):
                return self._proxy_to_worker(p)
            if p.startswith("/api/workers/"):
                return self._send(200, api_registry_delete(
                    p[len("/api/workers/"):]))
            if p.startswith("/api/workflows/"):
                return self._send(200, api_workflow_delete(
                    p[len("/api/workflows/"):]))
            # 问题7D: webhook 删除
            if p.startswith("/api/webhooks/"):
                wid = p[len("/api/webhooks/"):]
                ok = delete_webhook_by_id(wid)
                return self._send(200 if ok else 404,
                                  {"ok": ok, "id": wid})
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(422, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def _stream_events(self, since_id: int):
        """问题7D: SSE 长连接。
        协议: text/event-stream, 每条 event 是 JSON 行。
        客户端用 EventSource, 自动重连带 last-event-id 重放。"""
        # 头: SSE 标准
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # 防 nginx 缓冲
        self.end_headers()
        cur = int(since_id)
        try:
            while True:
                # issue #61 顺带审: SSE 循环每 2s 新建 Ledger, 旧写法从不
                # close —— 长连接每 2s 泄漏一个 sqlite fd, 是最热的泄漏点。
                ledger = Ledger(LEDGER_DB)
                try:
                    rows = ledger.list_events_since(cur, limit=50)
                finally:
                    ledger.close()
                if rows:
                    for r in rows:
                        ev_id = r["id"]
                        cur = ev_id
                        data = {
                            "id": ev_id,
                            "event_type": r["event_type"],
                            "task_id": r["task_id"] or "",
                            "payload": json.loads(r["payload"] or "{}"),
                            "created_at": r["created_at"],
                        }
                        # SSE 格式: id: / event: / data:
                        msg = f"id: {ev_id}\nevent: {r['event_type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                        try:
                            # issue #62 A-3: SSE 帧虽小, 统一纪律成本为零
                            self._write_all(self.wfile, msg.encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                else:
                    # 心跳保活, 防中间设备超时
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                time.sleep(2)
        except Exception as e:
            try:
                self.wfile.write(f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n".encode())
            except Exception:
                pass

    def log_message(self, fmt, *args):  # 静音访问日志
        pass


# 2026-10-02: reaper 远程驱动 (ssh 不通场景的诊断入口)
def _api_reaper_status() -> dict:
    """看 reaper 进程 / 最后 reap 时间, 不用 ssh 也能判断它跑没跑。"""
    import os as _os, subprocess as _sp
    try:
        # 找同进程组的后台 reaper (entry.sh 启动的)
        out = _sp.run(["pgrep", "-af", "mw.cli reaper"],
                      capture_output=True, text=True, timeout=5).stdout
    except Exception as _e:
        out = f"(pgrep failed: {_e})"
    # marker 状态: 每个 task 卡的 spawn marker 文件 mtime
    spawn_dir = _os.environ.get("MW_HOME", "/mw_data") + "/spawn"
    marker_files = []
    try:
        import glob as _g
        for p in _g.glob(_os.path.join(spawn_dir, "t_*.json"))[:10]:
            st_obj = _os.stat(p)
            marker_files.append({
                "file": _os.path.basename(p),
                "mtime": int(st_obj.st_mtime),
                "age_s": int(_os.environ.get("_NOW_TS", "0")) - int(st_obj.st_mtime) if _os.environ.get("_NOW_TS") else None,
            })
    except Exception as _e:
        marker_files = [{"error": str(_e)}]
    return {
        "reaper_processes": out.strip().splitlines() if out.strip() else [],
        "spawn_dir": spawn_dir,
        "recent_spawn_markers": marker_files,
        "note": _t("console.note.reaper_offline_hint"),
    }


def _api_reaper_reap_once(payload: dict) -> dict:
    """立即调 reap_once() 跑一轮。 不阻塞 console 主线程太久 (最多 30s)。"""
    import os as _os, subprocess as _sp
    # 走 mw.cli reaper --once (跟 systemd 启的 reaper 用同样的参数初始化 ledger/registry)
    # 这样不用 console 重复构造 ledger, 也避免 import 时机不对导致连接失败
    try:
        py = _os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")
        proc = _sp.run([py, "-m", "mw.cli", "reaper", "--once"],
                       capture_output=True, text=True, timeout=60,
                       cwd="/opt/mwroot")
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-2000:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
            "note": _t("console.note.reaper_reap_done"),
        }
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "reap_once 60s 内没结束"}
    except Exception as _e:
        return {"ok": False, "error": str(_e)}


def _api_reaper_debug_todo_scan(payload: dict) -> dict:
    """2026-10-02: 深度调试 TODO.md '结果' 扫描。 不走 harvest, 只显示每个
    stuck 卡的 ledger 状态 + TODO.md 解析结果 + 是否满足触发条件。

    专门诊断 reap-once 返 [] 但卡还 stuck 的情况: 看每张卡是哪一步
    把 _todo_result_scan 拦住的。
    """
    import os as _os, subprocess as _sp
    debug_code = """
import json, os
from mw.ledger import Ledger
from mw.todo import parse as _todo_parse

led = Ledger(os.environ.get("MW_LEDGER_DB", "/mw_data/ledger.db"))
rows = led.pending_or_errored() if hasattr(led, "pending_or_errored") else led.pending()
out = {"pending_count": len(rows), "details": []}
for r in rows:
    tid = r["task_id"]
    pw = led.get_product_workspace(tid)
    entry = {"task_id": tid, "harv": r["harvest_status"], "product_workspace": pw}
    if not pw:
        entry["skip"] = "no product_workspace"
        out["details"].append(entry); continue
    tp = os.path.join(pw, "TODO.md")
    entry["todo_path"] = tp
    entry["exists"] = os.path.exists(tp)
    if not entry["exists"]:
        out["details"].append(entry); continue
    try:
        doc = _todo_parse(tp)
    except Exception as e:
        entry["parse_error"] = str(e)[:200]
        out["details"].append(entry); continue
    if doc is None:
        entry["skip"] = "todo parse None"; out["details"].append(entry); continue
    if doc.current is None:
        entry["skip"] = "no current task"; out["details"].append(entry); continue
    c = doc.current
    entry["current_step"] = c.step_id
    entry["kanban_task_id"] = c.kanban_task_id
    entry["has_result"] = c.has_result
    entry["result_preview"] = (c.result or "")[:100]
    entry["would_trigger"] = bool(c.kanban_task_id and c.kanban_task_id == tid and c.has_result)
    out["details"].append(entry)
print(json.dumps(out, ensure_ascii=False, indent=2))
"""
    try:
        py = _os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")
        proc = _sp.run([py, "-c", debug_code],
                       capture_output=True, text=True, timeout=20,
                       cwd="/opt/mwroot")
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr[-1000:] if proc.stderr else "",
            "note": _t("console.note.reaper_diag_intro"),
        }
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "debug 20s 内没结束"}
    except Exception as _e:
        return {"ok": False, "error": str(_e)}


def main():
    # Issue #51 (2026-10-17): 启动期一次性把 registry 里残留的 base64 avatar
    # 迁到 avatars/ 文件 + 替换为裸文件名。失败 fail-open, 不阻塞服务起服。
    try:
        stats = migrate_avatars()
        if stats.get("scanned", 0) > 0 or stats.get("written"):
            print(
                "[console] avatar migration: "
                f"scanned={stats.get('scanned', 0)} "
                f"migrated={stats.get('migrated', 0)} "
                f"failed_clear={stats.get('failed_clear', 0)} "
                f"written={stats.get('written', False)}")
    except Exception as e:
        print(f"[console] avatar migration skipped: {e}")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[console] listening on :{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
