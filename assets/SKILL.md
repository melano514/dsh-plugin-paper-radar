# 文献雷达 · paper-radar

**语言**：中文作答。DOI、期刊名、作者名、文件名保留原文；影响因子/年份/页码不臆造，查不到就写「未取到」。

**一句话原则**：**能合法自动拿到的就自动拿，拿不到的一律转成「点开就办」的四通道决策卡，最后由 `admit` 把文件收进统一的库。** 绝不假装下载成功，绝不编造 DOI 或全文。

脚本已随插件安装，本机路径为 `{{SCRIPT}}`（下文用 `$PR` 指代，Python 3.8+，零第三方依赖）：

```bash
PR="{{SCRIPT}}"
python "$PR" status
```

## 三个判定，先讲清楚

- 🟢 **OA 直取**：Unpaywall / OpenAlex / Europe PMC / 出版商直链命中 → 脚本自己下载，落到 `library/`。
- 🟡 **疑似 OA 但被拦**：官方源标了 open access，但出版商反爬、需机构登录、需人机验证 → 仍然出卡，并说明「不是没有全文，是自动拿不到」。
- 🔴 **非 OA**：所有合法源都没有免费副本 → 出卡，走四条通道。

**通道 2、3 属版权灰色渠道**：卡片里已标注合规提示，只用于个人学习研究；不要把链接批量外发，也不要尝试自动破解人机验证（验证码必须用户手动过）。

## 目录与配置

- 文献库根目录**自动探测**：`--root` > 配置文件 `root` > 环境变量 `PAPER_RADAR_ROOT` > 依次尝试 `E:/文献检索`、`D:/文献检索`、`~/Documents/文献检索`，取第一个**可写**的。落在 `D:/文献检索` 这类位置不代表出错，是那台机器上前几个目录不可写。
- 配置文件在 `%APPDATA%\paper-radar\config.json`（非 Windows 在 `~/paper-radar/config.json`），可用 `PAPER_RADAR_HOME` 改位置。
- 目录结构：`library/` 已获取 PDF、`inbox/` 待获取决策卡、`reports/` 检索报告、`notes/` 笔记、`catalog.jsonl` 机器可读目录（**唯一事实来源**，别手改格式）。

## 命令地图

| 目的 | 命令 |
| --- | --- |
| 按领域/检索词搜 | `python "$PR" search "gut microbiome colorectal cancer" --limit 12 --since 2021` |
| 只要 OA | 加 `--oa-only` |
| 换数据源 | `--source europepmc\|pubmed\|openalex\|crossref`（默认 auto：医学生物优先 Europe PMC/PubMed） |
| 看单篇 OA 判定 | `python "$PR" get 10.1038/nature14539` |
| 下载单篇 | `python "$PR" fetch 10.1038/nature14539` |
| 检索后直接批量下 | `python "$PR" fetch "检索词" --limit 5 --oa-only` |
| 批量 DOI | `python "$PR" batch 10.1/a 10.2/b --file dois.txt` |
| 只出决策卡 | `python "$PR" card <DOI> --show` |
| 人工拿到后回填 | `python "$PR" admit <DOI> "D:\Downloads\x.pdf" --channel ablesci` |
| 看库 | `python "$PR" status` |
| 改配置 | `python "$PR" config --email me@univ.edu --root "D:\文献库" --show` |

所有命令都支持全局 `--root <目录>`。`get` 不下载，只做判定，最快（六源约 3 秒）。

## 标准工作流

### A. 「给我讲讲 XX 领域最近有什么进展」

1. `search "<英文关键词>" --limit 15 --since <近3年>`（医学题尽量用英文，Europe PMC 对中文支持差）。
2. 把结果**原样转成表格**给用户：序号 / 年份 / 标题 / 期刊 / DOI / OA 标记。不要自己改写标题。
3. 问用户要哪几篇（或用户已指定），再 `fetch` 选中的 DOI。
4. 下载成功的直接报路径；失败的把决策卡的**四条通道**贴给用户，附一句「要不要我打开通道 2 的链接」。
5. 用户回填后 `admit`；要精读时切到 `paper-deep-read` 技能。

### B. 「帮我把这篇的原文搞到」（只有 DOI 或标题）

