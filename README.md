# dsh-plugin-paper-radar · 文献雷达

给 DeepSeek Harness 用的文献检索插件：**按领域、检索词或 DOI 找论文 → 自动判定能不能免费拿 → 能拿的直接下载 PDF → 拿不到的给你一张四条通道的"决策卡"**。

面向医学与生命科学（Europe PMC / PubMed 优先），也支持跨学科（OpenAlex / Crossref）。

## 它解决什么问题

研究生日常真正花时间的不是"搜到论文"，而是搜到之后的三种情况：

1. 这篇是不是 OA？——要挨个站点试，试完还不知道是不是真免费。
2. 明明是 OA，为什么下载失败？——出版商反爬、机构登录、代理限制，报错各不相同。
3. 真付费墙，去哪儿要？——科研通、Sci-Hub、SciDown、X-MOL 各有一套用法，每次都要重新想。

这个插件把 1 和 2 自动化，把 3 变成一张**点开就能办**的卡片，并在人工拿到文件后用一条命令回到统一的文献库。

## 安装

```bash
dsh plugin add melano514/dsh-plugin-paper-radar
```

安装后无需构建、无需 `npm install`：插件只有一个 ESM 入口（`index.js`），它把随包技能 `paper-radar` 注册进技能目录。
也可以用 Plugin Manager 的 `install_bundle` 指向本地目录。

**唯一依赖**：Python 3.8+（脚本只用标准库，不需要 `pip install` 任何东西）。没有 Python 时插件仍会安装，技能会提示先装 Python。

## 用法

装好后直接用自然语言说：

- 「帮我找 10 篇 2022 年以后肠道菌群和结直肠癌的文献，只要 OA 的」
- 「这几篇的 PDF 都下下来：10.1038/nature14539、10.1371/journal.pmed.1000097」
- 「这篇有没有免费全文？」
- 「按我的研究方向建个库」

也可以直接调脚本（技能知道路径，下面用 `$PR` 指代）：

```bash
python "$PR" search "gut microbiome colorectal cancer" --limit 12 --since 2021 --oa-only
python "$PR" get 10.1038/nature14539          # 只判定，不下载，约 3 秒
python "$PR" fetch 10.1038/nature14539        # 下载或出决策卡
python "$PR" batch --file dois.txt --delay 1.5
python "$PR" admit 10.1038/nature14539 "D:\Downloads\x.pdf" --channel scihub
python "$PR" status
python "$PR" config --email me@univ.edu --root "D:\文献库"
```

## 工作方式

| 环节 | 数据源 |
| --- | --- |
| 检索 | Europe PMC、PubMed E-utilities、OpenAlex、Crossref（默认按学科自动路由） |
| OA 判定 | Unpaywall、OpenAlex、Europe PMC 的 OA 标记 |
| PDF 直取 | Unpaywall 直链 → 出版商 URL 规律（PLOS/MDPI/Frontiers/Springer/Nature/BMJ/Wiley/T&F/SAGE/OUP/ScienceDirect）→ 落地页 `citation_pdf_url` |
| 全文校验 | 校验 `%PDF-` 文件头与页数，绝不把登录页当 PDF 存下来 |

判定结果只有三态：

- 🟢 **OA 直取**：判定成立且有直链 → 自动下载进 `library/`
- 🟡 **疑似 OA 但被拦**：官方源标了 OA，但自动下载被反爬/登录挡住 → 出卡，明说"不是没有全文，是自动拿不到"
- 🔴 **非 OA**：所有合法源都没有免费副本 → 出卡，走四通道

## 非 OA 的四条通道

卡片里固定按成功率/速度排序，并给出可直接点开的深链：

1. **科研通（AbleSci）**：发布文献互助 + 登录后站内搜索。成功率最高，通常几分钟到几小时。
2. **Sci-Hub**：已知 DOI 最快，但站点有人机验证，必须手工过验证码。
3. **SciDown**：国内直连、速度快，结果页是前端渲染。
4. **X-MOL**：适合顺带找预印本或其它版本的 OA 入口。

> **合规提醒**：通道 2、3 属于版权灰色渠道，仅供个人学习研究使用，请遵守所在机构与当地法律法规。插件只生成链接、不自动破解任何人机验证，也不批量分发全文。

## 目录与配置

- 文献库根目录**自动探测可写位置**：`--root` > 配置 `root` > `PAPER_RADAR_ROOT` > 候选目录列表，取第一个可写的。
- 配置文件在 `%APPDATA%\paper-radar\config.json`（非 Windows 为 `~/paper-radar/config.json`），可用 `PAPER_RADAR_HOME` 改位置，也可 `config` 子命令改。
- 库结构：`library/`（PDF）、`inbox/`（待获取决策卡）、`reports/`（检索报告）、`notes/`、`catalog.jsonl`（唯一事实来源）。

## 实测边界（写下来是为了不浪费你的时间）

实测 7 篇真实 DOI，5 篇自动成功，2 篇出卡：

| 案例 | 结果 |
| --- | --- |
| PLOS Medicine `10.1371/journal.pmed.1000097` | ✅ 自动下载 |
| Frontiers `10.3389/fcimb.2026.1907957` | ✅ 自动下载 |
| BMC `10.1186/s13045-024-01549-2` | ✅ 自动下载（落地页 `citation_pdf_url`） |
| Springer `10.1007/s00432-026-06635-1` | ✅ 自动下载 |
| MDPI `10.3390/cells15191800` | 🟡 出卡（www.mdpi.com 对非浏览器 IP 全线 403） |
| Nature `10.1038/nature14539` | ⚪ 出卡（Unpaywall 有 green 记录但 HAL 仓库抓不到 PDF） |

已知固定不可用的路径：Europe PMC `?pdf=render`、NCBI PMC `/pdf/` 在普通网络下返回 403/反爬页；Sci-Hub `.se` 域名失效、`.st` 403；科研通没有可用的按 DOI 检索接口。这些都已写进插件技能，不会反复重试。

## 测试

```bash
node test/smoke.mjs
```

用一个桩 Cordis 上下文加载插件，断言技能被正确注册、安装期路径占位符已替换、随包脚本存在且不是空壳。

## License

MIT。

---

### English summary

A DeepSeek Harness plugin that registers the `paper-radar` skill. It searches medical and life-science literature through Europe PMC, PubMed, OpenAlex and Crossref, decides open-access status through Unpaywall/OpenAlex/Europe PMC, and downloads the PDF when an OA route exists. When no legal OA copy is reachable it writes a decision card with four manual fallback channels (AbleSci, Sci-Hub, SciDown, X-MOL) plus the exact reason the automatic attempt failed. PDFs are verified by magic bytes and page count, never saved as publisher login HTML. Pure-Python standard library tool, no build step, no pip dependencies.
