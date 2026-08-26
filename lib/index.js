// ═══════════════════════════════════════════════════════════════════════════
// dsh-novel-ai —— cordis 插件包装层 v0.3
// ── 生命周期 ──
//   DSH web 启动 → apply() 自动拉起 Novel AI (引擎 + 静态服务 + 独立窗口)
//   DSH web 关闭 → ctx.effect 清理钩子停止所有子进程
// ── 安全 ──
//   v0.3: 以 inProcess 模式运行, 不写 PID_FILE —— 外部 stop 不会误杀 DSH 进程
// ── 控制端点 (挂在 DSH web 服务上) ──
//   GET  /dsh-novel-ai/status    运行状态
//   GET  /dsh-novel-ai/health    汇总: 服务 + 引擎进程详情 + watchdog
//   GET  /dsh-novel-ai/logs      最近日志 (启动器 + 引擎)
//   POST /dsh-novel-ai/open      重新打开独立窗口
//   POST /dsh-novel-ai/stop      停止 Novel AI
//   POST /dsh-novel-ai/restart   先停后启 (幂等)
//   POST /dsh-novel-ai/verify    全接口自检 (并发互斥)
//   POST /dsh-novel-ai/watchdog  {enabled: true|false} 运行时开关自动恢复
// ═══════════════════════════════════════════════════════════════════════════
import { start, stop, status, setWatchdog } from './launcher.js'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

const name = 'dsh-novel-ai'

// cordis 服务依赖声明 —— 必须显式声明才能访问 ctx.webServer
const inject = ['webServer']

const JSON_HEADERS = { 'Content-Type': 'application/json; charset=utf-8' }

// 日志目录 (与 launcher.js 的 DATA_ROOT 保持一致)
const DATA_ROOT = process.env.DSH_HOME
  ? path.join(process.env.DSH_HOME, 'novel-ai')
  : path.join(os.homedir(), '.novel-ai')
const LOG_DIR = path.join(DATA_ROOT, 'logs')

let verifyInFlight = false // verify 并发锁

function tailLog(file, n = 80) {
  try {
    const content = fs.readFileSync(file, 'utf8')
    const lines = content.split('\n').filter(Boolean)
    return lines.slice(-n).join('\n')
  } catch (_) { return '(日志不存在)' }
}

function apply(ctx) {
  // 启动 (异步, 不阻塞 DSH 主流程; inProcess 模式防止 PID_FILE 误杀 DSH)
  start({ inProcess: true })
    .then((r) => {
      if (!r.ok) console.warn(`[dsh-novel-ai] 启动失败: ${r.reason}`)
    })
    .catch((e) => console.warn(`[dsh-novel-ai] 启动异常: ${e.message}`))

  const disposers = []

  // ── 状态 ──
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

  // ── 汇总健康 ──
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/health',
      handler: (req, res) => {
        status().then((s) => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify({
            ok: s.engineOk && s.webPort > 0,
            ...s,
          }))
        })
      },
    })
  )

  // ── 重新开窗 ──
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/open',
      handler: (req, res) => {
        start({ inProcess: true }).then((r) => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify(r))
        })
      },
    })
  )

  // ── 停止 ──
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

  // ── 日志 ──
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

  // ── 重启 ──
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/restart',
      handler: (req, res) => {
        stop().then(() => start({ inProcess: true })).then((r) => {
          res.writeHead(200, JSON_HEADERS)
          res.end(JSON.stringify(r))
        })
      },
    })
  )

  // ── 全接口自检 (并发互斥) ──
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/verify',
      handler: (req, res) => {
        if (verifyInFlight) {
          res.writeHead(409, JSON_HEADERS)
          res.end(JSON.stringify({ ok: false, error: '自检已在运行中' }))
          return
        }
        verifyInFlight = true
        import('./verify.js').then(({ runVerify }) =>
          runVerify().then((report) => {
            res.writeHead(200, JSON_HEADERS)
            res.end(JSON.stringify(report))
          })
        ).catch((e) => {
          res.writeHead(500, JSON_HEADERS)
          res.end(JSON.stringify({ ok: false, error: e.message }))
        }).finally(() => { verifyInFlight = false })
      },
    })
  )

  // ── watchdog 开关 ──
  disposers.push(
    ctx.webServer.register({
      kind: 'exact',
      path: '/dsh-novel-ai/watchdog',
      handler: (req, res) => {
        let body = ''
        req.on('data', (c) => (body += c))
        req.on('end', () => {
          try {
            const { enabled } = JSON.parse(body || '{}')
            const now = setWatchdog(!!enabled)
            res.writeHead(200, JSON_HEADERS)
            res.end(JSON.stringify({ watchdogEnabled: now }))
          } catch (e) {
            res.writeHead(400, JSON_HEADERS)
            res.end(JSON.stringify({ ok: false, error: e.message }))
          }
        })
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

export { name, inject, apply }
