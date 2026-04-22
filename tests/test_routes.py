"""Tests for Flask web routes."""



class TestIndexRoute:
    """Test the main form page."""

    def test_index_returns_200(self, flask_test_client):
        """GET / returns 200."""
        resp = flask_test_client.get("/")
        assert resp.status_code == 200
        assert b"STAFF DUTY" in resp.data


class TestHealthRoute:
    """Test the health check endpoint."""

    def test_health_returns_200(self, flask_test_client):
        """GET /health returns 200 with minimal body.

        Phase 3 hardening reduced the response to ``{"ok": True}`` — version
        and uptime were removed to avoid info disclosure on the only
        unauthenticated endpoint.
        """
        resp = flask_test_client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}


class TestGenerateRoute:
    """Test the roster generation endpoint."""

    def test_generate_valid(self, flask_test_client, sample_config_json):
        """GET /generate with valid params returns 200."""
        resp = flask_test_client.get("/generate", query_string=sample_config_json)
        assert resp.status_code == 200
        assert b"cal-table" in resp.data

    def test_generate_invalid_dates(self, flask_test_client):
        """Returns 400 for end < start."""
        params = {
            "start": "2026-07-14",
            "end": "2026-07-01",
            "dir_name": ["G1", "G2"],
            "sdnco_count": ["5", "5"],
            "runner_count": ["4", "4"],
        }
        resp = flask_test_client.get("/generate", query_string=params)
        assert resp.status_code == 400

    def test_generate_missing_dirs(self, flask_test_client):
        """Returns 400 for < 2 directorates."""
        params = {
            "start": "2026-07-01",
            "end": "2026-07-14",
            "dir_name": ["G1"],
            "sdnco_count": ["5"],
            "runner_count": ["4"],
        }
        resp = flask_test_client.get("/generate", query_string=params)
        assert resp.status_code == 400


class TestExportExcelRoute:
    """Test Excel export endpoint."""

    def test_export_excel(self, flask_test_client, sample_config_json):
        """GET /export/excel with valid params returns xlsx content-type."""
        resp = flask_test_client.get("/export/excel", query_string=sample_config_json)
        assert resp.status_code == 200
        assert (
            "spreadsheetml" in resp.content_type
            or "application/vnd.openxmlformats" in resp.content_type
        )
