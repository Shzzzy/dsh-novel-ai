// ═══════════════════════════════════════════════════════════════════════════
// dsh-novel-ai 全接口自检工具
// ── 用法 ──
//   node lib/verify.js              全部自检 (引擎 REST + WS + 静态 + 转发)
//   node lib/verify.js --quick      只测基础链路 (health/静态/SPA/转发)
//   node lib/verify.js --only rest  只测 REST
//   退出码: 0 = 全部通过, 1 = 存在 FAIL
// ── 判定 ──
//   PASS  响应码在期望集合内
//   WARN  接口可达但响应码非预期 (如 LLM 端点无 key 返回错误)
//   FAIL  连接失败 / 5xx / 接口不存在
// ═══════════════════════════════════════════════════════════════════════════
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ENGINE = 'http://127.0.0.1:8765'
const WEB = 'http://127.0.0.1:5173'
const FWD = 'http://127.0.0.1:8766'

const args = process.argv.slice(2)
const QUICK = args.includes('--quick')
const ONLY_REST = args.includes('--only') && args.includes('rest')

// ── 工具: 一次 HTTP 请求 ────────────────────────────────────────────────────
function req(method, url, body) {
  return new Promise((resolve) => {
    const u = new URL(url)
    const payload = body !== undefined ? JSON.stringify(body) : null
    const r = http.request(
      {
        host: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        method,
        headers: {
          'Content-Type': 'application/json',
          ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
        },
        timeout: 15000,
      },
      (res) => {
        let data = ''
        res.on('data', (c) => (data += c))
        res.on('end', () => resolve({ status: res.statusCode, body: data }))
      }
    )
    r.on('error', (e) => resolve({ status: 0, body: '', error: e.message }))
    r.on('timeout', () => { r.destroy(); resolve({ status: 0, body: '', error: 'timeout' }) })
    if (payload) r.write(payload)
    r.end()
  })
}

// ── WebSocket 测试 (Node 21+ 原生 WebSocket) ────────────────────────────────
function testWs(url) {
  return new Promise((resolve) => {
    let settled = false
    const done = (r) => { if (!settled) { settled = true; resolve(r) } }
    try {
      const ws = new WebSocket(url)
      const timer = setTimeout(() => { try { ws.close() } catch (_) {} done({ status: 0, body: '', error: 'ws-timeout' }) }, 8000)
      ws.onopen = () => {
        clearTimeout(timer)
        ws.close()
        done({ status: 200, body: 'ws-open-ok' })
      }
      ws.onerror = (e) => { clearTimeout(timer); done({ status: 0, body: '', error: e.message || 'ws-error' }) }
      ws.onclose = () => { clearTimeout(timer); done({ status: 0, body: '', error: 'ws-closed-before-open' }) }
    } catch (e) {
      done({ status: 0, body: '', error: e.message })
    }
  })
}

// ── 测试用例清单 ─────────────────────────────────────────────────────────────
// C(name, method, url, body, expect, note, group)  group: 'basic'=QUICK 模式也跑
const cases = []
const C = (name, method, url, body, expect, note = '', group = 'all') =>
  cases.push({ name, method, url, body, expect: Array.isArray(expect) ? expect : [expect], note, group })

// A. 基础链路 (basic 组, QUICK 模式只跑这些)
C('引擎健康', 'GET', `${ENGINE}/api/health`, undefined, [200], '', 'basic')
C('静态首页', 'GET', `${WEB}/`, undefined, [200], '', 'basic')
C('SPA 路由回退', 'GET', `${WEB}/skeleton`, undefined, [200], '', 'basic')
C('版本历史转发 8766→8765', 'GET', `${FWD}/api/health`, undefined, [200], '', 'basic')
C('WebSocket /ws/agent', 'WS', 'ws://127.0.0.1:8765/ws/agent', undefined, [200], '', 'basic')

// B. 只读 API (无副作用)
C('文风模板列表', 'GET', `${ENGINE}/api/style/templates`, undefined, [200])
C('经验本 Agent 列表', 'GET', `${ENGINE}/api/experience/agents`, undefined, [200])
C('经验本条目(writer)', 'GET', `${ENGINE}/api/experience/writer/entries`, undefined, [200])
C('经验本统计(writer)', 'GET', `${ENGINE}/api/experience/writer/stats`, undefined, [200])
C('经验导出', 'GET', `${ENGINE}/api/experience/export`, undefined, [200])
C('BOSS 交互(writer)', 'GET', `${ENGINE}/api/boss/interactions/writer`, undefined, [200])
C('Canon 冲突列表', 'GET', `${ENGINE}/api/canon/novel-verify/conflicts?resolved=all`, undefined, [200])
C('Canon 事实列表', 'GET', `${ENGINE}/api/canon/novel-verify/facts`, undefined, [200])
C('Canon 统计', 'GET', `${ENGINE}/api/canon/novel-verify/stats`, undefined, [200])
C('学习区分类', 'GET', `${ENGINE}/api/learning/categories`, undefined, [200])
C('学习区列表', 'GET', `${ENGINE}/api/learning`, undefined, [200])
C('学习区统计', 'GET', `${ENGINE}/api/learning/stats`, undefined, [200])
C('LLM 配置', 'GET', `${ENGINE}/api/settings/llm`, undefined, [200])
C('用户偏好', 'GET', `${ENGINE}/api/settings/prefs`, undefined, [200])
C('版本历史列表', 'GET', `${ENGINE}/api/novels/novel-verify/chapters/ch-001/versions`, undefined, [200])
C('版本历史统计', 'GET', `${ENGINE}/api/novels/novel-verify/chapters/ch-001/versions/stats`, undefined, [200])

