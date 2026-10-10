# Scriptorium 实验室上手指南

本页面向第一次使用 Scriptorium 的实验室成员。按顺序做完下面六步，大约需要半小时，
不包括第一次审稿的模型运行时间。英文的完整契约见 [README](../README.md)、
[configuration](configuration.md) 和 [operations](operations.md)。

## 它是什么，不是什么

- 它是一个本机命令行工具，把 LaTeX 稿件的某个 Git commit 冻结下来，交给你自己的
  Codex、Claude Code 或 Antigravity 会话做结构化审稿，再由你逐条决定、批准补丁、
  独立验证、显式应用。每一步都有人工关卡。
- 它不选模型、不调模型、不付费。模型和费用都在你自己的 CLI 订阅或账户上。
- 它不是自动改稿机。模型声明的 claim check 和 scope 只是供人检查的记录，不等于科学正确。
- 它只读已提交的内容。未提交的改动永远不会进入审稿。

## 第一步：检查机器

在任意目录运行仓库自带的检查脚本，把 `/path/to/scriptorium` 换成你克隆的位置：

```bash
bash /path/to/scriptorium/utils/preflight.sh
```

它检查 Python 3.10+、pip、Git、`latexmk`、`kpsewhich` 及其 TeX 根目录、至少一个 LaTeX 引擎，
并用 Scriptorium 构建时相同的 `latexmk` 参数真实编译一个小文档。全部 `ok` 再继续。有 `FAIL` 先按提示装齐 TeX Live 或 MacTeX。`warn` 行是提醒，
不阻塞。Windows 不在脚本覆盖范围内，请在 WSL 里操作。

## 第二步：安装

把工具装进你的模型 CLI 实际使用的那个 Python 环境里。不确定是哪一个时，
用 `PYTHON=/path/to/python bash /path/to/scriptorium/utils/preflight.sh` 指定解释器再看结果，
然后用同一个解释器安装：

```bash
/path/to/python -m pip install /path/to/scriptorium
```

装完再跑一次 `preflight.sh`，确认 `scriptorium` 和 `pdf_rendering` 两行为 `ok`。

## 第三步：让模型客户端加载 skill

模型按 [skills/scriptorium/SKILL.md](../skills/scriptorium/SKILL.md) 里的说明操作 CLI。
把整个 `skills/scriptorium/` 目录复制或软链接到客户端的 skills 目录：

- Claude Code：`~/.claude/skills/scriptorium/`
- Codex：`~/.codex/skills/scriptorium/`
- Antigravity（命令行工具名为 `agy`）：按其文档的 skills 位置放置同一目录

重启客户端后，在对话里提到 Scriptorium 时它应能引用这份 skill。

## 第四步：准备稿件仓库

稿件必须在 Git 仓库里。还没有的话先 `git init` 并提交全部源文件。然后在仓库根目录运行下面的命令，
把 `--engine` 换成 preflight `compile` 一行里报告可用的引擎（`pdflatex`、`xelatex` 或 `lualatex`）：

```bash
scriptorium init . --main main.tex --engine pdflatex
git add scriptorium.toml .gitignore
git commit -m "Add Scriptorium configuration"
scriptorium --json doctor --revision HEAD --profile quick
```

`doctor` 会用冻结的 commit 真实编译一次。常见失败原因：主文件名不对、图片或参考文献
文件没有提交、引擎不匹配。改好后重新提交再跑，直到 `ok` 为 `true`。
有独立的补充材料时，在 `scriptorium.toml` 的 `[manuscript]` 下加 `supplements`，见
[configuration](configuration.md)。

## 第五步：第一次审稿

先用 `quick` profile，任务少、花费低：

```bash
scriptorium --json run start --revision HEAD --profile quick
scriptorium --json run status RUN_ID
```

然后打开你的模型客户端，在稿件仓库目录里开一个新对话，告诉它 run ID，让它按 skill
执行 `run status` 给出的 `next_actions`。模型会 claim 任务、读取冻结材料、提交结构化结果。
你只需要在它停下时回到终端：

- `finding decide` 逐条确认、拒绝或豁免发现；
- `run resume`：至少有一条确认的发现时进入修订任务，模型产出候选补丁；
  一条都没有确认时运行直接完成，后面的补丁和验证步骤不会出现；
- `patch decide` 批准或拒绝补丁，然后 `run resume`：批准会生成验证任务，
  拒绝会带着你的理由回到修订任务，模型重新产出补丁；
- 验证任务必须在一个全新的对话里做，否则结果记为 inconclusive；
- `patch apply` 把验证通过的补丁写入工作区，`run gate` 查看放行结论。

随时用 `scriptorium --json run status RUN_ID` 看当前位置，用
`scriptorium run report RUN_ID --format markdown > review.md` 导出可读报告。

## 预期与注意事项

- 时间和费用：以 `quick` profile 审一篇 10 页左右的稿件为参考，填入试点实测数据后更新本节。
- 运行记录在仓库的 `.scriptorium/` 下，已被 `.gitignore` 忽略，只存在于你的机器上。
  需要和导师或合作者分享时，导出 Markdown 报告放到约定位置。
- 不要手动编辑 `.scriptorium/` 里的任何文件。出错时修好外部原因，再用 `run retry`、
  `run resume` 或 `run continue` 继续。
- 模型提交被拒会附带校验报告。`run retry RUN_ID --task TASK_ID` 让任务可以重新 claim，
  新一轮会把诊断信息冻结进提示里。
- 同一个 clone 不要多人同时操作。合作者各自在自己的 clone 上运行。

## 反馈

遇到 `doctor` 失败、编译问题或提交被反复拒绝，把 `run status` 和 `run report` 的输出一起发给
维护者。记录下来的摩擦点会用于更新本页。
