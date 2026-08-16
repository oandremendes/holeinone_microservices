import json
import pytest
import sys
from pathlib import Path

# Add parent directory to path so we can import claude_extract
sys.path.insert(0, str(Path(__file__).parent.parent))

import claude_extract as ce


class FakeBlock:
    type = 'text'
    def __init__(self, text): self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason='end_turn'):
        self.stop_reason = stop_reason
        self.content = [FakeBlock(text)]


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = self
    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


GOOD = json.dumps({'supplier_name': 'Novadis', 'supplier_nif': '504350900',
                   'customer_nif': None, 'invoice_ref': 'FT FT100/011485',
                   'date': '2026-03-15', 'due_date': None, 'base_cents': 37458,
                   'iva_cents': 7235, 'total_cents': 44693,
                   'total_vasilhame_cents': None, 'total_document_cents': None,
                   'lines': [{'description': 'Barril', 'supplier_code': None,
                              'quantity': 2, 'unit_price_eur': None,
                              'discount_pct': None, 'iva_rate_pct': 23.0,
                              'line_net_cents': 37458, 'line_total_cents': 44693}],
                   'taxes': []})


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / 'x.pdf'
    p.write_bytes(b'%PDF-1.4 fake')
    return p


def test_extract_ok(pdf):
    fake = FakeClient([FakeResponse(f'aqui está:\n{GOOD}\nfim')])
    result, raw = ce.extract(pdf, supplier='novadis', client=fake)
    assert result.total_cents == 44693
    assert result.lines[0].description == 'Barril'
    assert 'Notas específicas' in fake.calls[0]['messages'][0]['content'][1]['text']
    assert fake.calls[0]['model'] == 'claude-opus-5'


def test_extract_retries_bad_json_once(pdf):
    fake = FakeClient([FakeResponse('not json at all'), FakeResponse(GOOD)])
    result, _ = ce.extract(pdf, client=fake)
    assert result.total_cents == 44693
    assert len(fake.calls) == 2


def test_extract_gives_up_after_two_bad(pdf):
    fake = FakeClient([FakeResponse('junk'), FakeResponse('junk')])
    with pytest.raises(RuntimeError) as e:
        ce.extract(pdf, client=fake)
    assert not isinstance(e.value, ce.PermanentExtractionError)


def test_refusal_is_permanent(pdf):
    fake = FakeClient([FakeResponse('', stop_reason='refusal')])
    with pytest.raises(ce.PermanentExtractionError):
        ce.extract(pdf, client=fake)
