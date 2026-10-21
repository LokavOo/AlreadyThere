# 观者自在 (AlreadyThere)

**多模型互查程序 · v2.11**

**A multi-model cross-verification program · v2.11**

且待云开见月明

[中文](#中文) · [English](#english)

---

## 中文

### 这是什么

让几个不同厂商的大模型分工合作，互相核对：

- **生产者**各自独立作答，互相看不到对方的答案；
- **审查者**不做题，只核对、比对每份答卷，并按答卷写的验证方案自己独立再验证一遍；
- **找茬者**专门找各方可能共同漏掉的地方。

凡是程序能核对的，一律由程序核对，不靠模型自觉，例如：答案是否一致、模型写的验证代码实际运行的结果、引用的原句是否真的在原文里、日期换算。只有各方真的一致才算闭合，不按多数票；到轮数上限仍不一致，就如实标为"有争议"，交给人裁决。全过程写进一本不能悄悄修改的账本（哈希链），每次模型调用的完整输入、原始返回和思考过程都存档。

这个程序要防的是：单个模型可能出错、但本来不该出错的地方。它不扩展大模型的能力边界，也不保证能消除所有幻觉。

"观者"指我们想要的答案，"自在"的主要意思是**本来就在**：要找的东西本来存在，只是被遮住了；也指不被幻觉和谄媚牵引。

### 能做什么

- **数学最终答案题**（2.x）：生产者作答并写验证代码，审查者逐份审查、独立验证，再把几份答卷放在一起对照，找出相互冲突的地方；意见发回生产者核实后修改或反驳，一轮一轮迭代。答案分三态（得出／部分得出／未得出），"不知道"是合法答案。标准答案只在流程全部结束后由程序比对，从不出现在任何提示词里。
- **资料整理**（1.x）：例如查人物的出生日期、某个奖项的历届得主，多方检索、逐条核对原文，并要求有独立来源支持。
- **窗口**：群聊式界面；运行中可以插话、打断、暂停后补充再继续；每个模型的思考过程实时可见；"解说者"边栏用大白话说明程序正在做什么；显示用时（停下的时间不算）；检测思考里的死循环（自动掐断，先续写，不成再从头重试一次）。

### 测试结果

**2.x：AIME 2026 试跑**（程序 v2.10.0，2026-10-21）

| 题 | 标准答案 | 第 1 轮答案（甲／乙／丙） | 最终 |
|---|---|---|---|
| AIME 2026 I 第 11 题 | 896 | 896／896／896 | 第 1 轮闭合，896 |
| 第 12 题 | 161 | 161／161／161 | 第 1 轮闭合，161 |
| 第 13 题 | 39 | 40／39／39 | 第 2 轮闭合，39（两位审查者都指出甲的错误，甲核实后改正） |
| 第 14 题 | 681 | 681／681／681 | 运行被用户中途停止，未进入审查 |
| 第 15 题 | 83 | 672／384／未完成 | 运行被用户中途停止，未进入审查 |

- 阵容：生产者甲＝Kimi K3、乙＝DeepSeek V4.1-Flash、丙＝腾讯混元 hy4-preview；审查者千问 Qwen3.8-Max、智谱 GLM-5.3；找茬者豆包 Seed 2.1 Pro。全部开启深度思考，允许运行模型写的代码。用的是各家的商业 API，不和公开榜单的数字直接比较。
- 每题只跑了一次，共 5 题，其中 2 题没有走完流程。第 15 题里，乙写了一段验证代码但没有运行，交的答案是 384；程序运行这段代码，输出 83，与它自己的断言不符，程序已把这一点记为"作者验证未通过"。
- 过程、用量和运行中发现的程序问题见 [`测试记录/2.10_AIME2026试跑/测试总结.md`](测试记录/2.10_AIME2026试跑/测试总结.md)。

**1.x：资料整理**

| 版本 | 题目 | 生产者记忆模式 | 轮次 | 查到 | 查到中的错误 |
|---|---|---|---|---|---|
| 1.5 | 明清 28 位皇帝的出生日期与生肖 | 全局 | 6 | 18 | 0 |
| 1.5 | 同上 | 全新 | 2 | 16 | 0 |
| 1.6 | 同上 | 全新 | 3 | 16 | 0 |
| 1.8 | 2016—2025 年诺贝尔奖六个奖项的得主与获奖原因（60 条） | 全新 | 6 | 7 | 0 |

- 生产者 DeepSeek（deepseek-v4-pro）与 Kimi（kimi-k2.6），审查者豆包（doubao-seed-2-1-pro-260915），三家均关闭深度思考；检索用 Tavily。每组只跑了一次。详见 [`测试记录/`](测试记录/)。

### 安全说明（请先读）

- **模型写的代码会在你的电脑上运行（默认关，要在参数面板里打开）。** 运行器只做了这些：单独的进程、限时、限输出长度、在临时文件夹里运行、不把密钥传给它。**这不是安全隔离**，代码理论上能读写你电脑上的文件，包括密钥文件。
- **密钥是明文保存的**，放在数据文件夹的 `密钥.env` 里。程序只读取它，界面上只显示"已填／未填"，每个密钥只发给它对应的服务（模型接口、检索服务）；但这个文件被别人拷走，就能直接看到你的密钥。
- 运行时，题目和相关材料会发给你在设置里配置的各家模型接口；账本、项目文件都存在你自己的电脑上。

### 怎么用（Windows）

- **不装 Python：** 在 [Releases](../../releases) 下载 `AlreadyThere-…-windows.zip`，解压后双击 `AlreadyThere.exe`。这个 exe 没有做代码签名，Windows 可能提示"Windows 已保护你的电脑"，点"更多信息"→"仍要运行"。
- **已装 Python 3.9 或更新的版本（如 Anaconda）：** 下载源码，双击 `启动观者自在.bat`，或在程序文件夹里运行 `python -m alreadythere app`。
- 先点"连通测试"；没有密钥也可以新建"演示"项目，不联网、不花钱。
- 模型接口的密钥要自己申请，填在数据文件夹的 `密钥.env` 里。详见 [`使用说明.md`](使用说明.md)。
- 只用 Python 标准库；在 Linux、macOS 上可以用命令行运行，窗口在默认浏览器里打开。

### 还没做的

见 [`待实现.md`](待实现.md)（准入门与证书等级、代码运行器隔离、密钥加密、资料整理流程重做等）。

### 相关记录

- 设计文档与迭代记录（Zenodo 记录 A，CC BY 4.0）：[doi.org/10.5281/zenodo.22938084](https://doi.org/10.5281/zenodo.22938084)。记录 A 反映的是 1.x 时的设计；2.x 的设计变化写在本仓库的 [`迭代记录.md`](迭代记录.md) 里。
- 本程序的存档（Zenodo 记录 B，所有版本）：[doi.org/10.5281/zenodo.22969156](https://doi.org/10.5281/zenodo.22969156)

### AI 协作声明

- 设计思想是在作者与 DeepSeek、豆包、Claude、ChatGPT 的长期对话中形成的；部分设计问题（例如死循环的判定方法）另外征求过多家模型的意见，原文存档。所有取舍和最终决定由作者做出。
- 程序代码由 Claude（Anthropic）编写。
- 在实跑中担任程序角色的模型：1.x 为 DeepSeek、Kimi（生产者）和豆包（审查者）；2.x 为 Kimi、DeepSeek、腾讯混元（生产者），千问、智谱 GLM（审查者）和豆包（找茬者）。
- 模型不列为作者。

### 许可

- 代码：Apache-2.0（见 LICENSE）。作者：LokavOo-。
- 农历换算用的历表数据：法鼓文理学院（DILA，原 DDBC）时间规范资料库，CC BY-SA 3.0，在 `alreadythere/data/` 里。
- `测试记录/2.10_AIME2026试跑/` 里的题目与标准答案：来自 MathArena 的 `aime_2026` 数据集，CC BY-NC-SA 4.0；原题版权归美国数学协会（MAA）。不适用 Apache-2.0。

### 名称声明

"观者自在""AlreadyThere"是本项目的中文名和英文名，"且待云开见月明"是本项目的标语。本项目与任何同名或名称相似的第三方均无关联，也未获得其授权或认可。

---

## English

### What it is

Several large language models from different vendors divide the work and check each other:

- **Producers** answer independently and cannot see each other's answers.
- **Reviewers** do not solve the problem. They check and compare every answer sheet and independently verify each sheet again by following the verification method it describes.
- A **fault-finder** looks specifically for blind spots that all parties may share.

Everything the program can check, it checks itself, rather than trusting the models to check themselves: whether answers agree, what the models' verification code actually outputs when run, whether a quoted sentence really appears in its source, date conversions. A question is closed only when all parties genuinely agree — never by majority vote. If they still disagree when the round limit is reached, the question is honestly marked "disputed" and left for a human to decide. The whole process is written to a ledger that cannot be altered silently (a hash chain); the full input, raw response and reasoning of every model call are archived.

The program is meant to guard against errors that a single model may make but should not make. It does not extend what large language models can do, and it does not guarantee that all hallucinations will be eliminated.

In the Chinese name 观者自在, 观者 refers to the answer we are looking for, and 自在 mainly means "already there": what we are looking for already exists, only hidden from view. 自在 also means not being led astray by hallucination or flattery.

### What it can do

- **Math problems with a final answer** (2.x): producers answer and write verification code; reviewers review each sheet one by one, verify independently, then compare the sheets side by side to find conflicts between them. Comments go back to the producers, who check them and either revise or rebut, round after round. Answers have three states (solved / partly solved / not solved), and "I don't know" is a valid answer. The reference answer is compared by the program only after the whole process has finished; it never appears in any prompt.
- **Fact compilation** (1.x): for example, birth dates of people or the past laureates of a prize. Several parties search independently, every entry is checked against the source text, and an independent source is required to corroborate it.
- **The window**: a group-chat-style interface. While a run is in progress you can add information or instructions, interrupt it, or pause it, add more, and then continue. Each model's reasoning can be watched live. An "explainer" side panel describes in plain language what the program is doing. Run time is shown (time spent stopped is not counted). Looping in a model's reasoning is detected: the call is cut off automatically, the model is first asked to finish its answer from the reasoning it has already produced, and if that does not work, the call is retried once from scratch.

### Test results

**2.x: AIME 2026 trial runs** (program v2.10.0, 2026-10-21)

| Problem | Reference answer | Round 1 answers (A / B / C) | Final |
|---|---|---|---|
| AIME 2026 I, Problem 11 | 896 | 896 / 896 / 896 | Closed in round 1: 896 |
| Problem 12 | 161 | 161 / 161 / 161 | Closed in round 1: 161 |
| Problem 13 | 39 | 40 / 39 / 39 | Closed in round 2: 39 (both reviewers pointed out A's error; A checked and corrected it) |
| Problem 14 | 681 | 681 / 681 / 681 | Run stopped partway by the user; not reviewed |
| Problem 15 | 83 | 672 / 384 / not finished | Run stopped partway by the user; not reviewed |

- Line-up: producers A = Kimi K3, B = DeepSeek V4.1-Flash, C = Tencent Hunyuan hy4-preview; reviewers Qwen3.8-Max and Zhipu GLM-5.3; fault-finder Doubao Seed 2.1 Pro. Deep thinking was on for all of them, and running model-written code was allowed. These are the vendors' commercial APIs; we do not compare these results directly with the figures on public leaderboards.
- Each problem was run once: 5 problems in total, 2 of which did not complete the process. In Problem 15, B wrote verification code but did not run it, and submitted 384. The program ran the code: it printed 83, which contradicted the assertion in B's own code. The program recorded this as "verification by the answer's author failed".
- Process details, usage and the program issues found during the runs: [`测试记录/2.10_AIME2026试跑/测试总结.md`](测试记录/2.10_AIME2026试跑/测试总结.md) (test summary, in Chinese).

**1.x: fact compilation**

| Version | Task | Producer memory mode | Rounds | Found | Errors among entries "found" |
|---|---|---|---|---|---|
| 1.5 | Birth dates and Chinese zodiac animals of the 28 Ming and Qing emperors | global | 6 | 18 | 0 |
| 1.5 | same | fresh | 2 | 16 | 0 |
| 1.6 | same | fresh | 3 | 16 | 0 |
| 1.8 | Laureates and prize motivations of six Nobel Prize categories, 2016–2025 (60 entries) | fresh | 6 | 7 | 0 |

- Producers DeepSeek (deepseek-v4-pro) and Kimi (kimi-k2.6), reviewer Doubao (doubao-seed-2-1-pro-260915), deep thinking off for all three; search via Tavily. Each configuration was run only once. See [`测试记录/`](测试记录/) (test records, in Chinese).

### Security notes (please read first)

- **Code written by the models runs on your computer** (off by default; turn it on in the parameter panel). The runner does only the following: a separate process, a time limit, an output-size limit, a temporary folder, and no API keys passed to it. **This is not security isolation (not a sandbox).** In principle the code can read and write files on your computer, including the key file.
- **API keys are stored in plain text** in `密钥.env` in the data folder. The program only reads it, the interface only shows "filled / not filled", and each key is sent only to the service it belongs to (a model API or the search service). But anyone who copies that file can read your keys.
- During a run, the problem and related material are sent to the model APIs you configure in the settings. The ledger and project files stay on your own computer.

### How to use (Windows)

- **Without Python:** download `AlreadyThere-…-windows.zip` from [Releases](../../releases), unzip it and double-click `AlreadyThere.exe`. The exe is not code-signed; Windows may show "Windows protected your PC" — choose "More info" → "Run anyway".
- **With Python 3.9 or later (e.g. Anaconda):** download the source and double-click `启动观者自在.bat`, or run `python -m alreadythere app` in the program folder.
- First click 连通测试 (connectivity test). Even without any keys you can create a demo project, which runs offline at no cost.
- You need to obtain your own model API keys and enter them in `密钥.env` in the data folder. See [`使用说明.md`](使用说明.md) (user guide, in Chinese).
- Only the Python standard library is used. On Linux and macOS it runs from the command line and the window opens in your default browser.

### Not yet implemented

See [`待实现.md`](待实现.md) (to-do list, in Chinese): the admission gate and certificate levels, stronger isolation of the code runner, key encryption, a redesign of the fact-compilation workflow, and more.

### Related records

- Design documents and iteration records (Zenodo record A, CC BY 4.0): [doi.org/10.5281/zenodo.22938084](https://doi.org/10.5281/zenodo.22938084). Record A reflects the 1.x design; design changes in 2.x are recorded in [`迭代记录.md`](迭代记录.md) (iteration log, in Chinese) in this repository.
- Archive of this program (Zenodo record B, all versions): [doi.org/10.5281/zenodo.22969156](https://doi.org/10.5281/zenodo.22969156)

### AI collaboration statement

- The design ideas took shape in the author's long-running conversations with DeepSeek, Doubao, Claude and ChatGPT. On some design questions (for example, how to detect looping) several models were also consulted separately, and their answers are archived verbatim. All trade-offs and final decisions were made by the author.
- The program code was written by Claude (Anthropic).
- Models that filled the program's roles in real runs: in 1.x, DeepSeek and Kimi (producers) and Doubao (reviewer); in 2.x, Kimi, DeepSeek and Tencent Hunyuan (producers), Qwen and Zhipu GLM (reviewers), and Doubao (fault-finder).
- Models are not listed as authors.

### License

- Code: Apache-2.0 (see LICENSE). Author: LokavOo-.
- Calendar data used for lunar-date conversion: the Time Authority Database of the Dharma Drum Institute of Liberal Arts (DILA, formerly DDBC), CC BY-SA 3.0, in `alreadythere/data/`.
- Problems and reference answers in `测试记录/2.10_AIME2026试跑/`: from MathArena's `aime_2026` dataset, CC BY-NC-SA 4.0; the original problems are © Mathematical Association of America (MAA). Apache-2.0 does not apply to them.

### Name statement

"观者自在" and "AlreadyThere" are the Chinese and English names of this project, and "且待云开见月明" is its motto. This project is not affiliated with, and has not been authorized or endorsed by, any third party with the same or a similar name.

---

<p align="center"><sub><i>Namo Avalokiteśvarāya Bodhisattvāya Mahāsattvāya</i> — LokavOo-</sub></p>
