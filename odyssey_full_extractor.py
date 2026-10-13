import asyncio
import json
import os
import re
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# Identifiers for Extraction
PRIMARY_CASE_NUM = "77D01-2609-CM-000645"
ALT_IDENTIFIERS = ["77A09040526", "77-DM4020153"]
OUTPUT_DIR = f"./extraction_{PRIMARY_CASE_NUM.replace('-', '_')}"


async def scrape_indiana_odyssey(case_number: str, alt_ids: list, output_folder: str):
    os.makedirs(output_folder, exist_ok=True)
    attachments_folder = os.path.join(output_folder, "downloaded_documents")
    os.makedirs(attachments_folder, exist_ok=True)

    extracted_data = {
        "primary_case_number": case_number,
        "alternate_identifiers": alt_ids,
        "extraction_timestamp": datetime.now().isoformat(),
        "case_header": {},
        "parties": [],
        "charges": [],
        "hearings": [],
        "docket_entries": [],
        "downloaded_files": [],
        "external_links": []
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # Set to True for headless operation
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"[+] Navigating to Indiana MyCase (Odyssey Public Portal)...")
        await page.goto("https://public.courts.in.gov/mycase/", wait_until="networkidle")

        # Handle disclaimer modal if present
        try:
            accept_btn = page.locator("button:has-text('Accept'), button:has-text('I Agree')")
            if await accept_btn.is_visible(timeout=3000):
                await accept_btn.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Execute Search
        print(f"[+] Searching for primary case number: {case_number}")
        search_input = page.locator("input#SearchTerm, input[name='SearchTerm'], input#CaseSearch_SearchTerm").first
        await search_input.fill(case_number)
        
        search_button = page.locator("button[type='submit'], input[type='submit'], button:has-text('Search')").first
        await search_button.click()

        # Wait for search results
        try:
            await page.wait_for_selector(".search-result, .case-summary, tbody tr, .no-results", timeout=12000)
        except Exception:
            print("[-] Search result wait timed out. Checking secondary identifiers...")

        # Locate matching case row
        case_link = page.locator(f"a:has-text('{case_number}')").first
        
        if not await case_link.is_visible():
            print(f"[-] Primary case number not directly returned. Trying alternate IDs...")
            for alt_id in alt_ids:
                print(f"[+] Attempting search with alternate ID: {alt_id}")
                await search_input.fill(alt_id)
                await search_button.click()
                await page.wait_for_timeout(3000)
                case_link = page.locator(f"a:has-text('{case_number}'), a:has-text('{alt_id}')").first
                if await case_link.is_visible():
                    break

        if await case_link.is_visible():
            print(f"[✓] Case match located. Opening detailed case view...")
            await case_link.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)
        else:
            print("[-] Could not automatically open case page. Saving rendered search page DOM.")
            raw_html = await page.content()
            with open(os.path.join(output_folder, "search_results.html"), "w", encoding="utf-8") as f:
                f.write(raw_html)
            await browser.close()
            return

        # Parse Case Details with BeautifulSoup
        html_content = await page.content()
        soup = BeautifulSoup(html_content, "html.parser")

        # 1. Case Header Parsing
        header_elem = soup.find("div", class_=re.compile("case-header|case-details|caseInfo", re.I))
        if header_elem:
            extracted_data["case_header"]["raw_text"] = header_elem.get_text(separator="\n", strip=True)

        # Generic metadata extraction
        for row in soup.find_all(["tr", "div"], class_=re.compile("detail|info|header", re.I)):
            text = row.get_text(separator=" ", strip=True)
            if "Court:" in text:
                extracted_data["case_header"]["court"] = text
            elif "Filed:" in text:
                extracted_data["case_header"]["filed_date"] = text
            elif "Status:" in text:
                extracted_data["case_header"]["status"] = text
            elif "Judge:" in text:
                extracted_data["case_header"]["judicial_officer"] = text

        # 2. Parties Parsing
        party_sections = soup.find_all(["div", "table"], class_=re.compile("party|parties", re.I))
        for sec in party_sections:
            extracted_data["parties"].append(sec.get_text(separator="\n", strip=True))

        # 3. Charges / Counts Parsing
        charge_sections = soup.find_all(["div", "table", "tr"], class_=re.compile("charge|count|statute", re.I))
        for chg in charge_sections:
            text = chg.get_text(separator=" ", strip=True)
            if text and text not in extracted_data["charges"]:
                extracted_data["charges"].append(text)

        # 4. Hearings / Calendar Events
        event_elems = soup.find_all(["tr", "div"], class_=re.compile("event|hearing|schedule", re.I))
        for ev in event_elems:
            text = ev.get_text(separator=" ", strip=True)
            if text and text not in extracted_data["hearings"]:
                extracted_data["hearings"].append(text)

        # 5. Chronological Case Summary (CCS) Entries
        docket_rows = soup.find_all(["tr", "div"], class_=re.compile("ccs-entry|docket-entry|case-event|row", re.I))
        for row in docket_rows:
            entry_text = row.get_text(separator=" ", strip=True)
            if len(entry_text) > 15 and entry_text not in extracted_data["docket_entries"]:
                extracted_data["docket_entries"].append(entry_text)

        # 6. Extract External Media / Audio / Video Links embedded in text
        all_text = soup.get_text()
        found_urls = re.findall(
            r'https?://[^\s<>"]+(?:evidence\.com|drive\.google\.com|onedrive|dropbox|box\.com|youtube)[^\s<>"]*',
            all_text
        )
        extracted_data["external_links"] = list(set(found_urls))

        # 7. Document Attachment Downloads
        doc_links = await page.locator("a[href*='Document'], a[href*='Download'], a[href*='Pdf'], a[href*='Exhibit']").all()
        print(f"[+] Found {len(doc_links)} downloadable document references.")

        for idx, link in enumerate(doc_links):
            try:
                link_text = (await link.inner_text()).strip() or f"Document_{idx+1}"
                safe_name = re.sub(r'[\W_]+', '_', link_text)[:40]
                
                async with page.expect_download(timeout=8000) as download_info:
                    await link.click()
                
                download = await download_info.value
                file_filename = f"{idx+1:02d}_{safe_name}_{download.suggested_filename}"
                save_path = os.path.join(attachments_folder, file_filename)
                await download.save_as(save_path)
                
                extracted_data["downloaded_files"].append({
                    "title": link_text,
                    "saved_path": save_path
                })
                print(f"[✓] Extracted file saved: {save_path}")
            except Exception as e:
                print(f"[!] Could not auto-download document #{idx+1}: {e}")

        # Save Structured JSON Output
        json_out_path = os.path.join(output_folder, "full_case_extraction.json")
        with open(json_out_path, "w", encoding="utf-8") as f:
            json.dump(extracted_data, f, indent=2, ensure_ascii=False)

        # Save Human-Readable Summary Report
        report_out_path = os.path.join(output_folder, "case_summary_report.txt")
        with open(report_out_path, "w", encoding="utf-8") as f:
            f.write(f"OFFICIAL ODYSSEY CASE EXTRACTION REPORT\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"="*60 + "\n\n")
            f.write(f"Primary Case Number: {case_number}\n")
            f.write(f"Cross-Reference Control #: 77A09040526\n")
            f.write(f"Agency Incident Tracking #: 77-DM4020153\n\n")
            f.write(f"--- CASE HEADER & METADATA ---\n")
            for k, v in extracted_data["case_header"].items():
                f.write(f"{k}: {v}\n")
            f.write(f"\n--- CHARGES ---\n")
            for chg in extracted_data["charges"]:
                f.write(f"- {chg}\n")
            f.write(f"\n--- DOCKET ENTRIES / CCS ({len(extracted_data['docket_entries'])} entries) ---\n")
            for entry in extracted_data["docket_entries"]:
                f.write(f"* {entry}\n")

        print(f"\n[✓] Extraction complete. All data saved to '{output_folder}'.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(scrape_indiana_odyssey(PRIMARY_CASE_NUM, ALT_IDENTIFIERS, OUTPUT_DIR))
