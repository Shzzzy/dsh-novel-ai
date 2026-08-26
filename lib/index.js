// ═══════════════════════════════════════════════════════════════════════════
// dsh-novel-ai —— cordis 插件包装层
// ── 生命周期 ──
//   DSH web 启动 → apply() 自动拉起 Novel AI (引擎 + 静态服务 + 独立窗口)
//   DSH web 关闭 → ctx.effect 清理钩子停止所有子进程
// ── 控制端点 (挂在 DSH web 服务上) ──
//   GET /dsh-novel-ai/status   运行状态 JSON
//   POST /dsh-novel-ai/open    重新打开独立窗口
//   POST /dsh-novel-ai/stop    停止 Novel AI (引擎 + 服务)
// ═══════════════════════════════════════════════════════════════════════════
import { start, stop, status } from './launcher.js'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

const name = 'dsh-novel-ai'

const JSON_HEADERS = { 'Content-Type': 'application/json; charset=utf-8' }

// 日志目录 (与 launcher.js 的 DATA_ROOT 保持一致)
const DATA_ROOT = process.env.DSH_HOME
  ? path.join(process.env.DSH_HOME, 'novel-ai')
  : path.join(os.homedir(), '.novel-ai')
const LOG_DIR = path.join(DATA_ROOT, 'logs')

function tailLog(file, n = 50) {
  try {
    const content = fs.readFileSync(file, 'utf8')
    const lines = content.split('\n').filter(Boolean)
    return lines.slice(-n).join('\n')
  } catch (_) { return '(日志不存在)' }
}

function apply(ctx) {
  // 启动 (异步, 不阻塞 DSH 主流程)
  start()
    .then((r) => {
      if (!r.ok) console.warn(`[dsh-novel-ai] 启动失败: ${r.reason}`)
    })
    .catch((e) => console.warn(`[dsh-novel-ai] 启动异常: ${e.message}`))

  const disposers = []

  // ── 控制端点 ──
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/status',
      handler: (req, res) => {
        status().then((s) => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify(s))
        })
      },
    })
  )

  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/open',
      handler: (req, res) => {
        // 触发重新打开窗口: 直接复用 launcher 的 start 幂等逻辑
        start().then((r) => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify(r))
        })
      },
    })
  )

  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/stop',
      handler: (req, res) => {
        stop().then(() => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify({ ok: true }))
        })
      },
    })
  )

  // 查看运行日志 (最近 80 行, 排障用)
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/logs',
      handler: (req, res) => {
        res.writeHead(200, JSON_HEADERS)
        res.end(JSON.stringify({
          launcher: tailLog(path.join(LOG_DIR, 'novel-ai.log')),
          engine: tailLog(path.join(LOG_DIR, 'engine.log')),
        }))
      },
    })
  )

  // 重启 Novel AI (先停后启, 幂等)
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/restart',
      handler: (req, res) => {
        stop().then(() => start()).then((r) => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify(r))
        })
      },
    })
  )

  // 触发全接口自检 (结果直接返回)
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/verify',
      handler: (req, res) => {
        import('./verify.js').then(({ runVerify }) =>
          runVerify().then((report) => {
            res.writeHead(200, JSON_HEADERS)
            res.end(JSON.stringify(report))
          })
        )
      },
    })
  )

  // ── 清理: DSH 退出时停止所有子进程 ──
  ctx.effect(() => () => {
    stop().catch(() => {})
    for (const d of disposers) {
      try { d() } catch (_) {}
    }
  })
}

export { name, apply }
