// ═══════════════════════════════════════════════════════════════════════════
// dsh-novel-ai 启动器核心 v0.3
//
// v0.3 加固要点 (防挂死 / 防误杀 / 自恢复):
//   1. 进程内模式(inProcess): 插件在 DSH 进程内运行时, 不写 PID_FILE,
//      杜绝外部 stop 误杀 DSH 进程 (v0.2 的致命隐患)
//   2. findPython 全异步: 不再用 spawnSync 阻塞事件循环
//   3. 引擎快速失败: child 秒退时立即返回错误, 不干等健康超时
//   4. watchdog: 引擎崩溃自动重启 (最多 3 次, 指数退避 5s/15s/45s)
//   5. 单实例锁: 独立 CLI 模式防重复拉起
//   6. 日志轮转: 超过 5MB 自动备份为 .1
//   7. --daemon: CLI 自行后台化, 避免命令替换死等 (v0.1 挂死根因)
//
// ── 用法 ──
//   独立运行:  node lib/launcher.js start [--daemon] | stop | status | restart
//   cordis 插件: 由 lib/index.js 包装调用 (inProcess: true)
// ═══════════════════════════════════════════════════════════════════════════
import { spawn, spawnSync } from 'node:child_process'
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const RUNTIME_DIR = path.join(PACKAGE_ROOT, 'runtime')
const WEB_DIR = path.join(PACKAGE_ROOT, 'web')

// 数据目录: 优先 $DSH_HOME/novel-ai, 回退 ~/.novel-ai
const DATA_ROOT = process.env.DSH_HOME
  ? path.join(process.env.DSH_HOME, 'novel-ai')
  : path.join(os.homedir(), '.novel-ai')
const LOG_DIR = path.join(DATA_ROOT, 'logs')
const PID_FILE = path.join(DATA_ROOT, 'launcher.pid')
const ENGINE_PID_FILE = path.join(DATA_ROOT, 'engine.pid')
const STATUS_FILE = path.join(DATA_ROOT, 'runtime-status.json') // 跨进程状态落盘

// 端口约定 —— 与前端硬编码保持一致
const ENGINE_PORT = 8765
const VERSIONS_FORWARD_PORT = 8766
const WEB_PORT_START = 5173

// watchdog 参数
const WATCHDOG_MAX_RESTARTS = 3
const WATCHDOG_BASE_DELAY_MS = 5000
const ENGINE_START_GRACE_MS = 2000   // 引擎初始化宽限期
const LOG_ROTATE_BYTES = 5 * 1024 * 1024

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.json': 'application/json; charset=utf-8',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
}

const state = {
  children: [],        // 引擎子进程
  servers: [],         // http servers
  webPort: 0,
  engineOk: false,
  engineStartedAt: 0,
  enginePid: 0,
  restartCount: 0,
  watchdogEnabled: process.env.NOVEL_AI_WATCHDOG !== '0',
  inProcess: false,    // 是否运行在宿主进程内 (DSH cordis 插件)
  stopped: false,      // 收到停止信号后不再 watchdog 重启
}

// ── 日志 (带轮转) ────────────────────────────────────────────────────────────
function logFile() {
  return path.join(LOG_DIR, 'novel-ai.log')
}

function rotateIfNeeded(file) {
  try {
    const st = fs.statSync(file)
    if (st.size > LOG_ROTATE_BYTES) {
      fs.copyFileSync(file, `${file}.1`)
      fs.truncateSync(file, 0)
    }
  } catch (_) { /* 文件不存在等, 忽略 */ }
}

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    rotateIfNeeded(logFile())
    fs.appendFileSync(logFile(), line)
  } catch (_) { /* 日志失败不阻塞 */ }
  console.log(line.trimEnd())
}

// ── 工具 ────────────────────────────────────────────────────────────────────
function readPid(file) {
  try {
    const raw = fs.readFileSync(file, 'utf8').trim()
    return raw && /^\d+$/.test(raw) ? Number(raw) : null
  } catch (_) { return null }
}

function pidAlive(pid) {
  try { process.kill(pid, 0); return true } catch (_) { return false }
}

