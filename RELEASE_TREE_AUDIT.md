# LAM 仓库与测试集发布审计

审计日期：2026-07-18  
请求版本标注：0.6.1（用户确认：此前任务中的 0.6.0 均指 0.6.1）  
实际源码版本：0.6.1

## 结论

当前 `LAM_tools` 工作树可以构建不含用户文件的 0.6.1 source distribution；真实 Catalogue、论文、`.env`、library state 和开发资料均未进入该归档。测试隔离已加强，默认套件现在从 session 启动阶段阻止真实 library、项目 `.env`、管理员令牌、网络 socket 和 OCR 模型下载。

仓库还不能直接通过 `git archive` 发布：Git index 当前记录 106 个路径，工作树中另有 89 个实际未跟踪文件，需要在发布提交前审阅并暂存。本报告先前的“63 个”来自 `git status --short` 的分组路径数；Git 会把整个未跟踪目录显示为一行，所以它不是物理文件数。本任务没有代替用户执行 Git staging 或 commit。

## 内容分类

| 分类 | 路径或内容 | 发布策略 |
|---|---|---|
| public source | `LAM_tools/src/lam/**`、根 README/AGENTS/Workflows、package resources、项目 metadata | 进入 source distribution；runtime/package resources 可进入 binary |
| public development-only | `LAM_tools/tests/**`、`docs/**`、`packaging/**`、`scripts/**`、合成 test corpus、MANIFEST 和 release audits | 可进入源码包；不进入 PyInstaller binary |
| local developer-only | `.idea/`、`.agents/`、`dev_local/`、`tests/local/`、`reports_local/`、`开发资料/`、旧本地 audit/proposal | Git ignore；不进入任何发行物 |
| generated | `LAM_tools/.build/`、`dist/`、`tmp/`、`__pycache__/`、`.pytest_cache/`、egg-info | 删除或忽略；重新生成 |
| deprecated | `debug_test.py`、`scripts/search_literature.py`、`AGENTS_backup0605_zh-CN.md`、旧 CLI/Workflow backup | 删除明确无用项；历史审计/提案按版本归档至已忽略的 `开发资料/` |
| potentially sensitive | Catalogue/backups、Inbox/Registered/Topics/Exports、`.library_state/`、`LAM_tools/.env`、研究 PDF | 只报告、忽略并保留；绝不自动删除或打包 |
| unknown | 不需要发布且语义不明确的 `.agents/` 和历史工作材料 | 不读取内容，忽略并报告 |

完整机器可读分类见 [RELEASE_TREE_MANIFEST.json](RELEASE_TREE_MANIFEST.json)。

## 已实施的安全清理

### 已删除

- `LAM_tools/.build/`：16 个 PyInstaller 构建文件，267,073,374 bytes。
- `LAM_tools/dist/`：旧 `LAM-0.6.0` frozen build，7,297 个文件，826,543,692 bytes。
- `LAM_tools/tmp/`：仅包含空的 PDF render 目录。
- 7 个 `LAM_tools/**/__pycache__/`；未发现残留 `.pytest_cache/`。
- tracked 的零字节旧入口 `debug_test.py`。
- retired `scripts/search_literature.py` tombstone。
- 过时的 `AGENTS_backup0605_zh-CN.md`。
- 正式 `lam cleanup --include-test-artifacts --apply` 删除 40 个过期 metadata-cache/旧 snapshot 条目（48 个文件，374,526 bytes）。

已知释放空间合计 1,093,991,592 bytes，不含空目录和 Python cache 大小。

### 已移动或标准化

- legacy Catalogue 生成器从通用 `tests/conftest.py` 抽离至 `tests/fixtures/legacy/factory.py`。
- legacy preflight 用例移入 document migration 测试模块。
- Workflow 3 Documents 测试改为 strict current-schema fixture，不再借 legacy migration 建立普通测试状态。
- `tests/fixtures/legacy/README.md` 明确规定这里只能保存合成的 migration/recovery fixtures。
- `Workflows_backup0714.md` 通过 Git blob 精确确认属于 0.3.2，移至 `开发资料/0.3.2 版本审计资料/`。
- `CLI_AUDIT_0.5.0.md` 与 `MIGRATION_0.5.0.md` 移至 `开发资料/0.5.0 版本审计资料/`。
- 5 个 0.5.4 CLI audit/proposal/catalogue 文件移至 `开发资料/0.5.4 版本审计资料/`；它们与现有 `.zh-CN.md` 文件哈希不同，均保留而未误删为重复件。

### 已忽略

根和 standalone source 的 `.gitignore` 已覆盖：

