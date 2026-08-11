# NAS Bridge

AI 漫剧项目在多个 NAS 组内与制作部之间的素材同步、成片回传和一键交付管理平台。

```
┌──────────────┐    O:/NAS-后期     ┌──────────────┐
│  组内 NAS    │  ──────────────▶  │ 制作部 NAS   │
│  (剪辑一组)   │                   │ (N:/多部)    │
└──────────────┘                   └──────────────┘
        ▲                                ▲
        │                                │
        └────────── NAS Bridge ◀─────────┘
               Flask + SQLite + Watchdog
```

## 核心功能

| 功能 | 说明 |
|---|---|
| 项目扫描 | 递归扫描组内 / 制作部 NAS，自动识别所有项目目录 |
| 一键同步 | 组内 NAS → 制作部 NAS，使用 `robocopy /MT:8` 多线程 |
| 成片监听 | Watchdog 后台监听 `01上映单集版`，文件稳定后自动回传 |
| 一键交付 | `000交付` 文件夹整体推送到制作部 NAS，带实时进度条 |
| 版本交付 | 支持交付多版本文件夹 / 单文件 |
| 状态管理 | 项目生命周期：待处理 → 待同步 → 待交付 → 已完成 |
| 集数自动计数 | 扫描成片目录自动统计已输出集数 |

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10+ / Flask / Waitress (WSGI) |
| 数据库 | SQLite (WAL 模式) |
| 文件操作 | robocopy / cmd (UNC 路径绕过权限隔离) |
| 文件监听 | Watchdog |
| 前端 | 原生 HTML + JS (单页应用) |
| 桌面端 | pywebview 包装 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 config.yaml

关键项（根据实际环境修改）：

```yaml
nas:
  group_root: O:\AI漫剧剪辑一组                        # 组内 NAS 根目录
  production_roots:                                    # 制作部 NAS 根目录（可多个）
    - N:\AI漫剧二部中转
    - N:\AI漫剧一部中转\AI漫剧一部海外
  unc_map:                                             # UNC 映射（盘符 → 网络共享）
    'N:': \\192.168.8.234\ai漫剧中转盘
    'O:': \\192.168.8.93\ai漫剧后期
web:
  host: 0.0.0.0
  port: 8089
```

### 3. 启动服务

```bash
# 方式 A：直接启动（推荐）
python app.py

# 方式 B：桌面端（pywebview 包裹的原生窗口）
python desktop.py

# 方式 C：静默后台（.vbs）
start.vbs
```

访问 http://127.0.0.1:8089

### 4. 挂载 NAS 盘（首次）

```bash
net use N: \\192.168.8.234\ai漫剧中转盘 /persistent:yes
net use O: \\192.168.8.93\ai漫剧后期 /persistent:yes
```

## 目录结构

```
nas-bridge/
├── app.py              Flask 路由入口（30+ API）
├── sync_engine.py      核心同步/交付引擎（41 方法）
├── db.py               SQLite 封装（WAL 模式）
├── watcher.py          Watchdog 成片监听
├── desktop.py          pywebview 桌面端
├── config.yaml         配置文件
├── requirements.txt
├── start.bat / start.vbs
├── templates/
│   ├── index.html      单页前端
│   └── _check.js
└── nas_bridge.db       运行时自动生成
```

## API 速览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/projects` | 项目列表（按状态分桶） |
| POST | `/api/scan` | 重新扫描 NAS |
| POST | `/api/sync/<项目名>` | 组内 → 制作部 同步 |
| POST | `/api/deliver/<项目名>` | 一键交付（000交付 整体推送） |
| GET | `/api/project/<项目名>/deliver_status` | 交付进度（实时） |
| GET | `/api/project/<项目名>/delivery_stats` | 交付历史统计 |
| POST | `/api/project/<项目名>/custom_status` | 修改项目状态 |
| POST | `/api/project/<项目名>/episodes` | 设置集数 |
| GET | `/api/status` | 服务状态 |
| GET | `/api/logs` | 最近日志 |

## 设计要点

### robocopy + UNC 路径

Windows 服务进程在 SYSTEM 账户下无法访问用户级挂载盘（N:、O:）。所有跨 NAS 操作统一转成 UNC 路径后走 `cmd.exe` 执行，绕过权限隔离：

```
N:\项目\file.mp4  →  \\192.168.8.234\ai漫剧中转盘\项目\file.mp4
```

### 异步进度机制

一键交付不阻塞 API：

1. 启动：`deliver_to_production` 统计源文件数 → 启动 robocopy 线程 + 进度线程
2. 进度：独立后台线程每 2s `os.walk` 目标目录更新 `_deliver_tasks` 字典
3. 查询：`get_deliver_status` 纯内存读取（0.00s 响应），前端 1.5s 轮询

### 线程安全

三个共享状态字典统一用 `threading.RLock()` 保护：

- `_deliver_tasks` — 交付任务进度
- `_output_dir_cache` — 成片目录查找缓存
- DB 连接每次新建 + WAL 模式 + busy_timeout=5s

## 故障排查

| 现象 | 可能原因 | 检查 |
|---|---|---|
| 交付一直 0% | robocopy 网络阻塞 | 检查 NAS 连通性，`net use` 看盘是否挂上 |
| 交付 API timeout | `_deliver_tasks` 锁竞争 | 查 `nas_bridge.log` 有没有锁等待超时 |
| 项目列表为空 | 扫描时 NAS 未挂载 | 手动 `net use` 后点"重新扫描" |
| DB `database is locked` | 并发写 | 重启服务让 WAL 模式生效；busy_timeout 兜底 |

## 已知不足

1. **sync_engine.py 过大** — 1750 行、单类 41 方法，计划拆成 ScanEngine / DeliverEngine / StatusManager
2. **无健康检查端点** — 缺少 `/api/health`
3. **无优雅停机** — 正在跑的 robocopy 在 `taskkill` 时会中断
4. **前端单文件** — 97KB HTML+JS 全堆在一起，浏览器无法缓存 JS

## License

MIT