// ── Python 解释器探测 (全异步, 不阻塞事件循环) ────────────────────────────────
function findPython() {
  return new Promise((resolve) => {
    const candidates = [
      process.env.NOVEL_AI_PYTHON,
      '/home/host/miniconda3/bin/python3',
      ...(process.env.PATH || '').split(':')
        .filter(Boolean)
        .flatMap((dir) => ['python3', 'python'].map((n) => path.join(dir, n))),
    ].filter((c, i, arr) => c && arr.indexOf(c) === i) // 去重

    const tryOne = (idx) => {
      if (idx >= candidates.length) return resolve(null)
      const cmd = candidates[idx]
      // 存在性快查 (同步 fs 检查, 微秒级)
      if (cmd.includes('/') && !fs.existsSync(cmd)) return tryOne(idx + 1)
      // 异步可执行验证: 2s 超时
      let settled = false
      const c = spawn(cmd, ['-c', 'pass'], { stdio: 'ignore' })
      const t = setTimeout(() => {
        if (settled) return
        settled = true
        try { c.kill('SIGKILL') } catch (_) {}
        tryOne(idx + 1)
      }, 2000)
      c.on('error', () => {
        if (settled) return
        settled = true
        clearTimeout(t)
        tryOne(idx + 1)
      })
      c.on('exit', (code) => {
        if (settled) return
        settled = true
        clearTimeout(t)
        if (code === 0) resolve(cmd)
        else tryOne(idx + 1)
      })
    }
    tryOne(0)
  })
}

// ── 引擎: 启动 (快速失败) + 健康检查 ─────────────────────────────────────────
function startEngine(pythonBin) {
  return new Promise((resolve) => {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    const engLog = path.join(LOG_DIR, 'engine.log')
    rotateIfNeeded(engLog)
    const out = fs.openSync(engLog, 'a')
    const err = fs.openSync(engLog, 'a')
    const child = spawn(pythonBin, ['engine/main.py'], {
      cwd: RUNTIME_DIR,
      stdio: ['ignore', out, err],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    })
    state.children = state.children.filter((c) => c.pid !== child.pid)
    state.children.push(child)
    state.enginePid = child.pid
    state.engineStartedAt = Date.now()

    // 引擎进程退出处理 (watchdog 在这里触发)
    child.on('exit', (code) => {
      log(`引擎进程退出, code=${code}${state.stopped ? '' : ' (watchdog 接管)'}`)
      try { fs.rmSync(ENGINE_PID_FILE, { force: true }) } catch (_) {}
      state.engineOk = false
      state.enginePid = 0
      state.children = state.children.filter((c) => c.pid !== child.pid)
      if (!state.stopped && state.watchdogEnabled) scheduleRestart()
    })

    // 记录引擎 PID (仅独立模式, 避免与宿主 PID 混淆)
    if (!state.inProcess) {
      try { fs.writeFileSync(ENGINE_PID_FILE, String(child.pid)) } catch (_) {}
    }

    // 宽限期后标记启动完成: 若进程仍存活则正常, 若已退出则快速失败
    setTimeout(() => {
      if (child.exitCode === null) resolve({ exited: false, pid: child.pid })
      else resolve({ exited: true, code: child.exitCode })
    }, ENGINE_START_GRACE_MS)
  })
}

// watchdog: 指数退避重启, 最多 WATCHDOG_MAX_RESTARTS 次
function scheduleRestart() {
  if (state.restartCount >= WATCHDOG_MAX_RESTARTS) {
    log(`✗ watchdog: 已达最大重启次数(${WATCHDOG_MAX_RESTARTS}), 停止自动恢复`)
    state.watchdogEnabled = false
    return
  }
  const delay = WATCHDOG_BASE_DELAY_MS * 2 ** state.restartCount
  state.restartCount += 1
  log(`watchdog: ${delay / 1000}s 后自动重启引擎 (第 ${state.restartCount}/${WATCHDOG_MAX_RESTARTS} 次)`)
  setTimeout(async () => {
    if (state.stopped || !state.watchdogEnabled) return
    const pythonBin = await findPython()
    if (!pythonBin) {
      log('✗ watchdog: 找不到 Python, 放弃恢复')
      state.watchdogEnabled = false
      return
    }
    log('watchdog: 重启引擎...')
    const r = await startEngine(pythonBin)
    if (r.exited) return // 再次秒退, scheduleRestart 会再触发
    const ok = await waitEngineHealth(45000)
    if (ok) {
      state.restartCount = 0
      log('watchdog: 引擎已恢复 ✓')
    }
  }, delay).unref?.()
}

