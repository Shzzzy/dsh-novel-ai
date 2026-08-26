// ═══════════════════════════════════════════════════════════════════════════
// dsh-novel-ai 启动器核心
// ── 功能 ──
//   1. 拉起 Python AI 引擎 (FastAPI, 端口 8765)
//   2. 拉起前端静态服务 (优先 5173, 被占用时自动 +1)
//   3. 拉起 8766 → 8765 转发 (前端章节版本历史页硬编码 8766)
//   4. 打开独立浏览器窗口加载原界面
// ── 用法 ──
//   独立运行:  node lib/launcher.js start | stop | status
//   cordis 插件: 由 lib/index.js 包装调用
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

// 端口约定 —— 与前端硬编码保持一致 (见 src/pages/ChapterVersionHistory.tsx 等)
const ENGINE_PORT = 8765
const VERSIONS_FORWARD_PORT = 8766
const WEB_PORT_START = 5173

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
  children: [],      // 子进程列表 (引擎进程)
  servers: [],       // 需要关闭的 http server 列表
  webPort: 0,
  engineOk: false,
}

// ── 日志 ────────────────────────────────────────────────────────────────────
function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    fs.appendFileSync(path.join(LOG_DIR, 'novel-ai.log'), line)
  } catch (_) { /* 日志失败不阻塞 */ }
  console.log(line.trimEnd())
}

// ── Python 解释器探测 ────────────────────────────────────────────────────────
function findPython() {
  const candidates = [
    process.env.NOVEL_AI_PYTHON,
    '/home/host/miniconda3/bin/python3',
    'python3',
    'python',
  ].filter(Boolean)
  for (const cmd of candidates) {
    try {
      const r = spawnSync(cmd, ['-c', 'pass'], { timeout: 8000, stdio: 'ignore' })
      if (r.status === 0) return cmd
    } catch (_) { /* 尝试下一个 */ }
  }
  return null
}