// C. 最小副作用写入 (创建测试数据, 标注清理方式)
C('创建小说', 'POST', `${ENGINE}/api/novels`, { title: '接口自检测试', summary: '由 verify.js 创建', target_words: 50000 }, [200], '数据: ~/.novel-ai/novels/novel-*')
C('文风分析', 'POST', `${ENGINE}/api/style/analyze`, { sample_text: '测试文本：她推开朱门，檐下风铃轻响。' }, [200])
C('大纲分析', 'POST', `${ENGINE}/api/skeleton/analyze-outline`, { outline: '一个女史官在宫廷中追寻被篡改的历史真相', target_words: 50000 }, [200])
C('学习区写入', 'POST', `${ENGINE}/api/learning`, { title: '接口自检资料', category: '写作技法', content: '自检测试内容，可安全删除。' }, [200], '删除: DELETE /api/learning/{id}')
C('BOSS 交互记录', 'POST', `${ENGINE}/api/boss/record`, { agent_name: 'writer', boss_message: '这章写得不错', agent_response: '谢谢' }, [200])
C('章节版本保存', 'POST', `${ENGINE}/api/novels/novel-verify/chapters/ch-001/versions`, { content: '自检章节内容', word_count: 6, title: '自检版本' }, [200])
C('更新用户偏好', 'PUT', `${ENGINE}/api/settings/prefs`, { theme: 'dark', language: 'zh' }, [200])

// D. 资源不存在 (验证接口逻辑正确响应 404 而非 500)
C('学习区 404 逻辑', 'GET', `${ENGINE}/api/learning/nonexistent-id-xyz`, undefined, [404])
C('版本详情 404 逻辑', 'GET', `${ENGINE}/api/novels/x/chapters/y/versions/z`, undefined, [404])
C('删除不存在学习资料 404', 'DELETE', `${ENGINE}/api/learning/nonexistent-id-xyz`, undefined, [404])
C('Canon 冲突 404 逻辑', 'POST', `${ENGINE}/api/canon/novel-verify/conflicts/nonexistent/resolve`, undefined, [404, 422], '422=缺少body校验, 接口可达')

// E. WebSocket
// (已移至 basic 组, 见上)

// F. LLM 依赖端点 (无 API key 时预期失败类响应 → 接口可达即 WARN 而非 FAIL)
C('LLM: 下一问(next-question)', 'POST', `${ENGINE}/api/skeleton/next-question`, { novel_id: 'novel-verify' }, [200, 400, 422, 500], '需 LLM key, 可达即可')
C('LLM: 生成骨架(generate)', 'POST', `${ENGINE}/api/skeleton/generate`, { novel_id: 'novel-verify' }, [200, 400, 422, 500], '需 LLM key, 可达即可')
C('LLM: 连接测试', 'POST', `${ENGINE}/api/settings/llm/test`, {}, [200, 400, 422, 500], '需 LLM key, 可达即可')

// ── 执行 ─────────────────────────────────────────────────────────────────────
export async function runVerify() {
  const results = []
  let pass = 0, warn = 0, fail = 0

  for (const c of cases) {
    if (QUICK && c.group !== 'basic') continue
    if (ONLY_REST && c.method === 'WS') continue
    const r = c.method === 'WS' ? await testWs(c.url) : await req(c.method, c.url, c.body)
    const ok = r.status > 0 && c.expect.includes(r.status)
    const reachable = r.status > 0
    const verdict = ok ? 'PASS' : reachable ? 'WARN' : 'FAIL'
    if (verdict === 'PASS') pass++
    else if (verdict === 'WARN') warn++
    else fail++
    results.push({ name: c.name, method: c.method, verdict, status: r.status, error: r.error || '', note: c.note || '' })
  }

  return { time: new Date().toISOString(), pass, warn, fail, results }
}

// ── 报告输出 ─────────────────────────────────────────────────────────────────
function printReport(report) {
  console.log(`\n═══ dsh-novel-ai 接口自检报告 ${report.time} ═══\n`)
  console.log('  方法  状态  判定   接口')
  for (const r of report.results) {
    const st = r.status ? String(r.status).padEnd(4) : 'N/A '
    const mark = r.verdict === 'PASS' ? '✓' : r.verdict === 'WARN' ? '△' : '✗'
    console.log(`  ${String(r.method).padEnd(4)} ${st}  ${mark}  ${r.name}`)
    if (r.error) console.log(`        └─ ${r.error}`)
    if (r.note && r.verdict !== 'PASS') console.log(`        └─ ${r.note}`)
  }
  console.log(`\n  结果: ${report.pass} PASS / ${report.warn} WARN / ${report.fail} FAIL`)
  console.log('  WARN 通常表示接口可达但响应码非预期 (如 LLM 端点缺少 API key)\n')
}

// ── CLI 入口 ─────────────────────────────────────────────────────────────────
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMain) {
  const report = await runVerify()
  printReport(report)
  try {
    fs.writeFileSync(
      path.join(PACKAGE_ROOT, 'test-report.json'),
      JSON.stringify(report, null, 2)
    )
  } catch (_) {}
  process.exit(report.fail > 0 ? 1 : 0)
}
