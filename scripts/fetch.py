#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""slide-image-fetch — PPT 配图抓取（整合版）

策略链：Google Images（持久化 Edge + Playwright）→ Bing HTTP → Openverse → Wikimedia Commons
整合来源：
  - GPT 原型 v0.3（异步下载/PIL 验证/SQLite 缓存/失败分类/多路径合流提取）
  - 实测修正（2026-10-16）：
    * connect_over_cdp 改 launch_persistent_context（Edge 端口绑定不稳）
    * 运行前清除 *_proxy 环境变量对 127.0.0.1 的影响（playwright 会走系统代理导致 502）
    * Google sorry/验证码页检测 → 提示用户人工过一次（profile 持久化后 cookie 复用）
    * Commons 兜底必须过滤 MIME（原实现抓回一堆 PDF）
    * 候选命名 name_cN.ext（Agent 逐张 Read 核对后 mv 为正式名），不按尺寸降序选图
    * 黑名单域名（图库水印/新闻 CDN/内容农场/电商）
用法：
  python fetch.py --out <assets目录> --task name:"english keywords" [--task ...] [--cand 3]
"""
from __future__ import annotations

import argparse, asyncio, hashlib, io, json, logging, os, re, sqlite3, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urlparse

# 关键：清除代理环境变量，防止 playwright/系统组件把 127.0.0.1 也送进代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

LOG = logging.getLogger("slide-image-fetch")
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
GOOGLE_HOSTS = ("google.com", "googleusercontent.com", "gstatic.com", "googleapis.com")
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"}

# 水印图库 / 内容农场 / 新闻 CDN / 电商 黑名单
BAD_HOSTS = ["dreamstime", "alamy.", "shutterstock", "gettyimages", "istockphoto", "123rf",
             "stockfood", "depositphotos", "agefotostock", "stock.adobe", "vectorstock",
             "colourbox", "ismcdn.jp", "afpbb", "inkmaginecms", "garrisondental",
             "media-amazon.com", "pinterest.", "imgur.com", "facebook.com",
             "instagram.com", "tiktok.com", "shopify.com", "kuraraynoritake"]

EDGE_PROFILE = os.path.expandvars(r"%USERPROFILE%\.workbuddy\tmp\edge-imgfetch-profile2")


def host_bad(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(b in h for b in BAD_HOSTS)


def valid_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc) and not p.username and not p.password
    except (ValueError, TypeError):
        return False


def image_url_is_possible(url: str) -> bool:
    if not valid_http_url(url):
        return False
    h = urlparse(url).hostname or ""
    if any(h == g or h.endswith("." + g) for g in GOOGLE_HOSTS):
        return False
    return not host_bad(url)


def extract_imgres(href: str):
    if not valid_http_url(href):
        return None, None
    p = urlparse(href)
    if not ((p.hostname or "").endswith("google.com")):
        return None, None
    q = parse_qs(p.query)
    img, page = q.get("imgurl", [None])[0], q.get("imgrefurl", [None])[0]
    return (img if img and valid_http_url(img) else None,
            page if page and valid_http_url(page) else None)


def unique_records(records):
    seen, out = set(), []
    for r in records:
        u = r.get("url")
        if u and u not in seen and valid_http_url(u):
            seen.add(u)
            out.append(r)
    return out


# ---------------- 浏览器：持久化 Edge + sorry 人工通过 ----------------

async def open_browser(p, proxy):
    return await p.chromium.launch_persistent_context(
        EDGE_PROFILE, channel="msedge", headless=False,
        proxy={"server": proxy} if proxy else None,
        args=["--no-first-run", "--no-default-browser-check",
              "--disable-session-crashed-bubble", "--hide-crash-restore-bubble"])


async def wait_if_sorry(page, timeout_s=150):
    """Google 风控页：保持窗口打开，等用户人工点过 reCAPTCHA；cookie 存进持久化 profile。"""
    if "/sorry/" not in page.url:
        return True
    print("\n>>> Google 弹出人机验证：请在刚打开的 Edge 窗口里点一次\"我不是机器人\"，"
          "通过后脚本自动继续（profile 会记住，一般只需一次）…", flush=True)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        await page.wait_for_timeout(1500)
        if "/sorry/" not in page.url:
            print(">>> 验证已通过，继续抓取。\n", flush=True)
            return True
    print(">>> 等待超时，跳过 Google 通道。", flush=True)
    return False


async def discover_google(query, ctx, max_clicks=8, max_candidates=25):
    records, observed = [], []
    page = await ctx.new_page()
    page.set_default_timeout(4500)

    def on_response(resp):
        try:
            mime = resp.headers.get("content-type", "").split(";")[0].lower()
            if resp.ok and (mime.startswith("image/") or resp.request.resource_type == "image") \
                    and image_url_is_possible(resp.url):
                observed.append(resp.url)
        except Exception:
            pass

    page.on("response", on_response)
    await page.goto("https://www.google.com/search?tbm=isch&hl=en&q=" + quote_plus(query),
                    wait_until="domcontentloaded", timeout=25000)
    if not await wait_if_sorry(page):
        await page.close()
        return []
    try:
        await page.locator("img").first.wait_for(state="attached", timeout=9000)
    except Exception:
        LOG.warning("Google: no images attached")
    tiles = await page.evaluate("""() => {
      const els = Array.from(document.querySelectorAll('img'));
      const data = [];
      for (const img of els) {
        const r = img.getBoundingClientRect();
        if (r.width < 70 || r.height < 55) continue;
        const a = img.closest('a[href]');
        if (!a) continue;
        const id = 'wbc' + data.length;
        img.setAttribute('data-wb-id', id);
        data.push({id, thumb: img.currentSrc || img.src || '', href: a.href || '', alt: img.alt || ''});
        if (data.length >= 60) break;
      }
      return data;
    }""")
    LOG.info("Google tiles: %d", len(tiles))

    def add(url, method, thumb="", src_page="", alt=""):
        if url and image_url_is_possible(url):
            records.append({"url": url, "source_page": src_page, "thumbnail": thumb,
                            "alt": alt, "discovery": method, "provider": "google"})

    for t in tiles:  # imgres 链接直接含原图（多路径合流 1）
        img, pg = extract_imgres(t["href"])
        if img:
            add(img, "imgres_url", t["thumb"], pg or t["href"], t["alt"])

    for t in tiles[:max_clicks]:  # 点击预览拿大图（多路径合流 2/3）
        if len({x["url"] for x in records}) >= max_candidates:
            break
        try:
            pre = set(observed)
            await page.locator('img[data-wb-id="' + t["id"] + '"]').click(timeout=4000)
            await page.wait_for_timeout(400)
            preview = await page.evaluate("""() => Array.from(document.querySelectorAll('img'))
              .map(x => ({url:x.currentSrc||x.src||'', w:x.naturalWidth||0, h:x.naturalHeight||0,
                          vis:x.getBoundingClientRect().width||0}))
              .filter(x => x.vis > 160 && x.w >= 450 && x.h >= 280)
              .sort((a,b) => (b.w*b.h)-(a.w*a.h)).slice(0, 12)""")
            img, pg = extract_imgres(t["href"])
            for it in preview:
                add(it["url"], "preview_dom", t["thumb"], pg or t["href"], t["alt"])
            for u in observed:
                if u not in pre:
                    add(u, "click_network", t["thumb"], pg or t["href"], t["alt"])
        except Exception as exc:
            LOG.debug("click failed: %s", exc)
    if len(records) < max_candidates:
        for u in observed:
            add(u, "network_unlinked")
    await page.close()
    return unique_records(records)[:max_candidates]


# ---------------- 公共 API 兜底 ----------------

async def discover_openverse(query, client, limit):
    try:
        r = await client.get("https://api.openverse.org/v1/images/",
                             params={"q": query, "page_size": min(limit, 40)})
        r.raise_for_status()
        return [{"url": it["url"], "source_page": it.get("foreign_landing_url") or "",
                 "thumbnail": it.get("thumbnail") or "", "alt": it.get("title") or "",
                 "provider": "openverse", "discovery": "public_api",
                 "license": it.get("license"), "creator": it.get("creator")}
                for it in r.json().get("results", []) if valid_http_url(it.get("url", ""))
                and not host_bad(it["url"])]
    except Exception as exc:
        LOG.warning("Openverse failed: %s", exc)
        return []


async def discover_commons(query, client, limit):
    try:
        r = await client.get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
            "gsrlimit": min(limit, 40), "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "format": "json", "formatversion": 2})
        r.raise_for_status()
        out = []
        for page in r.json().get("query", {}).get("pages", []):
            infos = page.get("imageinfo", [])
            if not infos:
                continue
            info = infos[0]
            # 修正 GPT 版缺陷：必须过滤 MIME，否则会抓回 PDF/视频
            if info.get("mime", "") not in IMAGE_MIMES or not valid_http_url(info.get("url", "")):
                continue
            out.append({"url": info["url"], "source_page": info.get("descriptionurl", ""),
                        "alt": page.get("title", ""), "provider": "wikimedia_commons",
                        "discovery": "public_api",
                        "license": info.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")})
        return out
    except Exception as exc:
        LOG.warning("Commons failed: %s", exc)
        return []


# ---------------- Bing HTTP 快路径 ----------------

async def discover_bing(query, client, limit=40):
    import html as _html
    try:
        r = await client.get("https://www.bing.com/images/search",
                             params={"q": query, "form": "HDRSC2", "first": 1},
                             headers={"User-Agent": AGENT})
        body = r.text
        out = []
        for m in re.finditer(r'<a\b[^>]*class="iusc"[^>]*>', body):
            jm = re.search(r'm="(\{[^"]*?\})"', m.group(0))
            if not jm:
                continue
            try:
                d = json.loads(_html.unescape(jm.group(1)))
            except Exception:
                continue
            u = d.get("murl")
            if u and valid_http_url(u) and not host_bad(u):
                out.append({"url": u, "source_page": d.get("purl", ""),
                            "thumbnail": d.get("turl", ""), "alt": d.get("desc", ""),
                            "provider": "bing", "discovery": "bing_http"})
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        LOG.warning("Bing failed: %s", exc)
        return []


# ---------------- 异步下载 + 验证 + 缓存 ----------------

class Cache:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS assets "
                          "(url TEXT PRIMARY KEY, filename TEXT, sha256 TEXT, w INT, h INT, created REAL)")
        self.conn.commit()

    def read(self, url, folder):
        row = self.conn.execute("SELECT filename,sha256,w,h FROM assets WHERE url=?", (url,)).fetchone()
        if row and (folder / row[0]).exists():
            return {"filename": row[0], "sha256": row[1], "width": row[2], "height": row[3]}
        return None

    def save(self, url, filename, digest, w, h):
        self.conn.execute("INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?)",
                          (url, filename, digest, w, h, time.time()))
        self.conn.commit()


class Downloader:
    def __init__(self, folder, cache, client, max_bytes, min_short, min_long):
        self.folder, self.cache, self.client = folder, cache, client
        self.sem = asyncio.Semaphore(6)
        self.host_sem = {}
        self.max_bytes, self.min_short, self.min_long = max_bytes, min_short, min_long
        self.seen_hashes = set()
        self.failures = Counter()
        self.counter = 0

    async def download(self, record, name):
        url = record["url"]
        saved = self.cache.read(url, self.folder)
        if saved:
            self.counter += 1
            newname = "%s_c%d%s" % (name, self.counter, Path(saved["filename"]).suffix)
            src = self.folder / saved["filename"]
            dst = self.folder / newname
            if not dst.exists():
                dst.write_bytes(src.read_bytes())
            return {**record, **saved, "status": "cached", "filename": newname}
        host = urlparse(url).hostname or ""
        hs = self.host_sem.setdefault(host, asyncio.Semaphore(2))
        async with self.sem, hs:
            return await self._attempt(record, name)

    async def _attempt(self, record, name):
        url = record["url"]
        try:
            async with self.client.stream("GET", url,
                                          headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8"}) as resp:
                if resp.status_code in (403, 429):
                    raise RuntimeError("HTTP %d" % resp.status_code)
                resp.raise_for_status()
                declared = resp.headers.get("content-type", "").split(";")[0].lower().strip()
                if declared and not declared.startswith("image/"):
                    raise RuntimeError("non-image: " + declared)
                n, chunks = 0, []
                async for chunk in resp.aiter_bytes():
                    n += len(chunk)
                    if n > self.max_bytes:
                        raise RuntimeError("too_large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
            Image.MAX_IMAGE_PIXELS = 60_000_000
            im = Image.open(io.BytesIO(raw)); im.verify()
            im = Image.open(io.BytesIO(raw))
            w, h = im.size
            if min(w, h) < self.min_short or max(w, h) < self.min_long:
                raise RuntimeError("too_small:%dx%d" % (w, h))
            fmt = (im.format or "").upper()
            if fmt in ("WEBP", "GIF", "BMP", "TIFF"):
                im.seek(0)
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                mem = io.BytesIO(); im.save(mem, format="PNG")
                content, ext = mem.getvalue(), ".png"
            else:
                content, ext = raw, (".jpg" if fmt == "JPEG" else ".png")
            digest = hashlib.sha256(content).hexdigest()
            if digest in self.seen_hashes:
                return {**record, "status": "duplicate", "sha256": digest, "width": w, "height": h}
            self.seen_hashes.add(digest)
            self.counter += 1
            filename = "%s_c%d%s" % (name, self.counter, ext)
            (self.folder / filename).write_bytes(content)
            self.cache.save(url, filename, digest, w, h)
            return {**record, "status": "downloaded", "filename": filename,
                    "sha256": digest, "width": w, "height": h, "bytes": len(content)}
        except Exception as exc:
            reason = str(exc)[:160]
            self.failures[reason] += 1
            return {**record, "status": "failed", "error": reason}


# ---------------- 主流程 ----------------

async def run_task(name, query, ctx, client, cache, folder, args):
    records = []
    if not args.no_google and ctx is not None:
        try:
            records = await discover_google(query, ctx, args.clicks, args.candidates * 3)
        except Exception as exc:
            LOG.warning("google discovery failed: %s", exc)
    if len(records) < args.candidates:
        records.extend(await discover_bing(query, client))
    if len(records) < args.candidates:
        records.extend(await discover_openverse(query, client, args.candidates * 2))
    if len(records) < args.candidates:
        records.extend(await discover_commons(query, client, args.candidates * 2))
    records = unique_records(records)[: args.candidates * 2]
    LOG.info("[%s] candidates: %d", name, len(records))

    dl = Downloader(folder, cache, client, args.max_mb * 1024 * 1024, args.min_short, args.min_long)
    # 并发下载，凑够 cand 张有效候选即停（前 cand*2 个 URL）
    results = await asyncio.gather(*(dl.download(r, name) for r in records))
    good = [r for r in results if r["status"] in ("downloaded", "cached")]
    for r in results:
        r["task"] = name
    return results, good[: args.cand]


async def main_async(args):
    t0 = time.perf_counter()
    folder = Path(args.out).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    cache = Cache(folder / "cache.sqlite3")

    ctx = None
    if not args.no_google:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            ctx = await open_browser(pw, args.proxy)
        except Exception as exc:
            LOG.warning("browser launch failed: %s", exc)
            ctx = None
            await pw.stop()

    timeout = httpx.Timeout(connect=6, read=20, write=10, pool=10)
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "tasks": {}}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, http2=True,
                                     headers={"User-Agent": AGENT},
                                     limits=httpx.Limits(max_connections=10, max_keepalive_connections=6),
                                     proxy=args.proxy or None) as client:
            for t in args.task:
                name, kw = t.split(":", 1)
                name, kw = name.strip(), kw.strip().strip('"')
                results, good = await run_task(name, kw, ctx, client, cache, folder, args)
                manifest["tasks"][name] = {
                    "query": kw,
                    "stats": dict(Counter(x["status"] for x in results)),
                    "candidates": [{"file": g.get("filename"), "url": g["url"],
                                    "source_page": g.get("source_page", ""),
                                    "provider": g.get("provider"), "license": g.get("license", ""),
                                    "size": [g.get("width"), g.get("height")]} for g in good],
                }
                print("TASK %-18s 有效候选 %d/%d 张" % (name, len(good), args.cand))
    finally:
        if ctx is not None:
            await ctx.close()
            await pw.stop()

    mpath = folder.parent / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    # 同步生成 markdown 来源表
    md = ["# 配图来源（slide-image-fetch）", "",
          "| 用途 | 候选文件 | 引擎 | 尺寸 | 图片URL | 来源页 | 许可 |",
          "| :- | :- | :- | :- | :- | :- | :- |"]
    for name, task in manifest["tasks"].items():
        for c in task["candidates"]:
            md.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                name, c["file"], c["provider"],
                ("%sx%s" % tuple(c["size"])) if c["size"][0] else "?",
                c["url"], c["source_page"], c.get("license") or ""))
    (folder.parent / "image_sources.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"manifest": str(mpath),
                      "total_seconds": round(time.perf_counter() - t0, 1)}, ensure_ascii=False))
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--task", action="append", required=True, help='name:"english keywords"')
    ap.add_argument("--cand", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=10)
    ap.add_argument("--clicks", type=int, default=8)
    ap.add_argument("--proxy", default=os.environ.get("IMG_PROXY", "http://127.0.0.1:7890"))
    ap.add_argument("--no-google", action="store_true")
    ap.add_argument("--max-mb", type=int, default=20)
    ap.add_argument("--min-short", type=int, default=500)
    ap.add_argument("--min-long", type=int, default=900)
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


if __name__ == "__main__":
    a = parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(asyncio.run(main_async(a)))
    except KeyboardInterrupt:
        raise SystemExit(130)
