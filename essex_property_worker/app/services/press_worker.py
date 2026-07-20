from __future__ import annotations

import asyncio
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.models import Parcel, Party, RecordedDocument

# Real PRESS "By Block and Lot" municipality dropdown options (ClerkHome.aspx?op=basic,
# ddlMunTab4). NJ parcel GIS data returns MOD-IV style names ("NEWARK CITY",
# "SOUTH ORANGE VILLAGE TOWNSHIP", ...), which never match this list exactly.
PRESS_MUNICIPALITIES = [
    "BELLEVILLE",
    "BLOOMFIELD",
    "CALDWELL",
    "CEDAR GROVE",
    "EAST ORANGE",
    "ESSEX COUNTY",
    "ESSEX FELLS",
    "FAIRFIELD",
    "GLEN RIDGE",
    "IRVINGTON",
    "LIVINGSTON",
    "MAPLEWOOD",
    "MILLBURN",
    "MONTCLAIR",
    "NEWARK",
    "NORTH CALDWELL",
    "NUTLEY",
    "ORANGE",
    "ROSELAND",
    "SOUTH ORANGE VILLAGE",
    "VERONA",
    "WEST CALDWELL",
    "WEST ORANGE",
    "COUNTY WIDE",
]

_MUNICIPALITY_SUFFIXES = (" TOWNSHIP", " TWP", " BOROUGH", " BORO", " CITY", " TOWN")


def normalize_municipality(name: str) -> str:
    value = name.strip().upper()
    if value in PRESS_MUNICIPALITIES:
        return value

    stripped = value
    changed = True
    while changed:
        changed = False
        for suffix in _MUNICIPALITY_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)].strip()
                changed = True
    if stripped in PRESS_MUNICIPALITIES:
        return stripped

    for option in PRESS_MUNICIPALITIES:
        if option.startswith(stripped) or stripped.startswith(option):
            return option

    raise RuntimeError(f"Could not map municipality {name!r} to a PRESS municipality option.")


