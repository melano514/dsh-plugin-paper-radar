#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献雷达 paper-radar  ——  DOI / 检索词  →  OA 全文 PDF 自动获取；非 OA 生成四通道决策卡。

零第三方依赖（仅 Python 标准库），Windows / macOS / Linux 通用。

常用：
  python paper_radar.py search "gut microbiome colorectal cancer" --limit 10 --oa-only
  python paper_radar.py get 10.1038/nature14539
  python paper_radar.py fetch 10.1038/nature14539
  python paper_radar.py fetch 10.1038/nature14539 --open
  python paper_radar.py batch dois.txt
  python paper_radar.py card 10.1038/nature14539
  python paper_radar.py admit 10.1038/nature14539 "D:/Downloads/xxx.pdf"
  python paper_radar.py status
  python paper_radar.py config --email you@example.com --root "D:/文献检索"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import html as _html
import io
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 基础设置

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，强制 UTF-8
    except Exception:
        pass

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 配置放在用户数据目录，而不是安装目录：插件装进 profile/node_modules 后那里可能只读。
APP_DIR = os.environ.get("PAPER_RADAR_HOME") or os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "paper-radar")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
UA_BOT = "paper-radar/1.0 (+literature retrieval; contact: {email})"
UA_PDF = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "email": "paper.radar@proton.me",
    "root": "",                     # 为空则自动挑选可写目录
    "root_candidates": ["E:/文献检索", "D:/文献检索", os.path.join(os.path.expanduser("~"), "Documents", "文献检索")],
    "timeout": 30,
    "download_timeout": 120,
    "scihub_mirrors": ["https://sci-hub.ru", "https://sci-hub.st", "https://sci-hub.se"],
    "ablesci_create": "https://www.ablesci.com/assist/create",
    "ablesci_search": "https://www.ablesci.com/assist/search?key={query}",
    "scidown": "https://www.scidown.cn/serpaper.php?doi={doi}",
    "xmol_paper": "https://www.x-mol.com/paper/doi/{doi}",
    "xmol_search": "https://www.x-mol.com/paper/search/q?option={query}",
    "bing": "https://www.bing.com/search?q={query}",
}

ISO = lambda: _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 配置与路径

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[warn] 配置文件损坏，使用默认值：{e}", file=sys.stderr)
    env_email = os.environ.get("PAPER_RADAR_EMAIL")
    if env_email:
        cfg["email"] = env_email
    env_root = os.environ.get("PAPER_RADAR_ROOT")
    if env_root:
        cfg["root"] = env_root
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(APP_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".wt_{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def resolve_root(cfg: dict, override: str | None = None) -> str:
    """根目录优先级：命令行 > 配置 > 环境变量 > 候选目录（取第一个可写）。"""
    if override:
        os.makedirs(override, exist_ok=True)
        return os.path.abspath(override)
    cur = (cfg.get("root") or "").strip()
    if cur and _writable(cur):
        return os.path.abspath(cur)
    for cand in cfg.get("root_candidates") or []:
        if cand and _writable(cand):
            cfg["root"] = cand
            return os.path.abspath(cand)
    fallback = os.path.join(os.path.expanduser("~"), "Documents", "文献检索")
    os.makedirs(fallback, exist_ok=True)
    cfg["root"] = fallback
    return os.path.abspath(fallback)


class Paths:
    def __init__(self, root: str):
        self.root = root
        self.library = os.path.join(root, "library")
        self.inbox = os.path.join(root, "inbox")
        self.reports = os.path.join(root, "reports")
        self.notes = os.path.join(root, "notes")
        self.catalog = os.path.join(root, "catalog.jsonl")
        for d in (self.library, self.inbox, self.reports, self.notes):
            os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- HTTP

class Resp:
    __slots__ = ("status", "headers", "content", "url")

    def __init__(self, status, headers, content, url):
        self.status, self.headers, self.content, self.url = status, headers, content, url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


_UNSAFE_CTX = None


def _unsafe_ctx():
    """部分机构库/镜像站证书链不全，Python 内置根证书校验会失败 —— 退化为不校验。"""
    global _UNSAFE_CTX
    if _UNSAFE_CTX is None:
        _UNSAFE_CTX = ssl.create_default_context()
        _UNSAFE_CTX.check_hostname = False
        _UNSAFE_CTX.verify_mode = ssl.CERT_NONE
    return _UNSAFE_CTX


def _urlopen(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except (ssl.SSLError, urllib.error.URLError) as e:
        s = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in s or "certificate verify failed" in s:
            return urllib.request.urlopen(req, timeout=timeout, context=_unsafe_ctx())
        raise


def http_get(url: str, *, headers: dict | None = None, timeout: int = 30,
             retries: int = 2, max_bytes: int = 40 * 1024 * 1024) -> Resp:
    h = {"User-Agent": UA_BROWSER, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with _urlopen(req, timeout) as r:
                raw = r.read(max_bytes)
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                elif enc == "deflate":
                    try:
                        import zlib
                        raw = zlib.decompress(raw)
                    except Exception:
                        pass
                return Resp(r.status, dict(r.headers), raw, r.geturl())
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            last = Resp(e.code, dict(e.headers or {}), body, url)
            if e.code in (400, 401, 403, 404, 422):
                return last
        except Exception as e:  # 网络抖动
            last = Resp(0, {}, str(e).encode(), url)
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    return last  # type: ignore[return-value]


def jget(url: str, *, timeout: int = 30, retries: int = 2):
    r = http_get(url, headers={"Accept": "application/json"}, timeout=timeout, retries=retries)
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status} @ {url}")
    return r.json()


# ---------------------------------------------------------------- DOI 与文本工具

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)


def normalize_doi(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*|DOI:\s*)", "", s, flags=re.I)
    s = s.strip().rstrip(".,;)").strip()
    m = DOI_RE.search(s)
    return m.group(0).rstrip(".").lower() if m else s.lower()


def is_doi(s: str) -> bool:
    return bool(DOI_RE.fullmatch((s or "").strip()) or DOI_RE.search((s or "").strip()))


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def abstract_from_inverted(inv: dict | None) -> str:
    if not inv:
        return ""
    slots: list[tuple[int, str]] = []
    for word, positions in inv.items():
        for p in positions:
            slots.append((p, word))
    slots.sort()
    return " ".join(w for _, w in slots)[:2000]


def safe_name(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", "", s or "")
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("._ ")
    return (s or "untitled")[:maxlen]


def tidy_title(t: str) -> str:
    return re.sub(r"\s+", " ", strip_tags(t or "")).strip().rstrip(".")


def year_of(meta: dict) -> str:
    if isinstance(meta, dict) and meta.get("date-parts"):   # Crossref 直接传日期字典
        try:
            return str(meta["date-parts"][0][0])
        except Exception:
            pass
    for k in ("year", "publication_year", "issued", "published"):
        v = meta.get(k)
        if isinstance(v, int):
            return str(v)
        if isinstance(v, str) and v[:4].isdigit():
            return v[:4]
        if isinstance(v, dict) and v.get("date-parts"):
            try:
                return str(v["date-parts"][0][0])
            except Exception:
                pass
    return ""


def first_author_surname(authors: list[str]) -> str:
    if not authors:
        return "Anon"
    a = authors[0].strip()
    if not a:
        return "Anon"
    if "," in a:                      # "Smith, John" / "Smith J"
        return safe_name(a.split(",")[0], 24)
    parts = a.split()
    return safe_name(parts[-1] if parts else a, 24)


# ---------------------------------------------------------------- 元数据源

def crossref_meta(doi: str, cfg: dict) -> dict:
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}"
    if cfg.get("email"):
        url += f"?mailto={urllib.parse.quote(cfg['email'])}"
    data = jget(url)["message"]
    authors = []
    for a in data.get("author", []) or []:
        nm = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
        if nm:
            authors.append(nm)
    issued = (data.get("published-print") or data.get("published-online")
              or data.get("issued") or {})
    links = [l.get("URL") for l in (data.get("link") or [])
             if "pdf" in (l.get("content-type") or "").lower() and l.get("URL")]
    return {
        "doi": doi,
        "title": tidy_title((data.get("title") or [""])[0]),
        "authors": authors,
        "journal": strip_tags((data.get("container-title") or [""])[0]),
        "year": year_of(issued),
        "type": data.get("type", ""),
        "abstract": strip_tags(data.get("abstract", ""))[:2000],
        "crossref_pdf_links": links,
        "alternative_ids": data.get("alternative-id") or [],
        "publisher": data.get("publisher", ""),
        "source_meta": "crossref",
    }


def openalex_meta(doi: str, cfg: dict) -> dict:
    url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='/')}"
    if cfg.get("email"):
        url += f"?mailto={urllib.parse.quote(cfg['email'])}"
    w = jget(url)
    authors = [a.get("author", {}).get("display_name", "") for a in (w.get("authorships") or [])]
    loc = w.get("primary_location") or {}
    src = (loc.get("source") or {})
    ids = w.get("ids") or {}
    return {
        "doi": doi,
        "title": tidy_title(w.get("title") or w.get("display_name") or ""),
        "authors": [a for a in authors if a],
        "journal": src.get("display_name", "") or "",
        "year": str(w.get("publication_year") or ""),
        "type": w.get("type", ""),
        "abstract": abstract_from_inverted(w.get("abstract_inverted_index")),
        "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
        "oa_status": (w.get("open_access") or {}).get("oa_status", ""),
        "oa_url": (w.get("open_access") or {}).get("oa_url", ""),
        "best_pdf": ((w.get("best_oa_location") or {}).get("pdf_url") or ""),
        "locations": [
            {"pdf": l.get("pdf_url") or "", "landing": l.get("landing_page_url") or "",
             "version": l.get("version") or "", "host": (l.get("source") or {}).get("display_name", "")}
            for l in (w.get("locations") or []) if l.get("pdf_url")
        ],
        "pmid": (ids.get("pmid") or "").rsplit("/", 1)[-1],
        "pmcid": (ids.get("pmcid") or "").rsplit("/", 1)[-1],
        "arxiv": (ids.get("arxiv") or "").rsplit("/", 1)[-1] if ids.get("arxiv") else "",
        "source_meta": "openalex",
    }


