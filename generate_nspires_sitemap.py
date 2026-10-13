#!/usr/bin/env python3
"""
NASA NSPIRES (nspires.nasaprs.com) Harvester, Mirror & Sitemap Generator
========================================================================
Harvests all NASA research solicitations (open, future, closed, past),
enriches each with detailed schedules, notices, and document links,
and generates:
  1. Pre-rendered HTML project cards (solicitations/<SID>.html)
  2. Primary sitemap with clean extensionless URLs (nspires_sitemap.xml)
  3. Direct live NASA sitemap (nspires_direct_sitemap.xml)
  4. Plain text URL lists (nspires_urls.txt & nspires_direct_urls.txt)
  5. Structured JSON Lines dataset (nspires_solicitations.jsonl)
  6. Client-side searchable interactive catalog (index.html)
"""

import os
import sys
import re
import json
import time
import html
from html import escape
from datetime import datetime, timezone
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOLICITATIONS_DIR = os.path.join(BASE_DIR, "solicitations")
CACHE_DIR = os.path.join(BASE_DIR, ".nspires_cache")
RAW_CACHE_DIR = os.path.join(CACHE_DIR, "raw")
DISCOVERED_CACHE = os.path.join(CACHE_DIR, "discovered_solicitations.json")
JSONL_FILE = os.path.join(BASE_DIR, "nspires_solicitations.jsonl")
SITEMAP_FILE = os.path.join(BASE_DIR, "nspires_sitemap.xml")
DIRECT_SITEMAP_FILE = os.path.join(BASE_DIR, "nspires_direct_sitemap.xml")
URLS_FILE = os.path.join(BASE_DIR, "nspires_urls.txt")
DIRECT_URLS_FILE = os.path.join(BASE_DIR, "nspires_direct_urls.txt")
INDEX_HTML = os.path.join(BASE_DIR, "index.html")

NSPIRES_HOST = "https://nspires.nasaprs.com"
JSON_API_URL = f"{NSPIRES_HOST}/external/solicitations/solicitationsJSON.do?path=open"
SUMMARY_BASE_URL = f"{NSPIRES_HOST}/external/solicitations/summary.do?path=&method=init&solId="
DOC_BASE_URL = f"{NSPIRES_HOST}/external/viewrepositorydocument"

GH_PAGES_BASE = os.environ.get("GH_PAGES_BASE", "https://oht8woowi8yait9n.github.io/nspires").rstrip("/")
FETCH_LIMIT = int(os.environ.get("FETCH_DETAILS_LIMIT", "0"))  # 0 = unlimited / all
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "6"))

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 (NASA Enterprise Knowledge Ingestion)"


def clean_guid(sid_str: str) -> str:
    """Strip leading/trailing curly braces from a GUID."""
    if not sid_str:
        return ""
    return sid_str.strip().strip("{}")


def get_live_url(clean_sid: str) -> str:
    """Return the official direct live URL for a solicitation."""
    return f"{SUMMARY_BASE_URL}{clean_sid}"


def get_mirror_url(clean_sid: str) -> str:
    """Return the clean extensionless GitHub Pages mirror URL."""
    return f"{GH_PAGES_BASE}/solicitations/{clean_sid}"