// ── 引擎: 启动 + 健康检查 ───────────────────────────────────────────────────
function startEngine(pythonBin) {
  return new Promise((resolve) => {
    const logFile = path.join(LOG_DIR, 'engine.log')
    const out = fs.openSync(logFile, 'a')
    const err = fs.openSync(logFile, 'a')
    const child = spawn(pythonBin, ['engine/main.py'], {
      cwd: RUNTIME_DIR,
      stdio: ['ignore', out, err],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    })
    state.children.push(child)
    // 记录引擎 PID, 供跨进程 stop 兜底清理
    try {
      fs.mkdirSync(DATA_ROOT, { recursive: true })
      fs.writeFileSync(ENGINE_PID_FILE, String(child.pid))
    } catch (_) { /* 忽略 */ }
    child.on('exit', (code) => {
      log(`引擎进程退出, code=${code}`)
      try { fs.rmSync(ENGINE_PID_FILE, { force: true }) } catch (_) {}
    })
    // 给引擎 1 秒初始化时间, 再做健康轮询
    setTimeout(() => resolve(child), 1000)
  })
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

// ── 前端静态服务 (SPA 回退, 顺序探测端口, 首个成功即停) ─────────────────────
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
        // 路径穿越防护
        res.writeHead(403); res.end(); return
      }
      fs.stat(filePath, (err, stat) => {
        if (err || !stat.isFile()) {
          // SPA 回退: 非资源路径一律返回 index.html
          filePath = path.join(WEB_DIR, 'index.html')
        }
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
        // 端口被占用 → 探测下一个
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

// ── 8766 → 8765 转发 (章节版本历史页) ───────────────────────────────────────
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

// ── 打开独立窗口 ────────────────────────────────────────────────────────────
function openBrowser(url) {
  const platform = process.platform
  let cmd, args
  if (platform === 'darwin') { cmd = 'open'; args = [url] }
  else if (platform === 'win32') { cmd = 'cmd'; args = ['/c', 'start', '', url] }
  else { cmd = 'xdg-open'; args = [url] }
  try {
    const child = spawn(cmd, args, { detached: true, stdio: 'ignore' })
    child.unref()
    log(`已在独立窗口打开: ${url}`)
  } catch (e) {
    log(`自动打开浏览器失败(可手动访问 ${url}): ${e.message}`)
  }
}

// ── 主流程 ──────────────────────────────────────────────────────────────────
export async function start() {
  // 幂等: 引擎已在运行则跳过
  const already = await waitEngineHealth(3000)
  if (already) {
    log('检测到引擎已在运行, 复用现有实例')
  } else {
    const pythonBin = findPython()
    if (!pythonBin) {
      log('✗ 未找到可用 Python 解释器, 请设置 NOVEL_AI_PYTHON 环境变量')
      return { ok: false, reason: 'python-not-found' }
    }
    log(`使用 Python: ${pythonBin}`)
    await startEngine(pythonBin)
    const healthy = await waitEngineHealth()
    if (!healthy) {
      log('✗ 引擎健康检查超时, 请查看 logs/engine.log')
      return { ok: false, reason: 'engine-unhealthy' }
    }
    log(`引擎就绪: http://localhost:${ENGINE_PORT}/api/health`)
  }

  await startForwarder().catch((e) => log(`转发服务启动失败(可忽略): ${e.message}`))
  const port = await startWebServer()
  const url = `http://localhost:${port}`
  openBrowser(url)

  // 记录 PID, 便于外部 stop
  try {
    fs.mkdirSync(DATA_ROOT, { recursive: true })
    fs.writeFileSync(PID_FILE, String(process.pid))
  } catch (_) { /* 忽略 */ }
  return { ok: true, url, enginePort: ENGINE_PORT, webPort: port }
}

export async function stop() {
  // 跨进程停止: 若本进程不是运行中的启动器, 委托信号给记录在案的启动器 PID
  const recordedPid = readPid(PID_FILE)
  if (recordedPid && Number(recordedPid) !== process.pid) {
    try {
      process.kill(Number(recordedPid), 'SIGTERM')
      log(`已向启动器进程 ${recordedPid} 发送停止信号`)
      // 兜底: 若启动器进程已死, 直接清理引擎
      setTimeout(() => {
        const engPid = readPid(ENGINE_PID_FILE)
        if (engPid) {
          try { process.kill(Number(engPid), 'SIGTERM') } catch (_) {}
          try { fs.rmSync(ENGINE_PID_FILE, { force: true }) } catch (_) {}
        }
      }, 3000).unref()
      return { ok: true, delegated: true }
    } catch (_) { /* 进程已死, 走直接清理 */ }
  }

  // 直接清理 (同进程内调用)
  for (const s of state.servers) { try { s.close() } catch (_) {} }
  state.servers = []
  // 终止子进程: 先 SIGTERM, 2 秒后 SIGKILL
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
  // 兜底: 清理可能残留的引擎进程 (例如启动器被 SIGKILL 后成为孤儿)
  const engPid = readPid(ENGINE_PID_FILE)
  if (engPid && !children.some((c) => c.pid === Number(engPid))) {
    try { process.kill(Number(engPid), 'SIGKILL') } catch (_) {}
  }
  try { fs.rmSync(PID_FILE, { force: true }) } catch (_) {}
  try { fs.rmSync(ENGINE_PID_FILE, { force: true }) } catch (_) {}
  log('Novel AI 已停止')
  return { ok: true }
}

function readPid(file) {
  try {
    const raw = fs.readFileSync(file, 'utf8').trim()
    return raw ? Number(raw) : null
  } catch (_) { return null }
}

export async function status() {
  const healthy = await waitEngineHealth(3000)
  return {
    enginePort: ENGINE_PORT,
    webPort: state.webPort,
    engineOk: healthy,
    url: state.webPort ? `http://localhost:${state.webPort}` : null,
    pid: process.pid,
  }
}

// ── CLI 入口 (独立运行用) ───────────────────────────────────────────────────
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMain) {
  const cmd = process.argv[2] || 'start'
  if (cmd === 'start') {
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
  }
}
