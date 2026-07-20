from __future__ import annotations

import traceback
from datetime import datetime, timezone

from app.models import LookupRequest, LookupResponse, LookupStatus
from app.services.address_resolver import AddressResolver, AddressResolutionError, OutsideEssexCountyError
from app.services.press_worker import PressSearchWorker
from app.services.title_summary import TitleSummaryBuilder


class LookupOrchestrator:
    def __init__(
        self,
        resolver: AddressResolver | None = None,
        press_worker: PressSearchWorker | None = None,
        summary_builder: TitleSummaryBuilder | None = None,
    ) -> None:
        self.resolver = resolver or AddressResolver()
        self.press_worker = press_worker or PressSearchWorker()
        self.summary_builder = summary_builder or TitleSummaryBuilder()

    async def lookup(self, request: LookupRequest, started_at: datetime | None = None) -> LookupResponse:
        started_at = started_at or datetime.now(timezone.utc)
        warnings: list[str] = []

        try:
            parcel = await self.resolver.resolve(request.address)
        except OutsideEssexCountyError as exc:
            return LookupResponse(
                status=LookupStatus.outside_county,
                warnings=[str(exc)],
                error=str(exc),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        except AddressResolutionError as exc:
            return LookupResponse(
                status=LookupStatus.address_not_found,
                warnings=[str(exc)],
                error=str(exc),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )

        try:
            documents = await self.press_worker.search_by_block_lot(
                parcel=parcel,
                from_date=request.from_date,
                to_date=request.to_date,
                download_documents=request.download_documents,
            )
        except Exception as exc:
            error = self._format_error(exc)
            return LookupResponse(
                status=LookupStatus.failed,
                parcel=parcel,
                warnings=[
                    "Address resolution succeeded, but the PRESS browser automation step failed.",
                    "Check that Playwright Chromium is installed and that the PRESS selectors still match the live site.",
                ],
                error=error,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )

        if not documents:
            warnings.append("Parcel resolved successfully, but no PRESS records were found for this block/lot.")
            return LookupResponse(
                status=LookupStatus.no_records_found,
                parcel=parcel,
                documents=[],
                warnings=warnings,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )

        summary = self.summary_builder.build(documents)
        warnings.extend(summary.warnings)
        return LookupResponse(
            status=LookupStatus.completed,
            parcel=parcel,
            summary=summary,
            documents=documents,
            warnings=warnings,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )

    def _format_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8)).strip()
        if message:
            return f"{type(exc).__name__}: {message}\n{stack}"
        return stack or repr(exc)