def make_request(url: str, data: bytes = None, headers: dict = None, max_retries: int = 4) -> bytes:
    """Send an HTTP request with polite exponential backoff."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"[!] Request failed for {url}: {e}", file=sys.stderr)
                return b""
            sleep_time = (2 ** attempt) * 0.75
            time.sleep(sleep_time)
    return b""


def discover_all_solicitations() -> list:
    """Query NSPIRES JSON endpoint for all solicitations."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(RAW_CACHE_DIR, exist_ok=True)
    
    # Check cache freshness (valid for 12 hours unless overridden)
    if os.path.exists(DISCOVERED_CACHE) and os.environ.get("FORCE_REFRESH") != "1":
        age_hours = (time.time() - os.path.getmtime(DISCOVERED_CACHE)) / 3600
        if age_hours < 12:
            try:
                with open(DISCOVERED_CACHE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached and len(cached) > 1000:
                    print(f"[+] Loaded {len(cached)} discovered solicitations from cache ({DISCOVERED_CACHE})")
                    return cached
            except Exception as e:
                print(f"[*] Cache read error: {e}, fetching fresh catalog...", file=sys.stderr)

    print("[*] Fetching complete solicitation catalog from NSPIRES JSON API...")
    payload = urllib.parse.urlencode({"filtered": "false"}).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    }
    raw = make_request(JSON_API_URL, data=payload, headers=headers)
    if not raw:
        if os.path.exists(DISCOVERED_CACHE):
            try:
                with open(DISCOVERED_CACHE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached and len(cached) > 1000:
                    print(f"[+] Network unavailable; falling back to {len(cached)} solicitations from cache ({DISCOVERED_CACHE})")
                    return cached
            except Exception:
                pass
        print("[!] Failed to fetch solicitation JSON!", file=sys.stderr)
        return []
        
    try:
        obj = json.loads(raw.decode("utf-8", errors="ignore"))
        data = obj.get("aaData", [])
        print(f"[+] Discovered {len(data)} total solicitations.")
        
        # Normalize and sort by release date descending
        normalized = []
        for d in data:
            sid = clean_guid(d.get("sId", ""))
            if not sid:
                continue
            normalized.append({
                "clean_sid": sid,
                "raw_sid": d.get("sId", ""),
                "title": (d.get("title") or "").strip(),
                "solicitation_number": (d.get("solicitation_number") or "").strip(),
                "release_date": (d.get("release_date") or "").strip(),
                "fiscal_year": str(d.get("fiscal_year") or "").strip(),
                "proposal_due": (d.get("proposal_due") or "").strip(),
                "noi_due": (d.get("noi_due") or "").strip(),
                "status": (d.get("status") or "").strip(),
                "container_type": (d.get("container_type") or "").strip(),
                "announcement_type": (d.get("announcement_type") or "").strip(),
                "live_url": get_live_url(sid),
                "mirror_url": get_mirror_url(sid)
            })
            
        with open(DISCOVERED_CACHE, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
            
        return normalized
    except Exception as e:
        print(f"[!] Error parsing solicitations JSON: {e}", file=sys.stderr)
        return []


def parse_summary_html(html_text: str, base_meta: dict) -> dict:
    """Extract detailed dates, directorate, notices, and documents from summary.do HTML."""
    record = dict(base_meta)
    
    # Directorate
    m_dir = re.search(r'Directorate:</span>\s*<span[^>]*>\s*<strong>(.*?)</strong>', html_text, re.DOTALL | re.IGNORECASE)
    if m_dir:
        record["directorate"] = re.sub(r'<[^>]+>', '', m_dir.group(1)).strip()
    else:
        record["directorate"] = "N/A"
        
    # Extract main content container
    m_main = re.search(r'<div class=[\"\']mainContentOneCol[\"\']>(.*?)<div class=[\"\']footerContent[\"\']', html_text, re.DOTALL)
    content = m_main.group(1) if m_main else html_text
    
    # Parse Tables
    tables = re.findall(r'<table\b[^>]*>(.*?)</table>', content, re.DOTALL)
    
    # Schedule / Dates breakdown
    detailed_dates = []
    if len(tables) >= 1:
        date_rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', tables[0], re.DOTALL)
        for row in date_rows[1:]:  # skip header
            cols = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.DOTALL)]
            if len(cols) >= 2:
                detailed_dates.append({"label": cols[0], "date": cols[1]})
    record["detailed_dates"] = detailed_dates
    
    # Other Dates / Town Halls
    other_dates = []
    if len(tables) >= 2 and "Other Dates" in content:
        other_rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', tables[1], re.DOTALL)
        for row in other_rows[1:]:
            cols = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.DOTALL)]
            if len(cols) >= 2:
                other_dates.append({"title": cols[0], "date": cols[1]})
    record["other_dates"] = other_dates
    
    # Notices block
    m_notice = re.search(r'<div class=[\"\']dataDisplayRow solNoticeBox[\"\']>(.*?)</div>\s*</div>', content, re.DOTALL)
    if m_notice:
        clean_notice = re.sub(r'<script\b[^>]*>.*?</script>', '', m_notice.group(1), flags=re.DOTALL)
        clean_notice = re.sub(r'<[^>]+>', ' ', clean_notice)
        clean_notice = re.sub(r'\s+', ' ', clean_notice).strip()
        record["notices"] = clean_notice
    else:
        record["notices"] = ""
        
    # Documents
    documents = []
    doc_matches = re.findall(r'<a\b[^>]*href=[\"\']([^\"\']*(?:viewrepositorydocument|\.pdf)[^\"\']*)[\"\'][^>]*>(.*?)</a>', content, re.DOTALL | re.IGNORECASE)
    for href, doc_title in doc_matches:
        clean_title = re.sub(r'<[^>]+>', '', doc_title).strip()
        if not clean_title or clean_title.lower() in ["download", "view"]:
            continue
        # Format absolute URL
        if href.startswith("../"):
            href = f"{NSPIRES_HOST}/external/{href[3:]}"
        elif href.startswith("/"):
            href = f"{NSPIRES_HOST}{href}"
        elif not href.startswith("http"):
            href = f"{NSPIRES_HOST}/external/{href}"
            
        documents.append({
            "title": clean_title,
            "url": href
        })
    record["documents"] = documents
    
    # Omnibus Info
    m_omni = re.search(r'Omnibus Information</h3>\s*<div[^>]*>(.*?)</div>', content, re.DOTALL)
    if m_omni:
        omni_text = re.sub(r'<[^>]+>', ' ', m_omni.group(1)).strip()
        record["omnibus_info"] = omni_text
    else:
        record["omnibus_info"] = ""
        
    return record


