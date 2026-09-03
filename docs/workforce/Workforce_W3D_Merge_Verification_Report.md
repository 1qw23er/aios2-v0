# W3-D Trial — Merge Verification Report

**最终状态（2026-09-03 19:00）：✅ 闭环。PR #10 已 Squash and merge 入 `main`，落地 squash commit `3f313ad7878b72328fb4a32d438112021b2a8cd0`。三铁证零偏差（实现面 diff 空 / blob 字节级等同 / `TRIALING` 就位），alembic 单 head 前移至 `20260903_0001_workforce_trial`，W3-D 31 测试 31 passed，ruff 全绿。W3-A→W3-B→W3-C→W3-D 全链路落地，下一阶段 W4（Employee 任命）。详见 §8。**

**历史轨迹（留痕，勿删）**：首轮 R7 误合 PR#8（docs）→ 方案 A cherry-pick 建 `w3d-trial-impl` → PR#9 base 错选 `feat/real-agent-identity`（`origin/HEAD` 污染）→ Close PR#9 + 直链重开 **PR#10** → Squash merge 成功。

---

## 1. R7 授权记录（Merge Authorization）

| 项 | 值 |
|---|---|
| 授权方 | R7（黎叔），owner |
| 操作 | Squash and merge（**禁止** merge commit） |
| exact-head SHA | `7cd262a6b49f3f8b32f8322f4d7107727273284e` |
| tree SHA | `ed315532cf78135bbb3bcb668efafa669cb1b568` |
| base main | `9364a6750d348712954e63eddc365e144bcf3838` |
| branch | `w3d-trial-spec` → `origin/w3d-trial-spec` |
| §12 C-1…C-8 | **全部确认接受** |
| 授权范围 | 仅限 W3-D exact-head，**不授权任何额外修改** |

---

## 2. 合并前核验（Pre-Merge Verification）— 全部 PASS（无变化）

| 检查 | 结果 |
|---|---|
| 本地/远端 `w3d-trial-spec` HEAD | `7cd262a6b49f3f8b32f8322f4d7107727273284e` ✅ = 授权 exact-head |
| `w3d-trial-spec` tree | `ed315532cf78135bbb3bcb668efafa669cb1b568` ✅ = 授权 tree |
| base `origin/main`（合并前） | `9364a6750d348712954e63eddc365e144bcf3838` ✅ |
| Alembic 单 head（分支上） | `20260903_0001_workforce_trial` ✅ |
| 全量测试 | `1255 passed, 0 failed` ✅ |
| W3-D 31 测试 | `31 passed` ✅ |
| Ruff | All checks passed ✅ |
| DSH 实现态审计 | GO WITH CONDITIONS，A–G 全 PASS，P0/P1/P2=0 ✅ |

---

## 3. 首轮合并核验（Post-Merge Verification #1）— ❌ 偏差（已由 §8 PR#10 修正）

R7 报告「已合并」后，WB 执行 `git fetch origin` 本地核验（无需 PAT）：

| 项 | 授权/预期 | 实际 main (`origin/main`) | 结果 |
|---|---|---|---|
| main HEAD | 新 squash commit（应为 W3-D 内容） | `ff2c72248f73d9ce8144f1ee0d7ee4946f0b14ea` | ⚠️ commit message = **`docs(workforce): archive W3 design/audit corpus under docs/workforce with index (#8)`** |
| **main tree** | `ed315532cf78135bbb3bcb668efafa669cb1b568` | `35a373b977fdef132e4809df7bd6eb30319424fa` | ❌ **字节级不等** |
| `workforce_trial.py` 是否在 main | 应存在 | **ABSENT** | ❌ 未合入 |
| W3-D 迁移 `20260903_0001_workforce_trial.py` | 应存在 | **ABSENT** | ❌ 未合入 |
| `CandidateStatus.TRIALING` 是否在 main | 应存在 | **0 处**（不在 main） | ❌ 未合入 |
| Alembic 单 head（main 内） | `20260903_0001_workforce_trial` | `20260902_0001_workforce_recommendation`（W3-C） | ❌ 未前移 |
| impl commit `7cd262a6` 是 main 祖先？ | 应是 | **NO（not ancestor）** | ❌ 未合并 |
| `origin/main` 自 PR#7 起的提交 | 应含 W3-D squash | 仅 1 个：`ff2c722` (PR **#8** docs) | ❌ 合错 PR |

