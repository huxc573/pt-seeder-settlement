# pt-seeder-settlement · PT 保种组工资结算工具

> **当前版本：v1.0.0**（`VERSION` = `1.0.0`　·　2026-09-16 00:35）
> 变更记录见 [CHANGELOG.md](CHANGELOG.md)

给 PT 站点（NexusPHP 系）保种组组长用的**工资结算 + 发放**工具。

- **零第三方依赖**：只用 Python 标准库（3.8+），拷到任何机器直接跑，不用 `pip install`。
- **只读优先**：采集全程只读；发放是独立阶段 —— 先出表、人工核对、再决定发。
- **防超发**：每笔发出**前**都数一遍「这个人本周期已经发了几笔」，发满 `N` 次的自动跳过 ——
  重跑、手抖重点都不会多给；想再发就「重置周期」（`N` = 界面上「每 [月] [2] 次」那个 2）。
- **一个配置文件**：`config.json` 管全部（站点地址、接口路径、cookie、考核口径、方案薪资、税率、赠送表单）。
- **有图形界面**：`python pt-seeder-settlement.py`，tkinter 是 Python 自带的，同样不用装东西。

整仓以 **MIT** 许可公开发布（[LICENSE](LICENSE)）。

> 📌 本文档内的路径、UID、用户名**均为占位示例**，不含任何真实凭据。

---

## 1. 目录结构与文件清单

```
pt-seeder-settlement/
├── README.md               本文件：总览 / 快速开始 / 用法 / 算法 / 发放 / 配置 / 测试 / 版本管理
├── CHANGELOG.md            版本变更记录
├── VERSION                 当前版本号（唯一来源）
├── LICENSE                 MIT 许可全文（版权人 pt-seeder-settlement）
├── .gitignore              凭据与运行产物防护
├── .gitattributes          行尾统一为 LF
│
├── pt-seeder-settlement.py 图形界面入口（tkinter，四个页签）
├── settle.py               结算：读考核表 → 出工资表
├── payout.py               清单 + 真实发放
├── ledger.py               发放台账（防重复发的凭据）
├── probe.py                站点接口只读探测
├── login.py                cookie 获取向导
├── nexus.py                HTTP 会话 + 页面解析 + JSONC 配置读写
├── roster.py               考核表读取（.xlsx / .csv）
├── clipboard.py            剪贴板读取（Windows / macOS / Linux）
├── config.example.json     脱敏配置模板（可入库）
├── roster.example.csv      脱敏样例考核表（可入库）
│
├── docs/DESIGN.md          设计说明（为什么这么做，不是怎么用）
├── tests/
│   ├── selftest_http.py    本地假站点：整条发放链路的端到端测试
│   └── selftest_gui.py     界面层测试（真起一次 tkinter 量出来）
├── samples/
│   └── *.sample.html       离线自测用的假响应页（4 个）
│
├── config.json             🔴 唯一配置（含 cookie）· 已忽略
├── roster*.csv             🔴 真实考核表 · 已忽略（roster.example.csv 除外）
├── payout_ledger.jsonl     发放台账 · 已忽略
├── payroll_*.csv           工资表 / 清单输出 · 已忽略
└── logs/                   运行日志（按天一个，全部保留）· 已忽略
```

| 文件 | 作用 | 含敏感信息 |
|---|---|---|
| `pt-seeder-settlement.py` | 图形界面（tkinter） | 否 |
| `settle.py` | 结算：读考核表 → 达标判定 → 出工资表 | 否 |
| `payout.py` | 发放清单（dry-run）+ 真实发放 | 否 |
| `ledger.py` | 发放台账：每人已发几次，全靠它 | 否 |
| `probe.py` | 站点接口只读探测（含赠送表单生成） | 否 |
| `login.py` | cookie 获取向导（剪贴板 / 账密登录） | 否 |
| `nexus.py` | HTTP 会话 + 页面解析 + JSONC 配置读写 | 否 |
| `roster.py` | 考核表读取（.xlsx / .csv，按内容识别） | 否 |
| `clipboard.py` | 剪贴板读取（三平台） | 否 |
| `config.example.json` | 脱敏模板（`pt.example.com` 占位，cookie 为空） | 否 |
| `LICENSE` / `VERSION` / `CHANGELOG.md` | 许可 / 版本 / 变更记录 | 否 |
| `docs/DESIGN.md` | 设计说明 | 否 |
| `tests/selftest_http.py` | 本地假站点端到端测试 | 否 |
| `tests/selftest_gui.py` | 界面层测试（需 tkinter） | 否 |
| `samples/*.sample.html`、`roster.example.csv` | 离线自测假数据（假名 + `10001` 起假 UID） | 否 |
| `config.json` | 唯一配置：站点 + cookie + 方案 + 税 | 🔴 是，已忽略 |
| `roster*.csv`（`roster.example.csv` 除外） | 真实考核表 | 🔴 是，已忽略 |
| `payout_ledger.jsonl` | 发放台账 | 🟡 含真实 uid / 用户名，已忽略 |
| `payroll_*.csv` | 工资表 / 清单输出 | 🔴 是，已忽略 |
| `logs/` | 运行日志 | 🟡 含 uid / 用户名，已忽略 |

