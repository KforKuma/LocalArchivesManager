# LAM 0.6.1 新手完整教程

这份教程面向第一次使用 LAM 的 Windows 用户。你不需要先理解程序内部实现；只要能在文件管理器中找到目录，并能在 PowerShell 中复制命令即可。

示例使用两个互不相同的目录：

- 程序或源码：`C:\LAM\LAM-0.6.1-windows-x64` 或 `D:\Projects\LAM`
- 你的文献库：`D:\MyResearchLibrary`

请把示例路径替换成自己的路径。**不要把文献库建在程序目录或源码目录内。**

> 版本说明：0.6.1 的 release candidate 可能显示为 `0.6.1-rc1`。这是同一 0.6.1 contract 的候选构建；正式包应显示正式发布时确定的版本号。

## 1. LAM 是什么

LAM（Local Archives Manager）用于管理本地论文 PDF、参考文献记录和主题目录。它把“论文是什么”和“文件现在在哪里”分开记录：

- `catalogue.xlsx` 的 **Catalogue** sheet：一篇论文一行，保存题名、作者、年份、DOI、主题等论文级信息。
- `catalogue.xlsx` 的 **Documents** sheet：一个受管理文件一行，保存文件名、相对路径、哈希和文件状态。不要手工编辑这个 sheet。
- `Inbox/`：新放入、等待识别或登记的 PDF 和参考文献文本。
- `Registered/`：已经识别并登记，但尚未按主题归档的文件。
- `Topics/`：最终主题目录。文件会进入 `Topics/<topic_folder>/`。
- `.library_state/`：LAM 的缓存、报告、运行记录和派生状态。不要手工修改或删除其中的单个文件。
- `summary.md`：完全排除在 LAM 读取范围之外。你可以在主题目录中维护自己的 `summary.md`，LAM 不会读取、匹配或改写它。

LAM 有两种使用方式：

1. **源码开发版**：适合开发者和合作者，需要 Python 3.14 与 Conda，可以查看和修改代码。
2. **Windows onedir 发行版**：适合普通用户，解压后运行 `lam.exe`，不需要安装 Python 或 Conda。

两种方式操作的是同一种文献库结构。程序可以更新，文献库仍是独立目录。

## 2. 使用前的重要概念

### 2.1 library root

library root 是文献库的最外层目录，例如：

```text
D:\MyResearchLibrary
```

初始化后，它大致包含：

```text
D:\MyResearchLibrary\
├── catalogue.xlsx
├── Inbox\
├── Registered\
├── Topics\
├── Imports\
├── Exports\
├── .library_state\
├── AGENTS.md
└── Workflows.md
```

程序目录与 library root 不是同一个目录：

```text
C:\LAM\LAM-0.6.1-windows-x64\   ← 程序
D:\MyResearchLibrary\           ← 你的数据
```

LAM 不会因为你在某个目录启动它就自动猜测文献库。最稳妥的做法是在每条命令中显式写 `--root`。

### 2.2 dry-run

`--dry-run` 表示预览业务操作。它不会提交 Catalogue、Documents、受管理文件、正式 snapshot 或 change log 的变更。

但 dry-run 不是“绝对不写磁盘”：为了审计和排错，它仍可能写入运行报告、invocation 日志、普通日志或 provider cache。不要把测试命令指向真实文献库，除非你确实是在预览该文献库的操作。

### 2.3 apply

`--apply` 表示实际执行修改。不过当前 CLI 有两种模式，必须分清：

| 命令类型 | 预览 | 实际执行 |
|---|---|---|
| 日常命令：`check`、`register`、`search`、`file` | 加 `--dry-run` | **去掉 `--dry-run`**；这些命令不接受 `--apply` |
| 维护命令：`init`、`delete`、`export zotero`、`review`、恢复、迁移、`cleanup` | 加 `--dry-run` | 明确加 `--apply` |
| 诊断命令：`status`、`doctor`、`commands` | 不适用 | 只诊断，没有 dry-run/apply |

因此，下面这条命令已经会执行登记：

```powershell
lam --root D:\MyResearchLibrary register
```

不要写 `lam register --apply`，因为当前 `register` 没有这个参数。

### 2.4 JSON status 与退出码

加 `--json` 后，LAM 会输出一个 JSON 对象。最重要的字段是 `status`、`exit_code`、`errors`、`warnings`、`report_path` 和 `details`。