def unpaywall_meta(doi: str, cfg: dict) -> dict:
    email = cfg.get("email") or "paper.radar@proton.me"
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='/')}?email={urllib.parse.quote(email)}"
    d = jget(url)
    best = d.get("best_oa_location") or {}
    locs = []
    for l in (d.get("oa_locations") or []):
        locs.append({
            "pdf": l.get("url_for_pdf") or "",
            "landing": l.get("url_for_landing_page") or l.get("url") or "",
            "host": l.get("host_type", ""),
            "version": l.get("version", ""),
            "license": l.get("license", ""),
        })
    return {
        "is_oa": bool(d.get("is_oa")),
        "oa_status": d.get("oa_status", ""),
        "best_pdf": best.get("url_for_pdf") or "",
        "best_landing": best.get("url_for_landing_page") or best.get("url") or "",
        "best_host": best.get("host_type", ""),
        "locations": locs,
        "journal_is_oa": d.get("journal_is_oa"),
        "published_date": d.get("published_date", ""),
        "title": strip_tags(d.get("title", "")),
        "source_meta": "unpaywall",
    }


def europepmc_lookup_by_doi(doi: str, cfg: dict) -> dict:
    q = f'DOI:"{doi}"'
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           f"?query={urllib.parse.quote(q)}&format=json&resultType=core&pageSize=1")
    d = jget(url)
    res = (d.get("resultList") or {}).get("result") or []
    if not res:
        return {}
    r = res[0]
    pdfs = []
    for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        if (u.get("documentStyle") or "").lower() in ("pdf", "") and u.get("url"):
            pdfs.append(u["url"])
    return {
        "pmid": r.get("pmid", "") or "",
        "pmcid": r.get("pmcid", "") or "",
        "is_oa": (r.get("isOpenAccess") or "").upper() == "Y",
        "has_pdf": (r.get("hasPDF") or "").upper() == "Y",
        "pdfs": pdfs,
        "abstract": strip_tags(r.get("abstractText", "")),
        "title": tidy_title(r.get("title", "")),
        "journal": (r.get("journalInfo") or {}).get("journal", {}).get("title", "") or "",
        "year": r.get("pubYear", "") or "",
        "authors": r.get("authorString", "") or "",
        "source_meta": "europepmc",
    }


# ---------------------------------------------------------------- 检索

def europepmc_search(query: str, limit: int, cfg: dict) -> list[dict]:
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           f"?query={urllib.parse.quote(query)}&format=json&resultType=core&pageSize={limit}")
    d = jget(url, timeout=45)
    out = []
    for r in (d.get("resultList") or {}).get("result") or []:
        doi = (r.get("doi") or "").lower()
        if not doi:
            if (r.get("source") or "").upper() == "DOI" and r.get("id"):
                doi = str(r["id"]).lower()
            else:
                for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
                    m = DOI_RE.search(u.get("url") or "")
                    if m:
                        doi = m.group(0).lower()
                        break
        year = str(r.get("pubYear") or "") or str(r.get("firstPublicationDate") or "")[:4]
        pdfs = [u["url"] for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or [])
                if (u.get("documentStyle") or "").lower() == "pdf" and u.get("url")]
        out.append({
            "doi": doi,
            "title": tidy_title(r.get("title", "")),
            "authors": [a.strip() for a in (r.get("authorString") or "").split(",") if a.strip()],
            "journal": (r.get("journalInfo") or {}).get("journal", {}).get("title", "") or "",
            "year": year,
            "abstract": strip_tags(r.get("abstractText", ""))[:1200],
            "is_oa": (r.get("isOpenAccess") or "").upper() == "Y",
            "pmid": r.get("pmid", "") or "",
            "pmcid": r.get("pmcid", "") or "",
            "pdf_hints": pdfs,
            "cited": r.get("citedByCount", 0),
            "source": "europepmc",
        })
    return out