**为什么可执行文件与模块都放在根目录？**
这样「整个文件夹拷到另一台机器」就一定能跑：`python pt-seeder-settlement.py` 启动时，
Python 会把脚本所在目录加进搜索路径，同目录的模块直接 import 得到，不需要安装、不需要改 `sys.path`。
测试与样例另行归入 `tests/` 与 `samples/`，是为了让根目录一眼只看到「能跑的东西」。

---

## 2. 快速开始

### 2.1 环境要求

结论先行：**没有 `requirements.txt`，也不需要**。

全部代码只 `import` 标准库（`argparse` / `json` / `urllib` / `tkinter` / `zipfile` 等），
不依赖任何第三方包，所以不需要 `pip install`、不需要虚拟环境，**Python ≥ 3.8** 即可运行。

```bash
python --version                      # 需 >= 3.8
python pt-seeder-settlement.py        # 图形界面（tkinter 是自带的，不用装）
```

**首次运行会自动补齐两个本地文件**（都在 `.gitignore` 里，不会进 git）：
`config.json` 从 `config.example.json` 复制（站点地址还是占位值，去 ① 页填成
自己的站点）；`roster.csv` 从 `roster.example.csv` 复制（里面是 `10001` 起的
假数据，**必须有 UID 和方案两列**，换成你的考核表即可）。

少数官方精简安装可能没带 tkinter（很少见）。那种情况界面启动时会明确告诉你，
直接用下面这套命令行流程即可，**功能一模一样**。

### 2.2 先确认代码没坏

```bash
python settle.py --selftest
python probe.py --selftest
python payout.py --selftest
python ledger.py --selftest
python tests/selftest_http.py
```