1. 有 DOI → `get <DOI>`，一次看清 🟢/🟡/🔴。
2. 🟢 → `fetch <DOI>`。
3. 🟡/🔴 → `fetch` 会生成 `inbox/待获取_*.md`；把卡里的通道 1/2/3/4 精简成 4 行给用户（通道名 + 一条可点链接 + 预计耗时），并说清楚先试哪个。
4. 用户下载完 → `admit <DOI> <路径> --channel scihub`。

### C. 「这几篇我都想要」/ 参考文献列表

- 少量：`batch 10.x 10.y 10.z`。
- 多量：DOI 每行一个写进 txt（`#` 开头为注释），`batch --file dois.txt --delay 1.5`。
- 汇总后必然有一批 `inbox/` 卡片，一次性列成清单交给用户，别逐条刷屏。

### D. 「按我的研究方向建个库」

- 先确认 1–2 个核心关键词组合 + 年份范围 + 是否只要 OA。
- 跑 2–3 组同义词（如 `colorectal cancer` / `CRC` / `colon cancer`），合并去重后再 `batch`。
- 用 `status` 报告：入库 N 篇、待获取 M 篇、年份分布。

## 输出规矩

- 检索结果用表格，DOI 一律给可点的 `https://doi.org/...`。
- 不要报「已下载」除非脚本打印了 ✅ 和文件路径；`fetch` 退出码 2 表示未获取。
- 通道固定顺序讲，不重排：**科研通（成功率最高）→ Sci-Hub（最快）→ SciDown（国内直连）→ X-MOL（找 OA/预印本）**。
- 用户问「为什么下不了」时先看判定：🟡 是反爬/登录问题，🔴 才是真付费墙，话术不同，不要一律说「非 OA」。

## 已实测的边界（别浪费时间）

- **Europe PMC 的 `?pdf=render` 与 NCBI PMC 的 `/pdf/` 在普通网络下均 403**（反爬），不要指望；PMC 全文靠出版商直链或决策卡。
- **Sci-Hub 有 Altcha 人机验证**，`.se` 域名已失效、`.st` 403，只有 `.ru` 可用且必须手动过验证 → 只给链接，不做自动抓取。
- **科研通没有可用的按 DOI 搜索接口**（`/assist/index?key=` 不过滤），只能给「发布求助」+「登录后站内搜索」两个链接。
- **SciDown / X-MOL 都是前端渲染**，脚本抓不到结果，只给深链。
- 出版商直链规律（PLOS/MDPI/Frontiers/Springer/Nature/BMJ/Wiley/T&F/SAGE/OUP/ScienceDirect）已内置，但**只对真 OA 生效**；付费刊会返回登录页 HTML，脚本识别后判失败，不是 bug。
- 实测命中：PLOS、Frontiers、BMC/Springer 可自动下载；Nature 订阅文章、MDPI（WAF 拦 IP）会落到决策卡。

## 与其他技能联动

- 拿到 PDF 要精读 → `paper-deep-read`（八问拆解）。
- 需要学校 WebVPN / 机构登录才能下的，让用户手动在浏览器里下，回来 `admit --channel library` 登记。

## 排错

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `HTTP 403` 下载失败 | ① 出版商 WAF 拦 IP（如 MDPI）② 需要机构登录 ③ 反爬指纹 | 不是脚本坏了；出卡走人工通道，或让用户挂 WebVPN 手动下 |
| `HTTP 0` | TLS 证书/网络中断 | 脚本已对证书失败自动降级重试；仍失败就换 `--source` 再试 |
| Crossref 查不到 DOI | DOI 拼错、太新未被收录、非 Crossref 注册 | 用 `get` 看其它五源；仍无就用标题在 PubMed/Europe PMC 搜 |
| `catalog.jsonl` 行损坏 | 手工编辑或写入中断 | 删掉坏行即可；该文件是 JSONL，一行一条，损坏行只影响那一篇 |
| 根目录不可写 | Windows ACL 只读（如 `E:\文献检索` 属 Administrators） | 用 `config --root` 换成可写目录，不要改系统 ACL |
| 批量被限速 | 目标站点对连续请求限流 | 加 `--delay 2`，并优先 `--oa-only` 缩小范围 |