常见状态：

- `success`：命令成功完成；通常退出码为 0。
- `needs_review`：安全执行已停止在需要人工判断的位置；通常退出码为 2。
- `no_changes`：命令正常运行，但没有需要修改的内容；通常退出码为 3。
- `failed`：操作失败；根据原因可能是配置、Catalogue、文件或网络错误。

`needs_review` 和 `no_changes` 可能使用非零退出码表达业务状态，不等于程序崩溃。在 PowerShell 中可用下面的命令查看刚才的退出码：

```powershell
$LASTEXITCODE
```

### 2.5 安全原则

- 不直接编辑 Documents sheet。
- Catalogue 中只手工维护用户字段：`manual_tags`、`topic_folder`、`notes`；需要记录人工决定时可保留并追加 `USER_CONFIRMED:` 说明。不要修改 `paper_uuid`。
- 不直接移动、改名或删除已经进入 Documents 的文件；使用 LAM 命令。
- 不要重复运行 `init`。已初始化的文献库使用 `status library` 和 `check`。
- 删除整篇论文使用 `delete`，不要只删除 Excel 行或只删除 PDF。
- 看到 `needs_review` 时，先看 JSON 的 `details` 和 `report_path`，不要不断重试。
- 应用命令前关闭正在打开的 `catalogue.xlsx`，避免 Excel 文件锁导致失败。
- 不要以管理员身份运行日常命令，也不要通过修改 ACL 来“修复”测试或临时目录问题。

## 3. 选择源码版还是发行版

| 项目 | 源码开发版 | Windows 发行版 |
|---|---|---|
| 目标用户 | 开发者、合作者 | 普通用户 |
| 需要 Python/Conda | 是 | 否 |
| 可以修改代码 | 是 | 否 |
| EasyOCR/Poppler | 开发环境配置 | 随发行目录提供 |
| 启动方式 | `lam` 或 `python -m lam` | `lam.exe` |
| 更新方式 | Git pull / 切换 tag | 解压新版本目录 |

如果只是管理自己的论文，优先选择 Windows 发行版。如果要修改代码、运行 pytest 或维护测试语料，使用源码版。

## 4. 源码开发版：创建环境

### 4.1 准备 Miniforge

安装 Miniforge，然后打开“Miniforge Prompt”，或在已经初始化 Conda 的 PowerShell 中操作。不要把源码克隆到真实文献库中。

假设源码位于 `D:\Projects\LAM`：

```powershell
Set-Location D:\Projects\LAM

conda env create -f environment.yml
conda activate lam-dev

python --version
python -m pip install -e .
```

当前 `environment.yml` 的实际环境名是 `lam-dev`，Python 必须是 3.14.x。该文件已经通过 `-e .[dev]` 安装 editable package 和开发依赖，所以最后一条 `pip install -e .` 通常只是再次确认源码入口。需要完整开发依赖时可明确运行：

```powershell
python -m pip install -e ".[dev]"
```

检查命令入口：

```powershell
lam --version
lam --help
```

如果 `lam` 找不到，先确认已经 `conda activate lam-dev`，再运行 editable install。也可临时使用：

```powershell
python -m lam --version
python -m lam --help
```

### 4.2 源码环境中的 OCR 与 Poppler

`environment.yml` 从 Conda 安装 Poppler；EasyOCR 由 Python 依赖安装。按照第 6 节初始化一次性测试库或新文献库后运行 doctor：

```powershell
lam --root D:\MyResearchLibrary doctor --json
```

如果源码模式缺少 EasyOCR 模型，并且你明确允许联网下载模型，可运行：

```powershell
lam --root D:\MyResearchLibrary doctor --initialize-ocr-models --json
```

这不是普通离线诊断：它可能下载 OCR 模型。Windows onedir 发行版应使用随包模型，不需要也不应依赖用户 EasyOCR cache。

### 4.3 开发者测试

在源码根目录运行：

```powershell
python -m pytest
python scripts/generate_cli_docs.py --check
python scripts/sync_package_templates.py --check
```

默认测试应离线、使用 pytest 临时目录，并且不得指向真实 library。不要设置真实 `LIBRARY_ROOT` 作为测试根，也不要把 pytest basetemp 放进真实文献库。

