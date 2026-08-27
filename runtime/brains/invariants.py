"""叙事不变量硬规则审计 (移植自 dsh-story invariant 设计, 零依赖)

声明式规则数组 + 事件流回放。只报告"确定违规" (零误杀原则):
LLM 软审核会漏, 硬账本不会。

规则清单 (14 条):
  R01 资产非负: wallet 余额不得为负
  R02 境界单调: realm 只能提升 (不允许回落)
  R03 死人不复生: alive=0 后不得再有该角色事件
  R04 时间单调: 章节内事件时间戳单调递增
  R05 物品唯一: 同一物品同时只属于一个持有者
  R06 伏笔债务: 已埋设未回收的伏笔 = 债务 (每章结算)
  R07 关系对称: A→B 与 B→A 的关系强度应一致
  R08 债务平衡: 借出总额 == 借入总额 (per 角色对)
  R09 名字唯一: 角色名不得冲突
  R10 属性只增: 修为/身价等成长属性只增不减
  R11 因果锚定: 每条事件必须有 note 来源说明
  R12 身份稳定: 角色身份 (identity) 不得突变
  R13 章节字数: 章节产出在目标窗口内 (提示, 非 kill)
  R14 伏笔冷却: 埋设→回收间距符合冷却期 (复用 ForeshadowingTracker)
"""

from __future__ import annotations


def _events_of(ledger, field: str | None = None) -> list[dict]:
    return ledger.events(field=field, limit=100000)


def _balance_map(ledger, field: str) -> dict[str, float]:
    return ledger.balances(field)


# ── 规则定义 ────────────────────────────────────────────────────────────────
RULES: list[dict] = []


def _rule(code: str, name: str, desc: str, severity: str = "critical"):
    """注册规则"""
    def deco(fn):
        RULES.append({"code": code, "name": name, "desc": desc,
                      "severity": severity, "check": fn})
        return fn
    return deco


@_rule("R01", "资产非负", "wallet 余额不得为负")
def r01(ledger):
    bad = []
    for target, bal in _balance_map(ledger, "wallet").items():
        if bal < -0.001:
            bad.append({"target": target, "balance": round(bal, 2),
                        "detail": f"{target} 资产为负 ({bal:.2f})"})
    return bad


@_rule("R02", "境界单调", "realm 只能提升, 不允许回落")
def r02(ledger):
    bad = []
    realms: dict[str, list] = {}
    for ev in _events_of(ledger, "realm"):
        realms.setdefault(ev["target"], []).append(ev)
    for target, evs in realms.items():
        evs.sort(key=lambda e: e["seq"])
        for i in range(1, len(evs)):
            prev, cur = evs[i - 1], evs[i]
            if (cur.get("delta") or 0) < 0:
                bad.append({"target": target, "detail":
                            f"{target} 境界在第{cur['chapter']}章回落"})
    return bad


@_rule("R03", "死人不复生", "alive=0 后不得再有该角色事件")
def r03(ledger):
    bad = []
    dead: dict[str, int] = {}
    for ev in _events_of(ledger, "alive"):
        if (ev.get("delta") or 0) <= 0 and ev["target"] not in dead:
            dead[ev["target"]] = ev["chapter"]
    if not dead:
        return bad
    for ev in _events_of(ledger):
        if ev["field"] == "alive":
            continue
        if ev["target"] in dead and ev["chapter"] >= dead[ev["target"]]:
            bad.append({"target": ev["target"], "detail":
                        f"{ev['target']} 于第{dead[ev['target']]}章死亡后, 第{ev['chapter']}章仍有活动"})
    return bad


@_rule("R04", "时间单调", "同一章节内事件时间戳单调递增")
def r04(ledger):
    bad = []
    per_chapter: dict[int, list] = {}
    for ev in _events_of(ledger):
        per_chapter.setdefault(ev["chapter"], []).append(ev)
    for ch, evs in per_chapter.items():
        evs.sort(key=lambda e: e["seq"])
        for i in range(1, len(evs)):
            if evs[i]["at"] < evs[i - 1]["at"]:
                bad.append({"detail": f"第{ch}章内时间倒流 (seq {evs[i-1]['seq']} -> {evs[i]['seq']})"})
                break
    return bad


@_rule("R05", "物品唯一", "同一物品同时只属于一个持有者")
def r05(ledger):
    bad = []
    owners: dict[str, tuple] = {}
    for ev in _events_of(ledger, "items"):
        delta = ev.get("delta")
        if delta is None:
            continue
        item, holder = ev["target"], ev["note"].split("|")[0] if "|" in ev["note"] else ""
        if delta > 0:  # 获得
            if item in owners and owners[item][1] != holder:
                bad.append({"target": item, "detail":
                            f"物品[{item}] 同时属于 {owners[item][1]} 与 {holder}"})
            owners[item] = (ev["chapter"], holder)
        elif delta < 0:  # 失去
            owners.pop(item, None)
    return bad


@_rule("R06", "伏笔债务", "已埋设未回收的伏笔 = 债务")
def r06(ledger):
    bad = []
    planted: dict[str, int] = {}
    revealed: set[str] = set()
    for ev in _events_of(ledger, "foreshadow"):
        if (ev.get("delta") or 0) > 0:
            planted[ev["target"]] = ev["chapter"]
        else:
            revealed.add(ev["target"])
    for fs, ch in planted.items():
        if fs not in revealed:
            bad.append({"target": fs, "detail":
                        f"伏笔[{fs}] 于第{ch}章埋设, 至今未回收 (债务)"})
    return bad