全部离线可跑（不联网、不读真实配置）。`tests/selftest_gui.py` 需要带 tkinter 的解释器，
只在改了界面布局时才需要跑。合计 **557 条断言**，明细见 [§12](#12-测试)。

### 2.3 命令行走一遍

```bash
# 1. 拿 cookie（两种方式，见第 7 节）
python login.py

# 2. 探测站点，确认接口返回结构（只读）
python probe.py

# 3. 出工资表（离线）
python settle.py

# 4. 联网刷新每个人的当前体积/数量，覆盖表里的手填值
python settle.py --refresh

# 5. 看清单（dry-run，一个字都不写）
python payout.py --period 2026-09

# 6. 真实发放（会二次确认，并且真的花你的魔力值）
python payout.py --period 2026-09 --confirm
```

`settle.py` 不需要 cookie，离线就能出表。只有 `--refresh`、`probe.py`、`payout.py --confirm` 要联网。

---

## 3. 两种用法：命令行 / 图形界面

两条路**背后是同一份代码、同一个台账文件**，行为完全一致，挑顺手的用。

```bash
python pt-seeder-settlement.py    # 图形界面：点点点，四个页签走完全流程
```

| 页签 | 干什么 |
|---|---|
| ① 站点与登录 | 填站点地址与接口路径；一键获取 cookie（复制粘贴 / 账密登录）；一键探测赠送表单 |
| ② 考核与方案 | 考核口径（体积/数量）、各方案门槛与月薪、发放参数、赠送表单 |
| ③ 算工资 | 选考核表（**选中即离线载入**；**双击**就地改方案 / 用户名 / 检查日期，改完写回考核表；外部改了 roster.csv 就点「重新载入」）→ 联网计算（**4 路并发**，边抓边显示、可勾选重抓重算）→ 出工资表 → 导出 CSV / 复制到剪贴板；**实测结果直接写回考核表**，roster.csv 就是缓存，下次打开自动载入 |
| ④ 发放与台账 | 生成清单（不达标 / 已发也列出、默认不勾）→ 勾选要发的人 → 发放 → 查台账 |

③ 表格整行上色，扫一眼就知道谁该发、谁的数据没抓到：

| 行色 | 意思 |
|---|---|
| 绿 | 达标 |
| 红 | 不达标 |
| 橙 | 未测（**不是**达标，但也不是判过的不达标 —— 差的是数据，重新抓一次就好） |

**结果列 = 计算结果**（对齐 `settle._result_note`，界面和导出的 CSV 里列名都叫「结果」）：

| 状态 | 结果里写什么 |
|---|---|
| 达标 / 不达标 | `5.711 TB / 要求 3 TB` —— 实测值和门槛并排，一眼看出差多少 |
| 未测 | `<抓取失败的原因>`（网络中断 / HTTP 码 / 登录态失效 / 模板对不上……） |
| 无方案 | `没有方案：9T 不在 config.json 的 plans 里` |

（结果里不带「达标 / 不达标 / 未测」字样 —— 左边「考核」列已经有了。）

抓取失败的行，**实测体积 / 检查日期一律留空，绝不填 0** —— 「站点说这个人是 0 做种」
和「没抓到」是两回事：前者是准确的实测值（0 必然不达标），后者是数据没拿到（未测）。
实测值拿到 0 只可能是站点自己回「没有记录」，那种情况照记 0。

**「结果」列的宽度按内容自适应**（`fit_column_width`，夹在 150~320px）：常见的
`6.012 TB / 要求 3 TB` 不会白占一长条；报错长了到 320px 封顶，
再长拖表格底部的横向滚动条。

**跑完自动勾上「未测 / 不达标」的行** —— 接着点「选中重算」，**勾上的行会全部
重新联网抓一遍**（原来有数据的也重抓，这就是「再试一次」的意思）；抓不到就按老规矩
清空实测列、记「未测」，不留旧数字冒充刚抓到的。**没勾的行一个字段都不碰**。
没 cookie 时抓不了，这时只拿已有数据重算，不会把好数据清掉。

联网计算默认 **4 路并发**（`measure_workers`）。抓一个人的耗时几乎全花在干等站点
响应上，并发就是把这段等待叠起来，20 人从「20 × 请求时间」降到「5 × 请求时间」；
每路仍按同样的节奏请求，站点承受的压力不变。调成 `1` 就退回一个人一个人抓。

**瞬时失败会自动重试**（1 次原始 + 2 次重试，间隔 1 秒 / 2 秒）。详情页和
做种列表偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`（连接被掐断），并发时更频繁 ——
不重试就会白白多出一批「未测」和假 0。只读请求重试绝对安全；404 这种重试也没用的
不重试。日志里能看到 `→ 网络中断（…），1 秒后重试` 这种行，说明重试生效了。
如果那种行很密，把 `measure_workers` 调小（比如 2）。

④ 表格的勾选框交互和 ③ 一样：**点格子切换、右键「勾选 / 取消勾选 / 反选 / 全局反选」、
底部「全选 / 全不选」**。清单最后两列是**「状态」和「结果」**：

| 「状态」列 | 意思 | 行色 | 默认勾选 |
|---|---|---|---|
| 待发 | 考核达标、本周期一次都还没发 | 橙 | ☑ |
| 已发 n 次 | 本周期发过 n 次，还没到上限 N | 绿 | ☑ |
| 发完 | 本周期已经发满 N 次 | 绿 | ☐ |
| 不发 | 考核不达标 / 未测 | 红 | ☐ |

「状态」「结果」两列的宽度**按内容自己量**（「已发 11 次」比「待发」宽得多），
不白占一长条，也不会被切掉；长到上限就拖表格底部的横向滚动条。

行色跟 ③ 一样是三色的：**待发橙**（还得你动手，发送中 / 发失败也在这档）→
**本周期发过 / 本次成功变绿** → 不发红。

**「结果」列**只记**这次点「发放」的结果**：`成功` / `失败` / `跳过`（发送中显示
`发送中…`），没发过的行是空的。它是「台账口径」和「本次动作」的分开记录 ——
发失败的人「状态」还是「待发」，可以直接再点一次「发放」续发。

**想让谁重新领 N 次**：选中那些行（可 Ctrl 多选，也可右键选中行）→ 右键菜单/底部按钮
**「重置周期」** → 确认。台账里会追加一条 `reset` 记录（**历史一条不删**，只是这些记录
不再算数），他们立刻回到「待发」，那几行的「结果」也一并清空。等价的命令行写法：

```bash
python ledger.py --reset-period --period 2026-09 --uid 10001,10002 --limit 2
```

界面与命令行的差别只有一处：**不达标 / 未测的人在命令行的清单里默认不出现**
（配置 `payout.include_unqualified` / `include_untested` 打开才出现），
界面上则**一律列出来、默认不勾**，发不发由你勾。发满 N 次的人也只在界面上出现
（列出来、状态「发完」、默认不勾），命令行里直接按「发满跳过」处理。

**界面上改的东西会自动写回 `config.json`，注释一行都不会丢**（写前自动备份成
`config.json.bak`）。窗口标题是**程序名 + 版本号**（`PT 保种组工资工具 v1.0.0`）；
界面上有任何一格改了、还没保存，标题后面会多一句「配置有改动（未保存）」，存一下就没了。

> 如果你的 Python 没编进 tkinter（少见，官方安装包默认都有），界面会告诉你，
> 直接用命令行版即可，功能一模一样。

---

## 4. 它是怎么算钱的

```
考核表（你自己维护的 WPS / Excel）
   UID | 方案 | 用户名（可不填，联网时自动取） | 检查日期
        ↓  读表（.xlsx 直接读，不需要 openpyxl）
   方案 → 月薪定额（查表，没有折算公式）
        ↓
   实测数据 ⚖ 方案门槛 → 达标 / 不达标 / 未测（只标记，不自动扣钱）
        ↓
   反算赠送量：X = ceil((月薪 + 固定税) / 税后系数)
        ↓
   工资表 CSV（送审）→ 你人工核对 → 清单（dry-run）→ 发放 → 台账留痕
```

**工资是定额，考核只负责标记谁没达标。** 所以改薪只改 `config.json` 里一个数字，不动代码。

### 考核口径可配

`assessment.metrics` 决定考核哪几项，**单选或多选都行**：

| 写法 | 含义 | 方案里要配 |
|---|---|---|
| `["volume"]` | 只考核保种体积 | `min_volume_tb` |
| `["count"]` | 只考核做种数量 | `min_count` |
| `["volume", "count"]` | 两项都考核，**两项都达标才算达标** | 两个都要配 |

填了几项，输出表就只出几列 —— 不考核的列不会出现。

---

## 5. 发放：怎么保证不重复发

站点对赠送有**点击间隔限制**（通常 10 秒），20 人就是 3 分多钟。中途网络一抖，
人就会想「刚才那笔到底成了没」，然后手一抖多发一次。工具的答案是**只有一道判据**：

| 机制 | 怎么算 | 挡住什么 |
|---|---|---|
| 发出前数一遍 | 每笔发出**前**重新数 `ledger.times_of(周期, uid)`，`≥ N` 就跳过 | 重跑、手抖重点、双进程、断线后续发 |

**「每周期 N 次」是「每人最多领 N 次」**：N = 2 时，同一个人这一期点两次「发放」
就会真的各领一笔 —— 这是设计如此（「每周 2 次」就该是这样）。
**工具不做逐人去重**，所以动手前看清清单里谁还是「待发」。
到了 N 次之后这个人状态就是「发完」，再点「发放」会被逐笔跳过、一个请求都不会发出去。

想让某个人**重新领 N 次**：在清单里选中他 → 「重置周期」。它会往台账追加一条 `reset`
作废记录（`ledger.reset_period()`），那些人的次数当场归零、历史一条不删。

台账是 append-only 的 JSONL，只有两种记录：

```jsonc
{"v":1,"type":"gift","period":"2026-09","uid":10001,"username":"user001","amount":222227,"plan":"3T","status":"ok","at":"2026-09-15T11:20:03"}
{"v":1,"type":"gift","period":"2026-09","uid":10001,"username":"user001","amount":222227,"plan":"3T","status":"ok","at":"2026-09-15T11:20:21"}
{"v":1,"type":"reset","period":"2026-09","uids":[10001],"note":"人工重置周期","at":"2026-09-15T11:24:11"}
```

`reset` 之后，前面那些 `gift` 对**这些 uid** 就不算数了（下一次 `gift` 又会重新算）。
`status:"fail"` 的记录也写在里面（记失败原因用），但它不算「已发次数」。

```
python payout.py --status                 # 看本周期状态（每人已发几次 / 还能领几次）
python payout.py --period 2026-09         # dry-run 清单（默认只有达标的）
python payout.py --period 2026-09 --only user001,user002
python payout.py --period 2026-09 --include-unqualified --include-untested
python payout.py --period 2026-09 --confirm --force      # 忽略「每人 N 次」上限，发满的人也发
python payout.py --mark-paid 9            # 人工补记：站点上确实收到了、工具判失败的
python ledger.py --period 2026-09 --limit 2 --reset-period --uid 10001,10002
```

`--include-unqualified` / `--include-untested` 也可以用配置常开：
`payout.include_unqualified` / `payout.include_untested`（默认都是 `false`）。
在命令行**列出来就等于会发**（CLI 没有勾选这一步），所以清单里这些行带 `★不勾` 标记，
表格里也有「考核」一列 —— 动手前逐行看清。

**典型场景**：发到第 12 个人网络断了。失败的人**不会**被记成成功（次数不加）。
把同一条命令再跑一遍即可续发 —— 前 11 个已经发满、自动跳过，只补剩下的。

### 赠送表单：不猜，现抓

真实 NexusPHP 的赠送表单里有 `<input type="hidden" name="action" value="gift">`，
不带这个字段服务端根本不认。各站二次开发后还可能加别的隐藏字段（含防重放令牌），
所以工具**每次发放前先 GET 一次赠送页**，把 action 和隐藏字段原样带过去，不写死在配置里。

字段名（收礼人 / 金额 / 留言叫什么）也不猜：

```bash
python probe.py        # 它会打印一段可以直接粘进 config.json 的 gift_form
```

### 成功判定：只看落点 URL

**这一项决定「这笔到底成没成」**。赠送页的实际行为：

| 动作 | URL |
|---|---|
| 点赠送（提交） | `mybonus.php?action=exchange` |
| 送成功 | 页面跳 `mybonus.php?do=transfer` |
| 10 秒内重复点 | 页面跳 `mybonus.php?do=duplicated`（= 这笔没送出） |

成功响应里**一个字都没有**，所以页面文字一个都不看，只认落点 URL：响应里的
JS 跳转目标（`window.location.href`），以及跟随 HTTP 重定向后的最终地址。
只认这两处取到的 URL，不整页搜串 —— 模板别处也可能带这两个词。
判不出来一律按「没送出去」处理（宁可少发，不会错发）。

配置就两项，默认值已经填好：

| 键 | 默认 | 含义 |
|---|---|---|
| `gift_form.success_url` | `do=transfer` | 成功落点认这个串 |
| `gift_form.duplicate_url` | `do=duplicated` | 重复提交 / 限速的落点串 |

两个都清空 = 没有成功判据，工具会拒绝真实发放。

**万一「人收到了、工具判失败」**（换站/换模板时最容易踩）：

1. **别直接重发** —— 先去站点收件箱核对那个人到底收到没有
2. 收到了 → `python payout.py --mark-paid <uid>` 补记，重跑时会自动跳过
3. 没收到 → 直接再点「发放」续发即可（失败只记账，不占 N 次里的任何一次）
4. 判定失败的响应会原样存进 `samples/debug_gift_*.html`，点底部「打开日志」
   也能看到每笔响应的正文开头 —— 拿它把落点后缀修准

> **只有「重复提交」才自动重试**：跳 `do=duplicated`，是站点自己说「这笔没送出」。
> 其它失败（网络中断、判不出落点、登录态失效）**一律不重试** —— 这些情况都可能
> 「其实已经提交了」，重试就是重复发钱。工具把它记成失败让人核对；失败**不算已发次数**，
> 重跑时那个人还能补发（他不占「每人 N 次」里的任何一次）。

### 结算周期与发放次数

「④ 发放与台账」页顶上一行就是**每 \[单位] \[N] 次**：

- **单位**：月 / 周 / 日，决定周期键长什么样 ——
  月 → `2026-09`，周 → `2026-W37`，日 → `2026-09-15`
- **N**：本周期**每人**能领几次（不是「整组共几次」），写进 `payout.rounds_per_period`
  （默认 1）

周期键跟着单位自动算好，也能手改成想结算的那一期（补结上个月就把它改回去）。
发满 N 次的人状态是「发完」，再点「发放」会被逐笔跳过（连请求都不会发出去）。
要让他重新领 N 次：清单里选中他 →「重置周期」。

**这两格是边打边生效的**（不用先点「生成清单」，也不用等保存配置）：改「N」立刻按新 N
重算每行的「已发 n 次 / 发完」，改「周期键」立刻按新周期对一遍台账 —— 谁算「已发」、
按钮点不点亮、汇总行说什么，全都当场变（清单不会被重做，勾选和「结果」都留着）。
N 填得不对会当场在汇总行里说清、按钮变灰，不会等存盘才报错。

周期键后面还有一个**「保存」按钮**：点它就把这两格当场写进 `config.json`（跟底部的
「保存配置」是一回事，只是不用翻到别处）。点了以后 `settings` 也当场按新值重读，
所以「生成清单」「发放」用的都是新周期。
「生成清单 / 台账状态 / 不勾也列出」这三个按钮和提示**另起一行**，不跟周期输入框挤。

**「能发几次」只有这一个来源**：界面那一格、每行状态、发放前的跳过判断、
写进 config 的值，全都读它。

### 运行日志

界面上的每行日志都带时间戳写进 `logs/log_YYYY-MM-DD.log`（在 config.json
旁边，按天一个，**全部保留不删**）。发放的每一笔、失败原因全都在里面，
出问题点底部状态栏右侧的「打开日志」直接发人排查。

**发放前不做任何预检**：点「发放」就直接一笔一笔试着发，成没成看每笔的落点 URL。
cookie 死了会在第一笔就报「登录态失效」并停下，一笔都不会错记。

发完弹一个**结果弹窗**，成功 / 失败 / 跳过三个数各用一个颜色摆出来（绿 / 红 / 灰），
下面一句灰字「**详情请确定后，查看清单。**」。点「确定」之后**不会再做任何操作**：
清单、勾选、「结果」全原样留着。没发成功的那几个还勾着，直接再点「发放」就续发了。

---

## 6. 考核表格式

就是你现有的 WPS 表，**表头按名字认，列顺序随便动**：

| UID | 方案 | 用户名 | 检查日期 | 备注 |
|---|---|---|---|---|
| 10001 | 3T | （可不填） | 260815-3.560 TB | |
| 10002 | 3T | （可不填） | 260815-4893 条 | |

**唯一要求**：表里要有 `UID` 列。**用户名可以整个不填** —— 算工资联网刷新时
会从详情页自动取，而且**以站点为准**（用户改名后表里不用跟着改）。
`.xlsx` 是按文件内容识别的（扩展名存错了也能读，比如 xlsx 另存成了 .csv）。

### 「检查日期」是个复合列

它同时装日期和实测值，三种写法都认：

| 写法 | 解析结果 |
|---|---|
| `260815-3.560 TB` | 日期 260815，体积 3.560 TB |
| `260815-4893 条` | 日期 260815，数量 4893 |
| `260815-3.560 TB / 4893 个` | 两样都要 |
| `260919-5.725T(1839)` | 程序自己写回的紧凑格式，同样认 |

还有两条兜底规则（没写单位也没写「条」时）：**带小数点 → 当体积（TB）**，**纯整数 → 当数量**。
所以 `260815-3.560` 和 `260815-4893` 也能读对。

> 体积**必须带单位**才会被认成体积。日期必须是 6 位（`260815`）或 8 位（`20260815`）——
> 这是为了避免把 `3.560` 里的 `3.56` 误读成日期。

也可以单开一列 `做种数量`，脚本会优先用复合列里的值。

---

## 7. cookie 怎么拿

界面上就一个按钮「**获取cookie**」：剪贴板里已经有 Cookie 行就**立刻**拿走写入；
没有 → **马上**打开系统默认浏览器的登录页，并弹**居中浮窗**给出三步指引
（登录 → F12 复制 Cookie 行 → 点浮窗里的「我复制好了」）。绝无轮询等待。

**粘贴来的 cookie 整行照收，不按名字筛** —— 各站 cookie 名五花八门
（老 NexusPHP 是 `c_secure_*`，新站是 `nexusphp_session` / `XSRF-TOKEN`），
只要求里面真有「名字=值」对，残缺的不会写进配置。

命令行两条路：`python login.py --password`（账号密码现登，不碰浏览器）或 `python login.py --paste`（等剪贴板，DevTools 复制一次 Cookie 头）。

**uid 自动识别**：老站的 `c_secure_uid` 就是 `base64(uid)`，直接反解；
新式站点没有这个 cookie，验证时会从个人详情页反推。

> cookie 必须从 **DevTools → Network → Request Headers** 复制，不能从 Console 抄 `document.cookie` ——
> 后者拿不到 HttpOnly 的 cookie（`c_secure_pass` 多半就是）。

---

## 8. 输出格式

列顺序对齐你的考核表，前 4 列原样保留，后面才是脚本加的：

```
UID | 方案 | 用户名 | 检查日期 | 体积 | 数量 | 方案要求 | 考核 | 月薪(实收) | 应赠送 | 差额 | 结果
```

- `体积` / `数量` 两列**恒显示**（考核口径只影响判定和「方案要求」），体积单位写在值里（`5.725T`）。
- `检查日期` 是**原始单元格文本**（联网算完写成 `260919-5.725T(1839)`）—— 方便你和原表逐格对照，也方便直接复制回去。
- `结果` = **计算结果**（实测值 / 要求值；抓取失败就写明报错），不是考核表里那一列的照抄。
  考核表「备注」列里原来写的东西不会再出现在输出里（考核表那边仍然认「备注」这个列名）。
- 编码是 `utf-8-sig`，WPS / Excel 双击直接开，中文不乱码。
- 界面上还能「复制表格」（制表符分隔），直接粘进腾讯文档 / Excel。

---

## 9. 配置说明

`config.json` 是 **JSONC**：支持 `//` 行注释、`/* */` 块注释、尾随逗号。
官网标准 JSON 不允许注释，本工具自己做了宽容解析。完整字段（带注释）见 `config.example.json`：

```jsonc
{
  "base_url": "https://你的站点",          // 装在子目录就连子目录一起写
  "endpoints": {
    "login":        "login.php",
    "takelogin":    "takelogin.php",
    "user_details": "userdetails.php?id={uid}",   // 个人页：昵称 / 累计值，老站做种汇总也在这页上
    "seeding_list": "getusertorrentlistajax.php?userid={uid}&type=seeding&page={page}",   // 做种列表：详情页没有汇总行时**真正**拿数据的地方（二次开发的站常靠它），建议都配上
    "gift_bonus":   "mybonus.php"
  },
  "cookie_names": ["c_secure_uid", "c_secure_pass", "c_secure_login"],
  "cookie": "",                            // login.py 自动填
  "uid": 0,                                // login.py 自动反解
  "timeout_seconds": 25,
  "measure_workers": 4,                    // 联网刷新的并发路数，1 = 串行

  "assessment": { "metrics": ["volume"] }, // 考核口径，见第 4 节

  "plans": [                               // 方案 = 门槛 + 月薪
    { "id": "3T",  "min_volume_tb": 3,  "min_count": 300,  "salary": 200000 },
    { "id": "6T",  "min_volume_tb": 6,  "min_count": 600,  "salary": 400000 },
    { "id": "12T", "min_volume_tb": 12, "min_count": 1200, "salary": 600000 }
  ],

  "payout": {
    "tax_rate": 0.9,                       // 收 = 0.9 × 送 − 4
    "tax_flat": 4,
    "interval_seconds": 10,                // 站点限制的两次赠送间隔
    "rounds_per_period": 1,                // ★ 每周期**每人**能领几次（不是整组共几次）
                                           //   （界面「每 [月] [1] 次」）。改成 2，同一个人
                                           //   再点一次「发放」就真的会再发一笔
    "period_unit": "month",                // 周期单位：month / week / day
    "ledger": "payout_ledger.jsonl",       // 「这人本周期发了几次」全靠它，别删
    "include_unqualified": false,          // 只影响 CLI：不达标的进不进清单
    "include_untested": false,             // 只影响 CLI：未测的进不进清单
    "message_template": "保种组 {period} 月薪 · {plan_id}"
  },

  "gift_form": {                           // probe.py 会生成好让你粘
    "action": "",
    "fields": { "username": "", "amount": "", "message": "" },
    "success_url": "do=transfer",          // ★ 成功落点后缀，换站要自己核
    "duplicate_url": "do=duplicated"       // 重复提交/限速的落点后缀
  }
}
```

`plans[].id` 必须和考核表「方案」列里的写法**完全一致**（大小写不敏感）。对不上的行会报错并剔除，不会静默算错钱。

---

## 10. 税务口径

接收者实收 = `tax_rate × 赠送量 − tax_flat`（NexusPHP 的赠送税）。

要让对方**实收 Y**，就得送：

```
X = ceil((Y + tax_flat) / tax_rate)
```

| 月薪（实收） | 需赠送 | 校验 |
|---|---|---|
| 200,000 | 222,227 | 222227 × 0.9 − 4 = 200,000.3 ✅ |
| 400,000 | 444,449 | 444449 × 0.9 − 4 = 400,000.1 ✅ |
| 600,000 | 666,672 | 666672 × 0.9 − 4 = 600,000.8 ✅ |

**向上取整，宁可多给一分，绝不少给。** 所以 `salary` 填的是**组员实收**，不是你要掏的数。

---

## 11. 注意事项

1. **别把密码给脚本。** 默认只用 cookie。cookie 失效时脚本立刻中止，不会一路空跑。
2. **cookie 必须从 DevTools → Network → Request Headers 复制**，见第 7 节。
3. **`getusertorrentlistajax.php` 给的是瞬时值**，只反映「此刻在做种」。要按月考核，建议每月固定采样几次取平均。
4. **赠送按 `username` 走，不是 uid。** 改名的成员会发错人 —— 发放清单里会把没有 uid 的行单独列出来提醒你。
5. **体积正则绝不能加 `re.I`。** 加了之后种子名 `2160p` 的 `p` 会被当成 PB，解析出天文数字。
6. **不达标默认只标记，不扣钱。** 界面上会列进清单但**默认不勾**；
   命令行默认不出现，要出现加 `--include-unqualified` 或配置 `payout.include_unqualified`。
7. **未测 ≠ 达标。** 界面上同样列出、同样默认不勾；命令行默认不出现。
8. **发过的人也会列在界面的清单里**（状态「已发 n 次」/「发完」，发满的默认不勾）。
   想让谁重发，先**「重置周期」**（右键菜单或底部按钮）—— 它只作废列到的人
   **本周期**的次数，台账历史一条不删。
9. **首月就能发。** 考核看的是结算日的快照值，不需要月度差分。

---

## 12. 测试

**没有一个测试会碰真实站点。**

| 测试 | 覆盖 | 断言数 |
|---|---|---|
| `settle.py --selftest` | 税收正反算、复合列解析、单选/多选考核、**结果列 = 计算结果（达标/不达标/未测/无方案、0 做种是准确的实测值）**、配置校验报错路径、xlsx/csv 读表（**含扩展名存错**）、用户名可选、列集合随口径变、未知方案报错 | 77 |
| `probe.py --selftest` | 体积解析（含短写单位）、三种汇总行、用户页、表单提取、登录态、URL 拼接、cookie 解析、uid 反解、**锚点 + h1 认用户名**、**JSONC 保注释改写**、模板脱敏 | 82 |
| `tests/selftest_http.py` | 假 NexusPHP：真实 cookie、HTTP 链路、**用户名以站点为准自动补齐/改名**、逐人进度回调、**并发抓取（workers>1）结果与串行一致 + on_member 每人一次**、**瞬时失败自动重试（前两次 500 → 第三次拿到数据；404 不重试）**、**「站点说没有记录」按 0 记 / 「抓不到」记未测且实测留空**、整条发放链路（真发→**同一个人再发一次就真领到第 2 次**→发满后第 3 次连请求都不发→失败记账→**重置周期后又能领**→cookie 失效）、**只有重复提交才重试，判不出落点绝不重试**、**落点 URL 判定（跳 do=transfer 才算成功）**、**成功URL后缀清空拒绝真发** | 173 |
| `ledger.py --selftest` | **每人已发次数（`times_of`）**、**每周期每人 N 次**、**重置周期（只作废列到的人 / 不删历史 / 重置后重新算数）**、失败人数按 uid 去重、坏行容错 | 39 |
| `payout.py --selftest` | 表单规格校验（**success_url 不清空即放行**）、**响应判定只看落点 URL（页面文字一律不看）**、**JS 跳转 + HTTP 重定向两处都认**、**失败响应快照**、**发满 N 次逐笔跳过（`skipped_full`）**、**状态文案 `pay_state`（待发 / 已发 n 次 / 发完）**、计划过滤（**不达标未测默认不出现 / 配置开关 / default_pick 看是否发满 / quota 汇总**）、**中文对齐补齐**、dry-run 不写库 | 89 |
| `pt-seeder-settlement.py --selftest` | 口径复选框、方案表→配置、制表符导出、台账周期汇总、**发放状态三态**、**不加逐人去重（发过的人照样进清单）**、**重置周期只动列到的人** | 56 |
| `tests/selftest_gui.py` | 真起一次 tkinter 量出来：**④ 清单整表放得进窗口**、**状态/结果列按内容自适应且不 stretch**、**待发 → 已发 n 次 → 发完 → 重置周期 → 待发**整条界面链路、**改 N 边打边生效**、**填错说清原因+按钮变灰**、**勾选按 uid 认回来**、**三态行色**、**③「选中重算」勾了谁就重抓谁（有数据的也重抓）** | 41 |

合计 **557 条断言**，全部离线可跑（`tests/selftest_gui.py` 需要带 tkinter 的解释器）。

---

## 13. 首次真实发放

**先试发 1 人，确认无误再全量**：

```bash
python payout.py --period 2026-09 --only 你自己的小号     # 先试发 1 人
python settle.py                                         # 看工资表数字对不对
python payout.py --period 2026-09 --confirm              # 确认无误再全量
```

记得先确认**余额够**：20 人满编约需 **7,333,430** 魔力值（见 `docs/DESIGN.md` 第 5 节）。

---

## 14. 开源与许可

整仓 **MIT**（[LICENSE](LICENSE)，版权人 `pt-seeder-settlement`）。代码全部为本项目自写，
未包含任何第三方源码或衍生素材，因此没有第三方声明文件。

已经处理好的脱敏：

- `config.json`（cookie）、`roster*.csv`（真实名单，`roster.example.csv` 除外）、
  `payout_ledger.jsonl`、`payroll_*.csv`、`logs/`、`samples/debug_*.html`、`.workbuddy/`
  全部在 `.gitignore` 里。
- `config.example.json` 用 `pt.example.com` 占位，cookie 为空，`gift_form.action/fields`
  留空（没探测过就禁止真发）；只填了站点通用的 `success_url` / `duplicate_url` 后缀。
- `roster.example.csv` 用 `user001`~`user007` 假名，UID 用 `10001` 起。
- 代码、文档、测试里**不含任何真实站点域名、UID、用户名**（`probe.py --selftest` 里有一条断言守着）。

发布前自查：

```bash
git status --ignored          # 确认凭据与运行产物都在忽略列表里
python probe.py --selftest    # 其中一项就是「模板里只用示例域名」
```

---

## 15. 版本管理

### 15.1 版本号规则（语义化版本 2.0.0）

格式 `MAJOR.MINOR.PATCH`：

| 段位 | 何时 +1 | 本项目示例 |
|---|---|---|
| `MAJOR` | 不兼容变更：配置结构破坏性调整、目录重构、运行方式改变 | `config.json` 字段改名 |
| `MINOR` | 向后兼容的新功能 | 新增考核口径、新增发放形态 |
| `PATCH` | 向后兼容的修复 | 修解析正则、改超时、安全加固 |

- **版本号的唯一来源是根目录 `VERSION`**（单行，如 `1.0.0`）；界面标题的版本号就是读它得到的。
- Git 标签命名 `v<MAJOR>.<MINOR>.<PATCH>`，对外简称可写 **v1.0**。
- `CHANGELOG.md` 每个版本段的标题除日期外必须带 **24 小时制、到分钟**的时间
  （格式 `YYYY-MM-DD HH:MM`），`README.md` 顶部版本号同样带该时间，便于精确回溯。

### 15.2 版本信息存放位置

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `VERSION` | 当前版本号（唯一来源） | ✅ |
| `CHANGELOG.md` | 每个版本的 Added / Changed / Fixed / Security | ✅ |
| `README.md`（本文档 §15） | 版本管理规则 | ✅ |
| Git tag `vX.Y.Z` | 与提交一一对应的不可变锚点 | ✅ |

### 15.3 提交信息约定（Conventional Commits 精简版）

```
<type>(<scope>): <一句话描述>
```

| type | 含义 | 对应版本段位 |
|---|---|---|
| `feat` | 新功能 | MINOR |
| `fix` | 修 bug | PATCH |
| `perf` | 性能 / 并发 / 重试策略 | PATCH |
| `refactor` | 重构，不改变外部行为 | PATCH |
| `docs` | 仅文档 | PATCH（或跳过发版） |
| `chore` | 构建 / 仓库配置 | 视情况 |
| `security` | 安全加固（凭据、权限、脱敏） | PATCH |

示例：`feat(settle): 新增做种数量考核口径`、`fix(payout): 修落点判定漏掉 HTTP 重定向`。

### 15.4 日常变更流程

```bash
# 1. 改动
# 2. 确认没有凭据入库
git status --short      # 不应出现 config.json / roster.csv / payout_ledger.jsonl / logs/ / .workbuddy
# 3. 提交
git add -A
git commit -m "fix(payout): 修落点判定漏掉 HTTP 重定向"
# 4. 需要发版时：更新 VERSION → CHANGELOG 顶部追加 → 提交 → 打标签
git add VERSION CHANGELOG.md README.md
git commit -m "chore(release): v1.0.0"
git tag -a v1.0.0 -m "1.0.0: 一句话概要"
```

### 15.5 发版检查清单

- [ ] `VERSION` 已更新
- [ ] `CHANGELOG.md` 已追加新版本段落（含日期 + 24 小时制时间）
- [ ] `README.md` 顶部版本号同步
- [ ] `python settle.py --selftest` / `probe.py --selftest` / `payout.py --selftest` /
      `ledger.py --selftest` / `tests/selftest_http.py` 全绿
- [ ] 改了界面布局时跑一次 `python tests/selftest_gui.py`
- [ ] 构建 Windows exe 并冒烟通过（见 §15.7）
- [ ] `git status --short` 中**没有**凭据或运行产物
- [ ] 已提交并 `git tag -a vX.Y.Z`
- [ ] 发版后同步标签：`git push origin main --follow-tags`

### 15.6 回滚

```bash
git log --oneline --decorate      # 找到目标 tag
git show v1.0.0                   # 查看该版本内容
git checkout v1.0.0 -- <文件>      # 只回滚单个文件
git checkout -b hotfix/x v1.0.0   # 从旧版本拉修复分支
```

回滚后记得同步修正 `VERSION` 与 `CHANGELOG.md`，避免版本号与实际代码不符。

### 15.7 打包 Windows exe（可选）

源码本身就是完整程序；exe 只是不想装 Python 的用户用的。用 PyInstaller 打成单文件：

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed \
  --name "pt-seeder-settlement_v$(cat VERSION)" \
  --add-data "VERSION;." \
  --add-data "config.example.json;." \
  --add-data "roster.example.csv;." \
  --add-data "samples;samples" \
  pt-seeder-settlement.py
```

- 产物名带版本号，如 `dist/pt-seeder-settlement_v1.0.0.exe`，双击即可运行；
  **不用管理员权限、不写注册表**。
- 打包后程序把**只读资源**（`VERSION` / `samples` / 两个 example）和**用户数据**
  （`config.json` / `roster.csv` / 台账 / 输出表）分开对待：前者从包内资源读，
  后者固定生成在 **exe 所在目录**（`nexus.RES_ROOT` / `nexus.DATA_ROOT`），
  换台机器拷 exe 时把生成的 `config.json`、`roster.csv` 一起拷走即可。
- 首次运行同样自举：在 exe 旁边生成 `config.json` 与 `roster.csv`（占位数据）。