def fetch_solicitation_detail(item: dict) -> dict:
    """Fetch and parse detailed solicitation metadata with local caching."""
    clean_sid = item["clean_sid"]
    raw_path = os.path.join(RAW_CACHE_DIR, f"{clean_sid}.html")
    
    html_text = ""
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                html_text = f.read()
        except Exception:
            pass
            
    if not html_text:
        url = get_live_url(clean_sid)
        raw_bytes = make_request(url)
        if raw_bytes:
            html_text = raw_bytes.decode("utf-8", errors="ignore")
            try:
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(html_text)
            except Exception:
                pass
                
    if html_text:
        return parse_summary_html(html_text, item)
    return item


def render_html_page(sol: dict) -> str:
    """Pre-render a responsive, accessible HTML project card for a solicitation."""
    title = escape(sol.get("title") or "NASA Solicitation")
    sol_no = escape(sol.get("solicitation_number") or "N/A")
    dir_name = escape(sol.get("directorate") or "N/A")
    status = escape(sol.get("status") or "N/A")
    ann_type = escape(sol.get("announcement_type") or "N/A")
    cont_type = escape(sol.get("container_type") or "N/A")
    fy = escape(sol.get("fiscal_year") or "N/A")
    rel_date = escape(sol.get("release_date") or "N/A")
    prop_due = escape(sol.get("proposal_due") or "N/A")
    noi_due = escape(sol.get("noi_due") or "N/A")
    notices = escape(sol.get("notices") or "")
    omni = escape(sol.get("omnibus_info") or "")
    
    clean_sid = sol["clean_sid"]
    live_url = escape(get_live_url(clean_sid))
    raw_live_url = get_live_url(clean_sid)
    
    # Detailed dates table rows
    dates_html = ""
    if sol.get("detailed_dates"):
        rows = "".join(f"<tr><td>{escape(d.get('label', ''))}</td><td><strong>{escape(d.get('date', ''))}</strong></td></tr>" for d in sol["detailed_dates"])
        dates_html = f"""
        <div class="card-section">
          <h3>Key Solicitation Dates</h3>
          <table class="table-dates">
            <thead><tr><th>Milestone / Label</th><th>Deadline / Date</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """
        
    # Other Dates (Town Halls)
    other_dates_html = ""
    if sol.get("other_dates"):
        rows = "".join(f"<tr><td>{escape(d.get('title', ''))}</td><td><strong>{escape(d.get('date', ''))}</strong></td></tr>" for d in sol["other_dates"])
        other_dates_html = f"""
        <div class="card-section">
          <h3>Events & Additional Milestones</h3>
          <table class="table-dates">
            <thead><tr><th>Event</th><th>Date</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """
        
    # Notices HTML
    notices_html = ""
    if notices:
        notices_html = f"""
        <div class="card-section">
          <h3>Special Notices & Amendments</h3>
          <div class="notice-box"><p>{notices}</p></div>
        </div>
        """
        
    # Documents HTML
    docs_html = ""
    if sol.get("documents"):
        items = "".join(
            f"<li><a href='{escape(doc['url'])}' target='_blank' rel='noopener noreferrer' class='doc-link'>"
            f"<span class='doc-icon'>📄</span> {escape(doc['title'])}</a></li>"
            for doc in sol["documents"]
        )
        docs_html = f"""
        <div class="card-section">
          <h3>Announcement Documents & Guidelines ({len(sol['documents'])})</h3>
          <ul class="doc-list">{items}</ul>
        </div>
        """
        
    # Omnibus HTML
    omni_html = ""
    if omni:
        omni_html = f"""
        <div class="card-section">
          <h3>Omnibus / Parent Announcement</h3>
          <p class="omnibus-box">{omni}</p>
        </div>
        """

    # Badge class
    status_lower = status.lower()
    if "open" in status_lower or "due" in status_lower:
        badge_cls = "badge-open"
    else:
        badge_cls = "badge-closed"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA NSPIRES: {title} ({sol_no})</title>
  <link rel="canonical" href="{live_url}">
  <meta property="og:url" content="{live_url}">
  <meta property="og:title" content="{title} - NASA NSPIRES">
  <meta property="og:description" content="Solicitation {sol_no} ({ann_type}) under {dir_name}. Status: {status}.">
  <meta property="og:site_name" content="NASA NSPIRES">
  <style>
    :root {{
      --nasa-blue: #0b3d91;
      --nasa-red: #e03c31;
      --nasa-dark: #111b24;
      --bg-gray: #f8f9fa;
      --border-gray: #e2e8f0;
      --text-dark: #1a202c;
      --text-muted: #4a5568;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      line-height: 1.6;
      color: var(--text-dark);
      background-color: var(--bg-gray);
      margin: 0;
      padding: 0;
    }}
    .header {{
      background: linear-gradient(135deg, var(--nasa-dark) 0%, var(--nasa-blue) 100%);
      color: #ffffff;
      padding: 2.5rem 1.5rem;
      text-align: center;
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
    }}
    .header h1 {{ margin: 0 0 0.5rem 0; font-size: 1.8rem; font-weight: 700; }}
    .header p {{ margin: 0; opacity: 0.9; font-size: 1.05rem; }}
    .container {{
      max-width: 1000px;
      margin: -1.5rem auto 3rem auto;
      padding: 0 1rem;
    }}
    .card {{
      background: #ffffff;
      border-radius: 8px;
      border: 1px solid var(--border-gray);
      box-shadow: 0 10px 15px -3px rgba(0,0,0,0.05);
      padding: 2rem;
      margin-bottom: 2rem;
    }}
    .card-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 1rem;
      border-bottom: 2px solid var(--bg-gray);
      padding-bottom: 1.25rem;
      margin-bottom: 1.5rem;
    }}
    .sol-title {{
      font-size: 1.5rem;
      font-weight: 700;
      color: var(--nasa-blue);
      margin: 0 0 0.5rem 0;
    }}
    .sol-number {{
      font-size: 1.1rem;
      color: var(--text-muted);
      font-weight: 600;
    }}
    .badges {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
    .badge {{
      display: inline-block;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.85rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .badge-open {{ background-color: #def7ec; color: #03543f; }}
    .badge-closed {{ background-color: #f3f4f6; color: #374151; }}
    .badge-type {{ background-color: #e1effe; color: #1e429f; }}
    .grid-meta {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1.25rem;
      margin-bottom: 1.5rem;
      background: var(--bg-gray);
      padding: 1.25rem;
      border-radius: 6px;
    }}
    .meta-item strong {{
      display: block;
      font-size: 0.8rem;
      text-transform: uppercase;
      color: var(--text-muted);
      letter-spacing: 0.05em;
      margin-bottom: 0.25rem;
    }}
    .meta-item span {{ font-size: 1rem; font-weight: 600; color: var(--text-dark); }}
    .card-section {{ margin-top: 2rem; }}
    .card-section h3 {{
      font-size: 1.15rem;
      color: var(--nasa-dark);
      border-bottom: 1px solid var(--border-gray);
      padding-bottom: 0.5rem;
      margin-bottom: 1rem;
    }}
    .table-dates {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 0.5rem;
    }}
    .table-dates th, .table-dates td {{
      padding: 0.75rem 1rem;
      text-align: left;
      border-bottom: 1px solid var(--border-gray);
    }}
    .table-dates th {{ background: var(--bg-gray); font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; }}
    .notice-box {{
      background: #fffbeb;
      border-left: 4px solid #f59e0b;
      padding: 1rem 1.25rem;
      border-radius: 4px;
      font-size: 0.95rem;
    }}
    .omnibus-box {{
      background: #eff6ff;
      border-left: 4px solid #3b82f6;
      padding: 1rem 1.25rem;
      border-radius: 4px;
      font-size: 0.95rem;
    }}
    .doc-list {{ list-style: none; padding: 0; margin: 0; }}
    .doc-list li {{ margin-bottom: 0.75rem; }}
    .doc-link {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.6rem 1rem;
      background: #f1f5f9;
      color: var(--nasa-blue);
      text-decoration: none;
      border-radius: 6px;
      font-weight: 600;
      transition: background 0.15s;
    }}
    .doc-link:hover {{ background: #e2e8f0; text-decoration: underline; }}
    .actions-bar {{
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      margin-top: 2rem;
      padding-top: 1.5rem;
      border-top: 1px solid var(--border-gray);
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.75rem 1.5rem;
      border-radius: 6px;
      font-weight: 600;
      font-size: 0.95rem;
      text-decoration: none;
      cursor: pointer;
    }}
    .btn-nasa {{ background: var(--nasa-blue); color: #ffffff; }}
    .btn-nasa:hover {{ background: #082d6b; }}
    .btn-catalog {{ background: #e2e8f0; color: var(--text-dark); }}
    .btn-catalog:hover {{ background: #cbd5e1; }}
    .footer {{
      text-align: center;
      font-size: 0.85rem;
      color: var(--text-muted);
      padding: 2rem 1rem;
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>NASA Research Opportunities Online</h1>
    <p>NASA Solicitation and Proposal Integrated Review and Evaluation System (NSPIRES)</p>
  </div>

  <div class="container">
    <div class="card">
      <div class="card-top">
        <div>
          <h2 class="sol-title">{title}</h2>
          <div class="sol-number">Solicitation Identifier: <strong>{sol_no}</strong></div>
        </div>
        <div class="badges">
          <span class="badge {badge_cls}">{status}</span>
          <span class="badge badge-type">{ann_type}</span>
          <span class="badge badge-type">FY {fy}</span>
        </div>
      </div>

      <div class="grid-meta">
        <div class="meta-item">
          <strong>Mission Directorate</strong>
          <span>{dir_name}</span>
        </div>
        <div class="meta-item">
          <strong>Release Date</strong>
          <span>{rel_date}</span>
        </div>
        <div class="meta-item">
          <strong>Proposal Due Date</strong>
          <span>{prop_due}</span>
        </div>
        <div class="meta-item">
          <strong>Notice of Intent Due</strong>
          <span>{noi_due}</span>
        </div>
        <div class="meta-item">
          <strong>Container Type</strong>
          <span>{cont_type}</span>
        </div>
      </div>

      {dates_html}
      {other_dates_html}
      {notices_html}
      {docs_html}
      {omni_html}

      <div class="actions-bar">
        <a href="{live_url}" class="btn btn-nasa" target="_blank" rel="noopener noreferrer">
          View Official Live Announcement on NASA NSPIRES &rarr;
        </a>
        <a href="{GH_PAGES_BASE}/" class="btn btn-catalog">
          &larr; Back to Full NSPIRES Catalog
        </a>
      </div>
    </div>
  </div>

  <div class="footer">
    <p>NASA Research Solicitations Archive &bull; Ingestion Mirror for NASA Enterprise Knowledge Systems &bull; Authoritative Source: <a href="https://nspires.nasaprs.com/" target="_blank" rel="noopener noreferrer">nspires.nasaprs.com</a></p>
  </div>
</body>
</html>
"""


def load_existing_lastmod(sitemap_path: str) -> dict:
    """Load existing URL -> lastmod mappings to prevent unnecessary diff churn."""
    mapping = {}
    if os.path.exists(sitemap_path):
        try:
            with open(sitemap_path, "r", encoding="utf-8") as f:
                content = f.read()
            for m in re.finditer(r'<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>', content):
                mapping[m.group(1).strip()] = m.group(2).strip()
        except Exception:
            pass
    return mapping


def write_if_changed(path: str, content: str) -> bool:
    """Write content to path only if changed to avoid updating mtimes and git diffs."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return False
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def generate_sitemaps(solicitations: list):
    """Generate primary XML sitemap, direct live XML sitemap, and text URL lists."""
    print("[*] Generating XML sitemaps and plain text URL lists...")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing_mirror_lastmod = load_existing_lastmod(SITEMAP_FILE)
    existing_direct_lastmod = load_existing_lastmod(DIRECT_SITEMAP_FILE)

    # 1. Primary GitHub Pages Sitemap (Clean extensionless URLs)
    root_url = f"{GH_PAGES_BASE}/"
    lines_mirror = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        '  <url>',
        f'    <loc>{root_url}</loc>',
        f'    <lastmod>{existing_mirror_lastmod.get(root_url, today)}</lastmod>',
        '    <changefreq>daily</changefreq>',
        '    <priority>1.0</priority>',
        '  </url>'
    ]
    mirror_urls = [root_url]

    # 2. Direct NASA Live Sitemap
    lines_direct = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    ]
    direct_urls = []

    for sol in solicitations:
        sid = sol["clean_sid"]
        m_url = get_mirror_url(sid)
        d_url = get_live_url(sid)
        
        status = (sol.get("status") or "").lower()
        if "open" in status or "due" in status:
            freq = "daily"
            prio = "0.9"
        else:
            freq = "monthly"
            prio = "0.7"
            
        m_lastmod = existing_mirror_lastmod.get(m_url, today)
        d_lastmod = existing_direct_lastmod.get(d_url, today)

        lines_mirror.append('  <url>')
        lines_mirror.append(f'    <loc>{m_url}</loc>')
        lines_mirror.append(f'    <lastmod>{m_lastmod}</lastmod>')
        lines_mirror.append(f'    <changefreq>{freq}</changefreq>')
        lines_mirror.append(f'    <priority>{prio}</priority>')
        lines_mirror.append('  </url>')
        mirror_urls.append(m_url)

        lines_direct.append('  <url>')
        lines_direct.append(f'    <loc>{escape(d_url)}</loc>')
        lines_direct.append(f'    <lastmod>{d_lastmod}</lastmod>')
        lines_direct.append(f'    <changefreq>{freq}</changefreq>')
        lines_direct.append(f'    <priority>{prio}</priority>')
        lines_direct.append('  </url>')
        direct_urls.append(d_url)

    lines_mirror.append('</urlset>')
    lines_direct.append('</urlset>')

    mirror_xml = "\n".join(lines_mirror) + "\n"
    direct_xml = "\n".join(lines_direct) + "\n"
    mirror_txt = "\n".join(mirror_urls) + "\n"
    direct_txt = "\n".join(direct_urls) + "\n"

    write_if_changed(SITEMAP_FILE, mirror_xml)
    print(f"[+] Wrote {SITEMAP_FILE} ({len(solicitations)} solicitations + root)")

    std_sitemap = os.path.join(BASE_DIR, "sitemap.xml")
    write_if_changed(std_sitemap, mirror_xml)
    print(f"[+] Wrote {std_sitemap}")

    write_if_changed(URLS_FILE, mirror_txt)
    print(f"[+] Wrote {URLS_FILE} ({len(mirror_urls)} URLs)")

    write_if_changed(DIRECT_SITEMAP_FILE, direct_xml)
    print(f"[+] Wrote {DIRECT_SITEMAP_FILE} ({len(solicitations)} URLs)")

    write_if_changed(DIRECT_URLS_FILE, direct_txt)
    print(f"[+] Wrote {DIRECT_URLS_FILE} ({len(direct_urls)} URLs)")



def generate_index_html(solicitations: list):
    """Generate the client-side searchable catalog index.html."""
    print("[*] Generating interactive searchable catalog (index.html)...")
    
    # Pre-compact data for client-side search
    compact_records = []
    fys = set()
    dirs = set()
    types = set()
    statuses = set()

    for s in solicitations:
        fy = s.get("fiscal_year") or ""
        dname = s.get("directorate") or "N/A"
        st = s.get("status") or "N/A"
        atype = s.get("announcement_type") or "N/A"
        
        if fy: fys.add(fy)
        if dname and dname != "N/A": dirs.add(dname)
        if st: statuses.add(st)
        if atype: types.add(atype)
        
        compact_records.append({
            "id": s["clean_sid"],
            "title": s.get("title", ""),
            "no": s.get("solicitation_number", ""),
            "dir": dname,
            "fy": fy,
            "rel": s.get("release_date", ""),
            "due": s.get("proposal_due", ""),
            "st": st,
            "type": atype,
            "docs": len(s.get("documents", []))
        })

    sorted_fys = sorted(fys, key=lambda x: str(x), reverse=True)
    sorted_dirs = sorted(dirs)
    sorted_statuses = sorted(statuses)

    records_json = json.dumps(compact_records)

    index_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NASA NSPIRES Research Opportunities & Solicitations Directory</title>
  <style>
    :root {{
      --nasa-blue: #0b3d91;
      --nasa-dark: #111b24;
      --bg-gray: #f8f9fa;
      --border-gray: #e2e8f0;
      --text-dark: #1a202c;
      --text-muted: #4a5568;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg-gray);
      color: var(--text-dark);
      margin: 0;
      padding: 0;
    }}
    .header {{
      background: linear-gradient(135deg, var(--nasa-dark) 0%, var(--nasa-blue) 100%);
      color: #ffffff;
      padding: 3rem 1.5rem;
      text-align: center;
    }}
    .header h1 {{ margin: 0 0 0.5rem 0; font-size: 2.2rem; font-weight: 700; }}
    .header p {{ margin: 0 auto; max-width: 750px; opacity: 0.9; font-size: 1.1rem; }}
    .container {{
      max-width: 1200px;
      margin: -2rem auto 3rem auto;
      padding: 0 1rem;
    }}
    .filter-card {{
      background: #ffffff;
      border-radius: 8px;
      border: 1px solid var(--border-gray);
      box-shadow: 0 10px 15px -3px rgba(0,0,0,0.05);
      padding: 1.5rem;
      margin-bottom: 1.5rem;
    }}
    .filter-grid {{
      display: grid;
      grid-template-columns: 2fr 1fr 1.5fr 1fr;
      gap: 1rem;
      align-items: flex-end;
    }}
    @media (max-width: 900px) {{
      .filter-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 600px) {{
      .filter-grid {{ grid-template-columns: 1fr; }}
    }}
    .form-group {{ display: flex; flex-direction: column; gap: 0.35rem; }}
    .form-group label {{ font-size: 0.85rem; font-weight: 600; text-transform: uppercase; color: var(--text-muted); }}
    .form-control {{
      padding: 0.65rem 0.85rem;
      border: 1px solid var(--border-gray);
      border-radius: 6px;
      font-size: 0.95rem;
      color: var(--text-dark);
      background: #ffffff;
    }}
    .stats-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
      font-size: 0.95rem;
      color: var(--text-muted);
    }}
    .table-container {{
      background: #ffffff;
      border-radius: 8px;
      border: 1px solid var(--border-gray);
      overflow-x: auto;
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
    }}
    table {{ width: 100%; border-collapse: collapse; text-align: left; }}
    th, td {{ padding: 1rem; border-bottom: 1px solid var(--border-gray); }}
    th {{ background: #f1f5f9; font-size: 0.85rem; text-transform: uppercase; color: var(--text-muted); }}
    tr:hover {{ background-color: #f8fafc; }}
    .sol-link {{ color: var(--nasa-blue); font-weight: 600; text-decoration: none; }}
    .sol-link:hover {{ text-decoration: underline; }}
    .badge {{
      display: inline-block;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
    }}
    .badge-open {{ background: #def7ec; color: #03543f; }}
    .badge-closed {{ background: #f3f4f6; color: #374151; }}
    .pagination {{
      display: flex;
      justify-content: center;
      gap: 0.5rem;
      margin-top: 1.5rem;
    }}
    .page-btn {{
      padding: 0.5rem 1rem;
      border: 1px solid var(--border-gray);
      background: #ffffff;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
    }}
    .page-btn.active {{ background: var(--nasa-blue); color: #ffffff; border-color: var(--nasa-blue); }}
    .page-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>NASA Research Solicitations Catalog</h1>
    <p>Search all {len(solicitations)} NASA research announcements, solicitations, guidelines, and document packages from NSPIRES.</p>
  </div>

  <div class="container">
    <div class="filter-card">
      <div class="filter-grid">
        <div class="form-group">
          <label for="searchInput">Search Title / Solicitation #</label>
          <input type="text" id="searchInput" class="form-control" placeholder="e.g. ROSES, Artemis, NNH26...">
        </div>
        <div class="form-group">
          <label for="fySelect">Fiscal Year</label>
          <select id="fySelect" class="form-control">
            <option value="">All Fiscal Years</option>
            {''.join(f'<option value="{fy}">{fy}</option>' for fy in sorted_fys)}
          </select>
        </div>
        <div class="form-group">
          <label for="dirSelect">Mission Directorate</label>
          <select id="dirSelect" class="form-control">
            <option value="">All Directorates</option>
            {''.join(f'<option value="{escape(d)}">{escape(d)}</option>' for d in sorted_dirs)}
          </select>
        </div>
        <div class="form-group">
          <label for="statusSelect">Status</label>
          <select id="statusSelect" class="form-control">
            <option value="">All Statuses</option>
            {''.join(f'<option value="{escape(st)}">{escape(st)}</option>' for st in sorted_statuses)}
          </select>
        </div>
      </div>
    </div>

    <div class="stats-bar">
      <div id="resultCount">Showing all {len(solicitations)} solicitations</div>
      <div>Indexed from <a href="https://nspires.nasaprs.com/" target="_blank" rel="noopener noreferrer">NSPIRES</a></div>
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>Solicitation Title</th>
            <th>Number</th>
            <th>Directorate</th>
            <th>FY</th>
            <th>Due Date</th>
            <th>Status</th>
            <th>Links</th>
          </tr>
        </thead>
        <tbody id="solTableBody"></tbody>
      </table>
    </div>

    <div class="pagination" id="paginationControls"></div>
  </div>

  <script>
    const allRecords = {records_json};
    let filteredRecords = allRecords;
    let currentPage = 1;
    const pageSize = 30;

    const searchInput = document.getElementById('searchInput');
    const fySelect = document.getElementById('fySelect');
    const dirSelect = document.getElementById('dirSelect');
    const statusSelect = document.getElementById('statusSelect');
    const tableBody = document.getElementById('solTableBody');
    const resultCount = document.getElementById('resultCount');
    const pagination = document.getElementById('paginationControls');

    function filterData() {{
      const query = searchInput.value.toLowerCase().trim();
      const fy = fySelect.value;
      const dir = dirSelect.value;
      const status = statusSelect.value;

      filteredRecords = allRecords.filter(r => {{
        if (query && !r.title.toLowerCase().includes(query) && !r.no.toLowerCase().includes(query)) return false;
        if (fy && r.fy !== fy) return false;
        if (dir && r.dir !== dir) return false;
        if (status && r.st !== status) return false;
        return true;
      }});

      currentPage = 1;
      renderTable();
    }}

    function renderTable() {{
      resultCount.textContent = `Showing ${{filteredRecords.length}} of ${{allRecords.length}} solicitations`;
      const start = (currentPage - 1) * pageSize;
      const pageItems = filteredRecords.slice(start, start + pageSize);

      tableBody.innerHTML = pageItems.map(item => {{
        const isClosed = item.st.toLowerCase().includes('closed') || item.st.toLowerCase().includes('past');
        const badgeCls = isClosed ? 'badge-closed' : 'badge-open';
        return `
          <tr>
            <td>
              <a href="solicitations/${{item.id}}" class="sol-link">${{item.title}}</a>
              ${{item.docs > 0 ? `<div style="font-size:0.75rem; color:#64748b; margin-top:2px;">📄 ${{item.docs}} document${{item.docs > 1 ? 's' : ''}}</div>` : ''}}
            </td>
            <td><strong>${{item.no || 'N/A'}}</strong></td>
            <td>${{item.dir || 'N/A'}}</td>
            <td>${{item.fy || 'N/A'}}</td>
            <td>${{item.due || '--'}}</td>
            <td><span class="badge ${{badgeCls}}">${{item.st}}</span></td>
            <td>
              <a href="https://nspires.nasaprs.com/external/solicitations/summary.do?path=&method=init&solId=${{item.id}}" target="_blank" rel="noopener noreferrer" style="font-size:0.85rem; color:#0b3d91;">NASA Live &rarr;</a>
            </td>
          </tr>
        `;
      }}).join('');

      renderPagination();
    }}

    function renderPagination() {{
      const totalPages = Math.ceil(filteredRecords.length / pageSize) || 1;
      pagination.innerHTML = '';
      if (totalPages <= 1) return;

      const prevBtn = document.createElement('button');
      prevBtn.className = 'page-btn';
      prevBtn.textContent = 'Prev';
      prevBtn.disabled = currentPage === 1;
      prevBtn.onclick = () => {{ currentPage--; renderTable(); }};
      pagination.appendChild(prevBtn);

      let startP = Math.max(1, currentPage - 2);
      let endP = Math.min(totalPages, currentPage + 2);

      for (let p = startP; p <= endP; p++) {{
        const pBtn = document.createElement('button');
        pBtn.className = `page-btn ${{p === currentPage ? 'active' : ''}}`;
        pBtn.textContent = p;
        pBtn.onclick = () => {{ currentPage = p; renderTable(); }};
        pagination.appendChild(pBtn);
      }}

      const nextBtn = document.createElement('button');
      nextBtn.className = 'page-btn';
      nextBtn.textContent = 'Next';
      nextBtn.disabled = currentPage === totalPages;
      nextBtn.onclick = () => {{ currentPage++; renderTable(); }};
      pagination.appendChild(nextBtn);
    }}

    searchInput.addEventListener('input', filterData);
    fySelect.addEventListener('change', filterData);
    dirSelect.addEventListener('change', filterData);
    statusSelect.addEventListener('change', filterData);

    renderTable();
  </script>
</body>
</html>
"""
    write_if_changed(INDEX_HTML, index_content)
    print(f"[+] Wrote {INDEX_HTML}")


def main():
    print("=" * 70)
    print(" NASA NSPIRES (nspires.nasaprs.com) Harvester & Static Mirror")
    print(f" Target Directory: {BASE_DIR}")
    print("=" * 70)

    # 1. Discover solicitations
    discovered = discover_all_solicitations()
    if not discovered:
        print("[!] No solicitations discovered. Exiting.", file=sys.stderr)
        sys.exit(1)

    total_count = len(discovered)
    target_count = FETCH_LIMIT if 0 < FETCH_LIMIT < total_count else total_count

    # 2. Check existing jsonl for fast incremental resume
    existing_records = {}
    if os.path.exists(JSONL_FILE):
        try:
            with open(JSONL_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        if "clean_sid" in r:
                            existing_records[r["clean_sid"]] = r
            print(f"[+] Found {len(existing_records)} previously parsed solicitations in {JSONL_FILE}")
        except Exception as e:
            print(f"[*] Warning reading existing JSONL: {e}", file=sys.stderr)

    # 3. Enrich details & pre-render static HTML cards
    print(f"[*] Harvesting detailed metadata and pre-rendering {target_count} solicitations...")
    os.makedirs(SOLICITATIONS_DIR, exist_ok=True)

    enriched_solicitations = []
    to_fetch = []

    for item in discovered[:target_count]:
        clean_sid = item["clean_sid"]
        if clean_sid in existing_records and os.path.exists(os.path.join(SOLICITATIONS_DIR, f"{clean_sid}.html")):
            # Fast reuse
            enriched_solicitations.append(existing_records[clean_sid])
        else:
            to_fetch.append(item)

    print(f"[*] {len(enriched_solicitations)} solicitations loaded from cache, {len(to_fetch)} to fetch/render.")

    if to_fetch:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_item = {executor.submit(fetch_solicitation_detail, it): it for it in to_fetch}
            completed = 0
            for future in as_completed(future_to_item):
                completed += 1
                try:
                    detail = future.result()
                    enriched_solicitations.append(detail)
                    
                    # Render and write HTML card
                    html_card = render_html_page(detail)
                    card_path = os.path.join(SOLICITATIONS_DIR, f"{detail['clean_sid']}.html")
                    with open(card_path, "w", encoding="utf-8") as f:
                        f.write(html_card)
                        
                    if completed % 100 == 0 or completed == len(to_fetch):
                        print(f"    Fetched & rendered {completed}/{len(to_fetch)} ({(completed/len(to_fetch))*100:.1f}%)...")
                except Exception as e:
                    print(f"[!] Error processing solicitation: {e}", file=sys.stderr)

    # Sort deterministically: clean_sid ascending, then release date descending
    enriched_solicitations.sort(key=lambda x: str(x.get("clean_sid") or ""))
    enriched_solicitations.sort(key=lambda x: str(x.get("release_date") or ""), reverse=True)

    # 4. Save JSON Lines dataset
    jsonl_content = "".join(json.dumps(sol) + "\n" for sol in enriched_solicitations)
    write_if_changed(JSONL_FILE, jsonl_content)
    print(f"[+] Wrote {JSONL_FILE} with {len(enriched_solicitations)} solicitation records.")

    # 5. Generate dual XML sitemaps and plain text lists
    generate_sitemaps(enriched_solicitations)

    # 6. Generate interactive catalog index.html
    generate_index_html(enriched_solicitations)

    print("\n" + "=" * 70)
    print("[✓] NSPIRES pipeline completed successfully!")
    print(f"    - Pre-rendered HTML Cards: {len(enriched_solicitations)} in {SOLICITATIONS_DIR}")
    print(f"    - Structured JSON Lines:   {JSONL_FILE}")
    print(f"    - Interactive Catalog:     {INDEX_HTML}")
    print(f"    - GitHub Pages Sitemap:    {SITEMAP_FILE}")
    print(f"    - Direct Live Sitemap:     {DIRECT_SITEMAP_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
