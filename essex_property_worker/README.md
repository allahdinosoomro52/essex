# Essex County Property Records Worker

Python/FastAPI backend worker for an Essex County, NJ property records lookup tool.

The worker follows the real workflow:

1. Resolve a street address to municipality, block, and lot (ArcGIS geocoder + NJ
   parcel layer).
2. Drive the Essex County PRESS WebForms portal with Playwright to search by
   block/lot, exactly as a person would (click the "By Block and Lot" tab, fill the
   real form controls, submit, page through results).
3. Parse the recorded-document grid and merge it into one document per instrument.
4. Build a plain-English title summary (current owner, ownership chain, active
   mortgages/liens).
5. Optionally download the actual recorded PDF for every document found.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/** for the demo UI: type an address, click Search, and
the summary/document list renders on the page. Every document has a "Download PDF"
button that fetches that one document on demand and saves it through the browser -
nothing is downloaded up front, so the initial search stays fast (PRESS itself, not
this app, is the slow part - each document view/download round-trip against the
live portal takes roughly 15-20 seconds).

The same flow is available headless via the API:

```http
POST /lookup
Content-Type: application/json

{
  "address": "450 Broad St, Newark, NJ",
  "download_documents": false
}
```

`download_documents: true` downloads every document up front instead and saves
them under `downloads/<instrument>_<document_type>.pdf` on the server - useful for
batch/offline use, but slow for an interactive demo since it blocks the response
until every document has been fetched. The frontend instead calls
`GET /documents/download` per document, which streams the PDF bytes straight back
without writing anything to disk.

## API

- `GET /` - the demo frontend (`app/static/index.html`)
- `GET /health`
- `GET /playwright-health` - launches Chromium once to confirm Playwright is installed correctly
- `POST /resolve` - address -> parcel (block/lot) only, no PRESS search
- `POST /lookup` - full pipeline (parcel, summary, document list)
- `GET /documents/download?municipality=&block=&lot=&instrument_number=&document_type=` -
  on-demand single-document PDF download, used by the frontend's "Download PDF"
  buttons

## How PRESS is actually driven

PRESS (`ClerkHome.aspx?op=basic`) renders all four search modes (Instrument,
Document Type, Block/Lot, Name) into the same page at once and only shows/hides
them client-side via the "By ..." tab links. The Block/Lot fields
(`ddlMunTab4` / `txtBlockTab4` / `txtLotTab4` / `btnSearchTab4`) stay hidden - and
therefore unfillable - until the "By Block and Lot" tab is clicked, so that click
happens before any field is touched.

Results come back in an ASP.NET DataGrid (`#dgdDeedMort`) with columns Type /
Direct Party / Indirect Party / Instrument # / Recorded / Town / Block / Lot /
Book / Page. Direct Party is the grantor and Indirect Party is the grantee. PRESS
emits **one row per grantor+grantee pair**, so an instrument with 2 grantors and 1
grantee comes back as 2 rows - the worker groups all rows by instrument number and
merges the parties into a single document.

Viewing/downloading a document has no plain URL. It is a multi-frame,
session-stateful flow: the grid row's "view" control is an ASP.NET image button
that navigates to `ShowDetails.aspx` (a classic `<frameset>`), whose head frame has
a "Get Image" button that loads an Atalasoft image-viewer control into the body
frame, whose own "Save" button triggers the real file transfer. Two things were
confirmed by hand against the live site and are worked around deliberately in
`press_worker.py`:

- The viewer needs its own thumbnail/tile AJAX calls to settle before the "Save"
  button's click handler actually fires the file request - a fixed short wait is
  not reliable, so the worker waits for network-idle (best effort) first.
- Chromium routes that file transfer through its native download manager, but
  because it originates from a frame nested inside the classic frameset (not the
  top-level page), Playwright's `Download` API reports it as "canceled" even
  though the server responds `200` with the real bytes. The worker instead
  captures the fully-resolved request URL via `expect_response` and re-fetches it
  with the browser context's own request client (same session cookies), which
  reliably returns the file. PRESS also always returns the same generic filename
  ("OPRSFile.pdf") in `Content-Disposition` regardless of which document was
  requested, so the saved filename is always prefixed with the instrument number.

Because a `page.go_back()` from inside that frameset only unwinds the last
frame-internal navigation (not the top-level results page), the worker never
relies on browser history to get back to the results grid - after each download it
simply re-runs the search from scratch and re-paginates forward.

## Address handling

- **Outside Essex County**: `POST /lookup` returns `status: "outside_county"`
  immediately after geocoding, before any PRESS search is attempted.
- **Address not found / not geocodable**: `status: "address_not_found"`.
- **No PRESS records for the resolved block/lot**: `status: "no_records_found"`.
  This is common and expected - e.g. municipal/government-owned parcels that have
  never been sold or mortgaged since PRESS's 1996 cutoff have no recorded
  instruments at all.
- PRESS's own "no match" behavior was confirmed by hand: a legitimate but
  non-existent block/lot returns a genuine "No results found" page (what the
  worker checks for). Block/lot values that overflow PRESS's expected numeric
  range instead return a single unrelated, spurious record every time - this is a
  quirk of the live site, not the scraper. Because the worker only ever searches
  with block/lot values that came from the county's own parcel GIS layer, this
  overflow case should not occur in normal use.
- NJ's parcel GIS layer returns MOD-IV style municipality names (`"NEWARK CITY"`,
  `"SOUTH ORANGE VILLAGE TOWNSHIP"`, ...), which never match PRESS's dropdown
  options (`"NEWARK"`, `"SOUTH ORANGE VILLAGE"`, ...) exactly. `press_worker.py`
  normalizes these before selecting the dropdown option.

## Notes

- PRESS records are limited by the portal disclaimer to records from `10/01/1996`
  onward.
- A block/lot search with no date range returns full history in one pass - no
  chunking is needed or performed by default. If `from_date`/`to_date` are both
  supplied, PRESS's 90-day max date range is respected by chunking automatically.
- Active mortgage/lien detection is heuristic (looks for a later
  discharge/release/satisfaction referencing the same instrument number or
  overlapping parties). Ambiguous matches are returned as warnings and should be
  reviewed.
- Every step above (search, grid parsing, party merging, municipality
  normalization, and the full document-download flow) has been verified against
  the live production PRESS site, not just mocked.
