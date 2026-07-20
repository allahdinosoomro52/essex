import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.models import Parcel


class ApiTests(unittest.TestCase):
    def test_health(self):
        response = TestClient(app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("app.main.AddressResolver")
    def test_resolve(self, resolver_class):
        resolver = resolver_class.return_value
        resolver.resolve = AsyncMock(
            return_value=Parcel(
                input_address="920 Broad St, Newark, NJ",
                normalized_address="920 Broad St, Newark, New Jersey, 07102",
                county="Essex",
                municipality="NEWARK CITY",
                block="873",
                lot="1.01",
                source="test",
            )
        )

        response = TestClient(app).post(
            "/resolve",
            json={"address": "920 Broad St, Newark, NJ", "download_documents": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["block"], "873")


if __name__ == "__main__":
    unittest.main()