**根因**：R7 在 UI 点击 Squash and merge 的对象是 **PR #8（文档归档）**，而非 W3-D 实现 PR（`w3d-trial-spec`）。PR #8 是纯 docs 重组，其 tree 不含任何 W3-D 代码，故与授权 exact-head tree 完全不符。

---

## 4. 代码安全性 — ✅ W3-D 实现零丢失

| 检查 | 结果 |
|---|---|
| `workforce_trial.py` 在 `origin/w3d-trial-spec` | **PRESENT**（代码完好） |
| 迁移 / models.py `TRIALING` / workforce.py 边 / 31 测试 | 均在分支上完好 |
| 分支是否受 PR#8 影响 | 否；`origin/w3d-trial-spec` 仍 = `7cd262a6…`，tree 仍 = `ed315532…` |

**结论**：W3-D 实现代码未丢失，可随时重新合并。仅需合并正确的分支/PR。

---

## 5. 修正路径（待 R7 决策，WB 不擅自 merge）

PR#8 已重组 `docs/workforce/` 目录（删除/移动了 W3-D 分支上 2 个旧文档提交中的同名文件）。因此直接重合并 `w3d-trial-spec` 会与 main 当前 docs 结构冲突。建议二选一：

- **方案 A（推荐）**：基于当前 `origin/main` (`ff2c722`) 新建分支，仅 `git cherry-pick 7cd262a6b49f3f8b32f8322f4d7107727273284e`（该提交 = 纯 W3-D 实现 + 测试更新，不含那 2 个文档提交），解决测试文件的少量冲突后开新 PR；
- **方案 B**：直接在 `w3d-trial-spec` 上 rebase 到 `origin/main` 解决 docs 冲突后开 PR。

两者最终目标一致：**让 `origin/main` tree 字节级等于 `ed315532cf78135bbb3bcb668efafa669cb1b568`**（即授权 exact-head tree），且 alembic head 前移为 `20260903_0001_workforce_trial`。

---

## 6. 裁决（Verdict）— ✅ 门禁闭环（2026-09-03 19:00 更新）

| 门禁 | 状态 |
|---|---|
| 独立审计层（DSH 路径③实现态） | ✅ A–G 全 PASS，P0/P1/P2 = 0 |
| 合并授权 | ✅ R7 对 exact-head `7cd262a6…` 授权，§12 C-1…C-8 接受 |
| 首轮实际合并（PR#8） | ❌ 偏差 — 合错 PR（docs），已修正 |
| 代码安全 | ✅ 全程零丢失（`w3d-trial-spec` → `w3d-trial-impl` → main） |
| 修正路径（方案 A） | ✅ 已执行，blob 字节级等同授权实现 |
| PR#9 base 错选事故 | ✅ 已 Close，直链重开 PR#10 |
| **最终合并（PR#10 Squash）** | ✅ **零偏差闭环**（见 §8） |
| **当前状态** | ✅ **W3-D 落地完成，W3 闭环达成，可进入 W4** |

---

## 7. 方案 A 执行记录（Plan A — cherry-pick onto current main）

**R7 决策**：走方案 A（2026-09-03 14:5x）。

### 7.1 执行步骤
1. `git fetch origin` → `origin/main` = `ff2c722…`（已含 PR#8 文档归档）。
2. 新建分支 `w3d-trial-impl`，base = `origin/main` (`ff2c722`)（手写 loose ref 规避 PortableGit 新建 ref 静默失败）。
3. `git cherry-pick 7cd262a6b49f3f8b32f8322f4d7107727273284e` → **干净落地**（因 `7cd262a6` 的父 `c63f5d5` 与 `ff2c722` 的 source/test/migration 树完全相同，仅 docs 不同；而 `7cd262a6` 不碰 docs → 零冲突）。
4. 新提交 `f7b45803adae167d16345b2c95e8f609a4cfea4a` 落在 `ff2c722` 之上。
5. `git push origin w3d-trial-impl` → remote tip = `f7b4580…`（已与本地核对一致）。