def openalex_search(query: str, limit: int, cfg: dict) -> list[dict]:
    url = ("https://api.openalex.org/works"
           f"?search={urllib.parse.quote(query)}&per-page={min(limit, 50)}"
           f"&sort=relevance_score:desc")
    if cfg.get("email"):
        url += f"&mailto={urllib.parse.quote(cfg['email'])}"
    d = jget(url, timeout=45)
    out = []
    for w in d.get("results") or []:
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
        loc = w.get("best_oa_location") or {}
        out.append({
            "doi": doi,
            "title": tidy_title(w.get("title") or ""),
            "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])
                        if a.get("author", {}).get("display_name")],
            "journal": ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "") or "",
            "year": str(w.get("publication_year") or ""),
            "abstract": abstract_from_inverted(w.get("abstract_inverted_index"))[:1200],
            "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
            "pmid": "",
            "pmcid": "",
            "pdf_hints": [loc.get("pdf_url")] if loc.get("pdf_url") else [],
            "cited": w.get("cited_by_count", 0),
            "source": "openalex",
        })
    return out


def crossref_search(query: str, limit: int, cfg: dict) -> list[dict]:
    url = ("https://api.crossref.org/works"
           f"?query.bibliographic={urllib.parse.quote(query)}&rows={min(limit, 50)}")
    if cfg.get("email"):
        url += f"&mailto={urllib.parse.quote(cfg['email'])}"
    d = jget(url, timeout=45)
    out = []
    for w in (d.get("message") or {}).get("items") or []:
        authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x)
                   for a in (w.get("author") or [])]
        issued = w.get("issued") or w.get("published-print") or {}
        out.append({
            "doi": (w.get("DOI") or "").lower(),
            "title": tidy_title((w.get("title") or [""])[0]),
            "authors": [a for a in authors if a],
            "journal": strip_tags((w.get("container-title") or [""])[0]),
            "year": year_of(issued),
            "abstract": strip_tags(w.get("abstract", ""))[:1200],
            "is_oa": None,
            "pmid": "", "pmcid": "",
            "pdf_hints": [],
            "cited": w.get("is-referenced-by-count", 0),
            "source": "crossref",
        })
    return out


def pubmed_search(query: str, limit: int, cfg: dict) -> list[dict]:
    """PubMed E-utilities：esearch → esummary（补充 Europe PMC 未覆盖的条目）。"""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    q = urllib.parse.quote(query)
    ids = jget(f"{base}/esearch.fcgi?db=pubmed&term={q}&retmax={limit}&retmode=json&sort=relevance",
               timeout=45)
    idlist = (ids.get("esearchresult") or {}).get("idlist") or []
    if not idlist:
        return []
    s = jget(f"{base}/esummary.fcgi?db=pubmed&id={','.join(idlist)}&retmode=json", timeout=45)
    out = []
    for pid in idlist:
        rec = (s.get("result") or {}).get(pid) or {}
        if not rec:
            continue
        doi = ""
        for aid in rec.get("articleids") or []:
            if aid.get("idtype") == "doi":
                doi = (aid.get("value") or "").lower()
        out.append({
            "doi": doi,
            "title": tidy_title(rec.get("title", "")),
            "authors": [a.get("name", "") for a in (rec.get("authors") or []) if a.get("name")],
            "journal": rec.get("fulljournalname") or rec.get("source") or "",
            "year": (rec.get("pubdate") or "")[:4],
            "abstract": "",
            "is_oa": None,
            "pmid": pid, "pmcid": "",
            "pdf_hints": [],
            "cited": 0,
            "source": "pubmed",
        })
    return out


