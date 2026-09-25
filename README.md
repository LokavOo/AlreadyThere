# 观者自在 (AlreadyThere)

**多模型互查的资料整理程序 · 原型第一期（1.x，当前 v1.9）**
**A multi-model cross-verification program for fact compilation · Prototype phase 1 (1.x, current v1.9)**

[中文](#中文) · [English](#english)

---

## 中文

### 这是什么

让几个不同厂商的大模型各自联网查资料、各自作答，再由另一个模型逐条审查；凡是程序能核对的（原句是不是真的在原文里、日期换算、名字是否出现在来源里、统计数字），一律由程序核对，不靠模型自觉。全过程写进一本不能悄悄修改的账本（哈希链），每次模型调用的完整输入和原始返回都存档。

"观者"指我们想要的答案，"自在"的主要意思是**本来就在**：要找的东西本来存在，只是被遮住了；也指不被幻觉和谄媚牵引。它不暗示"一定对"，我们不扩展大模型的能力边界，也不保证一定能消除所有幻觉。

### 一条结论怎样才算"查到"

两个生产者各自查到且一致、原句确实在各自引用的原文里、审查者判定原句与答案一致，**并且**另一个可信网站（生产者引用的网站以外）有独立来源支持。不满足的，如实标为"待复核""仅一方查到""有争议""资料缺载"或"未得出"。"查到"的意思是"从公开的可靠网站能查到、并能相互印证"，用来防网络谣言，不等于学术考证的定论。

### 第一期的测试结果

| 版本 | 题目 | 生产者记忆模式 | 轮次 | 查到 | 查到中的错误 |
|---|---|---|---|---|---|
| 1.5 | 明清 28 位皇帝的出生日期与生肖 | 全局 | 6 | 18 | 0 |
| 1.5 | 同上 | 全新 | 2 | 16 | 0 |
| 1.6 | 同上 | 全新 | 3 | 16 | 0 |
| 1.8 | 2016—2025 年诺贝尔奖六个奖项的得主与获奖原因（60 条） | 全新 | 6 | 7 | 0 |

- 前提条件：生产者为 DeepSeek（deepseek-v4-pro）与 Kimi（kimi-k2.6），审查者为豆包（doubao-seed-2-1-pro-260915），三家均关闭深度思考；检索用 Tavily。每组各只跑了一次，不足以比较记忆模式的优劣。
- 1.8 诺贝尔奖题"查到"比例低，原因是程序的两个问题（独立来源检索范围、分几部分颁发且各有获奖原因的奖），已在 1.9 修正；**其中"独立来源只在生产者未引用的可信网站里检索"这一处修正尚未经实跑验证。**
- 每次测试的设置、口径、逐条结论、用量见 [`测试记录/`](测试记录/)。原始检索存档含第三方网页全文，未随仓库公开。

### 怎么用（Windows）

- **不装 Python：** 在 [Releases](../../releases) 下载 `AlreadyThere-…-windows.zip`，解压后双击 `AlreadyThere.exe`。这个 exe 没有做代码签名，Windows 可能提示"Windows 已保护你的电脑"，点"更多信息"→"仍要运行"。
- **已装 Python 3.9+（如 Anaconda）：** 下载源码，双击 `启动观者自在.bat`，或在程序文件夹里运行 `python -m guanzhe app`。
- 先点"连通测试"；没有密钥也可以新建"演示"项目，不联网、不花钱。
- 需要自己的模型接口密钥与 Tavily 密钥，填在数据文件夹里的 `密钥.env`（程序只读取，只显示"已填/未填"）。详见 [`使用说明.md`](使用说明.md)。
- 程序只用 Python 标准库；Linux 与 Mac 可用命令行运行，窗口会用默认浏览器打开。

### 还没有的

找茬者、记录者、解说者、准入门、并行调用、计算类任务包、在窗口里建题——在第二期（2.x）。版本经过见 [`迭代记录.md`](迭代记录.md)。

### 相关记录

- 设计文档与迭代记录（Zenodo 记录 A，CC BY 4.0）：[doi.org/10.5281/zenodo.22938084](https://doi.org/10.5281/zenodo.22938084)
- 历表换算数据：法鼓文理学院时间规范资料库（DDBC/DILA），CC BY-SA 3.0，见 `guanzhe/data/`。

### AI 协作声明

- 设计思想是在作者与 DeepSeek、豆包、Claude、ChatGPT 的长期对话中形成的；全部取舍与裁定由作者决定。
- 程序代码由 Claude（Anthropic）编写。
- 原型实跑中担任程序角色的模型：DeepSeek、Kimi（生产者），豆包（审查者）。
- 模型不列为作者。

### 许可

代码：Apache-2.0（见 LICENSE）。作者：LokavOo-。

---

## English

### What it is

Several large language models from different vendors each search the web and answer independently; another model then reviews every entry. Everything a program can check is checked by the program rather than trusted to the models: whether a quoted sentence really appears in the archived source, calendar conversions, whether names appear in the source, and all statistics. The whole process is written to a tamper-evident ledger (hash chain), and the full input and raw output of every model call are archived.

In the Chinese name, *guanzhe* (观者) refers to the answer we are looking for, while *zizai* (自在) mainly means **already there** — what we look for already exists and is only obscured — and also *not being pulled along* by hallucination or flattery. It does not imply "always right": we do not extend the capabilities of large language models, nor do we guarantee that all hallucinations can be eliminated.

### When an entry counts as "found"

Two producers find it independently and agree; the quoted sentence really appears in each cited source; the reviewer judges the quote and the answer consistent; **and** an independent source on another trusted website (not one the producers cited) supports it. Otherwise the entry is honestly labelled "to be re-checked", "found by one side only", "disputed", "not in sources" or "not determined". "Found" means *findable and mutually corroborated on reliable public websites* — a guard against online rumours, not a scholarly verdict.

### Phase-1 test results

| Version | Task | Producer memory mode | Rounds | Found | Errors among found |
|---|---|---|---|---|---|
| 1.5 | Birth dates and zodiac signs of the 28 emperors of the Ming and Qing dynasties | global | 6 | 18 | 0 |
| 1.5 | same | fresh | 2 | 16 | 0 |
| 1.6 | same | fresh | 3 | 16 | 0 |
| 1.8 | Laureates and prize motivations (the official award citations) across the six Nobel Prize categories, 2016–2025 (60 entries) | fresh | 6 | 7 | 0 |

- Test conditions: producers DeepSeek (deepseek-v4-pro) and Kimi (kimi-k2.6), reviewer Doubao (doubao-seed-2-1-pro-260915), all with deep thinking disabled; search via Tavily. Each configuration was run once, which is not enough to tell which memory mode is better.
- The low "found" rate in the Nobel Prize task came from two program issues (the scope of the independent-source search, and prizes shared between laureates with separate prize motivations), fixed in 1.9; **the fix restricting the independent-source search to trusted sites not cited by the producers has not yet been verified in a real run.**
- Settings, scope, per-entry conclusions and usage of each run are in [`测试记录/`](测试记录/) (test records). Raw search archives contain full third-party web pages and are not published with the repository.

### How to use (Windows)

- **Without Python:** download `AlreadyThere-…-windows.zip` from [Releases](../../releases), unzip, and double-click `AlreadyThere.exe`. The exe is not code-signed; Windows may show "Windows protected your PC" — choose "More info" → "Run anyway".
- **With Python 3.9+ (e.g. Anaconda):** download the source and double-click `启动观者自在.bat`, or run `python -m guanzhe app` in the program folder.
- Start with the connectivity test. Even without any keys, you can create a demo project, which runs offline at no cost.
- You need your own model API keys and a Tavily key, entered in `密钥.env` in the data folder (the program only reads them and shows only "filled / not filled"). See [`使用说明.md`](使用说明.md) (in Chinese) for details.
- Only the Python standard library is used; on Linux and macOS it runs from the command line and opens in the default browser.

### Not yet included

Fault-finder, recorder and explainer roles, the admission gate (which screens questions before a project starts), parallel calls, the computation task pack, and creating tasks in the app window — planned for phase 2 (2.x). See [`迭代记录.md`](迭代记录.md) (iteration log, Chinese).

### Related records

- Design documents and iteration records (Zenodo record A, CC BY 4.0): [doi.org/10.5281/zenodo.22938084](https://doi.org/10.5281/zenodo.22938084)
- Calendar conversion data: the Time Authority Database of the Dharma Drum Institute of Liberal Arts (DILA, formerly DDBC), CC BY-SA 3.0, in `guanzhe/data/`.

### AI collaboration statement

- The design ideas took shape in the author's long-running conversations with DeepSeek, Doubao, Claude and ChatGPT; all trade-offs and final decisions were made by the author.
- The program code was written by Claude (Anthropic).
- Models serving as program roles in the prototype test runs: DeepSeek and Kimi (producers), Doubao (reviewer).
- Models are not listed as authors.

### License

Code: Apache-2.0 (see LICENSE). Author: LokavOo-.
