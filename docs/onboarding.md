# Scriptorium 实验室上手指南

本页面向第一次使用 Scriptorium 的实验室成员。按顺序做完下面六步，大约需要半小时，不包括模型审稿的时间。
命令、输出规则和恢复方法以英文文档为准：[README](../README.md)、[configuration](configuration.md)、
[operations](operations.md)。

## 它是什么，不是什么

- 一个本机命令行工具：把 LaTeX 稿件的某个 Git commit 冻结下来，交给你自己的 Codex、Claude Code 或
  Antigravity 会话做结构化审稿，再由你决定发现、批准修改计划；主笔按计划核对补丁后，由你批准补丁并显式应用。
  每个决定都有人工关卡。
- 它不选模型、不调模型、不付费。模型和费用都在你自己的 CLI 订阅或账户上。
- 它不是自动改稿机。模型声明的 claim check 和 limitations 是供人检查的记录，不等于科学正确。
- 它只读已提交的内容，未提交的改动不会进入审稿。

## 第一步：检查机器

在任意目录运行仓库自带的检查脚本，把 `/path/to/scriptorium` 换成你克隆的位置：

```bash
bash /path/to/scriptorium/utils/preflight.sh
```

它检查 Python 3.10+、pip、Git、`latexmk`、`kpsewhich` 及其 TeX 根目录、至少一个 LaTeX 引擎，并用
Scriptorium 构建时相同的 `latexmk` 参数真实编译一个小文档。全部 `ok` 再继续；有 `FAIL` 先按提示装齐
TeX Live 或 MacTeX；`warn` 行只是提醒。Windows 不在脚本覆盖范围内，请在 WSL 里操作。

## 第二步：安装

把工具装进模型 CLI 实际使用的那个 Python 环境。不确定是哪一个时，先用 `PYTHON=` 指定解释器跑一次检查，
再用同一个解释器安装：

```bash
PYTHON=/path/to/python bash /path/to/scriptorium/utils/preflight.sh
/path/to/python -m pip install /path/to/scriptorium
```

装完再跑一次 `preflight.sh`，确认 `scriptorium`、`scriptorium_cli` 和 `pdf_rendering` 三行为 `ok`。
`scriptorium_cli` 失败说明命令不在 PATH 里，按提示把该解释器的 scripts 目录加进 PATH。

## 第三步：让模型客户端加载 skill

模型按两份 skill 操作 CLI：

- [skills/scriptorium](../skills/scriptorium/SKILL.md)：操作者，负责和你商定审稿 brief、开始运行、读报告、按你的指示记录决定，
  并作为主笔和你商定修改计划、核对补丁；
- [skills/scriptorium-review](../skills/scriptorium-review/SKILL.md)：审稿者、写作者或核对者，负责 claim 任务、导出并阅读冻结的
  任务、引用证据、改写段落或给出核对结论、提交结果。

把这两个目录复制或软链接到客户端的 skills 目录：

- Claude Code：`~/.claude/skills/scriptorium/` 和 `~/.claude/skills/scriptorium-review/`
- Codex：`~/.codex/skills/scriptorium/` 和 `~/.codex/skills/scriptorium-review/`
- Antigravity（命令行工具名为 `agy`）：按其文档的 skills 位置放置这两个目录

重启客户端后，在对话里提到 Scriptorium 时它应能引用这两份 skill。

## 第四步：准备稿件仓库

稿件必须在 Git 仓库里，还没有的话先 `git init` 并提交全部源文件。在仓库根目录运行下面的命令，
`--engine` 用 preflight `compile` 一行报告可用的引擎（`pdflatex`、`xelatex` 或 `lualatex`）：

```bash
scriptorium init . --main main.tex --engine pdflatex
git add scriptorium.toml .gitignore
git commit -m "Add Scriptorium configuration"
scriptorium --json doctor --revision HEAD --profile quick
```

