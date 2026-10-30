/**
 * dsh-plugin-paper-radar — registers the `paper-radar` skill.
 *
 * The skill owns the retrieval workflow (topic / keyword / DOI → open-access
 * PDF, and a four-channel decision card when the paper sits behind a
 * paywall). The work is done by `scripts/paper_radar.py`, which ships with
 * this package and uses only the Python standard library: no pip install, no
 * build step, no network services of its own beyond the public metadata APIs
 * (Crossref, OpenAlex, Europe PMC, PubMed, Unpaywall) and publisher PDF links.
 *
 * Registration goes through the skill registry (`ctx.skills`), so the skill is
 * catalogued for every session of the profile and can be loaded on demand.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/** Cordis plugin name. */
export const name = 'paper-radar'

/** The skill registry is the only service this plugin needs. */
export const inject = ['skills']

/** Absolute path of the bundled Python tool on this machine. */
export const scriptPath = fileURLToPath(new URL('./scripts/paper_radar.py', import.meta.url))

const DESCRIPTION =
  '文献雷达：按领域、检索词或 DOI 检索医学与生命科学文献，自动判定并抓取开放获取（OA）全文 PDF；' +
  '遇到非 OA 付费墙文献时，生成含科研通、Sci-Hub、SciDown、X-MOL 四条取全文通道的决策卡，' +
  '人工取回后用一条命令登记入库。Retrieve medical literature PDFs by topic or DOI, with a manual four-channel fallback for paywalled papers.'

const WHEN_TO_USE =
  '用户说「帮我找几篇 XX 方向的文献」「把这些 DOI 的 PDF 下载下来」「这篇论文有没有免费全文」' +
  '「帮我搞到原文」「建个文献库」时使用；也用于批量整理参考文献列表、组会或开题前集中备料。'

const SKILL_URL = new URL('./assets/SKILL.md', import.meta.url)

/** Replace the install-time placeholders in the skill body. */
export function renderSkillBody(script) {
  return readFileSync(SKILL_URL, 'utf8').split('{{SCRIPT}}').join(script)
}

export function apply(ctx) {
  ctx.skills.register({
    name: 'paper-radar',
    description: DESCRIPTION,
    whenToUse: WHEN_TO_USE,
    source: 'plugin:dsh-plugin-paper-radar',
    content: renderSkillBody(scriptPath),
  })
}
