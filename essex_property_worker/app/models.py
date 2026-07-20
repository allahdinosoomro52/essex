from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LookupStatus(str, Enum):
    completed = "completed"
    outside_county = "outside_county"
    address_not_found = "address_not_found"
    no_records_found = "no_records_found"
    failed = "failed"


class LookupRequest(BaseModel):
    address: str = Field(..., min_length=5)
    download_documents: bool = False
    from_date: date | None = None
    to_date: date | None = None


class Parcel(BaseModel):
    input_address: str
    normalized_address: str
    county: str | None = None
    municipality: str
    block: str
    lot: str
    qualifier: str | None = None
    parcel_id: str | None = None
    owner_name: str | None = None
    source: str
    raw: dict[str, Any] = Field(default_factory=dict)


class Party(BaseModel):
    role: str
    name: str


class RecordedDocument(BaseModel):
    instrument_number: str | None = None
    document_type: str
    recording_date: date | None = None
    book: str | None = None
    page: str | None = None
    parties: list[Party] = Field(default_factory=list)
    amount: str | None = None
    source_detail_url: str | None = None
    document_url: str | None = None
    downloaded_path: str | None = None
    plain_english: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TitleSummary(BaseModel):
    current_owner: str | None = None
    active_mortgages: list[RecordedDocument] = Field(default_factory=list)
    active_liens: list[RecordedDocument] = Field(default_factory=list)
    ownership_chain: list[RecordedDocument] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LookupResponse(BaseModel):
    status: LookupStatus
    parcel: Parcel | None = None
    summary: TitleSummary | None = None
    documents: list[RecordedDocument] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime
    completed_at: datetime

