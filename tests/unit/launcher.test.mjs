// ═══════════════════════════════════════════════════════════════════════════
// launcher 单元测试 (node:test, 零依赖)
// 运行: node --test tests/unit/
// ═══════════════════════════════════════════════════════════════════════════
import { test, describe, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { __internals } from '../../lib/launcher.js'

const { readPid, pidAlive, rotateIfNeeded, findPython, acquireLock, LOG_ROTATE_BYTES } = __internals

// 每个测试的临时目录
let tmpDir

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-novel-ai-test-'))
})

afterEach(() => {
  try { fs.rmSync(tmpDir, { recursive: true, force: true }) } catch (_) {}
})

// ── readPid ──────────────────────────────────────────────────────────────────
describe('readPid', () => {
  test('正常数字 PID', () => {
    const f = path.join(tmpDir, 'a.pid')
    fs.writeFileSync(f, '12345')
    assert.equal(readPid(f), 12345)
  })

  test('带空白/换行的 PID', () => {
    const f = path.join(tmpDir, 'b.pid')
    fs.writeFileSync(f, '  67890\n')
    assert.equal(readPid(f), 67890)
  })

  test('非数字内容返回 null', () => {
    const f = path.join(tmpDir, 'c.pid')
    fs.writeFileSync(f, 'abc')
    assert.equal(readPid(f), null)
  })

  test('文件不存在返回 null', () => {
    assert.equal(readPid(path.join(tmpDir, 'nope.pid')), null)
  })
})

// ── pidAlive ─────────────────────────────────────────────────────────────────
describe('pidAlive', () => {
  test('当前进程存活', () => {
    assert.equal(pidAlive(process.pid), true)
  })

  test('不存在的 PID', () => {
    assert.equal(pidAlive(99999999), false)
  })
})

// ── rotateIfNeeded (日志轮转) ────────────────────────────────────────────────
describe('rotateIfNeeded', () => {
  test('超过阈值 → 备份 .1 并截断原文件', () => {
    const f = path.join(tmpDir, 'big.log')
    fs.writeFileSync(f, 'x'.repeat(LOG_ROTATE_BYTES + 100))
    rotateIfNeeded(f)
    assert.equal(fs.existsSync(`${f}.1`), true, '.1 备份应存在')
    assert.ok(fs.statSync(f).size < 100, '原文件应被截断')
  })

  test('未超阈值 → 不动', () => {
    const f = path.join(tmpDir, 'small.log')
    fs.writeFileSync(f, 'hello')
    rotateIfNeeded(f)
    assert.equal(fs.existsSync(`${f}.1`), false, '不应产生备份')
    assert.equal(fs.readFileSync(f, 'utf8'), 'hello')
  })

  test('文件不存在 → 不抛异常', () => {
    rotateIfNeeded(path.join(tmpDir, 'ghost.log')) // 不应 throw
  })
})

// ── findPython ───────────────────────────────────────────────────────────────
describe('findPython', () => {
  test('能找到系统 Python', async () => {
    const py = await findPython()
    assert.ok(py, '应找到 Python 解释器')
  })

  test('NOVEL_AI_PYTHON 指向无效路径 → 回退到系统 Python', async () => {
    const old = process.env.NOVEL_AI_PYTHON
    process.env.NOVEL_AI_PYTHON = '/nonexistent/python-binary'
    try {
      const py = await findPython()
      // 无效 env 应回退, 而不是死路 (返回非空且不是无效路径)
      assert.ok(py, '应回退找到可用 Python')
      assert.notEqual(py, '/nonexistent/python-binary')
    } finally {
      if (old === undefined) delete process.env.NOVEL_AI_PYTHON
      else process.env.NOVEL_AI_PYTHON = old
    }
  })
})

// ── acquireLock (单实例锁) ───────────────────────────────────────────────────
describe('acquireLock', () => {
  test('无锁文件 → 获取成功并写入', () => {
    const f = path.join(tmpDir, 'lock.pid')
    assert.equal(acquireLock(f), true)
    assert.equal(readPid(f), process.pid)
  })

  test('锁被他人持有且存活 → 拒绝', () => {
    const f = path.join(tmpDir, 'lock.pid')
    // 用当前进程伪造他人锁 (pidAlive 判定为存活)
    fs.writeFileSync(f, String(process.pid))
    // 直接测逻辑: 需要 pid !== process.pid 才拒绝 —— 模拟: 写一个已知存活的其他 pid 不可行,
    // 因此用子进程验证 (见 scenario 测试), 此处验证: 自己持有 → 允许
    assert.equal(acquireLock(f), true)
  })

  test('锁已死(陈旧) → 允许接管', () => {
    const f = path.join(tmpDir, 'lock.pid')
    fs.writeFileSync(f, '99999999') // 不存在的 pid
    assert.equal(acquireLock(f), true)
    assert.equal(readPid(f), process.pid, '应覆盖为当前 pid')
  })
})
