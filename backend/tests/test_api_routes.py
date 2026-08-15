from datetime import date

from fastapi.testclient import TestClient

from elections.api.app import create_app
from elections.api.schemas import (
    Accounting,
    DatasetStatus,
    Hierarchy,
    Party,
    PartyResult,
    PointFilters,
    PointPage,
    Region,
    ScatterPoint,
    SpecialType,
    Tik,
    UikDetail,
)


class FakeRepository:
    def __init__(self) -> None:
        self.point_filters: PointFilters | None = None

    def dataset_status(self) -> DatasetStatus:
        return DatasetStatus(
            election_slug="duma-2021",
            election_name="2021 State Duma",
            election_date=date(2021, 9, 19),
            ingestion_status="succeeded",
            result_records=1,
            matched_records=1,
        )

    def list_parties(self) -> list[Party]:
        return [Party(id=7, ballot_id=2, name="Example Party", position=1)]

    def list_regions(self) -> list[Region]:
        return [Region(name="Moscow", result_records=1)]

    def list_tiks(self, region: str | None = None) -> list[Tik]:
        if region not in (None, "Moscow"):
            return []
        return [Tik(name="Central TIK", region_name="Moscow", result_records=1)]

    def list_special_types(self) -> list[SpecialType]:
        return [SpecialType(value="deg", label="DEG", result_records=1, is_deg=True)]

    def list_points(self, filters: PointFilters) -> PointPage:
        self.point_filters = filters
        item = ScatterPoint(
            result_record_id=42,
            party_id=7,
            uik_number="123",
            tik_name="Central TIK",
            region_name="Moscow",
            registered_voters=1000,
            ballots_counted=500,
            party_votes=300,
            turnout_percent=50,
            party_percent=60,
            match_status="matched",
            validation_status="valid",
            special_type="none",
        )
        return PointPage(
            items=[item], offset=filters.offset, limit=filters.limit, total=1, has_more=False
        )

    def get_uik(self, result_record_id: int) -> UikDetail | None:
        if result_record_id != 42:
            return None
        return UikDetail(
            result_record_id=42,
            uik_number="123",
            hierarchy=Hierarchy(
                election="2021 State Duma",
                ballot="Federal party list",
                region="Moscow",
                tik="Central TIK",
                uik="123",
            ),
            accounting=Accounting(registered_voters=1000, valid_ballots=500),
            turnout_percent=50,
            party_results=[PartyResult(party_id=7, name="Example Party", votes=300, percent=60)],
            match_status="matched",
        )


def make_client() -> tuple[TestClient, FakeRepository]:
    repository = FakeRepository()
    return TestClient(create_app(lambda: repository)), repository


def test_health_and_dataset_status() -> None:
    client, _ = make_client()

    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/api/v1/dataset/status")
    assert status.status_code == 200
    assert status.json()["election_slug"] == "duma-2021"


def test_local_frontend_cors_for_read_requests() -> None:
    client, _ = make_client()

    response = client.get(
        "/health",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_filter_metadata() -> None:
    client, _ = make_client()

    assert client.get("/api/v1/parties").json()[0]["id"] == 7
    assert client.get("/api/v1/regions").json()[0]["name"] == "Moscow"
    assert (
        client.get("/api/v1/tiks", params={"region": "Moscow"}).json()[0]["name"] == "Central TIK"
    )
    assert client.get("/api/v1/special-types").json()[0]["is_deg"] is True


def test_points_forward_filters_and_pagination() -> None:
    client, repository = make_client()
    response = client.get(
        "/api/v1/points",
        params=[
            ("party_id", "7"),
            ("party_id", "8"),
            ("region", "Moscow"),
            ("match_status", "matched"),
            ("match_status", "special"),
            ("is_deg", "false"),
            ("turnout_min", "25"),
            ("result_max", "80"),
            ("offset", "10"),
            ("limit", "100"),
        ],
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["party_votes"] == 300
    assert repository.point_filters == PointFilters(
        party_ids=[7, 8],
        regions=["Moscow"],
        match_statuses=["matched", "special"],
        is_deg=False,
        turnout_min=25,
        result_max=80,
        offset=10,
        limit=100,
    )


def test_points_reject_inverted_ranges() -> None:
    client, _ = make_client()

    response = client.get("/api/v1/points?turnout_min=70&turnout_max=50")
    assert response.status_code == 422
    assert response.json()["detail"] == "turnout_min must not exceed turnout_max"


def test_uik_detail_and_missing_result() -> None:
    client, _ = make_client()

    response = client.get("/api/v1/uiks/42")
    assert response.status_code == 200
    assert response.json()["hierarchy"]["tik"] == "Central TIK"
    assert response.json()["party_results"][0]["votes"] == 300
    assert client.get("/api/v1/uiks/404").status_code == 404


def test_openapi_documents_only_read_operations() -> None:
    client, _ = make_client()
    document = client.get("/openapi.json").json()

    expected_paths = {
        "/health",
        "/api/v1/dataset/status",
        "/api/v1/parties",
        "/api/v1/regions",
        "/api/v1/tiks",
        "/api/v1/special-types",
        "/api/v1/points",
        "/api/v1/uiks/{result_record_id}",
    }
    assert expected_paths <= document["paths"].keys()
    assert {operation for path in document["paths"].values() for operation in path} == {"get"}