## 5. Windows onedir 发行版：解压和检查

### 5.1 解压完整目录

把 ZIP 解压到一个普通可写目录，例如：

```text
C:\LAM\LAM-0.6.1-windows-x64\
```

不要只复制 `lam.exe`。onedir 需要保持整个目录结构，至少应看到：

```text
lam.exe
_internal\
models\easyocr\
vendor\poppler\
AGENTS.md
Workflows.md
README.md
LICENSE
THIRD_PARTY_NOTICES.md
.env.example
setup-lam.bat
open-lam-terminal.bat
```

发行版不要求安装 Python、Conda、EasyOCR 或系统 Poppler，也不要求修改系统 PATH。

### 5.2 在 PowerShell 中启动

打开 PowerShell，进入发行目录：

```powershell
Set-Location C:\LAM\LAM-0.6.1-windows-x64
.\lam.exe --version
.\lam.exe --help
```

为减少重复输入，可以设置当前 PowerShell 窗口内的变量：

```powershell
$Lam = "C:\LAM\LAM-0.6.1-windows-x64\lam.exe"
& $Lam --version
& $Lam --help
```

`open-lam-terminal.bat` 会打开一个位于发行目录的 CMD 窗口，适合直接输入 `lam.exe --help`。本教程其余示例使用 PowerShell，因此不要把 `$Lam`、`$Library` 变量原样复制到 CMD。

`setup-lam.bat <新文献库目录>` 是初始化快捷方式，它会直接执行 `init --apply`。新人建议先按照下一节手工做一次 dry-run；只有在目标目录不存在或确定为空时才使用这个批处理。

### 5.3 验证发行资源

初始化新库后运行：

```powershell
& $Lam --root D:\MyResearchLibrary doctor --json
```

frozen doctor 应报告：处于 frozen 模式、bundle root 正确、EasyOCR 可导入、模型完整且禁止下载、Poppler 位于发行目录内，并且 `pdftoppm` / `pdftocairo` 可以执行。缺少这些资源时不要从别的机器随意拼 DLL 或模型；重新获取完整、校验过的发行包。

更新发行版时，把新版本解压到新的程序目录。不要把新旧 `_internal`、模型或 DLL 混在同一个目录。验证新 `lam.exe --version` 和 `doctor` 后，再让新程序指向原来的独立 library root。

### 5.4 可选的 provider 联系信息

PubMed 和 Unpaywall 等服务可能要求联系邮箱。最简单的临时配置是在当前 PowerShell 窗口设置环境变量：

```powershell
$env:NCBI_EMAIL = "you@example.com"
$env:UNPAYWALL_EMAIL = "you@example.com"
$env:CROSSREF_EMAIL = "you@example.com"
```

关闭该窗口后这些值会消失。需要持久配置时，可以在源码根目录或 `lam.exe` 所在目录创建本机 `.env`，只写实际需要的项目，例如：

```dotenv
NCBI_EMAIL=you@example.com
UNPAYWALL_EMAIL=you@example.com
CROSSREF_EMAIL=you@example.com
```

不要把 `.env` 放进 Git、发行 ZIP、测试语料或文献库备份，也不要把 API key 写进教程、命令历史或运行报告。若每条命令都显式传 `--root`，不必在 `.env` 中设置 `LIBRARY_ROOT`。可用下面的命令只查看“是否已配置”，不会在普通 JSON 中回显 secret：

```powershell
lam --root D:\MyResearchLibrary --json status config
```

发行版把开头替换为 `& $Lam`。

## 6. 第一次初始化文献库

下面同时适用于源码版和发行版。先选择一组命令前缀：

- 源码版：使用 `lam`
- 发行版：使用 `& $Lam`

以下以源码版写法为主。设置 library root：

```powershell
$Library = "D:\MyResearchLibrary"
```

目标目录必须不存在，或能够证明是空目录。先预览：

```powershell
lam --root $Library --json init --dry-run
```

确认路径正确且没有阻塞项后再初始化：

```powershell
lam --root $Library --json init --apply
```

初始化完成后检查：

```powershell
lam --root $Library --json doctor
lam --root $Library --json status library
lam --root $Library --json check --dry-run
```

若使用发行版，只需替换命令开头：

