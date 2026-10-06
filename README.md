# NASA NSPIRES Scraper, Mirror & Sitemap Pipeline

[![Update NSPIRES Solicitations & Sitemap](https://github.com/Oht8wooWi8yait9n/nspires/actions/workflows/update-sitemap.yml/badge.svg)](https://github.com/Oht8wooWi8yait9n/nspires/actions/workflows/update-sitemap.yml)

A comprehensive harvester, static HTML mirror, client-side searchable catalog, dual XML sitemaps, and automated synchronization pipeline for **NASA NSPIRES** ([https://nspires.nasaprs.com/](https://nspires.nasaprs.com/)) — the **NASA Solicitation and Proposal Integrated Review and Evaluation System**.

NSPIRES hosts all NASA research solicitations, announcements of opportunity (AO), NASA research announcements (NRA), cooperative agreement notices (CAN), requests for information (RFI), and broad agency announcements (BAA).

---

## Live Endpoints & Resources

| Resource | URL / Path |
| :--- | :--- |
| **Interactive Catalog** | [https://oht8woowi8yait9n.github.io/nspires/](https://oht8woowi8yait9n.github.io/nspires/) |
| **Primary XML Sitemap (Onyx Web Connector)** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/nspires/main/nspires_sitemap.xml` |
| **Direct NASA Live Sitemap** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/nspires/main/nspires_direct_sitemap.xml` |
| **Primary URLs List** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/nspires/main/nspires_urls.txt` |
| **Direct NASA URLs List** | `https://raw.githubusercontent.com/Oht8wooWi8yait9n/nspires/main/nspires_direct_urls.txt` |
| **Structured Dataset** | `nspires_solicitations.jsonl` (3,471 solicitations) |

---

## Features

1. **Complete Database Harvest**: Discovers and extracts all **3,471 NASA research solicitations** spanning FY2001 to present across all NASA Mission Directorates (SMD, STMD, ESDMD, ARMD, SOMD).
2. **Pre-rendered Static Project Cards**: 3,471 lightweight, mobile-responsive HTML cards with key milestone dates, proposal submission deadlines, special notices, town halls, and direct links to official PDF announcement documents.
3. **Source of Truth Citation Linking**:
   - Every card includes `<link rel="canonical">` and OpenGraph metadata pointing to the live NASA NSPIRES URL.
   - Built-in smart browser redirect forwards human visitors to `https://nspires.nasaprs.com/external/solicitations/summary.do?path=&method=init&solId=...` while allowing headless crawlers to index the static mirror smoothly.
4. **Client-Side Searchable Dashboard**: Interactive `index.html` offering instant filtering by title, solicitation identifier, fiscal year, mission directorate, and status.
5. **Automated Weekly Synchronization**: GitHub Actions workflow runs every Sunday at midnight UTC (`0 0 * * 0`) to harvest newly announced opportunities, amendments, and deadlines.

---

## Onyx Web Connector Configuration

To ingest the full catalog into Onyx with citations pointing directly to NASA source-of-truth pages:

1. In **Onyx Admin**, navigate to **Add Connector &rarr; Web**.
2. Select **Sitemap** mode.
3. Enter the raw sitemap URL:
   ```
   https://raw.githubusercontent.com/Oht8wooWi8yait9n/nspires/main/nspires_sitemap.xml
   ```
4. Under **URL Rewrites** (in Advanced Options), add a prefix rewrite rule so citations in Onyx link directly to the official NASA page:
   * **Source Prefix**: `https://oht8woowi8yait9n.github.io/nspires/solicitations/`
   * **Target Prefix**: `https://nspires.nasaprs.com/external/solicitations/summary.do?path=&method=init&solId=`
5. Set crawling frequency (e.g., **Weekly**).
6. Attach the connector to your desired **Document Set** (e.g. `NASA NSPIRES Solicitations`) and link it to your assistant personas.
7. Trigger an initial sync. Onyx will crawl the static mirror with 100% reliability and zero rate-limiting, storing citations that point straight to NASA.

---

## Running Locally

```bash
# Clone the repository
git clone https://github.com/Oht8wooWi8yait9n/nspires.git
cd nspires

# Run the complete harvest and site generator
python3 generate_nspires_sitemap.py
```