### 7.2 内容完整性核验（cherry-pick 后）
| 检查 | 结果 |
|---|---|
| `git diff 7cd262a6 f7b4580 --name-only -- src/ tests/ alembic/` | **EMPTY** → W3-D 源码/测试/迁移字节级等同授权实现 ✅ |
| `git diff 7cd262a6 f7b4580 --name-only` | 仅 16 个 `docs/` 文件不同（PR#8 重组 vs 旧 2 文档提交），W3-D 实现零偏差 ✅ |
| `workforce_trial.py` blob SHA | `7cd`= `4bfddab…` = `f7b` `4bfddab…` ✅ |
| 迁移 blob SHA | `7cd`= `b89c0ab…` = `f7b` `b89c0ab…` ✅ |
| `CandidateStatus.TRIALING` 枚举 | 就位（1 处）✅ |
| Alembic 单 head | `20260903_0001_workforce_trial` ✅ |
| W3-D 31 测试 | `31 passed (15.47s)` ✅ |
| Ruff（新实现 + 变更测试） | All checks passed ✅ |
| 新 HEAD tree | `db47b87efaa7e7ddc1664d10fdf2753eb4b60a1e`（= `ff2c722` 树 + W3-D 源码；与授权 `ed315532…` 仅差 PR#8 文档重组，符合预期） |

### 7.3 新 PR 元数据（待 R7 在 UI 开 + Squash merge）
- **仓库**：`1qw23er/aios2-v0`
- **Base**：`main`（`ff2c722…`，即当前 `origin/main`）
- **Head / compare**：`w3d-trial-impl`（`f7b45803adae167d16345b2c95e8f609a4cfea4a`）
- **GitHub 开 PR 直链**：`https://github.com/1qw23er/aios2-v0/pull/new/w3d-trial-impl`
- **Title**：`feat(workforce): W3-D Trial -- additive trial table + fail-closed create_trial_from_approval`
- **授权说明**：本 PR head `f7b4580…` 的 W3-D 实现内容字节级等同 R7 已授权的 exact-head `7cd262a6…`（仅 commit 父链不同：建于已含 PR#8 的 main 之上）。§12 C-1…C-8 仍全部接受。
- **合并要求**：仅允许 **Squash and merge**；**禁止** Create a merge commit；禁止追加代码。

### 7.4 合并后核验脚本（R7 点完「已 merge」后由 WB 执行）
```
git fetch origin
# 1) main HEAD / tree
git rev-parse origin/main
git rev-parse origin/main^{tree}
# 2) W3-D 实现入 main 的证据
git cat-file -e origin/main:src/aios/workforce_trial.py   # 应 EXISTS
git cat-file -e origin/main:alembic/versions/20260903_0001_workforce_trial.py  # 应 EXISTS
git show origin/main:src/aios/models.py | grep -c 'TRIALING = "trialing"'  # 应 1
# 3) alembic 单 head
#    -> 20260903_0001_workforce_trial
# 4) 实现 blob 与 f7b4580 字节级一致（即与授权 7cd262a6 一致）
git rev-parse origin/main:src/aios/workforce_trial.py  # = 4bfddab...
# 5) 全量测试 1255 passed（CI 绿）
```
注：因 main 已含 PR#8 文档，合并后 main tree 不会等于原授权 `ed315532…`（那是建于旧 docs 的 tree）；**正确判据是「W3-D 实现 blob/文件入 main + alembic 单 head 前移 + 测试绿」**，而非 tree 字面相等。

---

## 8. 最终核验（Post-Merge Verification #2）— ✅ PR #10 零偏差闭环

**合并事实**：PR #10（`base=main` ← `compare=w3d-trial-impl`）由 R7 在 GitHub UI 执行 **Squash and merge**，2026-09-03 19:00。

### 8.1 落地 commit

