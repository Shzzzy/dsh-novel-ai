// ═══════════════════════════════════════════════════════════════════════════
// 场景测试 (集成级, 需要真实服务)
// 运行: node --test tests/scenario/
// 依赖: 引擎依赖已装 (scripts/install-deps.sh)
// ═══════════════════════════════════════════════════════════════════════════
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import { fileURLToPath } from 'node:url'

const LAUNCHER = fileURLToPath(new URL('../../lib/launcher.js', import.meta.url))
const DATA_ROOT = (process.env.DSH_HOME || path.join(os.homedir(), '.novel-ai'))
  + (process.env.DSH_HOME ? '/novel-ai' : '')

// 工具: 执行 launcher CLI 并返回输出 (watchdog 默认关, 防测试间干扰)
function runCli(args, { watchdog = false, timeoutMs = 30000 } = {}) {
  return new Promise((resolve) => {
    const r = spawnSync(process.execPath, [LAUNCHER, ...args], {
      encoding: 'utf8',
      timeout: timeoutMs,
      env: { ...process.env, NOVEL_AI_WATCHDOG: watchdog ? '1' : '0' },
    })
    resolve({ code: r.status, out: (r.stdout || '') + (r.stderr || '') })
  })
}

// 工具: 健康探测
function healthOk(port = 8765, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve) => {
    const poll = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/api/health', timeout: 2000 }, (res) => {
        res.resume()
        if (res.statusCode === 200) return resolve(true)
        retry()
      })
      req.on('error', retry)
      req.on('timeout', () => req.destroy())
    }
    const retry = () => {
      if (Date.now() > deadline) return resolve(false)
      setTimeout(poll, 500)
    }
    poll()
  })
}

describe('CLI 锁竞争', () => {
  test('两个进程同时抢锁, 只有一个成功', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-lock-test-'))
    const lockFile = path.join(tmp, 'lock.pid')
    // 模拟: 进程 A 先持锁 (用自身写锁文件 + 保持存活)
    fs.writeFileSync(lockFile, String(process.pid))
    // 进程 B (子进程) 尝试获取 → 应被拒 (pidAlive(process.pid) = true)
    const script = `
      import { __internals } from ${JSON.stringify(fileURLToPath(new URL('../../lib/launcher.js', import.meta.url)))}
      const ok = __internals.acquireLock(${JSON.stringify(lockFile)})
      console.log(ok ? 'LOCK_OK' : 'LOCK_DENIED')
    `
    const r = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
      encoding: 'utf8', timeout: 15000,
    })
    assert.match(r.stdout, /LOCK_DENIED/, '他人持锁时应被拒绝')
    fs.rmSync(tmp, { recursive: true, force: true })
  })
})

describe('daemon 生命周期', () => {
  test('start --daemon 应成功且服务就绪', async () => {
    const r = await runCli(['start', '--daemon'])
    assert.match(r.out, /daemon 已启动/, `应提示 daemon 已启动: ${r.out}`)
    assert.ok(await healthOk(), '引擎应健康')
  }, 60000)

  test('重复 start 应被锁拒绝', async () => {
    const r = await runCli(['start', '--daemon'])
    assert.match(r.out, /daemon 启动失败|已有启动器实例/, `应拒绝: ${r.out}`)
  }, 30000)

  test('stop 后服务应全部释放', async () => {
    const r = await runCli(['stop'])
    assert.equal(r.code, 0, `stop 应成功: ${r.out}`)
    // 等待清理完成, 探测 8765 是否释放
    const deadline = Date.now() + 15000
    let down = false
    while (Date.now() < deadline) {
      try {
        await fetch('http://127.0.0.1:8765/api/health', { signal: AbortSignal.timeout(1000) })
      } catch (_) {
        down = true // 连接失败 = 端口已释放
        break
      }
      await new Promise((res) => setTimeout(res, 800))
    }
    assert.equal(down, true, '引擎端口应已释放')
  }, 30000)
})

describe('watchdog 崩溃自愈', () => {
  test('引擎被杀后自动重启恢复', async () => {
    // 启动 (开启 watchdog)
    const r = await runCli(['start', '--daemon'], { watchdog: true })
    assert.match(r.out, /daemon 已启动/, `应启动成功: ${r.out}`)
    assert.ok(await healthOk(), '启动后引擎健康')

    // 找到引擎 pid 并强杀
    const engPid = spawnSync('pgrep', ['-f', 'engine/main.py'], { encoding: 'utf8' })
      .stdout.trim().split('\n')[0]
    assert.ok(engPid, '应找到引擎进程')
    process.kill(Number(engPid), 'SIGKILL')
    await new Promise((res) => setTimeout(res, 1500))
    assert.equal(await healthOk(8765, 2000), false, '强杀后引擎应不可用')

    // 等待 watchdog 恢复 (5s 退避 + 启动时间, 最多 40s)
    assert.ok(await healthOk(8765, 40000), 'watchdog 应自动恢复引擎')

    await runCli(['stop'])
  }, 90000)
})
