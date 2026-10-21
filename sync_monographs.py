import os
import re
import json
import hashlib
from datetime import datetime
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

BASE_URL = "https://www.clinicalguidelines.scot.nhs.uk"
START_URLS = [
    # Direct Monograph Directories
    "http://clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/ggc-paediatric-guidelines/neonatology/neonatal-drug-monographs/neonatal-parenteral-drug-monographs",
    "https://www.clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/ggc-paediatric-guidelines/neonatology/neonatal-drug-monographs/neonatal-oral-other-routes-drug-monographs/",
    # General Neonatology & Paediatric Guidelines
    "https://www.clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/ggc-paediatric-guidelines/neonatology/",
    "https://www.clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/ggc-paediatric-guidelines/",
    "https://www.clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/other-guidelines/",
]

PDF_DIR = "./public/pdfs"
INDEX_FILE = "./public/search_index.json"
MANIFEST_FILE = "./manifest.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GGC-Guideline-Index-Sync/1.0)"
}

os.makedirs(PDF_DIR, exist_ok=True)


def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        try:
            with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_manifest(manifest):
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def extract_editorial_dates(soup):
    try:
        text = soup.get_text(" ", strip=True)
        last_rev = re.search(r"Last reviewed:\s*(\d{2}/\d{2}/\d{4})", text)
        next_rev = re.search(r"Next review date:\s*(\d{2}/\d{2}/\d{4})", text)
        return {
            "lastReviewed": last_rev.group(1) if last_rev else None,
            "nextReview": next_rev.group(1) if next_rev else None,
        }
    except Exception:
        return {"lastReviewed": None, "nextReview": None}


def extract_pdf_text(pdf_path):
    full_text = []
    try:
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if text:
                    full_text.append(f"[Page {page_num}] {text}")
    except Exception as e:
        print(f"  [!] Failed to parse PDF {pdf_path}: {e}")
    return "\n".join(full_text)


def crawl_and_sync():
    manifest = load_manifest()
    visited_pages = set()
    queue = list(START_URLS)
    documents_dict = {}

    session = requests.Session()
    session.headers.update(HEADERS)

    while queue:
        current_url = queue.pop(0)
        clean_url = current_url.split("?")[0].split("#")[0].rstrip("/")
        if clean_url in visited_pages:
            continue
        visited_pages.add(clean_url)

        print(f"Scanning: {clean_url}")
        try:
            resp = session.get(current_url, timeout=15)
            if resp.status_code != 200:
                continue
        except Exception as e:
            print(f"  [!] Error fetching {current_url}: {e}")
            continue

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            h1 = soup.find("h1")
            page_title = h1.get_text(strip=True) if h1 else clean_url.split("/")[-1]
            dates = extract_editorial_dates(soup)
        except Exception as e:
            print(f"  [!] Error parsing HTML for {clean_url}: {e}")
            continue

        url_lower = clean_url.lower()
        
        # Categorization Logic
        if "parenteral" in url_lower or "iv" in url_lower:
            category = "Neonatal IV Monograph"
        elif "oral" in url_lower:
            category = "Neonatal Oral Monograph"
        elif "neonatology" in url_lower:
            category = "Neonatal Guideline"
        else:
            category = "Paediatric Guideline"

        # Find direct PDF links or child guideline/monograph pages
        pdf_links = []
        for a in soup.find_all("a", href=True):
            try:
                href = a["href"]
                full_href = urljoin(BASE_URL, href)
                if ".pdf" in full_href.lower() and "/media/" in full_href.lower():
                    pdf_links.append((full_href, a.get_text(strip=True) or page_title))
                elif "neonatal-parenteral-drug-monographs" in full_href.lower() or "neonatal-oral-other-routes-drug-monographs" in full_href.lower() or full_href.startswith("https://www.clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/"):
                    child_clean = full_href.split("?")[0].split("#")[0].rstrip("/")
                    if child_clean not in visited_pages:
                        queue.append(child_clean)
            except Exception:
                continue

        # 1. Process PDFs if present
        has_pdf = False
        if pdf_links:
            has_pdf = True
            for pdf_url, link_text in set(pdf_links):
                try:
                    url_hash = hashlib.md5(pdf_url.encode()).hexdigest()[:10]
                    orig_filename = os.path.basename(urlparse(pdf_url).path)
                    local_filename = f"{url_hash}_{orig_filename}"
                    local_path = os.path.join(PDF_DIR, local_filename)

                    if pdf_url in manifest and os.path.exists(local_path):
                        extracted_text = manifest[pdf_url]["content"]
                    else:
                        pdf_resp = session.get(pdf_url, timeout=20)
                        pdf_resp.raise_for_status()
                        with open(local_path, "wb") as f:
                            f.write(pdf_resp.content)
                        extracted_text = extract_pdf_text(local_path)
                        manifest[pdf_url] = {
                            "localFile": local_filename,
                            "downloadedAt": datetime.utcnow().isoformat(),
                            "content": extracted_text,
                        }

                    documents_dict[url_hash] = {
                        "id": url_hash,
                        "title": page_title,
                        "linkText": link_text,
                        "category": category,
                        "sourcePageUrl": clean_url,
                        "remotePdfUrl": pdf_url,
                        "localPdfPath": f"/pdfs/{local_filename}",
                        "lastReviewed": dates["lastReviewed"],
                        "nextReview": dates["nextReview"],
                        "content": extracted_text,
                    }
                except Exception as e:
                    print(f"  [!] Failed processing PDF {pdf_url}: {e}")

        # 2. Process HTML Web Guidelines (skip monograph container landing pages)
        is_monograph_section = "drug-monographs" in url_lower or "parenteral" in url_lower or "oral" in url_lower
        is_root_landing = clean_url in [u.rstrip("/") for u in START_URLS]
        
        if not has_pdf and not is_monograph_section and not is_root_landing:
            try:
                content_container = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile("content|guideline", re.I))
                if content_container:
                    body_text = content_container.get_text(" ", strip=True)
                    if len(body_text) > 100:
                        doc_id = hashlib.md5(clean_url.encode()).hexdigest()[:10]
                        if doc_id not in documents_dict:
                            documents_dict[doc_id] = {
                                "id": doc_id,
                                "title": page_title,
                                "linkText": "Web Guideline (Accordion/Pulldown)",
                                "category": category,
                                "sourcePageUrl": clean_url,
                                "remotePdfUrl": None,
                                "localPdfPath": None,
                                "lastReviewed": dates["lastReviewed"],
                                "nextReview": dates["nextReview"],
                                "content": body_text,
                            }
            except Exception as e:
                print(f"  [!] Failed parsing web content for {clean_url}: {e}")

    documents = list(documents_dict.values())
    save_manifest(manifest)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "lastSynced": datetime.utcnow().isoformat(),
            "count": len(documents),
            "documents": documents
        }, f, indent=2)

    print(f"\nSync complete! Indexed {len(documents)} unique documents into {INDEX_FILE}.")


if __name__ == "__main__":
    crawl_and_sync()
