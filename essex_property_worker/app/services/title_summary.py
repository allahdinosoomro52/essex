from __future__ import annotations

from app.models import Party, RecordedDocument, TitleSummary


class TitleSummaryBuilder:
    deed_tokens = ("DEED",)
    mortgage_tokens = ("MORTGAGE",)
    lien_tokens = ("LIEN", "LIS PENDENS")
    clearing_tokens = ("DISCHARGE", "RELEASE", "CANCELLATION", "SATISFACTION")
    assignment_tokens = ("ASSIGNMENT",)
    # PRESS indexes municipal tax-sale certificates under the same "MORTGAGE" document
    # type as real mortgage loans (confirmed by reading an actual recorded certificate).
    # A municipality named as the mortgagee/grantee is a reliable, free signal - no OCR
    # needed - that a "MORTGAGE"-tagged document is really a tax lien, since cities don't
    # issue conventional real-estate mortgages.
    municipality_markers = ("CITY OF", "TOWNSHIP OF", "BOROUGH OF", "TOWN OF", "VILLAGE OF", "COUNTY OF")

    def build(self, documents: list[RecordedDocument]) -> TitleSummary:
        sorted_docs = sorted(documents, key=lambda d: d.recording_date or __import__("datetime").date.min, reverse=True)
        ownership_chain = [doc for doc in sorted_docs if self._is_deed(doc)]
        current_owner = self._current_owner(ownership_chain)

        mortgage_candidates = [
            doc
            for doc in sorted_docs
            if self._has_any(doc, self.mortgage_tokens) and not self._has_any(doc, self.assignment_tokens)
        ]
        tax_lien_candidates = [doc for doc in mortgage_candidates if self._is_municipal_lienholder(doc)]
        true_mortgage_candidates = [doc for doc in mortgage_candidates if doc not in tax_lien_candidates]
        lien_candidates = [doc for doc in sorted_docs if self._has_any(doc, self.lien_tokens)] + tax_lien_candidates

        active_mortgages = self._open_documents(sorted_docs, true_mortgage_candidates)
        active_liens = self._open_documents(sorted_docs, lien_candidates)

        warnings: list[str] = []
        if not ownership_chain:
            warnings.append("No deed was found in the returned PRESS records, so current owner could not be inferred.")
        if active_mortgages:
            warnings.append("Active mortgage detection is heuristic until instrument-level release matching is verified.")
        if active_liens:
            warnings.append("Active lien detection is heuristic until release/discharge matching is verified.")

        return TitleSummary(
            current_owner=current_owner,
            active_mortgages=active_mortgages,
            active_liens=active_liens,
            ownership_chain=ownership_chain,
            warnings=warnings,
        )

    def _current_owner(self, deeds: list[RecordedDocument]) -> str | None:
        if not deeds:
            return None
        grantees = [party.name for party in deeds[0].parties if party.role.lower() == "grantee"]
        if grantees:
            return "; ".join(grantees)
        return None

    def _open_documents(
        self,
        documents: list[RecordedDocument],
        candidates: list[RecordedDocument],
    ) -> list[RecordedDocument]:
        clearing_docs = [doc for doc in documents if self._has_any(doc, self.clearing_tokens)]
        open_docs: list[RecordedDocument] = []
        for doc in candidates:
            if self._has_any(doc, self.clearing_tokens):
                continue
            if self._has_later_clearing(doc, clearing_docs):
                continue
            open_docs.append(doc)
        return open_docs

    def _is_municipal_lienholder(self, document: RecordedDocument) -> bool:
        mortgagees = [party.name.upper() for party in document.parties if party.role == "grantee"]
        return any(marker in name for name in mortgagees for marker in self.municipality_markers)

    def _has_later_clearing(self, document: RecordedDocument, clearing_docs: list[RecordedDocument]) -> bool:
        for clearing in clearing_docs:
            if document.recording_date and clearing.recording_date and clearing.recording_date < document.recording_date:
                continue
            if document.instrument_number and self._raw_contains(clearing, document.instrument_number):
                return True
            if self._party_overlap(document.parties, clearing.parties):
                return True
        return False

    def _party_overlap(self, left: list[Party], right: list[Party]) -> bool:
        left_names = {party.name.upper() for party in left if party.name}
        right_names = {party.name.upper() for party in right if party.name}
        return bool(left_names & right_names)

    def _raw_contains(self, document: RecordedDocument, needle: str) -> bool:
        if not needle:
            return False
        haystack = " ".join(self._raw_text_fragments(document))
        return needle in haystack

    def _raw_text_fragments(self, document: RecordedDocument) -> list[str]:
        # press_worker stores merged grid rows under raw["rows"] (one dict per PRESS
        # grid row); hand-built documents (tests) may instead use raw["cells"]
        # (a flat list of cell strings). Support both so instrument-number matching
        # works against real, merged documents and not just the test shape.
        fragments: list[str] = []
        for row in document.raw.get("rows", []):
            if isinstance(row, dict):
                fragments.extend(str(value) for value in row.values())
            else:
                fragments.append(str(row))
        fragments.extend(str(value) for value in document.raw.get("cells", []))
        return fragments

    def _is_deed(self, document: RecordedDocument) -> bool:
        doc_type = document.document_type.upper()
        return "DEED" in doc_type and "MORTGAGE" not in doc_type

    def _has_any(self, document: RecordedDocument, tokens: tuple[str, ...]) -> bool:
        doc_type = document.document_type.upper()
        return any(token in doc_type for token in tokens)