function waitEngineHealth(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve) => {
    const poll = () => {
      const req = http.get(
        { host: '127.0.0.1', port: ENGINE_PORT, path: '/api/health', timeout: 2000 },
        (res) => {
          res.resume()
          if (res.statusCode === 200) {
            state.engineOk = true
            return resolve(true)
          }
          retry()
        }
      )
      req.on('error', retry)
      req.on('timeout', () => req.destroy())
    }
    const retry = () => {
      if (Date.now() > deadline) return resolve(false)
      setTimeout(poll, 1000)
    }
    poll()
  })
}

// ── 前端静态服务 (SPA 回退, 顺序探测端口) ─────────────────────────────────────
function startWebServer() {
  return new Promise((resolve) => {
    const serve = (req, res) => {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405); res.end(); return
      }
      let urlPath = decodeURIComponent((req.url || '/').split('?')[0])
      if (urlPath === '/') urlPath = '/index.html'
      let filePath = path.join(WEB_DIR, urlPath)
      if (!filePath.startsWith(WEB_DIR)) {
        res.writeHead(403); res.end(); return
      }
      fs.stat(filePath, (err, stat) => {
        if (err || !stat.isFile()) filePath = path.join(WEB_DIR, 'index.html')
        const ext = path.extname(filePath).toLowerCase()
        res.writeHead(200, {
          'Content-Type': MIME[ext] || 'application/octet-stream',
          'Cache-Control': ext === '.html' ? 'no-store' : 'public, max-age=86400',
        })
        if (req.method === 'HEAD') { res.end(); return }
        fs.createReadStream(filePath).pipe(res)
      })
    }

    let port = WEB_PORT_START
    const tryListen = () => {
      const server = http.createServer(serve)
      server.on('error', () => {
        port += 1
        if (port < WEB_PORT_START + 10) tryListen()
        else resolve(null)
      })
      server.listen(port, '127.0.0.1', () => {
        state.servers.push(server)
        state.webPort = port
        log(`前端静态服务: http://localhost:${port}`)
        resolve(port)
      })
    }
    tryListen()
  })
}

// ── 8766 → 8765 转发 ─────────────────────────────────────────────────────────
function startForwarder() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const proxyReq = http.request(
        {
          host: '127.0.0.1',
          port: ENGINE_PORT,
          path: req.url,
          method: req.method,
          headers: req.headers,
          timeout: 10000,
        },
        (pRes) => {
          res.writeHead(pRes.statusCode || 200, pRes.headers)
          pRes.pipe(res)
        }
      )
      proxyReq.on('error', () => {
        res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end('Novel AI 引擎不可用')
      })
      proxyReq.on('timeout', () => proxyReq.destroy())
      req.pipe(proxyReq)
    })
    server.on('error', (e) => reject(e))
    server.listen(VERSIONS_FORWARD_PORT, '127.0.0.1', () => {
      state.servers.push(server)
      log(`版本转发服务: 127.0.0.1:${VERSIONS_FORWARD_PORT} → ${ENGINE_PORT}`)
      resolve(server)
    })
  })
}

// ── 打开独立窗口 (带超时保护, 永不阻塞主流程) ────────────────────────────────
function openBrowser(url) {
  const platform = process.platform
  let cmd, args
  if (platform === 'darwin') { cmd = 'open'; args = [url] }
  else if (platform === 'win32') { cmd = 'cmd'; args = ['/c', 'start', '', url] }
  else { cmd = 'xdg-open'; args = [url] }
  try {
    const child = spawn(cmd, args, { detached: true, stdio: 'ignore' })
    child.unref()
    // 5s 后若浏览器进程仍存活则强制结束 (防无桌面环境挂起)
    setTimeout(() => {
      try { if (child.exitCode === null) child.kill('SIGKILL') } catch (_) {}
    }, 5000).unref()
    log(`已在独立窗口打开: ${url}`)
  } catch (e) {
    log(`自动打开浏览器失败(可手动访问 ${url}): ${e.message}`)
  }
}

// ── 单实例锁 (仅独立 CLI 模式) ───────────────────────────────────────────────
function acquireLock() {
  if (state.inProcess) return true // 进程内模式无需锁
  const pid = readPid(PID_FILE)
  if (pid && pid !== process.pid && pidAlive(pid)) {
    log(`✗ 已有启动器实例在运行 (pid=${pid}), 如需接管请先 stop 或使用 --force`)
    return false
  }
  try {
    fs.mkdirSync(DATA_ROOT, { recursive: true })
    fs.writeFileSync(PID_FILE, String(process.pid))
  } catch (_) {}
  return true
}

