#!/usr/bin/env python3
"""
fixverify — 修复验证闭环（基于 OpenCodeReview CLI 的本地独立工具）

解决「修 A 坏 B / 声称已修实际未修」：

    初审(review/import) → 修复代码 → 复验(verify，证伪协议) → 未收敛继续修 → 再 verify
                                                        ↑ 循环直到收敛（全部 VERIFIED）

依赖：Python 3.10+；ocr CLI（npm i -g @alibaba-group/open-code-review）已安装并配置好 LLM。
无第三方 Python 依赖（纯标准库）。

子命令：
  review  <workdir>   初审：对 workdir 的 diff 跑 ocr review，comments 归集为 findings
  import  <file.json> 从已有 ocr review 结果 JSON 导入 findings（不跑 CLI）
  verify  <workdir>   复验：内嵌 findings 的证伪协议审查修复 diff，解析判定回写
  status              查看 findings 状态与收敛判定
  show    [F3]        查看某条 finding 详情（缺省列全部）
  dismiss F3          人工关闭（误报，不再进复验清单）
  reopen  F3          重新打开
  selftest            内置自测（不依赖 ocr CLI）

状态存储：<运行目录>/.fixverify/（--state 可指定），每轮复验原始结果存 rounds/。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"

TOOL_DIR = Path(__file__).resolve().parent
REVIEW_RULE_PATH = TOOL_DIR / "rules" / "review_rule.json"
VERIFY_RULE_PATH = TOOL_DIR / "rules" / "verify_rule.json"

# ── 协议解析（纯函数）────────────────────────────────────────────────────────

# 判定评论："[VERIFIED] F3" / "[PARTIAL] F3：..." / "[NOT-FIXED] F12 ..."
VERDICT_RE = re.compile(
    r"^\s*\[(VERIFIED|PARTIAL|NOT[-_ ]?FIXED)\]\s*F?(\d+)\b", re.IGNORECASE
)
# 衍生问题："[R] ..." / "[R] 由 F15 的修复引入：..."
# 注意不能用 \b："]" 与空格之间不存在词边界
REGRESSION_RE = re.compile(r"^\s*\[R\]")
PARENT_RE = re.compile(r"F(\d+)\b", re.IGNORECASE)

VERDICT_TO_STATUS = {
    "verified": "verified",
    "partial": "partial",
    "not-fixed": "not_fixed",
    "not_fixed": "not_fixed",
    "notfixed": "not_fixed",
}


def parse_verdict(content: str) -> tuple[str, int] | None:
    """解析判定评论前缀，返回 (status, seq)；非判定评论返回 None"""
    m = VERDICT_RE.match(content or "")
    if not m:
        return None
    status = m.group(1).lower().replace("_", "-").replace(" ", "-")
    status = VERDICT_TO_STATUS.get(status)
    if status is None:
        return None
    return status, int(m.group(2))


def parse_regression(content: str) -> int | None:
    """识别 [R] 衍生问题评论，返回引用的父 finding seq（无引用时 None）"""
    if not REGRESSION_RE.match(content):
        return None
    rest = REGRESSION_RE.sub("", content, count=1)
    m = PARENT_RE.search(rest)
    return int(m.group(1)) if m else None


# ── 状态管理 ─────────────────────────────────────────────────────────────────

FINDING_DEFAULTS = {
    "seq": 0,
    "kind": "issue",          # issue / regression
    "status": "open",         # open / verified / partial / not_fixed / dismissed
    "severity": None,
    "category": None,
    "file_path": "",
    "start_line": 0,
    "end_line": 0,
    "content": "",
    "suggestion_code": None,
    "parent_seq": None,       # R 类：引入它的 finding 编号
    "verify_note": None,      # 复验判定证据
    "verify_round": None,     # 最近一次判定/发现所在轮次
    "created_at": None,
    "updated_at": None,
}


class State:
    """运行状态：findings.json + rounds/ 原始结果归档"""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.file = state_dir / "findings.json"
        self.rounds_dir = state_dir / "rounds"
        self.data: dict = {"version": 1, "next_seq": 0, "rounds": 0, "findings": []}

    def load(self) -> "State":
        if self.file.exists():
            with open(self.file, encoding="utf-8") as f:
                self.data = json.load(f)
        return self

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @property
    def findings(self) -> list[dict]:
        return self.data["findings"]

    def by_seq(self, seq: int) -> dict | None:
        for f in self.findings:
            if f["seq"] == seq:
                return f
        return None

    def add_finding(self, **kwargs) -> dict:
        self.data["next_seq"] += 1
        now = datetime.now().isoformat(timespec="seconds")
        f = {**FINDING_DEFAULTS, "created_at": now, "updated_at": now,
             **kwargs, "seq": self.data["next_seq"]}
        self.findings.append(f)
        return f

    def open_findings(self) -> list[dict]:
        """非 dismissed 的 findings（复验清单 = 这些）"""
        return [f for f in self.findings if f["status"] != "dismissed"]

    def bump_round(self) -> int:
        self.data["rounds"] += 1
        return self.data["rounds"]


# ── 归集：comments → findings ────────────────────────────────────────────────

def ingest_comments(state: State, comments: list) -> dict:
    """初审/import：把 ocr comments 归集为 findings（按 位置+类别 去重）"""
    existing_keys = {
        (f["file_path"], f["category"], f["start_line"])
        for f in state.findings if f["kind"] == "issue"
    }
    added = 0
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        path = (c.get("path") or "").replace("\\", "/")[:1024]
        if not path:
            continue
        key = (path, c.get("category"), int(c.get("start_line") or 0))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        state.add_finding(
            kind="issue",
            status="open",
            severity=c.get("severity"),
            category=c.get("category"),
            file_path=path,
            start_line=int(c.get("start_line") or 0),
            end_line=int(c.get("end_line") or 0),
            content=c.get("content") or "(无内容)",
            suggestion_code=c.get("suggestion_code"),
        )
        added += 1
    return {"added": added}


# ── 复验判定回写（纯函数，可自测）────────────────────────────────────────────

def apply_verify_result(state: State, result: dict, round_no: int) -> dict:
    """复验结果回写：

    - [VERIFIED]/[PARTIAL]/[NOT-FIXED] F<n> → 更新 finding 状态与证据
    - [R] → 新增 kind=regression finding（父由 F<n> 引用解析）
    - 无前缀评论 → 新增 issue finding（下一轮纳入核对）
    - status == "skipped" → 无 diff，直接返回
    """
    if not isinstance(result, dict) or result.get("status") == "skipped":
        return {"skipped": True}

    by_seq = {f["seq"]: f for f in state.findings}
    verdict_count = {"verified": 0, "partial": 0, "not_fixed": 0}
    missing = [f["seq"] for f in state.open_findings()]
    regressions: list[int] = []
    new_issues: list[int] = []

    for c in result.get("comments") or []:
        if not isinstance(c, dict):
            continue
        content = c.get("content") or ""
        verdict = parse_verdict(content)
        if verdict is not None:
            status, seq_no = verdict
            target = by_seq.get(seq_no)
            if target is None or target["status"] == "dismissed":
                continue
            target["status"] = status
            target["verify_note"] = content
            target["verify_round"] = round_no
            target["updated_at"] = datetime.now().isoformat(timespec="seconds")
            verdict_count[status] += 1
            if seq_no in missing:
                missing.remove(seq_no)
            continue

        is_r = REGRESSION_RE.match(content) is not None
        path = (c.get("path") or "").replace("\\", "/")[:1024]
        if not path:
            continue
        parent_seq = parse_regression(content) if is_r else None
        entry = state.add_finding(
            kind="regression" if is_r else "issue",
            status="open",
            severity=c.get("severity"),
            category=c.get("category"),
            file_path=path,
            start_line=int(c.get("start_line") or 0),
            end_line=int(c.get("end_line") or 0),
            content=content or "(无内容)",
            suggestion_code=c.get("suggestion_code"),
            parent_seq=parent_seq,
            verify_round=round_no,
        )
        (regressions if is_r else new_issues).append(entry["seq"])

    return {
        "verdicts": verdict_count,
        "missing_verdicts": sorted(missing),
        "regressions": regressions,
        "new_issues": new_issues,
    }


# ── 渲染：FINDINGS 清单 / 动态复验规则 ──────────────────────────────────────

def render_findings_md(findings: list[dict]) -> str:
    lines = [
        "# 待核对 Findings（初审发现清单）",
        "",
        "逐条核对以下初审发现，并按复验协议对**每一条**输出判定评论"
        "（[VERIFIED] / [PARTIAL] / [NOT-FIXED] + F编号）。",
        "清单中的行号基于初审时的代码，修复后行号可能漂移，**以文件名 + 内容定位为准**。",
        "",
    ]
    for f in findings:
        sev = f.get("severity") or "?"
        cat = f.get("category") or "?"
        loc = f"{f.get('file_path')}:{f.get('start_line') or 0}-{f.get('end_line') or 0}"
        kind_tag = "【衍生问题】" if f.get("kind") == "regression" else ""
        lines.append(f"## F{f['seq']} [{sev}][{cat}] {loc} {kind_tag}")
        lines.append("")
        lines.append((f.get("content") or "").strip())
        suggestion = (f.get("suggestion_code") or "").strip()
        if suggestion:
            lines += ["", "初审建议修复：", "```", suggestion, "```"]
        lines.append("")
    return "\n".join(lines)


def build_verify_rule_text(findings: list[dict], base_path: Path = VERIFY_RULE_PATH) -> str:
    """复验协议基座 + findings 清单内嵌 → 动态规则文件文本（--rule 直接可用）"""
    with open(base_path, encoding="utf-8") as f:
        data = json.load(f)
    md = render_findings_md(findings)
    rules = data.setdefault("rules", [])
    for r in rules:
        if r.get("path") == "**/*":
            r["rule"] = f"{r.get('rule', '')}\n\n---\n\n{md}"
            break
    else:
        rules.append({"path": "**/*", "rule": md})
    return json.dumps(data, ensure_ascii=False)


def write_verify_rule(state_dir: Path, findings: list[dict]) -> Path:
    """动态规则写到状态目录（绝对路径传 --rule，不污染被审项目的工作区）"""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "verify-rule.generated.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_verify_rule_text(findings))
    return path


# ── ocr CLI 调用 ─────────────────────────────────────────────────────────────

def find_ocr() -> str | None:
    """Windows 上 shutil.which 可能返回无扩展名的 shell 脚本，优先 .cmd"""
    path = shutil.which("ocr")
    if path and os.name == "nt" and os.path.isfile(path + ".cmd"):
        return path + ".cmd"
    return path


def run_ocr_review(workdir: Path, extra_args: list[str],
                   rule_path: Path | None, timeout_s: int) -> dict:
    """在 workdir 中执行 ocr review --format json --audience agent [--rule ...]"""
    ocr = find_ocr()
    if not ocr:
        sys.exit("[fixverify] ocr CLI 未安装：npm install -g @alibaba-group/open-code-review")
    if not (workdir / ".git").exists():
        sys.exit(f"[fixverify] {workdir} 不是 git 仓库（ocr review 需要 git diff）")

    cmd = [ocr, "review", "--format", "json", "--audience", "agent"]
    if rule_path:
        cmd += ["--rule", str(rule_path.resolve())]
    cmd += extra_args

    print(f"[fixverify] 运行: {' '.join(cmd[1:])}  (cwd={workdir})")
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, cwd=str(workdir),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                proc.kill()
        except Exception:
            pass
        sys.exit(f"[fixverify] ocr review 超时（>{timeout_s}s），可用 --timeout 加大")
    if proc.returncode != 0:
        sys.exit(f"[fixverify] ocr review 失败（exit {proc.returncode}）:\n{stderr[-2000:]}")
    print(f"[fixverify] ocr 完成，耗时 {time.time() - t0:.0f}s")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        sys.exit(f"[fixverify] ocr 输出不是有效 JSON: {stdout[:500]}")


# ── 报告输出 ─────────────────────────────────────────────────────────────────

STATUS_MARK = {
    "verified": "PASS",
    "partial": "PART",
    "not_fixed": "FAIL",
    "open": "OPEN",
    "dismissed": "OFF",
}


def warn_state_inside_project(state_dir: str, workdir: str) -> None:
    """状态目录在被审项目内时警告：会污染项目 git status，且 .fixverify 可能混进 diff"""
    try:
        s = Path(state_dir).resolve()
        w = Path(workdir).resolve()
        if s == w or w in s.parents:
            print("[fixverify] 警告：状态目录在被审项目内（会污染 git status / diff）。"
                  "建议 --state 指到项目外，如 D:\\runs\\<任务名>")
    except Exception:
        pass


def print_findings_table(findings: list[dict]) -> None:
    if not findings:
        print("  （无 findings）")
        return
    for f in findings:
        mark = STATUS_MARK.get(f["status"], "?")
        kind = "R " if f["kind"] == "regression" else "  "
        parent = f" <- F{f['parent_seq']}" if f.get("parent_seq") else ""
        loc = f"{f['file_path']}:{f['start_line'] or 0}"
        print(f"  F{f['seq']:<3}{kind} [{f.get('category') or '?'}/{f.get('severity') or '?'}] "
              f"{loc}  [{mark}] {parent}")
        note = (f.get("verify_note") or "").replace("\n", " ")
        if note:
            print(f"        - {note[:160]}")


def convergence(findings: list[dict]) -> tuple[bool, list[dict]]:
    """(是否收敛, 未通过清单)。判据：所有未 dismissed 的 finding 均为 verified"""
    outstanding = [f for f in findings
                   if f["status"] not in ("dismissed", "verified")]
    return (len(findings) > 0 and not outstanding), outstanding


# ── 子命令 ───────────────────────────────────────────────────────────────────

def cmd_review(args) -> None:
    warn_state_inside_project(args.state, args.workdir)
    state = State(Path(args.state)).load()
    if state.findings and not args.force:
        sys.exit("[fixverify] 状态中已有 findings；追加用 --force，或先 verify / dismiss")
    result = run_ocr_review(
        Path(args.workdir), args.ocr_args or [],
        rule_path=None if args.no_rule else REVIEW_RULE_PATH, timeout_s=args.timeout,
    )
    state_dir = Path(args.state)
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "review.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if result.get("status") == "skipped":
        print("[fixverify] 没有差异，无需审查（git diff HEAD 为空？）")
        return
    summary = ingest_comments(state, result.get("comments") or [])
    state.save()
    print(f"[fixverify] 初审完成：新增 {summary['added']} 条 findings（共 {len(state.findings)} 条）")
    print("[fixverify] 下一步：修复代码后运行  fixverify.py verify <workdir>")


def cmd_import(args) -> None:
    state = State(Path(args.state)).load()
    # utf-8-sig：兼容带 BOM 的 JSON（Windows 工具链常见）
    with open(args.file, encoding="utf-8-sig") as f:
        result = json.load(f)
    summary = ingest_comments(state, result.get("comments") or [])
    state.save()
    print(f"[fixverify] 导入完成：新增 {summary['added']} 条 findings（共 {len(state.findings)} 条）")


def cmd_verify(args) -> None:
    warn_state_inside_project(args.state, args.workdir)
    state = State(Path(args.state)).load()
    targets = state.open_findings()
    if not targets:
        sys.exit("[fixverify] 没有可复验的 findings（先跑 review 或 import）")

    round_no = state.bump_round()
    rule_path = write_verify_rule(Path(args.state), targets)
    print(f"[fixverify] 第 {round_no} 轮复验，核对 {len(targets)} 条 findings，规则: {rule_path}")

    result = run_ocr_review(Path(args.workdir), args.ocr_args or [],
                            rule_path=rule_path, timeout_s=args.timeout)
    if result.get("status") == "skipped":
        print("[fixverify] 没有差异，无需审查（修复 diff 为空？）")
        return

    summary = apply_verify_result(state, result, round_no)
    state.save()

    rounds_dir = Path(args.state) / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    with open(rounds_dir / f"verify-{round_no:02d}.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "result": result}, ensure_ascii=False, indent=2)

    print(f"\n=== 复验报告（第 {round_no} 轮）===")
    print_findings_table(state.open_findings())
    v = summary["verdicts"]
    print("----------")
    print(f"判定: PASS={v['verified']} PART={v['partial']} FAIL={v['not_fixed']} "
          f"新增R={len(summary['regressions'])} 新增问题={len(summary['new_issues'])} "
          f"漏判={len(summary['missing_verdicts'])}")
    if summary["missing_verdicts"]:
        ids = " ".join(f"F{i}" for i in summary["missing_verdicts"])
        print(f"漏判（无判定评论）: {ids} → 建议重跑本 verify")
    done, outstanding = convergence(state.findings)
    if done:
        print("收敛: 全部 findings 已 VERIFIED（dismissed 不计），可以合并")
    else:
        ids = " ".join(f"F{f['seq']}" for f in outstanding)
        print(f"收敛: 未通过 {len(outstanding)} 条（{ids}）→ 修复后再次 verify")


def cmd_status(args) -> None:
    state = State(Path(args.state)).load()
    print(f"状态目录: {state.dir}")
    print(f"轮次: {state.data['rounds']}  findings: {len(state.findings)}")
    print_findings_table(state.findings)
    if not state.findings:
        return
    done, outstanding = convergence(state.findings)
    if done:
        print("收敛: 是")
    else:
        print(f"收敛: 否（未通过 {len(outstanding)} 条）")


def cmd_show(args) -> None:
    state = State(Path(args.state)).load()
    if args.finding:
        seq = int(re.sub(r"^[Ff]", "", args.finding))
        f = state.by_seq(seq)
        if not f:
            sys.exit(f"F{seq} 不存在")
        print(json.dumps(f, ensure_ascii=False, indent=2))
    else:
        print_findings_table(state.findings)


def cmd_setstatus(args) -> None:
    state = State(Path(args.state)).load()
    seq = int(re.sub(r"^[Ff]", "", args.finding))
    f = state.by_seq(seq)
    if not f:
        sys.exit(f"F{seq} 不存在")
    f["status"] = args.action  # dismissed / open
    f["updated_at"] = datetime.now().isoformat(timespec="seconds")
    state.save()
    print(f"[fixverify] F{seq} -> {args.action}")


# ── 自测（不依赖 ocr CLI）────────────────────────────────────────────────────

def cmd_selftest(args) -> None:
    import tempfile

    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}")

    print("== 判定解析 ==")
    check("VERIFIED", parse_verdict("[VERIFIED] F3") == ("verified", 3))
    check("PARTIAL+内容", parse_verdict("[PARTIAL] F12 缺 chunked") == ("partial", 12))
    check("NOT-FIXED", parse_verdict("[NOT-FIXED] F7") == ("not_fixed", 7))
    check("NOT_FIXED 下划线", parse_verdict("[NOT_FIXED] F7") == ("not_fixed", 7))
    check("小写", parse_verdict("[not-fixed] F7") == ("not_fixed", 7))
    check("非判定", parse_verdict("SQL 注入风险") is None)
    check("R 不是判定", parse_verdict("[R] 由 F15 引入") is None)

    print("== R 解析 ==")
    check("[R] 无引用", parse_regression("[R] 修复引入日志明文") is None)
    check("[R] 引用 F15", parse_regression("[R] 由 F15 的修复引入：匿名被 401") == 15)
    check("非 R", parse_regression("[VERIFIED] F3") is None)

    print("== 动态规则 ==")
    fs = [{"seq": 1, "kind": "issue", "severity": "medium", "category": "bug",
           "file_path": "A.java", "start_line": 450, "end_line": 455,
           "content": "事件目录在提交审核时就 upsert", "suggestion_code": "挪到上线时机"}]
    text = build_verify_rule_text(fs)
    data = json.loads(text)
    rule = [r for r in data["rules"] if r["path"] == "**/*"][0]["rule"]
    check("协议 + findings 内嵌", "VERIFIED" in rule and "F1" in rule and "证伪" in rule)

    print("== 模拟一轮复验 ==")
    with tempfile.TemporaryDirectory() as td:
        st = State(Path(td)).load()
        st.add_finding(kind="issue", status="open", file_path="A.java", start_line=450,
                       content="初始问题", severity="medium", category="bug")
        st.data["rounds"] = 1
        fake = {"comments": [
            {"path": "A.java", "start_line": 451, "category": "bug", "severity": "medium",
             "content": "[VERIFIED] F1 逐路径核对通过"},
            {"path": "B.java", "start_line": 10, "category": "security", "severity": "high",
             "content": "[R] 由 F1 的修复引入：同步挪到上线事务后，超长字段导致 500"},
            {"path": "C.java", "start_line": 9, "category": "bug", "severity": "low",
             "content": "普通新问题（无前缀）"},
        ]}
        s = apply_verify_result(st, fake, 2)
        check("F1 -> verified", st.by_seq(1)["status"] == "verified")
        check("R 归因 F1", st.by_seq(2)["kind"] == "regression"
              and st.by_seq(2)["parent_seq"] == 1)
        check("新问题入账", st.by_seq(3)["kind"] == "issue")
        check("汇总", s["verdicts"]["verified"] == 1
              and len(s["regressions"]) == 1 and len(s["new_issues"]) == 1
              and s["missing_verdicts"] == [])
        done, outstanding = convergence(st.findings)
        check("未收敛（R+新问题 open）", done is False and len(outstanding) == 2)
        st.by_seq(2)["status"] = "verified"
        st.by_seq(3)["status"] = "verified"
        done, _ = convergence(st.findings)
        check("全 verified 收敛", done is True)

    print()
    print(f"passed={passed} failed={failed}")
    sys.exit(1 if failed else 0)


# ── 入口 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fixverify", description="修复验证闭环（基于 ocr CLI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--state", default=".fixverify",
                        help="状态目录（默认 ./ .fixverify）")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("review", help="初审：跑 ocr review 并归集 findings")
    p.add_argument("workdir", help="被审项目目录（git 仓库）")
    p.add_argument("--no-rule", action="store_true", help="不传初审规则文件")
    p.add_argument("--timeout", type=int, default=7200, help="ocr 总超时秒数")
    p.add_argument("--force", action="store_true", help="已有 findings 时强制追加")
    p.add_argument("ocr_args", nargs=argparse.REMAINDER, help="透传给 ocr 的额外参数（放最后）")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("import", help="从 ocr review 结果 JSON 导入 findings")
    p.add_argument("file")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("verify", help="复验一轮（证伪协议）")
    p.add_argument("workdir", help="包含修复 diff 的项目目录")
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument("ocr_args", nargs=argparse.REMAINDER, help="透传给 ocr 的额外参数（放最后）")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("status", help="findings 状态 + 收敛判定")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("show", help="查看 finding 详情")
    p.add_argument("finding", nargs="?", default=None, help="如 F3")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("dismiss", help="人工关闭 finding（误报）")
    p.add_argument("finding", help="如 F3")
    p.set_defaults(fn=cmd_setstatus, action="dismissed")

    p = sub.add_parser("reopen", help="重新打开 finding")
    p.add_argument("finding", help="如 F3")
    p.set_defaults(fn=cmd_setstatus, action="open")

    p = sub.add_parser("selftest", help="内置自测（不依赖 ocr CLI）")
    p.set_defaults(fn=cmd_selftest)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