`doctor` 会用已提交的版本真实编译一次，全部检查通过时返回 `ok: true`。常见失败原因：主文件名不对、
图片或参考文献文件没有提交、引擎没装。改好后重新提交再跑。有独立编译的补充材料时，在 `scriptorium.toml`
的 `[manuscript]` 下加 `supplements`，见 [configuration](configuration.md#supplements)。

## 第五步：第一次审稿

先用 `quick` profile，任务少、花费低。想让审稿有针对性时，先在模型客户端里按操作者 skill 和你商定一份审稿
brief，保存为 `brief.json`，在下面的 `run start` 后加 `--brief brief.json`（格式见
[configuration](configuration.md#review-brief)）。运行开始后 brief 不能再改。

```bash
scriptorium --json run start --revision HEAD --profile quick
scriptorium --json run status RUN_ID
```

然后在稿件仓库目录里打开模型客户端，开一个新对话，告诉它 run ID，让它按 skill 执行 `run status` 给出的
`next_actions`。模型会 claim 任务，用 `task export` 把冻结的稿件和任务说明导出到一个目录，用自己的工具阅读，
再检查并提交结构化结果。

## 第六步：做决定

模型在人工关卡停下时，由你决定：

- `finding decide`：确认、拒绝或豁免发现。可以一次列出同一运行的多个发现 ID，共用同一个决定和理由。
  之后运行 `run resume`：至少有一条确认的发现时进入 `planning`；一条都没有确认时运行直接完成。不想改的发现请豁免（`--waive`）。
- 修改计划：操作者作为主笔和你商定论文要讲的故事，写成一个目录：`plan.md` 写整体思路，`assignments/` 下每个
  `NAME.md` 是一项写作任务，开头列出要改写的行范围（`target: 路径:起始行-结束行`）和对应的发现
  （`findings: ID, ID`），后面是改写要求（格式见 [operations](operations.md#the-plan-directory)）。每条确认的发现
  都必须分配到某项任务。`plan check` 检查计划并显示每个范围的首行和末行，确认无误后由你同意，操作者再运行
  `plan approve`。
- 写作任务：每项分配生成一个写作任务，可以交给另一个模型会话（例如 Antigravity）。写作者只改写导出目录里
  `task/passages/` 下的段落文件，用 `task submit --dir` 提交；它不写 JSON，也不写行号，Scriptorium 自己生成精确修改
  并编译。所有任务都通过后，运行合成一个候选补丁，进入 `checking`。
- 核对任务：主笔在一个没有做过本运行写作的宿主会话里 claim 核对任务，按批准的计划逐项检查补丁，每项分配给出
  `accept` 或 `redo`，`redo` 必须附上写给写作者的反馈。被退回的分配会生成重写任务，导出的段落文件是上一稿的文字；
  其余分配保留原稿。核对检查补丁是否执行了计划，不是重新审稿；方向由你对计划和补丁的批准把关。用做过本运行写作的
  会话，或没有宿主会话 ID 时，核对提交会被拒绝。
- `patch decide`：核对接受全部分配后，主笔把差异、核对结论和事实变更清单（`patch show` 的 `facts`）给你看，由你批准
  或拒绝补丁，然后 `run resume`。事实变更清单比较修改前后导航索引中的数值、引用、交叉引用、标签和插图，以及各文档
  页数；导航索引是启发式的，所以清单只是阅读辅助，不是证据，清单为空也不说明没有事实改变，任何关卡都不依赖它。
  批准后进入 `ready_to_apply`；拒绝会回到 `planning`，由主笔读你的理由（`patch show`）并和你修改计划。
- `patch apply` 把批准的补丁写入工作区，`run gate` 查看放行结论。

随时用 `scriptorium --json run status RUN_ID` 看当前位置，用
`scriptorium run report RUN_ID --format markdown > review.md` 导出可读报告。

## 注意事项

- 运行记录在仓库的 `.scriptorium/` 下（每个运行一个状态文件 `runs/RUN_ID/state.json`），已被 `.gitignore`
  忽略，只存在于你的机器上。需要和导师或合作者分享时，导出 Markdown 报告放到约定位置。
- 不要手动编辑 `.scriptorium/` 里的任何文件。出错时修好外部原因，再用 `run retry` 或 `run resume` 继续。
- 模型提交被拒会附带校验报告。`run retry RUN_ID --task TASK_ID` 让任务可以重新 claim，新一轮会把诊断信息冻结进提示里。
- 同一个 clone 不要多人同时操作，合作者各自在自己的 clone 上运行。

## 反馈

遇到 `doctor` 失败、编译问题或提交被反复拒绝，把 `run status` 和 `run report` 的输出一起发给维护者。
记录下来的摩擦点会用于更新本页。