| 项 | 值 |
|---|---|
| 落地 commit | `3f313ad7878b72328fb4a32d438112021b2a8cd0` |
| tree | `db47b87efaa7e7ddc1664d10fdf2753eb4b60a1e` |
| parent | **单亲 `ff2c72248f73d9ce8144f1ee0d7ee4946f0b14ea`** |
| 合并方式判定 | ✅ **真 Squash**（单亲；message 尾 `(#10)` 为 squash 特征） |
| author / committer | `1qw23er` / `GitHub`（GPG signed） |
| base 演进 | `9364a67`(PR#7) → `ff2c722`(PR#8 docs) → **`3f313ad`(PR#10 W3-D)** |

> 连续第 2 次 squash（PR#7、PR#10），已打破前 4 次 merge-commit 惯例。**结构零偏差**。

### 8.2 内容零偏差 — 三铁证

| # | 铁证 | 命令 / 判据 | 结果 |
|---|---|---|---|
| ① | 实现面零漂移 | `git diff 7cd262a6 origin/main -- src/ tests/ alembic/` | **EMPTY** ✅ |
| ② | blob 字节级等同 | `workforce_trial.py` main=`4bfddab…` vs 授权=`4bfddab…`<br>迁移 `20260903_0001_workforce_trial.py` main=`b89c0ab…` vs 授权=`b89c0ab…` | **IDENTICAL** ✅ |
| ③ | 枚举就位 | `models.py:1581` `TRIALING = "trialing"` | **PRESENT** ✅ |

**全 tree 差异**：仅 16 个 `docs/` 文件（+3800 / −1489），全部源自 PR#8 文档重组，与 W3-D 实现无关，符合预期。

### 8.3 门禁复验

| 检查 | 结果 |
|---|---|
| Alembic 单 head | ✅ `20260903_0001_workforce_trial`（down_revision = W3-C `20260902_0001_workforce_recommendation`） |
| W3-D 31 测试 | ✅ **31 passed**（本机 443.55s 慢档；CI 以 GitHub Actions 为准） |
| Ruff（`src` + `tests`） | ✅ All checks passed |
| `f7b4580` 是 main 祖先？ | NO — **符合预期**：squash 后原分支 commit 被丢弃，内容以新 commit 落地 |

---

## 9. PR #9 base 错选事故（根因与修正，留痕）

### 9.1 现象

R7 用 GitHub Web UI 直链 `pull/new/w3d-trial-impl` 开 PR#9 后：

| 项 | 预期 | 实际 |
|---|---|---|
| base | `main` | ❌ `feat/real-agent-identity` |
| commits | 1 | ❌ 14（含 `b88209e`/`bb291c6`/`c715c20`/`fdb72d4` 等 W1/W2 远古提交） |
| files changed | 15 / +1109 −42 | ❌ 40 / +13919 −22 |
| 可合并性 | — | ❌ 页面报 **Merge conflicts** |

### 9.2 根因（已实证）

```
git ls-remote origin HEAD
→ refs/heads/feat/real-agent-identity
```

**`origin/HEAD` ≠ `main`** —— 仓库 default branch 指向远古 Gap #3 特性分支 `feat/real-agent-identity`（最后 commit `1ae82ac`，W2 hardening 之前）。GitHub Web UI 开 PR 时 **base 默认 = HEAD 分支**，故 `pull/new/<branch>` 直链（未指定 base）自动落到该远古分支。

> 前 5 次 main 系列合并（PR#2/#3/#4/#5/#7）未踩此坑，是因为 R7 每次都手动改了 base 或事先开过 PR 才看到 base 提示。**这是长期潜伏的事实源失配，PR#9 首次暴露。**

### 9.3 分支本身无污染（核验）

| 检查 | 结果 |
|---|---|
| `git log origin/w3d-trial-impl --oneline` | 仅 2 commits：`f7b4580` ← `ff2c722` ✅ |
| `git merge-base origin/w3d-trial-impl origin/main` | `ff2c722` ✅ |
| `git cherry origin/main origin/w3d-trial-impl` | 仅 1 个 `+` ✅（14 是 base 选错的视图假象） |

### 9.4 修正

1. **Close PR#9**（base 错位 + 实质冲突，不可 merge）。
2. **用直链强制 base 重开**：`https://github.com/1qw23er/aios2-v0/compare/main...w3d-trial-impl?expand=1`
   → 页面显示 `base: main` ← `compare: w3d-trial-impl`，**"Able to merge. These branches can be automatically merged."**，一次成功 → **PR #10**。

### 9.5 教训（已固化为项目铁律）

| 教训 | 约定 |
|---|---|
| 直链 > 下拉 | **开 PR 一律用 `compare/main...<branch>?expand=1`**，不靠 UI 下拉手选 |
| 开 PR 先核 base | 每次开 PR / 发 Web URL 前，必须在 PR 页确认 base = `main` |
| 长期修复 | 把仓库 default branch 改回 `main`（Settings → General → Default branch → main → update，需 admin） |
| 分支卫生 | `feat/real-agent-identity` 为 Gap #3 远古冻分支，**不再 push / 不用作 base** |

---

## 10. 遗留项（不阻塞 W4，待 R7 决策）

| # | 项 | 说明 |
|---|---|---|
| 1 | 改回 default branch | `origin/HEAD` 仍指向 `feat/real-agent-identity`；改回 `main` 可一次性消除 PR#9 类事故风险 |
| 2 | 本报告归档入 main | 本文件当前仍是未跟踪文件（未 commit）；如需归档，需单独开 docs PR |