```powershell
& $Lam --root $Library --json init --dry-run
& $Lam --root $Library --json init --apply
& $Lam --root $Library --json doctor
```

以后不要再次执行 init。`status library` 用于查看当前状态；`check --dry-run` 用于预览 Catalogue 与文件系统之间的差异。

## 7. 第一次登记 PDF

### 7.1 把新 PDF 放入 Inbox

通过文件管理器，把待登记 PDF 复制到：

```text
D:\MyResearchLibrary\Inbox\
```

这一步是引入新的输入，不要把已经在 Registered 或 Topics 中受管理的文件手工搬回 Inbox。

### 7.2 先预览 register

```powershell
lam --root $Library --json register --dry-run
```

默认 `--ocr auto`：有可用文字层时优先使用本地文字；需要时才走 OCR 路径。登记可能访问 PubMed、Crossref、arXiv 或 Unpaywall 等 provider。若只想使用已有 cache：

```powershell
lam --root $Library --json register --dry-run --offline
```

`--offline` 不是“保证找到”，它只使用有效 cache。没有缓存时出现 unresolved 是正常的。

### 7.3 应用登记

如果 dry-run 是 `success` 或可接受的 `no_changes`，并且没有新的 review blocker，去掉 `--dry-run`：

```powershell
lam --root $Library --json register
```

注意：这里不能加 `--apply`。高置信度文件会被安全命名、写入 Catalogue/Documents，并从 Inbox 移到 Registered。不能安全确认的文件会留在 Inbox；LAM 可能记录 provisional custody，但不会因此声称论文身份已经确认。

如果返回 `needs_review`：

1. 查看 `report_path` 指向的报告和 JSON `details`；
2. 检查是否是元数据冲突、多个近似候选、文件名碰撞或 OCR 不足；
3. 保留文件原位；
4. 不要用不断 `--refresh` 或重复 apply 来替代人工判断。

## 8. 登记参考文献文本

普通 `register` 默认是 `--reference-text never`，会忽略 `.txt`。要处理从论文、网页或笔记复制出的参考文献列表，必须显式启用 reference-text 模式。

把文本保存为 UTF-8 文件，例如：

```text
D:\MyResearchLibrary\Inbox\references.txt
```

只处理这一个文本文件：

```powershell
$Refs = Join-Path $Library "Inbox\references.txt"

lam --root $Library --json register `
  --reference-text only `
  --reference-file $Refs `
  --dry-run
```

确认 candidate 数量、解析出的题名/作者/年份、provider 结果和 unresolved 项后，去掉 `--dry-run`：

```powershell
lam --root $Library --json register `
  --reference-text only `
  --reference-file $Refs
```

成功的文本记录会进入 Catalogue，但源 `.txt` 本身不创建 Documents 行。完成处理的文本会进入 `Imports/ReferenceText/Processed/`；仍有未解决项时通常留在 Inbox。

如果同时处理识别出的参考文献文本和 Inbox PDF，可用 `--reference-text auto`。如果希望对已确认的引用尝试获取公开可下载 PDF，可先 dry-run 检查：

```powershell
lam --root $Library --json register `
  --reference-text only `
  --reference-file $Refs `
  --download-missing `
  --dry-run
```

LAM 只使用允许的公开来源，不绕过付费墙。`--require-download` 会把“没有下载到 PDF”视为更严格的失败条件，第一次使用时不要随意加。

## 9. 编辑主题并归档到 Topics

登记后，PDF 通常位于 Registered。LAM 不会替用户决定最终主题；你需要在 `catalogue.xlsx` 的 Catalogue sheet 中填写 `topic_folder`。

例如希望文件进入：

```text
D:\MyResearchLibrary\Topics\Immunology\Interferon\
```

则 `topic_folder` 填：

```text
Immunology/Interferon
```

不要填 `Topics/Immunology/Interferon`，不要填绝对路径，也不要使用 `..`。你还可以编辑 `manual_tags` 和 `notes`。不要改 Documents、`paper_uuid` 或机器维护的路径/哈希字段。

保存并关闭 Excel 后，先预览 filing：

```powershell
lam --root $Library --json file --dry-run
```

确认目标路径和移动计划后应用：

```powershell
lam --root $Library --json file
```

这里同样不使用 `--apply`。成功后文件进入 `Topics/<topic_folder>/`，Documents 路径随之更新。最后检查：

```powershell
lam --root $Library --json check --dry-run
```

若需要让 check 提交客观可确定的 Catalogue/Documents 状态更新，先检查预览，再运行：

```powershell
lam --root $Library --json check
```

## 10. 搜索、补全元数据和公开下载

`search` 可以按 PMID、DOI、arXiv ID、题名、paper UUID、Catalogue 行号或批量缺失状态查询。先 dry-run，例如：

```powershell
lam --root $Library --json search `
  --doi "10.1234/example" `
  --dry-run
