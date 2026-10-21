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
    "https://www.clinicalguidelines.scot.nhs.uk/ggc-paediatric-guidelines/ggc-paediatric-guidelines/neonatology/",
]

PDF_DIR = "./public/pdfs"
INDEX_FILE = "./public/search_index.json"
MANIFEST_FILE = "./manifest.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GGC-Neonatal-Index-Sync/1.0)"
}

os.makedirs(PDF_DIR, exist_ok=True)


def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def extract_editorial_dates(soup):
    text = soup.get_text(" ", strip=True)
    last_rev = re.search(r"Last reviewed:\s*(\d{2}/\d{2}/\d{4})", text)
    next_rev = re.search(r"Next review date:\s*(\d{2}/\d{2}/\d{4})", text)
    return {
        "lastReviewed": last_rev.group(1) if last_rev else None,
        "nextReview": next_rev.group(1) if next_rev else None,
    }


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
    documents_dict = {}  # Use a dictionary keyed by ID to guarantee uniqueness

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
            resp = session.get(clean_url, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"  [!] Error fetching {clean_url}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        page_title = soup.find("h1").get_text(strip=True) if soup.find("h1") else clean_url.split("/")[-1]
        dates = extract_editorial_dates(soup)

        url_lower = clean_url.lower()
        if "parenteral" in url_lower:
            category = "Parenteral"
        elif "oral" in url_lower:
            category = "Oral & Other"
        elif "drug-monographs" in url_lower:
            category = "Monographs"
        else:
            category = "Clinical Guideline"

        # Find direct PDF links
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full_href = urljoin(BASE_URL, href)
            if ".pdf" in full_href.lower() and "/media/" in full_href.lower():
                pdf_links.append((full_href, a.get_text(strip=True) or page_title))
            elif full_href.startswith(tuple(u.rstrip("/") for u in START_URLS)):
                child_clean = full_href.split("?")[0].split("#")[0].rstrip("/")
                if child_clean not in visited_pages:
                    queue.append(child_clean)

        # 1. Process PDFs if present
        if pdf_links:
            for pdf_url, link_text in set(pdf_links):
                url_hash = hashlib.md5(pdf_url.encode()).hexdigest()[:10]
                orig_filename = os.path.basename(urlparse(pdf_url).path)
                local_filename = f"{url_hash}_{orig_filename}"
                local_path = os.path.join(PDF_DIR, local_filename)

                if pdf_url in manifest and os.path.exists(local_path):
                    extracted_text = manifest[pdf_url]["content"]
                else:
                    try:
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
                    except Exception as e:
                        print(f"  [!] Failed to download {pdf_url}: {e}")
                        continue

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

        # 2. Process HTML Web Guidelines
        if clean_url not in [u.rstrip("/") for u in START_URLS]:
            content_container = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile("content|guideline", re.I))
            
            if content_container:
                body_text = content_container.get_text(" ", strip=True)
                
                if len(body_text) > 100:
                    doc_id = hashlib.md5(clean_url.encode()).hexdigest()[:10]
                    if doc_id not in documents_dict:
                        documents_dict[doc_id] = {
                            "id": doc_id,
                            "title": page_title,
                            "linkText": "Web Guideline (Pulldown Sections)",
                            "category": category,
                            "sourcePageUrl": clean_url,
                            "remotePdfUrl": None,
                            "localPdfPath": None,
                            "lastReviewed": dates["lastReviewed"],
                            "nextReview": dates["nextReview"],
                            "content": body_text,
                        }

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
