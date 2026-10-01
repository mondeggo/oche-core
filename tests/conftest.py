import pytest

from ochecore.config import Settings

BOARD_ID = "11111111-1111-4111-8111-111111111111"
MATCH_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path, client_id="", board_id="", ui_enabled=True)