class PressSearchWorker:
    """Browser automation for the Essex County PRESS WebForms portal.

    PRESS (press.essexregister.com/prodpress/clerk/ClerkHome.aspx?op=basic) renders all
    four search modes (Instrument, Document Type, Block/Lot, Name) into the same DOM at
    once and shows/hides them client-side via the "By ..." tab links - the fields are
    real ASP.NET WebForms controls (ddlMunTab4 / txtBlockTab4 / txtLotTab4 / btnSearchTab4)
    that stay hidden (and therefore un-fillable) until the "By Block and Lot" tab link is
    clicked. Search results render in an ASP.NET DataGrid (#dgdDeedMort) whose columns are
    Type / Direct Party (grantor) / Indirect Party (grantee) / Instrument # / Recorded /
    Town Name / Block / Lot / Book / Page, plus one row per grantor+grantee combination
    (an instrument with 2 grantors and 1 grantee produces 2 rows that must be merged).
    Viewing a document is a multi-frame, session-stateful flow (grid "view" ImageButton ->
    ShowDetails.aspx frameset -> head frame "Get Image" button -> body frame loads an
    Atalasoft image viewer -> its "Save" button triggers the real file download), so there
    is no plain document URL to scrape - it has to be driven exactly like a person would.

    FastAPI endpoints stay async, but Playwright runs through the sync API inside a worker
    thread. This avoids Windows event-loop subprocess issues that can surface as bare
    NotImplementedError exceptions when launching Chromium.
    """

    NO_RESULTS_TEXT = "no results found"

    async def search_by_block_lot(
        self,
        parcel: Parcel,
        from_date: date | None,
        to_date: date | None,
        download_documents: bool = False,
    ) -> list[RecordedDocument]:
        return await asyncio.to_thread(
            self._search_by_block_lot_sync,
            parcel,
            from_date,
            to_date,
            download_documents,
        )

    async def download_document(self, parcel: Parcel, instrument_number: str) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._download_document_sync, parcel, instrument_number)

    def _download_document_sync(self, parcel: Parcel, instrument_number: str) -> tuple[bytes, str]:
        self._ensure_windows_subprocess_policy()
        from playwright.sync_api import sync_playwright

        municipality = normalize_municipality(parcel.municipality)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo_ms)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(settings.browser_timeout_ms)

            self._run_search(page, municipality, parcel.block, parcel.lot, None, None)
            if self._no_results(page):
                context.close()
                browser.close()
                raise RuntimeError("No PRESS records were found for this block/lot.")

            pending = {instrument_number: None}
            row = None
            while True:
                found = self._find_pending_row(page, pending)
                if found is not None:
                    row = found[1]
                    break
                if not self._goto_next_page(page):
                    break

            if row is None:
                context.close()
                browser.close()
                raise RuntimeError(f"Instrument {instrument_number!r} was not found in the current PRESS results.")

            body, content_disposition = self._fetch_document_bytes(page, row)

            context.close()
            browser.close()

        return body, content_disposition

    def browser_health_check(self) -> None:
        self._ensure_windows_subprocess_policy()
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.headless)
            page = browser.new_page()
            page.goto("about:blank")
            browser.close()

    def _search_by_block_lot_sync(
        self,
        parcel: Parcel,
        from_date: date | None,
        to_date: date | None,
        download_documents: bool,
    ) -> list[RecordedDocument]:
        self._ensure_windows_subprocess_policy()
        from playwright.sync_api import sync_playwright

        municipality = normalize_municipality(parcel.municipality)
        windows: list[tuple[date | None, date | None]]
        if from_date is not None and to_date is not None:
            windows = list(self._date_windows(from_date, to_date))
        else:
            windows = [(None, None)]

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo_ms)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(settings.browser_timeout_ms)

            all_rows: list[dict] = []
            for start, end in windows:
                self._run_search(page, municipality, parcel.block, parcel.lot, start, end)
                if self._no_results(page):
                    continue
                all_rows.extend(self._scan_all_pages(page))

            documents = self._merge_by_instrument(all_rows)

            if download_documents and documents:
                for start, end in windows:
                    def restart_search(start=start, end=end):
                        self._run_search(page, municipality, parcel.block, parcel.lot, start, end)

                    restart_search()
                    if self._no_results(page):
                        continue
                    self._download_pending(page, documents, restart_search)

            context.close()
            browser.close()

        return documents

    def _ensure_windows_subprocess_policy(self) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    # ------------------------------------------------------------------ search

    def _run_search(
        self,
        page,
        municipality: str,
        block: str,
        lot: str,
        start: date | None,
        end: date | None,
    ) -> None:
        page.goto(settings.press_base_url, wait_until="domcontentloaded")
        page.get_by_role("link", name="By Block and Lot", exact=True).click()

        page.locator("#ctl00_ContentPlaceHolder1_ddlMunTab4").select_option(label=municipality)
        page.locator("#ctl00_ContentPlaceHolder1_txtBlockTab4").fill(block)
        page.locator("#ctl00_ContentPlaceHolder1_txtLotTab4").fill(lot)

        if start is not None and end is not None:
            page.locator("#ctl00_ContentPlaceHolder1_txtFromTab4").fill(start.strftime("%m/%d/%Y"))
            page.locator("#ctl00_ContentPlaceHolder1_txtToTab4").fill(end.strftime("%m/%d/%Y"))

        self._select_if_present(page, "#ctl00_ContentPlaceHolder1_ddlShowRecTab4", str(settings.default_page_size))
        self._select_if_present(page, "#ctl00_ContentPlaceHolder1_ddlTotalRecTab4", str(settings.default_total_records))

        page.locator("#ctl00_ContentPlaceHolder1_btnSearchTab4").click()
        page.wait_for_load_state("domcontentloaded")

    def _select_if_present(self, page, selector: str, value: str) -> None:
        locator = page.locator(selector)
        if locator.count():
            try:
                locator.select_option(value=value)
            except Exception:
                pass

    def _no_results(self, page) -> bool:
        if page.locator("#ctl00_ContentPlaceHolder1_dgdDeedMort").count():
            return False
        body_text = self._clean(page.locator("body").inner_text()).lower()
        return self.NO_RESULTS_TEXT in body_text

    # ------------------------------------------------------------- result grid

    def _scan_all_pages(self, page) -> list[dict]:
        rows: list[dict] = []
        while True:
            rows.extend(self._extract_current_page(page))
            if not self._goto_next_page(page):
                break
        return rows

    def _extract_current_page(self, page) -> list[dict]:
        grid = page.locator("#ctl00_ContentPlaceHolder1_dgdDeedMort")
        if not grid.count():
            return []
        rows = grid.locator("tr")
        results: list[dict] = []
        for index in range(rows.count()):
            row = rows.nth(index)
            if "footer" in (row.get_attribute("class") or ""):
                continue
            cells = row.locator("td")
            if cells.count() < 10:
                continue
            texts = [self._clean(cells.nth(i).inner_text()) for i in range(10)]
            if texts[0].upper() == "TYPE":
                continue  # header row
            results.append(
                {
                    "type": texts[0],
                    "direct_party": texts[1],
                    "indirect_party": texts[2],
                    "instrument": texts[3],
                    "recorded": texts[4],
                    "town": texts[5],
                    "block": texts[6],
                    "lot": texts[7],
                    "book": texts[8],
                    "page": texts[9],
                }
            )
        return results

    def _goto_next_page(self, page) -> bool:
        pager = page.locator("#ctl00_ContentPlaceHolder1_dgdDeedMort tr.footer")
        if not pager.count():
            return False
        current_text = self._clean(pager.first.locator("span").inner_text())
        try:
            current = int(current_text)
        except ValueError:
            return False
        next_link = pager.first.locator("a", has_text=re.compile(rf"^{current + 1}$"))
        if not next_link.count():
            return False
        next_link.first.click()
        page.wait_for_load_state("domcontentloaded")
        return True

    def _merge_by_instrument(self, rows: list[dict]) -> list[RecordedDocument]:
        merged: dict[str, RecordedDocument] = {}
        order: list[str] = []
        for row in rows:
            key = row["instrument"] or f"{row['type']}|{row['recorded']}|{row['block']}|{row['lot']}"
            recording_date = self._parse_date(row["recorded"])
            if key not in merged:
                merged[key] = RecordedDocument(
                    instrument_number=row["instrument"] or None,
                    document_type=row["type"].upper() or "UNKNOWN",
                    recording_date=recording_date,
                    book=row["book"] or None,
                    page=row["page"] or None,
                    parties=[],
                    raw={"rows": []},
                )
                order.append(key)
            document = merged[key]
            document.raw["rows"].append(row)
            self._add_party(document, "grantor", row["direct_party"])
            self._add_party(document, "grantee", row["indirect_party"])

        documents = [merged[key] for key in order]
        for document in documents:
            document.plain_english = self._plain_english(document)
        return sorted(documents, key=lambda d: d.recording_date or date.min, reverse=True)

    def _add_party(self, document: RecordedDocument, role: str, name: str) -> None:
        if not name:
            return
        existing = {(p.role, p.name.upper()) for p in document.parties}
        if (role, name.upper()) in existing:
            return
        document.parties.append(Party(role=role, name=name))

    def _plain_english(self, document: RecordedDocument) -> str:
        date_text = self._natural_date(document.recording_date)
        grantors = ", ".join(p.name for p in document.parties if p.role == "grantor")
        grantees = ", ".join(p.name for p in document.parties if p.role == "grantee")
        doc_type = document.document_type.upper()
        is_deed = "DEED" in doc_type and "MORTGAGE" not in doc_type
        instrument_text = f"instrument {document.instrument_number}" if document.instrument_number else "an unrecorded instrument"

        if is_deed and grantors and grantees:
            lead = f"Ownership transferred from {grantors} to {grantees}"
        elif grantors and grantees:
            lead = f"{document.document_type.title()} from {grantors} to {grantees}"
        elif grantors or grantees:
            lead = f"{document.document_type.title()} involving {grantors or grantees}"
        else:
            lead = document.document_type.title()

        return f"{lead}; recorded {date_text} as {instrument_text}."

    # ---------------------------------------------------------------- download
    #
    # There is no plain document URL: viewing a document is a multi-frame,
    # session-stateful flow (grid "view" ImageButton -> ShowDetails.aspx frameset ->
    # head frame "Get Image" button -> body frame loads an Atalasoft image viewer ->
    # its "Save" button triggers the real file transfer). Two Playwright quirks were
    # confirmed by hand against the live site and are worked around deliberately:
    #
    #  1. The viewer's "Save" button click only produces the real file-transfer
    #     request once the viewer's own thumbnail/tile AJAX calls have settled; a
    #     fixed short wait is not reliable, so we wait for networkidle (best effort)
    #     plus a short buffer before clicking.
    #  2. Chromium routes that file transfer through its native download manager,
    #     but because it originates from a frame nested inside a classic <frameset>
    #     (not the top-level page), Playwright's Download API reports it as
    #     "canceled" even though the server responded 200 with the real bytes. We
    #     instead capture the fully-resolved request URL via expect_response and
    #     re-fetch it with the browser context's own request client (same session
    #     cookies), which reliably returns the bytes.
    #
    # Browser history is unreliable to return to the results grid after viewing a
    # document (a single page.go_back() only unwinds the last frame-internal
    # navigation, not the top-level page), so after each download the search is
    # simply re-run from scratch and re-paginated forward instead of navigating back.

    def _download_pending(self, page, documents: list[RecordedDocument], restart_search) -> None:
        pending = {d.instrument_number: d for d in documents if d.instrument_number and not d.downloaded_path}
        if not pending:
            return
        download_dir = Path("downloads")
        download_dir.mkdir(parents=True, exist_ok=True)

        current_page_number = 1
        while pending:
            found = self._find_pending_row(page, pending)
            if found is None:
                if self._goto_next_page(page):
                    current_page_number += 1
                    continue
                return

            instrument, row = found
            document = pending[instrument]
            try:
                body, content_disposition = self._fetch_document_bytes(page, row)
                path = self._save_document(body, content_disposition, document, download_dir)
                document.downloaded_path = str(path)
            except Exception as exc:  # noqa: BLE001 - best effort per document
                document.raw["download_error"] = str(exc)
            del pending[instrument]
            if not pending:
                return

            restart_search()
            for _ in range(current_page_number - 1):
                if not self._goto_next_page(page):
                    break

    def _find_pending_row(self, page, pending: dict[str, RecordedDocument]):
        grid = page.locator("#ctl00_ContentPlaceHolder1_dgdDeedMort")
        if not grid.count():
            return None
        rows = grid.locator("tr")
        for index in range(rows.count()):
            row = rows.nth(index)
            if "footer" in (row.get_attribute("class") or ""):
                continue
            cells = row.locator("td")
            if cells.count() < 10:
                continue
            instrument = self._clean(cells.nth(3).inner_text())
            if instrument in pending:
                return instrument, row
        return None

    def _fetch_document_bytes(self, page, row) -> tuple[bytes, str]:
        row.locator("input[type=image]").first.click()
        page.wait_for_load_state("domcontentloaded")

        head_frame = self._wait_for_frame(page, "InstViewerHeadFrame", settings.browser_timeout_ms)
        head_frame.wait_for_selector("input[name=btnImage]", timeout=settings.browser_timeout_ms)
        head_frame.click("input[name=btnImage]")

        body_frame = self._wait_for_frame(page, "InstViewerBodyFrame", settings.browser_timeout_ms)
        body_frame.wait_for_selector("#Button_SaveImage", timeout=settings.browser_timeout_ms)

        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(1_500)

        with page.expect_response(
            lambda r: "atala_rm=ReturnFileAsResponse" in r.url, timeout=settings.browser_timeout_ms
        ) as response_info:
            body_frame.locator("#Button_SaveImage").click()
        response = response_info.value

        api_response = page.context.request.get(response.url)
        return api_response.body(), api_response.headers.get("content-disposition", "")

    def _save_document(
        self,
        body: bytes,
        content_disposition: str,
        document: RecordedDocument,
        download_dir: Path,
    ) -> Path:
        # PRESS always returns the same generic filename (e.g. "OPRSFile.pdf") in
        # Content-Disposition regardless of which document was requested, so the
        # instrument number must be prefixed to keep downloads from colliding.
        match = re.search(r'filename="?([^";]+)"?', content_disposition)
        server_name = match.group(1).strip() if match else "document.pdf"
        suffix = Path(server_name).suffix or ".pdf"
        stem = self._safe_path_name(
            f"{document.instrument_number or 'document'}_{document.document_type}"
        )
        path = download_dir / f"{stem}{suffix}"
        path.write_bytes(body)
        return path

    def _wait_for_frame(self, page, name: str, timeout_ms: int):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            frame = page.frame(name=name)
            if frame is not None:
                return frame
            page.wait_for_timeout(200)
        raise RuntimeError(f"Timed out waiting for the {name!r} document viewer frame.")

    # ------------------------------------------------------------------ dates

    def _date_windows(self, start: date, end: date):
        current = start
        while current <= end:
            window_end = min(current + timedelta(days=settings.press_max_window_days - 1), end)
            yield current, window_end
            current = window_end + timedelta(days=1)

    def _parse_date(self, text: str) -> date | None:
        text = text.strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%m/%d/%Y").date()
        except ValueError:
            return None

    def _natural_date(self, value: date | None) -> str:
        if value is None:
            return "an unknown date"
        return f"{value.month}/{value.day}/{value.year}"

    def _clean(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _safe_path_name(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
