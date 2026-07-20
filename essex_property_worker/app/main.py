from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from app.models import LookupRequest, LookupResponse, LookupStatus, Parcel
from app.services.address_resolver import AddressResolver
from app.services.orchestrator import LookupOrchestrator
from app.services.press_worker import PressSearchWorker

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(title="Essex County Property Records Worker", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/playwright-health")
async def playwright_health() -> dict[str, str]:
    # Playwright's sync API cannot run inside a running asyncio loop, so - like the
    # search/download flows - it must be driven from a worker thread.
    await asyncio.to_thread(PressSearchWorker().browser_health_check)
    return {"status": "ok"}


@app.post("/resolve", response_model=Parcel)
async def resolve(request: LookupRequest) -> Parcel:
    return await AddressResolver().resolve(request.address)


@app.post("/lookup", response_model=LookupResponse)
async def lookup(request: LookupRequest) -> LookupResponse:
    started_at = datetime.now(timezone.utc)
    try:
        return await LookupOrchestrator().lookup(request, started_at=started_at)
    except Exception as exc:
        return LookupResponse(
            status=LookupStatus.failed,
            error=_format_error(exc),
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )


@app.get("/documents/download")
async def download_document(
    municipality: str,
    block: str,
    lot: str,
    instrument_number: str,
    document_type: str = "document",
) -> Response:
    parcel = Parcel(
        input_address="",
        normalized_address="",
        municipality=municipality,
        block=block,
        lot=lot,
        source="on-demand-download",
    )
    try:
        body, _content_disposition = await PressSearchWorker().download_document(parcel, instrument_number)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_format_error(exc)) from exc

    filename = f"{instrument_number}_{document_type}.pdf"
    return Response(
        content=body,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _format_error(exc: Exception) -> str:
    message = str(exc).strip()
    stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6)).strip()
    if message:
        return f"{type(exc).__name__}: {message}\n{stack}"
    return stack or repr(exc)
