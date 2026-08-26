# dsh-novel-ai

Novel AI 启动器插件 —— 在 DeepSeek Harness 中一键拉起 AI 长篇小说创作系统。

**模式**：独立窗口。插件负责启动 Python AI 引擎 + 前端静态服务，并在独立浏览器窗口打开原界面（不做 UI 改造，保留全部原有功能）。

## 安装

```bash
# 本地开发安装（link 方式，改代码即时生效）
dsh plugin --profile web add link:/home/host/deepseek_harmness/dsh-novel-ai

# 重启 dsh web 后生效
dsh web
```

## 卸载

```bash
dsh plugin --profile web rm dsh-novel-ai
```

## 启动后发生什么

| 组件 | 端口 | 说明 |
|------|------|------|
| Python AI 引擎 (FastAPI) | 8765 | 全部 REST + WebSocket 接口 |
| 前端静态服务 | 5173（占用时自动 +1） | 构建产物，SPA 回退 |
| 版本历史转发 | 8766 → 8765 | 兼容前端硬编码的版本历史页端口 |

启动完成后自动在独立窗口打开 `http://localhost:5173`。

## 控制端点（挂在 DSH web 服务上）

- `GET  /dsh-novel-ai/status`     — 运行状态 JSON
- `GET  /dsh-novel-ai/health`     — 汇总健康（服务 + 引擎进程详情 + watchdog）
- `GET  /dsh-novel-ai/logs`       — 最近日志（启动器 + 引擎，排障用）
- `POST /dsh-novel-ai/open`       — 重新打开独立窗口
- `POST /dsh-novel-ai/stop`       — 停止引擎与静态服务
- `POST /dsh-novel-ai/restart`    — 先停后启（幂等，重启后常驻）
- `POST /dsh-novel-ai/verify`     — 触发全接口自检（并发互斥），返回报告 JSON
- `POST /dsh-novel-ai/watchdog`   — `{"enabled": true|false}` 运行时开关自动恢复

## 健壮性设计（v0.3）

| 能力 | 说明 |
|------|------|
| **watchdog 自愈** | 引擎崩溃后自动重启（最多 3 次，指数退避 5s/15s/45s），可用 `NOVEL_AI_WATCHDOG=0` 或端点关闭 |
| **inProcess 安全** | 插件在 DSH 进程内运行时**不写 PID_FILE**，外部 `stop` 不会误杀 DSH 进程（v0.2 隐患修复） |
| **单实例锁** | 独立 CLI 重复启动被拒绝；`restart` 会等待旧实例完全退出再接管 |
| **防挂死** | Python 探测全异步；引擎秒退快速失败；开窗 5s 超时保护；HTTP 请求全部限时 |
| **--daemon 模式** | `start/restart --daemon` 自行后台化，命令替换不会死等，1s 内确认结果 |
| **日志轮转** | 日志超 5MB 自动备份为 `.1` |

## 独立运行（不经过 DSH）

```bash
node lib/launcher.js start --daemon    # 后台启动（推荐）
node lib/launcher.js start             # 前台启动（Ctrl+C 停止）
node lib/launcher.js stop              # 跨进程停止（委托信号，安全）
node lib/launcher.js status            # 状态查询（含引擎进程详情）
node lib/launcher.js restart --daemon  # 平滑重启
```

## 接口自检

```bash
node lib/verify.js             # 全量自检: 引擎 REST + WebSocket + 静态 + 转发
node lib/verify.js --quick     # 只测基础链路
node lib/verify.js --only rest # 跳过 WebSocket
```

判定规则：`PASS` 响应码符合预期 / `WARN` 接口可达但响应码非预期 / `FAIL` 连接失败或 5xx。
每次运行生成 `test-report.json`；退出码 0 = 全部通过，1 = 有 FAIL。

## 依赖安装

```bash
bash scripts/install-deps.sh   # 自动探测 Python 并安装引擎依赖
```

## 自救脚本（崩溃恢复）

任何原因导致服务中断（Agent 崩溃 / 会话清理 / 进程被杀）后，一键恢复现场：

```bash
bash scripts/self-heal.sh             # 检测并恢复 (幂等: 已在运行则复用)
bash scripts/self-heal.sh --force     # 强制干净重启 (先停后启)
bash scripts/self-heal.sh --status    # 只报告状态, 不动作
bash scripts/self-heal.sh --verify    # 恢复后自动跑全接口自检
```

自救流程：清理残留进程与陈旧 PID → 拉起引擎 + 静态服务 + 转发 + 开窗 →
健康确认 → 生成 `self-heal-report.json`。退出码 0 = 恢复成功。
服务由 `setsid` 放入独立会话，脱离脚本生命周期，可长期常驻。

## 数据位置

| 数据 | 路径 |
|------|------|
| 运行日志 | `$DSH_HOME/novel-ai/logs/`（未设置 DSH_HOME 时为 `~/.novel-ai/logs/`） |
| 进程 PID | `$DSH_HOME/novel-ai/{launcher,engine}.pid` |
| GBrain 小说库 | `~/.novel-ai/novels/`（引擎内硬编码，插件不干预） |

## 环境变量

- `NOVEL_AI_PYTHON` — 指定 Python 解释器（默认探测 miniconda → python3）

## 常见问题

- **引擎健康检查超时**：查看 `$DSH_HOME/novel-ai/logs/engine.log`，通常是 Python 依赖缺失（`bash scripts/install-deps.sh`）或端口被占用。
- **端口 5173 被占用**：自动改用 5174+，浏览器打开的 URL 以日志为准。
- **自动弹窗打扰**：当前版本随 DSH 启动即拉起并开窗；后续版本将增加"仅启动服务、按需开窗"配置项。

## 已知修复记录

- **路由 404 bug（重要）**：`engine/main.py` 的 `__main__` 入口块原本位于文件中部，
  `uvicorn.run()` 阻塞导致其后定义的版本历史 / settings 等路由永不注册（返回 404）。
  已将该块移至文件末尾，修复同步应用于插件运行时与源项目。
