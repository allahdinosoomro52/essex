from datetime import date
import unittest

from app.models import Party, RecordedDocument
from app.services.press_worker import PressSearchWorker, normalize_municipality
from app.services.title_summary import TitleSummaryBuilder


class TitleSummaryBuilderTests(unittest.TestCase):
    def test_current_owner_comes_from_latest_deed_grantee(self):
        docs = [
            RecordedDocument(
                document_type="DEED",
                recording_date=date(2020, 1, 2),
                parties=[Party(role="grantee", name="Current Owner LLC")],
            ),
            RecordedDocument(
                document_type="DEED",
                recording_date=date(2010, 1, 2),
                parties=[Party(role="grantee", name="Prior Owner")],
            ),
        ]

        summary = TitleSummaryBuilder().build(docs)

        self.assertEqual(summary.current_owner, "Current Owner LLC")
        self.assertEqual([d.parties[0].name for d in summary.ownership_chain], ["Current Owner LLC", "Prior Owner"])

    def test_mortgage_cleared_by_later_release_with_same_instrument(self):
        docs = [
            RecordedDocument(
                instrument_number="1234567",
                document_type="MORTGAGE",
                recording_date=date(2020, 1, 2),
                raw={"cells": ["MORTGAGE", "1234567"]},
            ),
            RecordedDocument(
                document_type="RELEASE OF MORTGAGE",
                recording_date=date(2021, 1, 2),
                raw={"cells": ["RELEASE OF MORTGAGE", "1234567"]},
            ),
        ]

        summary = TitleSummaryBuilder().build(docs)

        self.assertEqual(summary.active_mortgages, [])

    def test_mortgage_held_by_municipality_is_reclassified_as_a_lien(self):
        # Real case: Essex County PRESS indexes tax-sale certificates (a municipal
        # lien for unpaid property taxes) under the same "MORTGAGE" document type as
        # real mortgage loans. A municipality named as the mortgagee is a reliable
        # signal it's actually a tax lien, confirmed by reading an actual recorded
        # "Certificate of Sale for Unpaid Municipal Liens" (Essex County instrument
        # 448156, sold to "NEWARK CITY OF").
        docs = [
            RecordedDocument(
                instrument_number="448156",
                document_type="MORTGAGE",
                recording_date=date(2001, 7, 11),
                parties=[
                    Party(role="grantor", name="N J BELL (ECIA LESSEE)"),
                    Party(role="grantee", name="NEWARK CITY OF"),
                ],
            ),
        ]

        summary = TitleSummaryBuilder().build(docs)

        self.assertEqual(summary.active_mortgages, [])
        self.assertEqual([d.instrument_number for d in summary.active_liens], ["448156"])

    def test_instrument_match_reads_rows_shape_from_merge(self):
        # Regression: PressSearchWorker._merge_by_instrument stores merged grid rows
        # under raw["rows"] (list of dicts), NOT raw["cells"]. _has_later_clearing's
        # instrument-number match must scan that real shape; otherwise a mortgage
        # discharged by a later release that references it by instrument number (and
        # shares no party names) is wrongly reported as still active.
        mortgage = RecordedDocument(
            instrument_number="1234567",
            document_type="MORTGAGE",
            recording_date=date(2015, 1, 2),
            parties=[Party(role="grantor", name="JANE BORROWER")],
            raw={"rows": [{"type": "MORTGAGE", "instrument": "1234567"}]},
        )
        release = RecordedDocument(
            instrument_number="7654321",
            document_type="RELEASE OF MORTGAGE",
            recording_date=date(2020, 6, 1),
            parties=[Party(role="grantor", name="SERVICER XYZ")],
            raw={"rows": [{"type": "RELEASE", "instrument": "7654321", "ref": "1234567"}]},
        )

        summary = TitleSummaryBuilder().build([mortgage, release])

        self.assertEqual(summary.active_mortgages, [])

    def test_mortgage_held_by_private_lender_stays_a_mortgage(self):
        docs = [
            RecordedDocument(
                instrument_number="9999999",
                document_type="MORTGAGE",
                recording_date=date(2020, 1, 2),
                parties=[
                    Party(role="grantor", name="SOME BORROWER LLC"),
                    Party(role="grantee", name="WELLS FARGO BANK, NATIONAL ASSOCIATION"),
                ],
            ),
        ]

        summary = TitleSummaryBuilder().build(docs)

        self.assertEqual([d.instrument_number for d in summary.active_mortgages], ["9999999"])
        self.assertEqual(summary.active_liens, [])


class PressWorkerTests(unittest.TestCase):
    def test_date_windows_use_90_day_max(self):
        windows = list(PressSearchWorker()._date_windows(date(2020, 1, 1), date(2020, 4, 1)))

        self.assertEqual(windows[0], (date(2020, 1, 1), date(2020, 3, 30)))
        self.assertEqual(windows[1], (date(2020, 3, 31), date(2020, 4, 1)))

    def test_merge_by_instrument_combines_multi_party_rows_into_one_document(self):
        # PRESS emits one grid row per grantor/grantee pair, so a deed with 2 grantors
        # and 1 grantee comes back as 2 rows that must collapse into a single document.
        rows = [
            {
                "type": "DEED",
                "direct_party": "LEG 450 BROAD STREET LLC",
                "indirect_party": "MAY NEWARK LLC",
                "instrument": "2021057477",
                "recorded": "5/6/2021",
                "town": "NEWARK",
                "block": "26",
                "lot": "1",
                "book": "",
                "page": "",
            },
            {
                "type": "DEED",
                "direct_party": "KORMAN BENJAMIN",
                "indirect_party": "MAY NEWARK LLC",
                "instrument": "2021057477",
                "recorded": "5/6/2021",
                "town": "NEWARK",
                "block": "26",
                "lot": "1",
                "book": "",
                "page": "",
            },
        ]

        documents = PressSearchWorker()._merge_by_instrument(rows)

        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document.recording_date, date(2021, 5, 6))
        grantors = sorted(p.name for p in document.parties if p.role == "grantor")
        grantees = sorted(p.name for p in document.parties if p.role == "grantee")
        self.assertEqual(grantors, ["KORMAN BENJAMIN", "LEG 450 BROAD STREET LLC"])
        self.assertEqual(grantees, ["MAY NEWARK LLC"])


class NormalizeMunicipalityTests(unittest.TestCase):
    def test_strips_mod_iv_suffixes_to_match_press_dropdown(self):
        self.assertEqual(normalize_municipality("NEWARK CITY"), "NEWARK")
        self.assertEqual(normalize_municipality("BLOOMFIELD TOWNSHIP"), "BLOOMFIELD")
        self.assertEqual(normalize_municipality("CALDWELL BOROUGH"), "CALDWELL")

    def test_keeps_village_when_press_option_includes_it(self):
        self.assertEqual(normalize_municipality("SOUTH ORANGE VILLAGE TOWNSHIP"), "SOUTH ORANGE VILLAGE")

    def test_exact_match_passes_through(self):
        self.assertEqual(normalize_municipality("MONTCLAIR"), "MONTCLAIR")

    def test_unmappable_name_raises(self):
        with self.assertRaises(RuntimeError):
            normalize_municipality("NOT A REAL TOWN")


if __name__ == "__main__":
    unittest.main()