// ── 主流程 ──────────────────────────────────────────────────────────────────
export async function start(opts = {}) {
  if (opts.inProcess !== undefined) state.inProcess = !!opts.inProcess

  // 幂等: 引擎已在运行则复用
  const already = await waitEngineHealth(3000)
  if (already) {
    log('检测到引擎已在运行, 复用现有实例')
  } else {
    const pythonBin = await findPython()
    if (!pythonBin) {
      log('✗ 未找到可用 Python 解释器, 请设置 NOVEL_AI_PYTHON 环境变量')
      return { ok: false, reason: 'python-not-found' }
    }
    log(`使用 Python: ${pythonBin}`)
    const r = await startEngine(pythonBin)
    if (r.exited) {
      log(`✗ 引擎启动后立即退出 (code=${r.code}), 请查看 logs/engine.log`)
      return { ok: false, reason: 'engine-exited', code: r.code }
    }
    const healthy = await waitEngineHealth(60000)
    if (!healthy) {
      log('✗ 引擎健康检查超时, 请查看 logs/engine.log')
      return { ok: false, reason: 'engine-unhealthy' }
    }
    log(`引擎就绪: http://localhost:${ENGINE_PORT}/api/health`)
    state.restartCount = 0
  }

  await startForwarder().catch((e) => log(`转发服务启动失败(可忽略): ${e.message}`))
  const port = await startWebServer()
  if (!port) {
    log('✗ 前端静态服务端口全被占用 (5173-5182)')
    return { ok: false, reason: 'web-port-exhausted' }
  }
  const url = `http://localhost:${port}`
  openBrowser(url)
  // 落盘运行时状态 (供跨进程 status 读取)
  try {
    fs.mkdirSync(DATA_ROOT, { recursive: true })
    fs.writeFileSync(STATUS_FILE, JSON.stringify({
      webPort: port,
      engineStartedAt: state.engineStartedAt,
      launcherPid: process.pid,
      ts: Date.now(),
    }))
  } catch (_) {}
  return { ok: true, url, enginePort: ENGINE_PORT, webPort: port }
}

export async function stop() {
  state.stopped = true // 先停 watchdog

  // 跨进程委托: 仅在独立模式且有其他启动器实例时
  if (!state.inProcess) {
    const recordedPid = readPid(PID_FILE)
    if (recordedPid && Number(recordedPid) !== process.pid && pidAlive(recordedPid)) {
      try {
        process.kill(Number(recordedPid), 'SIGTERM')
        log(`已向启动器进程 ${recordedPid} 发送停止信号`)
        return { ok: true, delegated: true }
      } catch (_) { /* 进程已死, 走直接清理 */ }
    }
  }

  // 直接清理
  for (const s of state.servers) { try { s.close() } catch (_) {} }
  state.servers = []
  const children = [...state.children]
  state.children = []
  for (const c of children) {
    try { c.kill('SIGTERM') } catch (_) {}
  }
  setTimeout(() => {
    for (const c of children) {
      try { c.kill('SIGKILL') } catch (_) {}
    }
  }, 2000).unref()
  // 兜底: 清理孤儿引擎
  const engPid = readPid(ENGINE_PID_FILE)
  if (engPid && !children.some((c) => c.pid === engPid)) {
    try { process.kill(engPid, 'SIGKILL') } catch (_) {}
  }
  try { fs.rmSync(PID_FILE, { force: true }) } catch (_) {}
  try { fs.rmSync(ENGINE_PID_FILE, { force: true }) } catch (_) {}
  try { fs.rmSync(STATUS_FILE, { force: true }) } catch (_) {}
  log('Novel AI 已停止')
  return { ok: true }
}

function readStatusFile() {
  try {
    return JSON.parse(fs.readFileSync(STATUS_FILE, 'utf8'))
  } catch (_) { return null }
}

