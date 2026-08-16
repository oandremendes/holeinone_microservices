import pytest
import state


@pytest.fixture
def conn(tmp_path):
    c = state.connect(tmp_path / 'state.db')
    yield c
    c.close()
