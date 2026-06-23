import os
from dotenv import load_dotenv

# If LIVE_DB is set, force load real credentials from .env
# This overwrites the dummy values set by pytest.ini (via pytest-env)
if os.getenv("LIVE_DB") == "true":
    load_dotenv(override=True)

# Re-export fixtures so all cluster tests can use them without importing directly.
# This keeps cluster files clean and avoids ruff F401/F811 false positives on
# pytest fixture imports.
from tests.fixtures.google_api_mocks import mock_google_apis  # noqa: F401, E402