export async function status() {
  const healthy = await waitEngineHealth(3000)
  const enginePid = readPid(ENGINE_PID_FILE) || state.enginePid
  // 合并跨进程落盘状态 (daemon 进程内 state 无法被独立 status 进程读取)
  const disk = readStatusFile()
  const webPort = state.webPort || (disk && disk.webPort) || 0
  const startedAt = state.engineStartedAt || (disk && disk.engineStartedAt) || 0
  return {
    enginePort: ENGINE_PORT,
    webPort,
    engineOk: healthy,
    url: webPort ? `http://localhost:${webPort}` : null,
    pid: process.pid,
    launcherPid: (disk && disk.launcherPid) || null,
    inProcess: state.inProcess,
    watchdog: {
      enabled: state.watchdogEnabled,
      restartCount: state.restartCount,
      maxRestarts: WATCHDOG_MAX_RESTARTS,
    },
    engine: {
      pid: enginePid,
      startedAt: startedAt ? new Date(startedAt).toISOString() : null,
      uptimeSec: startedAt ? Math.round((Date.now() - startedAt) / 1000) : 0,
    },
  }
}

export function setWatchdog(enabled) {
  state.watchdogEnabled = !!enabled
  if (enabled && state.enginePid === 0 && !state.stopped) {
    // 引擎不在运行且未被停止 → 立即尝试拉起
    state.stopped = false
    state.restartCount = 0
    start().catch(() => {})
  }
  return state.watchdogEnabled
}

// ── CLI 入口 ─────────────────────────────────────────────────────────────────
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMain) {
  const argv = process.argv.slice(2)
  const cmd = argv[0] || 'start'
  const useDaemon = argv.includes('--daemon')

  if (useDaemon && !process.env.DSH_NOVEL_AI_DAEMON_CHILD) {
    // 后台化: 重新以 detached 方式启动自己, 然后 1s 后确认存活并退出 (防命令替换死等)
    const nodeBin = process.execPath
    const child = spawn(nodeBin, [fileURLToPath(import.meta.url), cmd], {
      detached: true,
      stdio: 'ignore',
      env: { ...process.env, DSH_NOVEL_AI_DAEMON_CHILD: '1' },
    })
    child.unref()
    // 等待 1s 确认子进程仍存活 (锁获取结果), 避免误导性提示
    // 注意: 此定时器不能 unref —— unref 后事件循环空转, 回调永不执行
    setTimeout(() => {
      try {
        if (child.exitCode === null && !child.killed) {
          console.log(`daemon 已启动 (pid=${child.pid})`)
          process.exit(0)
        }
        console.log(`daemon 启动失败 (pid=${child.pid}, exit=${child.exitCode ?? '?'})`)
        process.exit(1)
      } catch (_) {
        console.log(`daemon 已启动 (pid=${child.pid})`)
        process.exit(0)
      }
    }, 1000)
    // daemon 分支到此结束: 父进程由 setTimeout 内 process.exit 收尾,
    // 不再执行下方 CLI 逻辑
  } else {
    if (cmd === 'start') {
      if (!acquireLock()) process.exit(1)
      const r = await start()
      if (r.ok) {
        process.on('SIGINT', async () => { await stop(); process.exit(0) })
        process.on('SIGTERM', async () => { await stop(); process.exit(0) })
        console.log(`Novel AI 运行中: ${r.url} (Ctrl+C 停止)`)
      } else {
        process.exit(1)
      }
    } else if (cmd === 'stop') {
      await stop()
    } else if (cmd === 'status') {
      console.log(JSON.stringify(await status(), null, 2))
    } else if (cmd === 'restart') {
      await stop()
      // 等待旧实例完全退出 (委托停止需要清理时间, 防抢锁竞态)
      for (let i = 0; i < 20; i++) {
        const p = readPid(PID_FILE)
        if (!p || !pidAlive(p)) break
        await new Promise((r) => setTimeout(r, 500))
      }
      if (!acquireLock()) process.exit(1)
      const r = await start()
      if (r.ok) {
        // restart 成功后必须常驻 (daemon 模式: 不能 exit, 否则服务随进程死亡)
        process.on('SIGINT', async () => { await stop(); process.exit(0) })
        process.on('SIGTERM', async () => { await stop(); process.exit(0) })
        console.log(`Novel AI 已重启: ${r.url} (Ctrl+C 停止)`)
      } else {
        process.exit(1)
      }
    } else {
      console.log('用法: node lib/launcher.js start [--daemon] | stop | status | restart')
      process.exit(1)
    }
  }
}
