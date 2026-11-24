# fixverify — AI Agent 接入指南

如果你（AI agent）被用户带到这个仓库（"访问这个仓库，把验证闭环加入你的工作流"），
按本文件操作。本仓库是「修复验证闭环」工具：初审 findings → 修复 → 复验（证伪协议）→ 收敛，
用于抓两类问题：**修复引入的衍生 bug（R 类）** 和 **假修复（声称已修实际未修）**。

## 首次接入（每台机器一次）

```powershell
# 1. 前置检查
python --version          # ≥ 3.10
ocr version               # 未安装: npm i -g @alibaba-group/open-code-review
                          # 未配置 LLM: ocr config provider && ocr config model
# 2. 安装技能到本机各 agent 工具（opencode/Claude Code/Cursor 模板）
powershell -ExecutionPolicy Bypass -File install.ps1
# 3. 自测（预期 passed=17 failed=0）
python fixverify.py selftest
# 4. 提醒用户重启编辑器
```

## 两种使用模式

### 自动模式（默认，推荐）

适用于：有**实质代码变更**的开发任务（功能、bug 修复、重构）。

```
开发完成（准备提交 / 声明完成之前）
   ↓ 自动触发，不需用户提醒
初审：python fixverify.py review <项目路径>     （新文件先 git add -A）
   ↓
有 findings → 逐条修复 → verify → 循环直到报告「收敛」
   ↓
收敛后向用户汇报每条判定 + 证据，然后才提交
```

例外（可跳过初审）：纯文档改动、纯格式化、用户明确说"跳过验证"。
跳过时要向用户说明跳过了。

### 手动模式

仅在用户明确要求时执行（"帮我初审一下"、"复验"、"跑 fixverify"）。
执行对应命令并解读报告；**不**自动进入修复循环，除非用户要求。

## 命令速查

| 命令 | 用途 |
|---|---|
| `python fixverify.py review <workdir>` | 初审：diff → findings |
| `python fixverify.py import <file.json>` | 从已有 ocr 结果 JSON 导入 findings |
| `python fixverify.py verify <workdir>` | 复验一轮（证伪协议，判定回写） |
| `python fixverify.py status` | 状态表 + 收敛判定 |
| `python fixverify.py show F3` | 单条详情（含复验证据） |
| `python fixverify.py dismiss F5` / `reopen F5` | 人工关闭 / 重开 |
| `python fixverify.py selftest` | 自检 |

- 输出标记：`[PASS]`=已修(有证据) `[PART]`=部分 `[FAIL]`=未修 `R ... <- F<n>`=衍生问题
- **收敛判据**：全部未关闭 finding 均为 VERIFIED；每轮原始结果在 `.fixverify\rounds\`
- ocr 审查耗时几分钟到几十分钟，调用时给足超时（工具默认 7200s）
- 状态目录默认 = 当前工作目录 `.fixverify/`；**不要**在被审项目内建状态目录
  （工具会警告），用 `--state` 指到项目外，如 `D:\runs\<任务名>`
- 透传 ocr 参数放在 `--` 之后：`verify <proj> -- --commit HEAD` / `-- --from A --to B`

## 纪律

1. 修复后未经收敛确认，禁止声称"修复完成"
2. `[PART]` / `[FAIL]` 的处置是继续修，不是解释
3. `[R]` 衍生问题与原 finding 同级，必修到收敛
4. 漏判非零 → 重跑 verify
5. 最小 diff，禁止顺手重构

## 协议维护

`rules/verify_rule.json` 是复验协议（证伪导向 + 横切层/主体矩阵/失败拓扑/绕过枚举）。
每次发现逃逸 bug，评估后把它固化成协议新检查项——网越用越密。