```

若候选唯一且高置信度，去掉 dry-run：

```powershell
lam --root $Library --json search `
  --doi "10.1234/example"
```

按题名或补全已有记录：

```powershell
lam --root $Library --json search --title "Exact paper title" --dry-run
lam --root $Library --json search --missing-metadata --max-records 25 --dry-run
```

需要明确尝试公开下载时使用 `--download`：

```powershell
lam --root $Library --json search `
  --paper-uuid "<paper_uuid>" `
  --download `
  --dry-run
```

下载成功的文件进入 Inbox，之后仍应走 register。不要把一次近似题名查询的第一条 provider 结果当作已确认论文；`ambiguous` 或 `needs_review` 应由人工检查。

provider cache 参数：

- `--offline`：只读有效 cache，不联网；
- `--refresh`：忽略有效 cache，重新查询 provider；
- `--no-cache-write`：不写 provider cache 或持久 quota 计数。

`--refresh` 不是提高匹配置信度的开关，只用于确实需要刷新 provider 结果时。

## 11. review、status 与 Zotero 导出

### 11.1 查看状态

```powershell
lam --root $Library --json status library
lam --root $Library --json status recovery
lam --root $Library --json status config
lam --root $Library --json doctor
lam --root $Library --json commands
```

需要看某条命令的当前参数时，以 help 为准：

```powershell
lam register --help
lam export zotero --help
```

### 11.2 重新检查已解决的 blocker

`review` 只能清除已经有客观证据解决的机器 blocker，不能替用户创造确认或改写用户字段。先预览全部记录：

```powershell
lam --root $Library --json review --all --dry-run
```

确认后：

```powershell
lam --root $Library --json review --all --apply
```

也可以用 `--paper-uuid` 或 `--document-id` 限定对象。

### 11.3 导出到 Zotero

LAM 生成 Zotero 可导入的 NBIB 或 PubMed XML 文件，但不会直接修改 Zotero。导出不改变 Catalogue、Documents、PDF 或 Topics。

预览全部已登记记录的 NBIB 导出：

```powershell
lam --root $Library --json export zotero `
  --all `
  --format nbib `
  --dry-run
```

应用：

```powershell
lam --root $Library --json export zotero `
  --all `
  --format nbib `
  --apply
```

默认导出位于 library root 的 `Exports/Zotero/`。也可以用 `--paper-uuid` 或 `--topic-folder` 选择子集，或用 `--output` 指定 LAM 拥有的输出文件。

## 12. 删除、恢复和 cleanup

### 12.1 删除完整论文实体

不要只删除 Excel 行或 PDF。先从 Catalogue 找到 `paper_uuid`，然后：

```powershell
$Paper = "<paper_uuid>"

lam --root $Library --json delete `
  --paper-uuid $Paper `
  --dry-run
```

确认预览包含正确的 Catalogue 行、Documents 行和所有受管文件后，由用户执行：

```powershell
lam --root $Library --json delete `
  --paper-uuid $Paper `
  --apply
```

这会把完整实体移入可恢复的 LAM trash。Agent caller 被禁止执行 `delete --apply`。

### 12.2 查看和恢复 trash

```powershell
lam --root $Library --json recover --list-trash
```

根据列表中的 trash ID 先预览恢复：

```powershell
lam --root $Library --json recover `
  --trash-id "<trash-id>" `
  --dry-run
```

确认没有路径或 UUID 冲突后：

```powershell
lam --root $Library --json recover `
  --trash-id "<trash-id>" `
  --apply
```

### 12.3 清理机器生成的过期文件

普通 cleanup 只处理严格 allowlist 中的机器 artifact：

```powershell
lam --root $Library --json cleanup --dry-run
lam --root $Library --json cleanup --apply
```

永久清除过期 trash 必须额外写明：

```powershell
lam --root $Library --json cleanup `
  --purge-trash `
  --older-than 30d `
  --dry-run
```