- build、dist、tmp、cache、local reports/audit/usertest、release staging；
- `.env`、真实 library directories、Catalogue/backups 和 PDF；
- `dev_local/`、`tests/local/`、`reports_local/`、`开发资料/`；
- superseded CLI proposal/audit/catalogue、0.5.0 migration note 和 Workflow backup 工作文件。

忽略仅阻止误提交，不会删除现有文件。

## 测试集标准化

默认 pytest session 现在强制：

- basetemp 不得位于项目或真实 library 内；
- 显式 test root 不得等于或位于真实 library 下；
- 启动时清除 `LIBRARY_ROOT`，测试模式拒绝隐式 root；
- 测试模式不调用项目 `.env` loader；
- Windows 管理员令牌直接退出，无默认 bypass；
- `OCR_ENABLED=false` 且 `OCR_DOWNLOAD_ENABLED=false`；
- 非 live 测试封锁 `socket.create_connection`、`connect` 和 `connect_ex`；
- fixtures 使用系统 `tmp_path`，因此 reports、invocations、snapshots 和 Catalogue changes 都写入隔离测试 library。

网络例外只适用于用户显式选择的 `live`、`live_provider`、`live_download` 或 `ocr_live` marker；这些 marker 仍被默认配置排除。

## Source distribution 审计

新增根和 standalone `MANIFEST.in`，采用正向 allowlist。最终在独立临时副本中构建并检查：

| 项目 | 结果 |
|---|---:|
| archive | `lam_tools-0.6.1.tar.gz` |
| files | 186 |
| bytes | 450,973 |
| SHA-256 | `61b4e1981cff689af440946422488bba0e8867be8810690f2269cb1f2f9670d6` |
| forbidden members | 0 |
| included PDFs | 4 个 synthetic corpus fixtures |
| `.env.example` | included |
| archive retained locally | no |

归档未包含：真实/非 fixture PDF、Catalogue、Inbox/Registered/Topics/Exports、`.library_state`、`.env`、build/dist/tmp、downloaded corpus、logs、parts、cache 或 `summary.md`。

Setuptools 仅报告一个非泄露型警告：`project.license` 的 TOML table 写法计划在未来弃用。它不影响本次内容安全，但可在后续 packaging maintenance 中改为 SPDX/license-files 写法。

## Git 跟踪审计

当前复核时：

- tracked files：106；
- tracked real PDFs：0；
- tracked `.env`：0；
- tracked Catalogue workbooks：0；
- modified tracked paths：0；
- deleted tracked paths：2；
- untracked actual files：89。

Git 已跟踪集合没有用户论文或 Catalogue，但它也尚未包含大量本轮及前序 0.6.1 源码、测试和文档。因此 sdist 的安全结论针对已审计工作树；正式发布前必须审阅并 stage 预期的 89 个实际 release-candidate 文件，再重新运行 tracked-content test。

## 保留但不进入 binary release

- 完整 pytest 测试集及 legacy/reference fixtures；
- 开发教程和生成式 CLI 文档；
- synthetic test corpus；
- corpus fetch/generation、CLI docs、template sync 和 packaging scripts；
- `MANIFEST.in` 与 release audit 报告；
- source-only packaging spec 和说明。

PyInstaller spec 继续只收集 runtime package、必要依赖、package policy resources 和选定 release documents，不收集 tests、真实 `.env`、test corpus、local audits 或真实 library 数据。

## 等待用户确认

已解决：版本统一为 0.6.1；旧 audit/proposal 已归档；旧 Agents 与 retired writer 已删除；正式 cleanup 已执行。

仍待发布决策或后续证据：

1. 89 个实际 untracked release-candidate 文件中哪些进入正式发布 commit；当前没有执行 staging。
2. cleanup 跳过的 24 个 `.library_state/tmp/` 项目均被判定为 `unknown_temporary_artifact`。它们在没有更充分的 ownership/lifecycle 证据前应继续保留，不应直接递归删除。

## 测试结果

- 聚焦 release-tree、temp isolation、legacy migration 和 Workflow 3 测试：`38 passed`。
- 完整默认测试：`470 passed, 4 deselected`。
- 删除历史入口并执行 cleanup 后的定向回归：`8 passed`（`test_cli_safety_054.py`、`test_release_tree_061.py`）。
- 正式 cleanup：dry-run `success`/exit 0；apply `success`/exit 0，`failed=0`、`partial_success=false`。
- CLI 文档和 package template drift checks：通过前序发布检查并由完整套件覆盖。
- 测试 runner：工作区 Python 3.12.13；临时依赖已删除。

正式发行基线是 Python 3.14，因此在 stage 完整发布集合后，仍应在 `lam-dev` Python 3.14 环境复跑一次完整测试和 sdist audit。