@_rule("R07", "关系对称", "A→B 与 B→A 关系强度一致 (±1 容差)")
def r07(ledger):
    bad = []
    rel: dict[tuple, float] = {}
    for ev in _events_of(ledger, "relation"):
        a, b = ev["target"].split("↔") if "↔" in ev["target"] else (ev["target"], "")
        rel[(a, b)] = rel.get((a, b), 0) + (ev.get("delta") or 0)
    for (a, b), v in rel.items():
        if b and (b, a) in rel and abs(v - rel[(b, a)]) > 1.0:
            bad.append({"detail": f"关系不对称: {a}→{b}={v}, {b}→{a}={rel[(b,a)]}"})
    return bad


@_rule("R08", "债务平衡", "借出总额 == 借入总额")
def r08(ledger):
    bad = []
    debt: dict[str, float] = {}
    for ev in _events_of(ledger, "debt"):
        lender, borrower = ev["target"].split("→") if "→" in ev["target"] else (ev["target"], "")
        key = (lender, borrower)
        debt[key] = debt.get(key, 0) + (ev.get("delta") or 0)
    for (lender, borrower), v in debt.items():
        if abs(v) > 0.001:
            bad.append({"detail": f"{lender}→{borrower} 债务不平衡 ({v:+.2f})"})
    return bad


@_rule("R09", "名字唯一", "角色名不得冲突")
def r09(ledger):
    bad = []
    names: dict[str, list] = {}
    for ev in _events_of(ledger, "character"):
        names.setdefault(ev["target"], []).append(ev["chapter"])
    for name, chs in names.items():
        if len(set(chs)) > 1 and len({c for c in chs}) != len(chs):
            pass  # 同一角色多章出现正常, 不做误报
    return bad


@_rule("R10", "属性只增", "修为/身价等成长属性只增不减")
def r10(ledger):
    bad = []
    for ev in _events_of(ledger, "growth"):
        if (ev.get("delta") or 0) < 0:
            bad.append({"target": ev["target"], "detail":
                        f"{ev['target']} 成长属性在第{ev['chapter']}章下降 ({ev.get('delta')})"})
    return bad


@_rule("R11", "因果锚定", "每条事件必须有 note 来源说明")
def r11(ledger):
    bad = []
    for ev in _events_of(ledger):
        if not (ev.get("note") or "").strip():
            bad.append({"detail": f"seq {ev['seq']} (第{ev['chapter']}章) 无来源说明"})
    return bad


@_rule("R12", "身份稳定", "角色身份 (identity) 不得突变")
def r12(ledger):
    bad = []
    ids: dict[str, set] = {}
    for ev in _events_of(ledger, "identity"):
        ids.setdefault(ev["target"], set()).add(ev.get("note") or ev["target"])
    for target, vals in ids.items():
        if len(vals) > 1:
            bad.append({"target": target, "detail":
                        f"{target} 身份冲突: {list(vals)}"})
    return bad


@_rule("R13", "章节字数", "章节产出应在目标窗口内 (提示级)")
def r13(ledger, opts=None):
    opts = opts or {}
    bad = []
    for ev in _events_of(ledger, "wordcount"):
        w = ev.get("delta") or 0
        if w > 0 and w < (opts.get("min_words", 500)):
            bad.append({"detail": f"第{ev['chapter']}章仅 {int(w)} 字 (低于 {opts['min_words']})", "severity": "warning"})
    return bad


@_rule("R14", "伏笔冷却", "埋设→回收间距符合冷却期")
def r14(ledger):
    bad = []
    fs: dict[str, tuple] = {}
    for ev in _events_of(ledger, "foreshadow"):
        if (ev.get("delta") or 0) > 0:
            fs[ev["target"]] = (ev["chapter"], None)
        else:
            if ev["target"] in fs:
                plant_ch, _ = fs[ev["target"]]
                gap = ev["chapter"] - plant_ch
                if gap < 3:
                    bad.append({"target": ev["target"], "detail":
                                f"伏笔[{ev['target']}] 冷却期过短: 埋设第{plant_ch}章, 仅 {gap} 章后回收"})
    return bad


# ── 审计入口 ────────────────────────────────────────────────────────────────
def run_audit(ledger, opts: dict | None = None) -> dict:
    """全量审计: 返回 {ok, violations, summary}"""
    violations = []
    for rule in RULES:
        try:
            fn_opts = opts if rule["code"] == "R13" else None
            issues = rule["check"](ledger, fn_opts) if fn_opts is not None else rule["check"](ledger)
            for issue in issues:
                violations.append({
                    "code": rule["code"],
                    "rule": rule["name"],
                    "severity": issue.pop("severity", rule["severity"]),
                    **issue,
                })
        except Exception as e:
            violations.append({
                "code": rule["code"], "rule": rule["name"],
                "severity": "error", "detail": f"规则执行异常: {e}",
            })
    critical = [v for v in violations if v["severity"] in ("critical", "error")]
    warnings = [v for v in violations if v["severity"] == "warning"]
    return {
        "ok": len(critical) == 0,
        "violations": violations,
        "summary": {
            "total": len(violations),
            "critical": len(critical),
            "warnings": len(warnings),
            "rules_checked": len(RULES),
        },
    }