确认后才把 `--dry-run` 改成 `--apply`。这一步不可逆；不要把它和普通日常 cleanup 混为一谈。

## 13. 以后让 Agent 使用 LAM

建议先亲自完成一次“init → Inbox → register → 填 topic_folder → file → check”，理解纯 CLI 如何工作，再让 Agent 代为选择命令。

初始化生成的 `AGENTS.md` 和 `Workflows.md` 是 Agent 的操作规则。Agent 应当：

- 只通过公开 `lam` CLI 工作；
- 每次调用明确传 `--caller agent`；
- 对可能修改业务状态的相同 selection 先 dry-run；
- 不直接编辑 Catalogue/Documents，不直接搬动或删除文件；
- 不读取 `summary.md`；
- 遇到 `needs_review` 时停止受影响操作并报告；
- 保留 Catalogue 中的 `USER_CONFIRMED:` 内容；
- 不执行 `delete --apply`。

例如 Agent 预览登记：

```powershell
lam --root $Library --caller agent --json register --dry-run
```

日常命令的 Agent apply 仍然是去掉 `--dry-run`，不是添加 `--apply`。是否允许 apply 还取决于用户请求、dry-run 结果和 `AGENTS.md` 的安全规则。

## 14. 常见问题

### `lam` 不是可识别的命令

- 源码版：运行 `conda activate lam-dev`，确认 editable install；或用 `python -m lam`。
- 发行版：进入解压目录后使用 `.\lam.exe`，或用绝对路径 `& "C:\...\lam.exe"`。
- 不必为发行版修改系统 PATH。

### “unrecognized arguments: --apply”

你很可能对 `check`、`register`、`search` 或 `file` 使用了 `--apply`。这些日常命令先加 `--dry-run` 预览，然后去掉该参数执行。

### `no_changes` 但退出码不是 0

这是正常业务状态。`no_changes` 通常是退出码 3，表示命令成功检查后没有要改的内容。

### `needs_review`

这是安全停顿，不是让你盲目重试。查看 `errors`、`warnings`、`details` 和 `report_path`，处理具体冲突后再用 `review` 或原命令的 dry-run 检查。

### Catalogue 无法写入

关闭 Excel 和任何正在预览 `catalogue.xlsx` 的程序后重试 dry-run。不要通过杀进程、修改 ACL、管理员运行或手工重建 Documents 来绕过文件锁。

### image-only PDF 无法 OCR

先运行 `doctor --json`。源码版检查 Conda Poppler 和 EasyOCR 模型；发行版检查完整的 `models/easyocr/` 与 `vendor/poppler/`。不要从不明来源替换模型或 DLL。

### reference text 没有被处理

默认是 `--reference-text never`。使用 `--reference-text auto` 或 `only`；若提供 `--reference-file`，必须同时启用其中一种模式。

### 已经初始化，是否再运行 init

不要。运行：

```powershell
lam --root $Library --json status library
lam --root $Library --json check --dry-run
```

### 如何确认当前 CLI，而不是相信旧教程

```powershell
lam --root $Library --json commands
lam <命令> --help
```

完整生成式参考见 `docs/CLI_COMMANDS.md`。代码、registry 和实际 help 高于旧文档。

## 15. 一次完整的最小日常流程

下面是最值得记住的一组命令：

```powershell
$Library = "D:\MyResearchLibrary"

# 只在全新、空的目标上执行一次
lam --root $Library --json init --dry-run
lam --root $Library --json init --apply

# 把新 PDF 复制到 Inbox 后
lam --root $Library --json register --dry-run
lam --root $Library --json register

# 在 Catalogue sheet 填好 topic_folder，保存并关闭 Excel 后
lam --root $Library --json file --dry-run
lam --root $Library --json file

# 最后检查
lam --root $Library --json check --dry-run

# 需要时导出 Zotero
lam --root $Library --json export zotero --all --format nbib --dry-run
lam --root $Library --json export zotero --all --format nbib --apply
```

遇到任何不确定项，保留文件原位、保留现有 Catalogue 内容、阅读报告，再决定下一步。LAM 的目标不是“尽可能自动改”，而是在可追踪、可恢复的边界内完成高置信度操作。
