# fixverify — 修复验证闭环

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

基于 [OpenCodeReview CLI](https://github.com/alibaba/open-code-review)（`ocr` 命令）的本地独立工具，解决 AI/人工修 bug 的两个顽疾：

- **修 A 坏 B**：修复引入衍生 bug（R 类问题）
- **假修复**：声称已修，实际只修了一部分（✅/🟡/❌ 逐条判定）

不依赖任何平台、不改任何项目代码。任何 git 仓库直接可用。

> **fix-verify** — a fix-verification loop on the OpenCodeReview CLI.
> Catches derivative bugs introduced by fixes (R-class) and fake fixes
> (claimed-fixed-but-not), via an adversarial falsification protocol that
> loops until every finding is VERIFIED with evidence. Works with any git
> repo; ships as an installable skill for AI agents (opencode, Claude Code,
> Cursor) and a plain CLI for humans.

## AI agent 集成（skill）

本仓库自带 agent 技能：`skills/fix-verify/SKILL.md`。安装后，opencode / Claude Code
等支持技能的 agent 会在「修 bug → 声明完成」场景自动加载这套工作流
（修复后必须复验、不收敛不许说完成、[R] 衍生问题必修）。

```powershell
# 安装到本机所有支持的 agent 工具：
powershell -ExecutionPolicy Bypass -File install.ps1
# ① 复制到 ~/.agents/skills/fix-verify/   → opencode 自动加载（外部技能扫描）
# ② 复制到 ~/.claude/skills/fix-verify/   → Claude Code 加载
# ③ 生成 integrations/fix-verify.mdc      → Cursor 用（复制到目标项目 .cursor/rules/）
```

**新环境（新电脑/新编辑器）接入**，对 AI 说一句：

> 访问 D:\project\fixverify（新机器先 git clone https://github.com/wang-c808/fixverify），按 AGENTS.md 把验证闭环加入你的工作流

AI 会读 `AGENTS.md` 自举完成接入（前置检查 → install.ps1 → selftest），
之后默认**自动模式**：开发完成、提交前自动初审 → 修复 → 复验到收敛；
说"跳过验证"切手动模式。

- **迁移到新电脑**：`git clone` 本仓库 → 确认 Python 3.10+ 和 ocr CLI → 重跑 `install.ps1`
  （会把新路径写进技能副本）
- **改技能内容**：改 `skills/fix-verify/SKILL.md`（唯一源）→ 重跑 `install.ps1` 分发
- 为什么不用 MCP：本工具是 CLI + 文件状态，shell 就是 agent 的通用接口；
  ocr 一跑几十分钟，MCP 长调用反而别扭。等将来需要「完成通知/结构化任务」再包 MCP 不迟。
- opencode 重启后技能才生效。

## 原理

```
初审 findings ──→ 修复代码 ──→ 复验（证伪协议）──→ 收敛？
     │                              │
     │                    [VERIFIED] F1  修好了（附证据）
     │                    [PARTIAL]  F2  修了一半（缺 chunked 绕过）
     │                    [NOT-FIXED] F3 没修
     │                    [R] 由 F15 的修复引入…  ← 衍生问题自动归因
     │                              │
     └────── 未收敛 ←── 带着证据继续修 ←┘
```

- **初审**：对项目当前 diff 跑 `ocr review`，所有评论归集为编号 findings（F1、F2…）
- **复验**：把 findings 清单**内嵌进审查规则**（证伪协议），审查修复后的 diff，
  按前缀解析判定回写状态；修复引入的新问题自动挂到引入它的 finding 上
- **收敛判据**：所有未关闭 finding 均为 VERIFIED（复验方交白卷），而不是"CI 绿了"

## 准备

```powershell
# 1. Python 3.10+（无第三方依赖）
# 2. ocr CLI + LLM 配置
npm install -g @alibaba-group/open-code-review
ocr config provider     # 选择 LLM
ocr config model        # 选择模型

# 3. 确认可用
ocr version
```

## 使用（每轮 bug 修复会话）

```powershell
# 建议为每个修复会话建一个运行目录，状态存在 .fixverify/ 里
cd D:\runs\bug-123

# ① 初审：审查项目当前 diff（新增文件先 git add -A，否则 diff 里看不到）
python D:\project\fixverify\fixverify.py review D:\path\to\project

# ② 修复代码（AI 或人工）

# ③ 复验一轮：生成"证伪协议 + findings 清单"规则 → 审查修复 diff → 解析回写
python D:\project\fixverify\fixverify.py verify D:\path\to\project

# 未收敛 → 继续修 → 再 verify，直到报告"收敛"
python D:\project\fixverify\fixverify.py verify D:\path\to\project

# 随时查看
python D:\project\fixverify\fixverify.py status
python D:\project\fixverify\fixverify.py show F3
python D:\project\fixverify\fixverify.py dismiss F3    # 误报人工关闭
```

已有 ocr 审查结果（如从 Web 平台导出的 JSON）可直接导入，不跑 CLI：

```powershell
python D:\project\fixverify\fixverify.py import review-result.json
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `review <workdir>` | 初审：跑 ocr review，评论归集为 findings（默认用 `rules/review_rule.json`，含交互面检查） |
| `import <file.json>` | 从已有 ocr review 结果 JSON 导入 findings |
| `verify <workdir>` | 复验一轮：动态生成"协议+findings"规则 → ocr review --rule → 判定回写 |
| `status` | findings 状态表 + 收敛判定 |
| `show [F3]` | 查看详情（完整内容 / 复验证据） |
| `dismiss F3` / `reopen F3` | 人工关闭 / 重开（dismissed 不进复验清单） |
| `selftest` | 内置自测（不依赖 ocr CLI） |

通用参数：

- `--state <dir>`：状态目录（默认 `./.fixverify`）
- `--timeout <秒>`：ocr 总超时（默认 7200）
- `--no-rule`（review）：不传初审规则
- 传给 ocr 的额外参数放在最后，原样透传：

```powershell
# 例：审查两个分支间的 diff
python D:\project\fixverify\fixverify.py verify D:\proj -- --from main --to fix-branch
# 例：控制 token 预算
python D:\project\fixverify\fixverify.py review D:\proj -- --max-tokens-budget 500000 --exclude "**/*_test.go,**/generated/**"
```

## 输出说明

复验报告（每轮）：

```
=== 复验报告（第 2 轮）===
  F1    [bug/medium] AppPublishService.java:450  [PASS]
        - [VERIFIED] F1 逐路径核对通过：超管直发/审核通过/构建成功均确认
  F2    [security/high] TrackingController.java:36  [PART]
        - [PARTIAL] F2 只挡了 Content-Length，chunked 绕过未处理
  F3  R [bug/medium] RequestLoggingFilter.java:39  [OPEN]  <- F2
        - [R] 由 F2 的修复引入：新路径不在脱敏前缀里
----------
判定: PASS=1 PART=1 FAIL=0 新增R=1 新增问题=0 漏判=0
收敛: 未通过 2 条（F2 F3）→ 修复后再次 verify
```

- `PASS` = VERIFIED（有证据）；`PART` = 部分修复；`FAIL` = 未修复；`OPEN` = 待核对；`OFF` = 人工关闭
- `R` 行 + `<- F15` = 衍生问题及其引入源（R 类随轮次累积，也会进后续复验清单）
- **漏判**：agent 没给某条 finding 判定 → 建议重跑本 verify
- 每轮原始结果存在 `.fixverify/rounds/verify-NN.json`

## 文件结构

```
fixverify/
├── AGENTS.md               # AI agent 接入指南（新环境让 AI 读这个文件自举接入）
├── fixverify.py            # 主脚本（纯标准库，Python 3.10+）
├── rules/
│   ├── review_rule.json    # 初审规则：通用清单 + 交互面检查 + severity 标准
│   └── verify_rule.json    # 复验协议基座（findings 运行时动态内嵌）
├── skills/fix-verify/SKILL.md  # agent 技能（唯一源，install.ps1 分发）
├── install.ps1             # 一键安装技能到本机各 agent 工具
├── integrations/fix-verify.mdc # Cursor 项目规则模板（生成物）
└── README.md
```

### 新环境接入（给 AI 的口令）

对 AI 说一句即可，AI 会读 `AGENTS.md` 自举完成接入：

> 访问 D:\project\fixverify（或 clone https://github.com/wang-c808/fixverify），
> 按 AGENTS.md 把验证闭环加入你的工作流

接入后默认**自动模式**：开发完成（准备提交前）自动初审 → 修复 → 复验到收敛；
说"跳过验证"切手动模式。

复验协议核心（`rules/verify_rule.json`，可按团队经验持续增补）：

1. **证伪导向**：优先推翻"已修"声明，而不是确认它
2. **证据强制**：拿不出证据的判定一律降级 PARTIAL
3. **横切层清单**：新增对外路径时逐层核对 auth/限流/体积/日志脱敏/时区/索引/保留
4. **主体×状态矩阵**：匿名/登录/订阅/角色逐格核对，警惕"白捡"行为被收走
5. **失败拓扑三问**：代码被移动位置时——失败发生在哪个事务？未修 finding 受影响吗？承诺还成立吗？
6. **绕过枚举**：chunked/缺头/边界值/同族词/并发/重复提交
7. **R 类归因**：衍生问题注明由哪条修复引入

每个逃逸的 bug 都值得回流成协议的新检查项——这是让网越收越紧的机制。

## 给 AI agent 用

fixverify 是纯脚本接口，天然可被 agent 编排（伪流程）：

```
1. agent 修复 bug
2. 运行 fixverify.py verify <workdir>
3. 解析 stdout：若 "未通过 N 条" → 把报告喂回 agent 继续修 → 回到 2
4. 出现 "收敛" → 结束
```

注意事项：

- 初审/复审针对 **git diff**：未提交的新文件先 `git add -A`；已提交可用 `-- --commit HEAD`
- 复验必须能看到修复 diff（工作区未提交变更，或 `--from/--to`/`--commit` 指定）
- ocr 的 LLM 配置质量决定判定质量；复验建议在 `ocr config` 里用较强的模型
- 行号可能漂移：协议已要求 agent 按内容定位，不依赖行号