def search_literature(query: str, limit: int, cfg: dict, source: str = "auto") -> list[dict]:
    """自动路由：生物医学走 Europe PMC，宽口径走 OpenAlex，兜底 Crossref / PubMed。"""
    def run(fn, name):
        try:
            t0 = time.time()
            res = fn(query, limit, cfg)
            print(f"  · {name:<11s} {len(res):>3d} 条  {time.time()-t0:.1f}s", file=sys.stderr)
            return res
        except Exception as e:
            print(f"  · {name:<11s} 失败：{type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
            return []

    print(f"[search] {query}", file=sys.stderr)
    if source == "europepmc":
        return run(europepmc_search, "europepmc")
    if source == "openalex":
        return run(openalex_search, "openalex")
    if source == "crossref":
        return run(crossref_search, "crossref")
    if source == "pubmed":
        return run(pubmed_search, "pubmed")

    res = run(europepmc_search, "europepmc")
    if len(res) < max(3, limit // 3):
        res += run(openalex_search, "openalex")
    if not res:
        res += run(crossref_search, "crossref")
    # 去重（按 DOI，其次标题）
    seen, merged = set(), []
    for r in res:
        key = r.get("doi") or re.sub(r"\W+", "", (r.get("title") or "").lower())[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(r)
    return merged[:max(limit, 1)]


# ---------------------------------------------------------------- OA 解析

def publisher_pdf_guesses(doi: str, meta: dict | None = None) -> list[tuple[str, str]]:
    """按 DOI 前缀推出已知出版商的 PDF 直链（只是"猜测"，仍需实测校验）。"""
    d = (doi or "").lower()
    out: list[tuple[str, str]] = []

    def add(url: str, tag: str):
        if url and (url, tag) not in out:
            out.append((url, tag))

    if d.startswith("10.1371/"):
        m = re.match(r"10\.1371/(journal\.[a-z]+)\.", d)
        if m:
            slug = {
                "journal.pmed": "plosmedicine", "journal.pone": "plosone",
                "journal.pbio": "plosbiology", "journal.ppat": "plospathogens",
                "journal.pgen": "plosgenetics", "journal.pcbi": "ploscompbiol",
                "journal.ntd": "plosntds", "journal.pntd": "plosntds",
                "journal.pdig": "plosdigitalhealth", "journal.pgph": "plosglobalpublichealth",
            }.get(m.group(1), m.group(1).replace("journal.", "plos"))
            add(f"https://journals.plos.org/{slug}/article/file?id={doi}&type=printable", "plos")
    if d.startswith("10.3390/"):
        add(f"https://www.mdpi.com/resolver?doi={doi}", "mdpi")
    if d.startswith("10.3389/"):
        add(f"https://www.frontiersin.org/articles/{doi}/pdf", "frontiers")
    if d.startswith("10.1186/"):
        add(f"https://link.springer.com/content/pdf/{doi}.pdf", "bmc")
    if d.startswith("10.1007/"):
        add(f"https://link.springer.com/content/pdf/{doi}.pdf", "springer")
    if d.startswith("10.1038/"):
        add(f"https://www.nature.com/articles/{doi.split('/', 1)[1]}.pdf", "nature")
    if d.startswith("10.1136/"):
        add(f"https://www.bmj.com/content/{doi}.full.pdf", "bmj")
    if d.startswith("10.1002/") or d.startswith("10.1111/"):
        add(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}", "wiley")
    if d.startswith("10.1080/"):
        add(f"https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true", "tandf")
    if d.startswith("10.1177/"):
        add(f"https://journals.sagepub.com/doi/pdf/{doi}", "sage")
    if d.startswith("10.1093/"):
        add(f"https://academic.oup.com/doi/pdf/{doi}", "oup")
    for alt in (meta or {}).get("alternative_ids") or []:
        if re.fullmatch(r"[A-Z0-9]{16,24}", str(alt)):
            add(f"https://www.sciencedirect.com/science/article/pii/{alt}"
                f"/pdfft?isDTMRedir=true&download=true", "sciencedirect")
    return out


def _landing_pdf_url(doi: str) -> str:
    """抓 doi.org 落地页的 citation_pdf_url（出版商通用约定，命中率最高的兜底）。"""
    try:
        land = http_get(f"https://doi.org/{doi}", headers={"Accept": "text/html"},
                        timeout=25, retries=0, max_bytes=3 * 1024 * 1024)
        if land.status != 200:
            return ""
        t = land.text
        for pat in (
            r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url',
            r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)',
        ):
            m = re.search(pat, t, re.I)
            if m:
                return _html.unescape(m.group(1))
    except Exception:
        pass
    return ""


def resolve_oa(doi: str, cfg: dict, meta: dict | None = None,
               verbose: bool = True) -> tuple[list[dict], dict]:
    """按可靠性依次收集候选 PDF 链接。返回 (candidates, report)。"""
    doi = normalize_doi(doi)
    cands: list[dict] = []
    report = {"unpaywall": None, "openalex": None, "europepmc": None, "tried": 0}

    def add(url: str, rank: int, src: str, note: str = ""):
        if not url or not isinstance(url, str):
            return
        url = url.strip()
        if not url.lower().startswith("http"):
            return
        if any(c["url"] == url for c in cands):
            return
        cands.append({"url": url, "rank": rank, "source": src, "note": note})

    # 1) Unpaywall —— 最权威的 OA 判定
    up = {}
    try:
        up = unpaywall_meta(doi, cfg)
        report["unpaywall"] = {
            "is_oa": up.get("is_oa"), "status": up.get("oa_status"),
            "host": up.get("best_host"), "email": cfg.get("email"),
        }
        add(up.get("best_pdf", ""), 10, "unpaywall", "best_oa_location")
        if not up.get("best_pdf") and up.get("best_landing"):
            add(up["best_landing"], 25, "unpaywall-landing", up.get("best_host") or "")
        for l in up.get("locations") or []:
            add(l.get("pdf"), 20, "unpaywall", l.get("host") or "")
            if not l.get("pdf") and l.get("landing"):
                add(l.get("landing"), 30, "unpaywall-landing", l.get("host") or "")
    except Exception as e:
        report["unpaywall"] = {"error": f"{type(e).__name__}: {str(e)[:90]}"}

    # 2) OpenAlex
    oa = {}
    try:
        oa = openalex_meta(doi, cfg)
        report["openalex"] = {"is_oa": oa.get("is_oa"), "status": oa.get("oa_status")}
        add(oa.get("best_pdf", ""), 30, "openalex", "best_oa_location")
        for l in oa.get("locations") or []:
            add(l.get("pdf"), 40, "openalex", l.get("host") or "")
    except Exception as e:
        report["openalex"] = {"error": f"{type(e).__name__}: {str(e)[:90]}"}

    # 3) Europe PMC（生物医学主战场）
    ep = {}
    try:
        ep = europepmc_lookup_by_doi(doi, cfg)
        if ep:
            report["europepmc"] = {"pmcid": ep.get("pmcid"), "is_oa": ep.get("is_oa"),
                                   "has_pdf": ep.get("has_pdf")}
            for u in ep.get("pdfs") or []:
                add(u, 50, "europepmc", "fullTextUrl")
            pmcid = ep.get("pmcid") or oa.get("pmcid")
            if pmcid:
                add(f"https://europepmc.org/articles/{pmcid}?pdf=render", 60, "epmc-render", pmcid)
    except Exception as e:
        report["europepmc"] = {"error": f"{type(e).__name__}: {str(e)[:90]}"}

    # 4) 预印本 / 仓储
    axid = oa.get("arxiv") or ""
    if axid:
        add(f"https://arxiv.org/pdf/{axid}", 70, "arxiv", axid)
    if doi.startswith("10.1101/"):
        server = "medrxiv" if "medrxiv" in (doi + (meta or {}).get("journal", "")).lower() else "biorxiv"
        add(f"https://www.{server}.org/content/{doi}v1.full.pdf", 75, server, "v1")
        add(f"https://www.{server}.org/content/{doi}.full.pdf", 76, server, "latest")

    # 5) 出版商 PDF 直链规律（PLOS / MDPI / Frontiers / Springer / Nature / BMJ / Wiley…）
    for u, tag in publisher_pdf_guesses(doi, meta):
        add(u, 55, f"guess:{tag}", "publisher pattern")

    # 6) 出版商 link（Crossref 的 application/pdf）
    for u in (meta or {}).get("crossref_pdf_links") or []:
        add(u, 80, "publisher", "crossref link")

    # 7) 兜底：doi.org 落地页的 citation_pdf_url（出版商通用元数据约定，命中率高）
    u = _landing_pdf_url(doi)
    if u:
        add(u, 85, "citation_pdf_url", "landing meta")

    cands.sort(key=lambda c: c["rank"])
    report["tried"] = len(cands)
    if verbose:
        srcs = ", ".join(sorted({c["source"] for c in cands})) or "无"
        print(f"  · 候选 PDF {len(cands)} 个（{srcs}）", file=sys.stderr)
    return cands, report


# ---------------------------------------------------------------- 下载与校验

def verify_pdf(data: bytes) -> tuple[bool, str]:
    if not data:
        return False, "空响应"
    head = data[:2048].lstrip()
    if not head.startswith(b"%PDF"):
        if b"<html" in head[:600].lower():
            return False, "返回的是网页而非 PDF（可能需要登录/验证码）"
        return False, "不是 PDF 文件"
    if len(data) < 15_000:
        return False, f"文件过小（{len(data)//1024} KB），疑似残缺"
    pages = data.count(b"/Type /Page") + data.count(b"/Type/Page")
    return True, f"{len(data)/1024/1024:.1f} MB，约 {max(pages,1)} 页"


def extract_pdf_link(html_text: str, base_url: str = "") -> str:
    """从落地页 HTML 里找出真正的 PDF 链接（出版商/机构库的通用约定）。"""
    pats = (
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url',
        r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)',
        r'<meta[^>]+name=["\']eprints.document_url["\'][^>]+content=["\']([^"\']+)',
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    )
    for pat in pats:
        m = re.search(pat, html_text, re.I)
        if m:
            url = _html.unescape(m.group(1)).strip()
            if url and base_url:
                url = urllib.parse.urljoin(base_url, url)
            return url
    return ""


def download_pdf(url: str, dest: str, cfg: dict, referer: str | None = None,
                 depth: int = 0) -> tuple[bool, str]:
    headers = {"User-Agent": UA_PDF, "Accept": "application/pdf,*/*"}
    if referer:
        headers["Referer"] = referer
    for attempt in range(2):
        try:
            r = http_get(url, headers=headers, timeout=cfg.get("download_timeout", 120),
                         retries=0, max_bytes=80 * 1024 * 1024)
            if r.status != 200:
                return False, f"HTTP {r.status}"
            ok, msg = verify_pdf(r.content)
            if not ok:
                # 落地页：抓 citation_pdf_url 再下一跳
                if depth < 2 and (b"<html" in r.content[:2000].lower()
                                  or b"<!doctype" in r.content[:2000].lower()):
                    nxt = extract_pdf_link(r.text, r.url or url)
                    if nxt and nxt != url:
                        return download_pdf(nxt, dest, cfg, referer=r.url or url, depth=depth + 1)
                return False, msg
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, dest)
            return True, msg
        except Exception as e:
            if attempt == 0:
                time.sleep(1.0)
                continue
            return False, f"{type(e).__name__}: {str(e)[:100]}"
    return False, "未知错误"


