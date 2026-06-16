#!/usr/bin/env python3
"""批量抓取 tpdog.com（托普量化）全部接口文档并结构化为 JSON。

流程：
  1. 抓 https://www.tpdog.com/p/index/1 导航页，提取所有
     `<dd class="doc" data="{cat}/{id}">` 清单（约 90 个接口）。
  2. 对每个接口，抓 https://www.tpdog.com/doc/p/get/{cat}/{id}，
     用正则解析 HTML 中的：标题、套餐/频率/积分等元信息、接口地址、
     参数表、响应字段表、响应实例 JSON。
  3. 落地到 agent/src/data/tpdog_doc.json，供 loader 查阅。

无需 token、无需登录（文档页是公开的）。带礼貌延时避免压站。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://www.tpdog.com"
NAV_URL = f"{BASE}/p/index/1"
DOC_URL = f"{BASE}/doc/p/get/{{cat}}/{{id}}"
OUTPUT = Path(__file__).resolve().parent.parent / "agent" / "src" / "data" / "tpdog_doc.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": NAV_URL,
}

# 导航页里每个分类块的大标题，用来给 doc 编号归类。
# 导航结构：<a>大分类</a> <dl><dd class="doc" data="1/00101"><a>子项名</a></dd>...</dl>
# 我们用 (dd data, 子项名文本) 配对，并按出现顺序记录最近一个非 doc 的 <a> 作为父分类。


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_nav(html: str) -> list[dict]:
    """从导航页提取 [{category, sub, cat, id}, ...]。

    导航 HTML 形如：
      <a href="javascript:;">基础数据</a>
      <dl class="layui-nav-child">
        <dd class="doc" data="1/00101">
          <a href="/p/index/1/00101" onclick="return false;">股票列表</a>
        </dd>
        ...
      </dl>
    我们记录最近出现的父分类文本，并把每个 doc 的 data 切成 cat/id。
    """
    items: list[dict] = []
    current_cat = ""

    # 按行扫描，跟踪「最近一个顶层 <a> 文本」作为父分类。
    # dd 的 data 和它内部的 <a> 子项名通常紧邻。
    pending_data: dict | None = None  # 正在等待子项名的 doc data

    for line in html.splitlines():
        # 顶层分类：<a href="javascript:;">XXX</a> —— 注意 dd 里的 <a> 也有，需排除。
        # dd 里的 a href 形如 /p/index/1/00101，顶层是 javascript:;
        m_top = re.search(r'<a href="javascript:;">([^<]+)</a>', line)
        if m_top:
            current_cat = m_top.group(1).strip()

        m_dd = re.search(r'class="doc" data="([^"]+)"', line)
        if m_dd:
            pending_data = m_dd.group(1)  # 如 "1/00101"
            continue

        # 子项名：在含 pending_data 之后的下一个 <a href="/p/index/..."> 里。
        if pending_data:
            m_sub = re.search(r'<a href="/p/index/[^"]+"[^>]*>([^<]+)</a>', line)
            if m_sub:
                sub_name = m_sub.group(1).strip()
                cat, _, id_ = pending_data.partition("/")
                items.append(
                    {"category": current_cat, "sub": sub_name, "cat": cat, "id": id_}
                )
                pending_data = None

    return items


def _strip_tags(s: str) -> str:
    """去 HTML 标签 + 折叠空白 + 解码常见实体。"""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'")
          .replace("&nbsp;", " "))
    return s.strip()


def parse_table(html: str, start_marker: str) -> list[dict]:
    """解析某一段标题（如『参数说明』）之后的第一个 <table>，返回行字典列表。

    用第一行 thead 作为 keys。
    """
    # 定位标题后的内容段
    idx = html.find(start_marker)
    if idx < 0:
        return []
    seg = html[idx:]
    # 第一个 <table>...</table>
    m = re.search(r"<table[^>]*>(.*?)</table>", seg, re.S)
    if not m:
        return []

    body = m.group(1)
    # 提取所有行
    thead = re.search(r"<thead>(.*?)</thead>", body, re.S)
    headers: list[str] = []
    if thead:
        for th in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), re.S):
            headers.append(_strip_tags(th))

    rows: list[dict] = []
    tbody = re.search(r"<tbody>(.*?)</tbody>", body, re.S)
    if not tbody:
        return rows
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(1), re.S):
        cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if not cells:
            continue
        if headers and len(headers) == len(cells):
            rows.append(dict(zip(headers, cells)))
        else:
            rows.append({"_raw": cells})
    return rows


def parse_endpoints(html: str) -> dict:
    """提取『接口地址』段里的有TOKEN / 无TOKEN URL（去 token 参数）。"""
    idx = html.find("接口地址")
    seg = html[idx:] if idx >= 0 else ""
    urls = re.findall(r'href="(https?://[^"]+)"[^>]*>[^<]*-?\s*(https?://[^<]+)', seg)
    result = {"with_token": None, "without_token": None}
    # 简化：直接抓所有 api URL，按出现顺序：有TOKEN在前
    all_urls = re.findall(r"(https?://www\.tpdog\.com/api/[^\"<\s]+)", seg)
    # 去重保序
    seen = []
    for u in all_urls:
        if u not in seen:
            seen.append(u)
    if len(seen) >= 1:
        result["with_token"] = seen[0]
    if len(seen) >= 2:
        result["without_token"] = seen[1]
    return result


def parse_example_json(html: str) -> str | None:
    """提取『响应实例』段里 <pre>...</pre> 的 JSON 原文。"""
    idx = html.find("响应实例")
    seg = html[idx:] if idx >= 0 else ""
    m = re.search(r"<pre>(.*?)</pre>", seg, re.S)
    if not m:
        return None
    raw = m.group(1)
    # 去 <span> 包裹
    raw = re.sub(r"<[^>]+>", "", raw)
    return raw.strip()


def parse_meta(html: str) -> dict:
    """提取套餐限制/请求频率/更新频率/开放时间/积分。"""
    meta = {}
    for key in ["套餐限制", "请求频率", "更新频率", "开放时间", "每次调用消耗积分"]:
        m = re.search(rf"{key}[：:]\s*([^<]+)<", html)
        if m:
            meta[key] = _strip_tags(m.group(1))
    return meta


def parse_doc(html: str, nav_item: dict) -> dict:
    """解析单个 doc 页面。"""
    # 标题
    m_title = re.search(r'layui-timeline-title">([^<]+)</h2>', html)
    title = _strip_tags(m_title.group(1)) if m_title else nav_item.get("sub", "")

    return {
        "title": title,
        "nav_category": nav_item.get("category", ""),
        "nav_sub": nav_item.get("sub", ""),
        "cat": nav_item.get("cat", ""),
        "id": nav_item.get("id", ""),
        "meta": parse_meta(html),
        "endpoints": parse_endpoints(html),
        "params": parse_table(html, "参数说明"),
        "response_fields": parse_table(html, "响应说明"),
        "example_raw": parse_example_json(html),
    }


def main() -> None:
    print(f"[*] 抓取导航页 {NAV_URL}")
    nav_html = _fetch(NAV_URL)
    items = parse_nav(nav_html)
    print(f"[*] 导航页解析到 {len(items)} 个接口")

    if not items:
        print("[!] 未解析到接口，HTML 结构可能变化", file=sys.stderr)
        sys.exit(1)

    docs: list[dict] = []
    for i, item in enumerate(items, 1):
        url = DOC_URL.format(cat=item["cat"], id=item["id"])
        try:
            html = _fetch(url)
            doc = parse_doc(html, item)
            docs.append(doc)
            print(f"  [{i:>2}/{len(items)}] {item['cat']}/{item['id']} "
                  f"{item['category']} > {item['sub']}  ->  "
                  f"{doc['endpoints'].get('with_token') or '(无URL)'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i:>2}/{len(items)}] {item['cat']}/{item['id']} 失败: {exc}",
                  file=sys.stderr)
        time.sleep(0.15)  # 礼貌延时

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": BASE,
        "fetched_note": "Auto-fetched via scripts/fetch_tpdog_docs.py",
        "count": len(docs),
        "endpoints": docs,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[*] 已写入 {OUTPUT}  （{len(docs)} 个接口）")


if __name__ == "__main__":
    main()