# ---------------------------------------------------------------- 落库

def build_record(meta: dict, cfg: dict) -> dict:
    doi = normalize_doi(meta.get("doi", ""))
    year = year_of(meta) or ""
    first = first_author_surname(meta.get("authors") or [])
    name = safe_name(f"{year}_{first}_{meta.get('title','')}", 110) + ".pdf"
    return {
        "doi": doi,
        "title": tidy_title(meta.get("title", "")),
        "authors": meta.get("authors") or [],
        "journal": meta.get("journal", ""),
        "year": year,
        "type": meta.get("type", ""),
        "filename": name,
        "catalogued_at": ISO(),
    }


def catalog_upsert(paths: Paths, record: dict) -> None:
    """catalog.jsonl：以 DOI 为键做 upsert。"""
    rows, found = [], False
    if os.path.exists(paths.catalog):
        with open(paths.catalog, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if record.get("doi") and row.get("doi") == record.get("doi"):
                    row.update(record)
                    rows.append(row)
                    found = True
                else:
                    rows.append(row)
    if not found:
        rows.append(record)
    tmp = paths.catalog + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, paths.catalog)


def catalog_load(paths: Paths) -> list[dict]:
    rows = []
    if not os.path.exists(paths.catalog):
        return rows
    with open(paths.catalog, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


# ---------------------------------------------------------------- 非 OA 决策卡

def channel_links(doi: str, cfg: dict, title: str = "") -> list[dict]:
    q = urllib.parse.quote(doi, safe="")
    qs = urllib.parse.quote(f'"{title}"' if title else doi, safe="")
    return [
        {
            "no": 1, "name": "科研通 文献互助", "badge": "推荐 · 成功率高",
            "links": [
                ("发布求助", cfg["ablesci_create"]),
                ("站内搜是否已有人求助", cfg["ablesci_search"].format(query=q)),
            ],
            "steps": [
                "打开链接 → 微信/邮箱注册登录（一次即可，浏览器会记住）",
                "点「发布求助」，粘贴 DOI 或标题，选择悬赏积分（一般 50–200 分）",
                "等待应助（常见 10 分钟–24 小时），站内信/邮件通知后下载 PDF",
                f"回填：`python paper_radar.py admit {doi} <下载到的pdf路径>`",
            ],
            "eta": "10 分钟 – 24 小时", "cost": "积分（签到/互助可积累）",
        },
        {
            "no": 2, "name": "Sci-Hub", "badge": "最快 · 有验证码",
            "links": [("按 DOI 直达", f"{cfg['scihub_mirrors'][0]}/{doi}")]
                     + [(f"备用镜像 {i+1}", f"{m}/{doi}") for i, m in enumerate(cfg["scihub_mirrors"][1:])],
            "steps": [
                "打开链接 → 若出现人机验证（滑块/Altcha），手动完成",
                "PDF 直接内嵌显示；点右上角下载保存",
                "镜像失效时换用备用链接，或搜索引擎搜「sci-hub 最新域名」",
            ],
            "eta": "1 分钟", "cost": "免费（灰色渠道）",
        },
        {
            "no": 3, "name": "SciDown", "badge": "国内直连",
            "links": [("按 DOI 检索", cfg["scidown"].format(doi=doi))],
            "steps": [
                "打开链接，页面会自动带入 DOI",
                "等待结果 → 有「下载/预览」按钮则点击保存",
                "未命中时把 DOI 换成标题再搜一次",
            ],
            "eta": "1–3 分钟", "cost": "免费（灰色渠道）",
        },
        {
            "no": 4, "name": "X-MOL", "badge": "可找 OA 版/预印本",
            "links": [
                ("按 DOI 打开文献页", cfg["xmol_paper"].format(doi=doi)),
                ("站内搜索", cfg["xmol_search"].format(query=q)),
            ],
            "steps": [
                "打开文献页，看「原文链接」「OA 全文」标签",
                "若只有摘要，看页内是否列出预印本/机构库版本",
                "仍无 → 回退到通道 1 或学校图书馆",
            ],
            "eta": "1–5 分钟", "cost": "免费",
        },
    ]


def card_markdown(rec: dict, cfg: dict, paths: Paths) -> str:
    doi = rec.get("doi", "")
    title = rec.get("title", "") or "(标题缺失)"
    authors = rec.get("authors") or []
    au = "、".join(authors[:3]) + (" 等" if len(authors) > 3 else "")
    oa_rep = rec.get("oa_report") or {}
    attempts = rec.get("attempts") or []
    up = oa_rep.get("unpaywall") or {}
    oal = oa_rep.get("openalex") or {}
    claimed = bool(up.get("is_oa")) or bool(oal.get("is_oa"))
    if claimed:
        oa_line = ("**OA 判定**：🟡 官方源标记为开放获取，但自动下载被打断"
                   "（出版商反爬 / 需机构登录 / 需人机验证）")
    elif attempts:
        oa_line = f"**OA 判定**：🔴 非 OA（已试 {len(attempts)} 个候选链接，均无免费全文）"
    else:
        oa_line = "**OA 判定**：🔴 非 OA / 未找到合法免费副本（Unpaywall、OpenAlex、Europe PMC 均无）"
    lines = [
        f"# 📄 待获取全文：{title}",
        "",
        f"- **DOI**：`{doi}`",
        f"- **期刊**：{rec.get('journal') or '—'}（{rec.get('year') or '—'}）",
        f"- **作者**：{au or '—'}",
        f"- {oa_line}",
        f"- **生成时间**：{ISO()}",
        f"- **原始链接**：https://doi.org/{doi}",
        "",
        "> 自动获取失败 ≠ 没有全文。下面四条通道按成功率排序，任一条拿到后请用 `admit` 回填入库。",
        "",
    ]
    if claimed:
        host = (up.get("host") or oal.get("host") or "").strip()
        host = {"publisher": "出版商站点", "repository": "机构库/仓储"}.get(host, host) or "官方源"
        landing = (up.get("best_landing") or "").strip()
        lines += [
            "## 通道 0 · 浏览器直开  `优先试这个`",
            "",
            f"- [打开出版商页面](https://doi.org/{doi})",
        ]
        if landing and "doi.org" not in landing:
            lines.append(f"- [Unpaywall 指到的开放版本]({landing})")
        lines += [
            "",
            f"**为什么**：Unpaywall / OpenAlex 判定这篇**是开放获取**（{host}），"
            "只是脚本被出版商的机器人拦截页挡住。用你自己的浏览器打开上面的链接"
            "（带正常 Cookie 与浏览器指纹），通常能直接看到 PDF 或「Download PDF」按钮。",
            "",
            "> 浏览器里也打不开、或只给摘要 → 再往下走通道 1~4。",
            "",
        ]
    for ch in channel_links(doi, cfg, title):
        lines.append(f"## 通道 {ch['no']} · {ch['name']}  `{ch['badge']}`")
        lines.append("")
        for label, url in ch["links"]:
            lines.append(f"- [{label}]({url})")
        lines.append("")
        lines.append("**操作步骤**")
        for i, s in enumerate(ch["steps"], 1):
            lines.append(f"{i}. {s}")
        lines.append("")
        lines.append(f"**预计**：{ch['eta']} ｜ **成本**：{ch['cost']}")
        lines.append("")
    if attempts:
        lines += ["## 本次自动尝试记录（全部失败）", ""]
        for a in attempts:
            lines.append(f"- `{a.get('source','')}` — {a.get('error','')}")
            lines.append(f"  {a.get('url','')[:120]}")
        lines.append("")
    lines += [
        "## 兜底方案（都失败时）",
        "",
        f"1. **学校图书馆**：在 [馆藏检索]({cfg['bing'].format(query=urllib.parse.quote(title + ' ' + doi))}) 里搜标题，或走学校 WebVPN / CARSI 直连出版商",
        "2. **文献传递**：CALIS / CASHL / 中科院文献情报中心，通常 1–3 个工作日，价格低",
        "3. **直接联系作者**：ResearchGate 求全文，或按通讯作者邮箱发一封 3 行邮件（成功率意外地高）",
        "",
        "---",
        "",
        "⚠️ **合规提示**：通道 2、3 属版权灰色渠道，请遵守所在单位/期刊的版权规定，仅用于个人学习研究。",
        "",
    ]
    return "\n".join(lines)


def write_card(paths: Paths, rec: dict, cfg: dict) -> str:
    md = card_markdown(rec, cfg, paths)
    fn = safe_name(f"待获取_{rec.get('year','')}_{first_author_surname(rec.get('authors') or [])}_"
                   f"{normalize_doi(rec.get('doi','')).replace('/', '_')}", 120) + ".md"
    path = os.path.join(paths.inbox, fn)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path, md


# ---------------------------------------------------------------- 呈现

def print_record(rec: dict, card_path: str = "", pdf: str = "") -> None:
    print()
    print("┌─ " + (rec.get("title") or "(无标题)")[:100])
    au = rec.get("authors") or []
    print("│ 作者 : " + ("、".join(au[:4]) + (" 等" if len(au) > 4 else "") or "—"))
    print(f"│ 期刊 : {rec.get('journal') or '—'} ({rec.get('year') or '—'})")
    print(f"│ DOI  : {rec.get('doi') or '—'}")
    if pdf:
        print(f"│ 状态 : ✅ 已获取 PDF")
        print(f"│ 文件 : {pdf}")
    else:
        print(f"│ 状态 : ⚠️ 未获取（非 OA 或下载失败）")
        if card_path:
            print(f"│ 决策卡: {card_path}")
    print("└" + "─" * 60)


def results_table(results: list[dict]) -> str:
    if not results:
        return "（无结果）"
    lines = []
    for i, r in enumerate(results, 1):
        oa = "🟢" if r.get("is_oa") else ("🔴" if r.get("is_oa") is False else "⚪")
        ident = r.get("doi") or (f"PMC:{r['pmcid']}" if r.get("pmcid") else "—")
        lines.append(f"{i:>2}. {oa} [{r.get('year') or '----'}] {r.get('title','')[:96]}")
        lines.append(f"    {r.get('journal','')[:60]}  |  {ident}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 子命令

def cmd_search(args, cfg: dict, paths: Paths) -> int:
    res = search_literature(args.query, args.limit, cfg, args.source)
    if args.oa_only:
        res = [r for r in res if r.get("is_oa")]
    if args.since:
        res = [r for r in res if (r.get("year") or "0") >= args.since]
    print(results_table(res))
    report = os.path.join(paths.reports, f"search_{_dt.datetime.now():%Y%m%d_%H%M%S}.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"# 检索报告\n\n- 检索词：`{args.query}`\n- 时间：{ISO()}\n"
                f"- 结果：{len(res)} 条\n\n```\n{results_table(res)}\n```\n")
    print(f"\n（报告已存档：{report}）")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.fetch:
        print("\n→ 开始批量获取全文…")
        todo = [r for r in res[: args.limit] if r.get("doi")]
        ok = 0
        for r in todo:
            try:
                got = fetch_one(r["doi"], cfg, paths, open_browser=False, quiet=not args.verbose)
            except Exception as e:
                print(f"  ✗ {r.get('title','')[:56]}  ({type(e).__name__})")
                continue
            rec = got.get("record") or {}
            if got.get("pdf"):
                ok += 1
                print(f"  ✅ {(rec.get('title') or r.get('title',''))[:56]}")
            else:
                print(f"  📄 {(rec.get('title') or r.get('title',''))[:56]}  → 已出决策卡")
            time.sleep(1.0)
        print(f"\n汇总：{ok}/{len(todo)} 篇自动获取成功；其余见 inbox/ 决策卡。")
    return 0


def fetch_one(doi: str, cfg: dict, paths: Paths, *, open_browser: bool = False,
              quiet: bool = False, keep_meta: dict | None = None) -> dict:
    doi = normalize_doi(doi)
    if not quiet:
        print(f"\n[fetch] {doi}")

    meta: dict = {}
    if keep_meta:
        meta.update(keep_meta)
    try:
        meta.update(crossref_meta(doi, cfg))
    except Exception as e:
        if not quiet:
            print(f"  · Crossref 元数据失败：{type(e).__name__}", file=sys.stderr)
    if not meta.get("title"):
        try:
            oa_m = openalex_meta(doi, cfg)
            for k in ("title", "authors", "journal", "year"):
                meta.setdefault(k, oa_m.get(k))
        except Exception:
            pass

    rec = build_record(meta, cfg)
    dest = os.path.join(paths.library, rec["filename"])

    # 已存在 → 跳过
    if os.path.exists(dest):
        rec.update({"status": "exists", "pdf": os.path.relpath(dest, paths.root)})
        catalog_upsert(paths, rec)
        if not quiet:
            print(f"  · 已存在：{rec['pdf']}")
        return {"record": rec, "pdf": dest, "card": ""}

    cands, rep = resolve_oa(doi, cfg, meta, verbose=not quiet)
    attempts: list[dict] = []
    for c in cands:
        if not quiet:
            print(f"  · 试 {c['source']:<14s} {c['url'][:88]}", file=sys.stderr)
        ok, msg = download_pdf(c["url"], dest, cfg)
        if ok:
            rec.update({"status": "downloaded", "oa": True, "oa_source": c["source"],
                        "pdf_url": c["url"], "pdf": os.path.relpath(dest, paths.root),
                        "retrieved_at": ISO(), "attempts": attempts})
            catalog_upsert(paths, rec)
            if not quiet:
                print(f"  · ✅ {msg}  ← {c['source']}")
                print_record(rec, pdf=dest)
            return {"record": rec, "pdf": dest, "card": ""}
        attempts.append({"source": c["source"], "url": c["url"], "error": msg})
        if not quiet:
            print(f"  · ✗ {msg}", file=sys.stderr)

    # 非 OA / 全部失败
    rec.update({"status": "closed", "oa": False, "oa_report": rep, "attempts": attempts,
                "pdf": "", "retrieved_at": ISO()})
    catalog_upsert(paths, rec)
    card_path, md = write_card(paths, rec, cfg)
    if not quiet:
        print_record(rec, card_path=card_path)
        print(f"\n  → 已生成四通道决策卡：{card_path}")
    if open_browser:
        url = f"{cfg['scihub_mirrors'][0]}/{doi}"
        try:
            import webbrowser
            webbrowser.open(url)
            if not quiet:
                print(f"  → 已用浏览器打开：{url}")
        except Exception:
            pass
    return {"record": rec, "pdf": "", "card": card_path, "card_md": md}


def cmd_get(args, cfg: dict, paths: Paths) -> int:
    doi = normalize_doi(args.doi)
    meta = {}
    for fn, name in ((crossref_meta, "Crossref"), (openalex_meta, "OpenAlex")):
        try:
            meta.update({k: v for k, v in fn(doi, cfg).items() if v})
            print(f"  · {name} 元数据 OK", file=sys.stderr)
        except Exception as e:
            print(f"  · {name} 失败：{type(e).__name__}", file=sys.stderr)
    if not meta:
        print("❌ 无法获取元数据，检查 DOI 或网络。")
        return 1
    rec = build_record(meta, cfg)
    cands, rep = resolve_oa(doi, cfg, meta)
    up = rep.get("unpaywall") or {}
    oal = rep.get("openalex") or {}
    oa_claimed = bool(up.get("is_oa")) or bool(oal.get("is_oa"))
    if cands and oa_claimed:
        verdict = "🟢 可免费获取（OA 判定成立 + 有直链）"
    elif cands:
        verdict = "🟡 有候选链接但 OA 判定未证实，需实测下载"
    else:
        verdict = "🔴 非 OA / 未找到合法免费副本"
    print()
    print("=" * 70)
    print(f"标题  {rec['title']}")
    print(f"作者  {('、'.join(rec['authors'][:5]) or '—')}")
    print(f"期刊  {rec['journal']} ({rec['year']})")
    print(f"DOI   {rec['doi']}")
    print(f"类型  {rec.get('type') or '—'}")
    print("-" * 70)
    print(f"OA 判定  {verdict}")
    if rep.get("unpaywall"):
        u = rep["unpaywall"]
        if u.get("error"):
            print(f"  Unpaywall : 查询失败 {u['error'][:60]}")
        else:
            print(f"  Unpaywall : is_oa={u.get('is_oa')} status={u.get('status') or '—'} host={u.get('host') or '—'}")
    if rep.get("openalex"):
        o = rep["openalex"]
        if o.get("error"):
            print(f"  OpenAlex  : 查询失败 {o['error'][:60]}")
        else:
            print(f"  OpenAlex  : is_oa={o.get('is_oa')} status={o.get('status') or '—'}")
    if rep.get("europepmc"):
        e = rep["europepmc"]
        if e.get("error"):
            print(f"  EuropePMC : 查询失败 {e['error'][:60]}")
        else:
            print(f"  EuropePMC : pmcid={e.get('pmcid') or '—'} oa={e.get('is_oa')} pdf={e.get('has_pdf')}")
    if cands:
        print(f"  候选 PDF  : {len(cands)} 个")
        for c in cands[:6]:
            print(f"    - [{c['source']}] {c['url'][:96]}")
    print("=" * 70)
    if meta.get("abstract"):
        print("\n摘要：" + meta["abstract"][:500] + ("…" if len(meta["abstract"]) > 500 else ""))
    if not (cands and oa_claimed):
        card_path, md = write_card(paths, rec, cfg)
        print(f"\n→ 已生成四通道决策卡：{card_path}")
        if args.show_card:
            print("\n" + md)
        if args.open:
            try:
                import webbrowser
                webbrowser.open(f"{cfg['scihub_mirrors'][0]}/{doi}")
                print(f"→ 已用浏览器打开 Sci-Hub：{cfg['scihub_mirrors'][0]}/{doi}")
            except Exception:
                pass
    elif args.open and cands:
        try:
            import webbrowser
            webbrowser.open(cands[0]["url"])
        except Exception:
            pass
    catalog_upsert(paths, {**rec, "status": "open" if oa_claimed else "closed", "oa": oa_claimed})
    return 0


def cmd_fetch(args, cfg: dict, paths: Paths) -> int:
    target = args.target
    if is_doi(target):
        r = fetch_one(normalize_doi(target), cfg, paths, open_browser=args.open)
        return 0 if r.get("pdf") else 2
    # 检索词 → 批量取
    res = search_literature(target, args.limit, cfg, args.source)
    if args.oa_only:
        res = [r for r in res if r.get("is_oa")]
    if not res:
        print("❌ 没有检索到结果。")
        return 1
    print(results_table(res))
    ok = 0
    for r in res:
        if not r.get("doi"):
            continue
        print(f"\n[{r['doi']}] {r.get('title','')[:70]}")
        try:
            rec = fetch_one(r["doi"], cfg, paths, keep_meta=r)
            ok += 1 if rec.get("pdf") else 0
        except Exception as e:
            print(f"  · 失败：{type(e).__name__}: {e}")
        time.sleep(max(0.0, args.delay))
    print(f"\n汇总：{ok}/{len(res)} 篇自动获取成功；其余见 inbox/ 决策卡。")
    return 0


def cmd_batch(args, cfg: dict, paths: Paths) -> int:
    dois = list(args.dois or [])
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    dois.extend(DOI_RE.findall(line) or [line])
    seen, uniq = set(), []
    for d in dois:
        n = normalize_doi(d)
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    print(f"[batch] 共 {len(uniq)} 个 DOI")
    ok = 0
    for i, d in enumerate(uniq, 1):
        print(f"\n({i}/{len(uniq)})")
        try:
            r = fetch_one(d, cfg, paths, quiet=False)
            if r.get("pdf"):
                ok += 1
        except Exception as e:
            print(f"  · 失败：{type(e).__name__}: {e}")
        time.sleep(max(0.0, args.delay))
    print(f"\n汇总：{ok}/{len(uniq)} 篇自动获取成功。")
    return 0


def cmd_card(args, cfg: dict, paths: Paths) -> int:
    doi = normalize_doi(args.doi)
    meta = {}
    try:
        meta = crossref_meta(doi, cfg)
    except Exception:
        pass
    rec = build_record(meta, cfg)
    if args.title:
        rec["title"] = args.title
    card_path, md = write_card(paths, rec, cfg)
    print(md)
    print(f"\n（已保存：{card_path}）")
    if args.open:
        try:
            import webbrowser
            webbrowser.open(f"{cfg['scihub_mirrors'][0]}/{doi}")
        except Exception:
            pass
    return 0


def cmd_admit(args, cfg: dict, paths: Paths) -> int:
    doi = normalize_doi(args.doi)
    src = os.path.abspath(args.pdf)
    if not os.path.exists(src):
        print(f"❌ 文件不存在：{src}")
        return 1
    with open(src, "rb") as f:
        head = f.read(8)
    if not head.startswith(b"%PDF"):
        print("❌ 这不是 PDF 文件（可能是网页/快捷方式）。")
        return 1
    meta = {}
    try:
        meta = crossref_meta(doi, cfg)
    except Exception:
        pass
    rec = build_record(meta, cfg)
    rec["doi"] = doi
    dest = os.path.join(paths.library, rec["filename"])
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
    size = os.path.getsize(dest) / 1024 / 1024
    rec.update({"status": "manual", "oa": False, "pdf": os.path.relpath(dest, paths.root),
                "source_channel": args.channel or "manual", "retrieved_at": ISO()})
    catalog_upsert(paths, rec)
    print(f"✅ 已入库：{dest}\n   大小 {size:.1f} MB ｜ DOI {doi}")
    print(f"   → 下一步可精读：让 agent 用 paper-deep-read 技能读这篇。")
    return 0


def cmd_status(args, cfg: dict, paths: Paths) -> int:
    rows = catalog_load(paths)
    pdfs = [r for r in rows if r.get("pdf")]
    closed = [r for r in rows if r.get("status") == "closed"]
    cards = [f for f in os.listdir(paths.inbox)] if os.path.isdir(paths.inbox) else []
    print("📚 文献雷达 · 状态")
    print("=" * 56)
    print(f"根目录   {paths.root}")
    print(f"PDF 库   {paths.library}（{len([f for f in os.listdir(paths.library) if f.lower().endswith('.pdf')])} 个文件）")
    print(f"目录记录 {len(rows)} 条 ｜ 已入库 {len(pdfs)} ｜ 待获取 {len(closed)}")
    print(f"待获取决策卡 {len(cards)} 个（inbox/）")
    if pdfs:
        by_year = {}
        for r in pdfs:
            by_year[r.get("year") or "—"] = by_year.get(r.get("year") or "—", 0) + 1
        tops = sorted(by_year.items(), reverse=True)[:8]
        print("年份分布 " + "  ".join(f"{y}:{n}" for y, n in tops))
        oa = sum(1 for r in pdfs if r.get("oa"))
        print(f"自动获取 {oa} 篇 ｜ 人工回填 {len(pdfs)-oa} 篇")
    if cards:
        print("\n最近待获取：")
        for c in sorted(cards)[-5:]:
            print("  · " + c)
    print("=" * 56)
    return 0


def cmd_config(args, cfg: dict, paths: Paths) -> int:
    if args.email:
        cfg["email"] = args.email
    if args.root:
        cfg["root"] = args.root
    if args.show:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    save_config(cfg)
    print(f"✅ 配置已保存：{CONFIG_PATH}")
    print(json.dumps({k: cfg[k] for k in ("email", "root")}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper_radar",
        description="文献雷达：DOI/检索词 → OA 全文 PDF；非 OA 生成四通道决策卡",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  paper_radar.py search "gut microbiome colorectal cancer" --limit 10 --oa-only
  paper_radar.py get 10.1038/nature14539 --open
  paper_radar.py fetch 10.1038/nature14539
  paper_radar.py card 10.1038/nature14539 --show
  paper_radar.py admit 10.1038/nature14539 D:/Downloads/paper.pdf --channel ablesci
  paper_radar.py status""")
    p.add_argument("--root", help="文献库根目录（覆盖配置）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="检索文献")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--source", default="auto",
                   choices=["auto", "europepmc", "openalex", "crossref", "pubmed"])
    s.add_argument("--oa-only", action="store_true", help="只要 OA 可得的")
    s.add_argument("--since", help="起始年份，如 2020")
    s.add_argument("--fetch", action="store_true", help="检索后直接批量取全文")
    s.add_argument("--json", action="store_true")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_search)

    g = sub.add_parser("get", help="按 DOI 查看元数据与 OA 判定（不下载）")
    g.add_argument("doi")
    g.add_argument("--open", action="store_true", help="浏览器打开候选 PDF 或 Sci-Hub")
    g.add_argument("--show-card", action="store_true")
    g.set_defaults(func=cmd_get)

    f = sub.add_parser("fetch", help="按 DOI 或检索词自动下载 PDF")
    f.add_argument("target")
    f.add_argument("--limit", type=int, default=5)
    f.add_argument("--source", default="auto",
                   choices=["auto", "europepmc", "openalex", "crossref", "pubmed"])
    f.add_argument("--oa-only", action="store_true")
    f.add_argument("--open", action="store_true", help="非 OA 时自动打开 Sci-Hub")
    f.add_argument("--delay", type=float, default=1.0)
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("batch", help="批量 DOI")
    b.add_argument("dois", nargs="*")
    b.add_argument("--file", help="每行一个 DOI（# 开头为注释）")
    b.add_argument("--delay", type=float, default=1.0)
    b.set_defaults(func=cmd_batch)

    c = sub.add_parser("card", help="只生成非 OA 四通道决策卡")
    c.add_argument("doi")
    c.add_argument("--title", help="标题缺失时手动补")
    c.add_argument("--open", action="store_true")
    c.set_defaults(func=cmd_card)

    a = sub.add_parser("admit", help="把人工下载到的 PDF 登记入库")
    a.add_argument("doi")
    a.add_argument("pdf")
    a.add_argument("--channel", help="来源：ablesci/scihub/scidown/xmol/library")
    a.set_defaults(func=cmd_admit)

    st = sub.add_parser("status", help="查看文献库状态")
    st.set_defaults(func=cmd_status)

    cf = sub.add_parser("config", help="配置邮箱/根目录")
    cf.add_argument("--email")
    cf.add_argument("--root")
    cf.add_argument("--show", action="store_true")
    cf.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    try:
        root = resolve_root(cfg, args.root)
    except Exception as e:
        print(f"❌ 无法确定文献库目录：{e}")
        return 1
    paths = Paths(root)
    if not os.path.exists(CONFIG_PATH):
        try:
            save_config(cfg)
        except Exception:
            pass
    return args.func(args, cfg, paths)


if __name__ == "__main__":
    raise SystemExit(main())
