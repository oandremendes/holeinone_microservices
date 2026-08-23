"""Extração de linhas de fatura com Claude."""
import base64
from pathlib import Path

from pydantic import BaseModel, Field

MODEL = 'claude-opus-5'

PROMPT = (
    'Esta é uma fatura digitalizada (normalmente portuguesa). Extrai os dados '
    'do documento e todas as linhas de detalhe (artigos/serviços). Valores '
    'monetários em cêntimos inteiros (ex.: 78,71 € -> 7871). Em cada linha, '
    'line_net_cents é o valor SEM IVA e line_total_cents é o valor COM IVA; '
    'preenche ambos quando o documento o permitir — se só mostrar um deles, '
    'calcula o outro com a taxa de IVA da linha. unit_price_eur é o preço '
    'unitário como impresso no documento. Linhas de vasilhame/tara ficam em '
    'lines como as restantes; total_vasilhame_cents resume-as se o documento '
    'as totalizar. Extrai também o quadro-resumo de IVA (taxes), com base e '
    'valor por taxa, se existir. Se um campo não existir no documento, usa '
    'null. Não inventes linhas: extrai apenas o que está legível.'
)


# Restauração/cafés: não interessa o que se comeu — as linhas passam a ser o
# resumo de IVA do talão, uma por taxa, com o nome que o Odoo emparelha.
# Conjunto extensível: basta acrescentar a supplier_key aqui.
MEAL_SUPPLIERS = {
    'americansmash', 'anticapizzeria', 'apaisagem', 'bagga', 'beiramar',
    'burgerking', 'butchers', 'dominos', 'eurolatina', 'goldenmarina',
    'graziemille', 'gustozza', 'matchpoint', 'mcdonalds', 'mourapao',
    'osakasushi', 'pizzahut', 'plate', 'prosaaromaticas', 'reichurrasco',
    'sushimishi', 'sweetcup', 'tabernamodesto', 'tribulum', 'tuttapanna',
    'zorba',
}

MEAL_PROMPT = (
    'REGRA ESPECIAL (documento de restauração/café): NÃO extraias os artigos '
    'individuais consumidos. Em lines produz exatamente uma linha por taxa de '
    'IVA do quadro-resumo do talão, com description "Despesa Refeição 6%", '
    '"Despesa Refeição 13%", "Despesa Refeição 23%" (conforme as taxas '
    'presentes), quantity 1, line_net_cents = base dessa taxa, '
    'line_total_cents = base + IVA dessa taxa, unit_price_eur = base em '
    'euros, supplier_code null. O quadro taxes preenche-se na mesma como '
    'impresso.'
)

# Instruções específicas por fornecedor (chave = supplier_key do QA, em minúsculas).
# O esquema de saída é único; isto só orienta onde encontrar a informação.
SUPPLIER_HINTS = {
    'garcias': (
        'Distribuidor de bebidas. Cada linha tem o código do artigo na primeira '
        'coluna (supplier_code). Linhas "(OFERTA)" são bonificações: quantidade '
        'real, desconto 100%, valores 0. O quadro de IVA pode repetir a mesma '
        'taxa em várias sublinhas — extrai cada sublinha tal como impressa.'
    ),
    'novadis': (
        'Distribuidor de bebidas (grupo Heineken). Tem linhas de vasilhame/tara '
        'a 0% de IVA — inclui-as em lines e soma-as em total_vasilhame_cents. '
        '"CAUCIONAMENTOS" e "DESCAUCIONAMENTOS" são títulos de secção da fatura, '
        'não fazem parte do nome do artigo: em description usa só o nome do '
        'artigo tal como impresso na linha, sem esses títulos nem variantes '
        'como "(caucionamento)". '
        'Os preços unitários podem ter descontos comerciais aplicados no total '
        'da linha; usa o total impresso, não qty*preço. Data de vencimento '
        'normalmente impressa no rodapé.'
    ),
    'justdrinks': 'Distribuidor de bebidas. Fatura com códigos de artigo por linha.',
    'teofilo': (
        'Armazenista (Estabelecimentos Teofilo Fontainhas Neto). Ficheiros '
        '"_nc" são notas de crédito — os valores continuam positivos no '
        'documento. Linhas com código de artigo.'
    ),
    'jmv': 'Distribuidor de bebidas. Fatura com códigos de artigo por linha.',
    'overseas': 'Importador de bebidas/vinhos. Fatura com códigos de artigo por linha.',
    'absolutlyvintage': 'Comércio de vinhos. Fatura com códigos de artigo por linha.',
    'garrafeiranacional': 'Garrafeira (vinhos e destilados).',
    'makro': (
        'Cash & carry. As linhas têm código de artigo Makro; a fatura mostra '
        'valores por linha COM e SEM IVA e um resumo por taxa no final. Pode '
        'ter várias páginas — extrai as linhas de todas.'
    ),
    'soares': 'Fornecedor local. Extrai as linhas tal como impressas.',
}


class Line(BaseModel):
    description: str
    supplier_code: str | None = Field(None, description='código/referência do artigo no fornecedor')
    quantity: float | None = None
    unit_price_eur: float | None = Field(None, description='preço unitário em euros, como impresso, ex.: 1.599')
    discount_pct: float | None = Field(None, description='desconto da linha em percentagem, ex.: 15')
    iva_rate_pct: float | None = Field(None, description='taxa de IVA em percentagem, ex.: 23')
    line_net_cents: int | None = Field(None, description='total da linha SEM IVA, em cêntimos')
    line_total_cents: int | None = Field(None, description='total da linha COM IVA, em cêntimos')


class TaxLine(BaseModel):
    rate_pct: float | None = Field(None, description='taxa de IVA em percentagem')
    base_cents: int | None = Field(None, description='base tributável desta taxa, em cêntimos')
    value_cents: int | None = Field(None, description='valor de IVA desta taxa, em cêntimos')


class Extraction(BaseModel):
    supplier_name: str | None
    supplier_nif: str | None = Field(None, description='NIF do emitente, 9 dígitos')
    customer_nif: str | None = Field(None, description='NIF do cliente/adquirente, sem prefixo de país')
    invoice_ref: str | None = Field(None, description='número/referência da fatura, ex.: FT FC26/017806')
    date: str | None = Field(None, description='data de emissão, YYYY-MM-DD')
    due_date: str | None = Field(None, description='data de vencimento, YYYY-MM-DD')
    base_cents: int | None = Field(None, description='base tributável em cêntimos')
    iva_cents: int | None = Field(None, description='total de IVA em cêntimos')
    total_cents: int | None = Field(None, description='total da mercadoria com IVA, em cêntimos')
    total_vasilhame_cents: int | None = Field(None, description='total de vasilhame/taras, em cêntimos')
    total_document_cents: int | None = Field(
        None, description='total a pagar do documento (mercadoria + vasilhame), se distinto de total_cents')
    lines: list[Line]
    taxes: list[TaxLine] = Field(default_factory=list, description='quadro-resumo de IVA por taxa')


def build_content(pdf_path, supplier=None):
    """Blocos de conteúdo (PDF + prompt) para um pedido de extração."""
    pdf_b64 = base64.standard_b64encode(Path(pdf_path).read_bytes()).decode()
    schema = Extraction.model_json_schema()
    prompt = PROMPT
    base_key = (supplier or '').lower().removesuffix('_nc')
    hint = SUPPLIER_HINTS.get(base_key)
    if hint:
        prompt += f'\n\nNotas específicas deste fornecedor: {hint}'
    if base_key in MEAL_SUPPLIERS:
        prompt += f'\n\n{MEAL_PROMPT}'
    return [
        {'type': 'document',
         'source': {'type': 'base64', 'media_type': 'application/pdf',
                    'data': pdf_b64}},
        {'type': 'text', 'text':
         f'{prompt}\n\nResponde APENAS com um objeto JSON válido que respeite '
         f'este JSON Schema (sem texto antes ou depois):\n{schema}'},
    ]


def _parse_json_response(text):
    """Extrai o objeto JSON da resposta (tolera texto/cercas à volta)."""
    import json
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end <= start:
        raise ValueError('resposta sem objeto JSON')
    return json.loads(text[start:end + 1])


class PermanentExtractionError(RuntimeError):
    """Failure that must not be retried (refusal, unreadable input)."""


def extract(pdf_path, supplier=None, api_key=None, client=None):
    """PDF -> (Extraction, raw_json_str, served_model). `client` injectable.

    Pede fallback server-side ("default"): se os classificadores do Opus 5
    recusarem, a API reexecuta o mesmo pedido no Opus 4.8 dentro da mesma
    chamada; served_model identifica o modelo que respondeu. Um refusal na
    resposta final significa que toda a cadeia recusou ->
    PermanentExtractionError. Respostas com JSON inválido têm uma repetição;
    ainda inválido -> RuntimeError (transiente, repetido pela fila).
    """
    import pydantic
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    content = build_content(pdf_path, supplier)
    messages = [{'role': 'user', 'content': content}]
    last_err = None
    for _ in range(2):
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, messages=messages,
            betas=['server-side-fallback-2026-07-01'], fallbacks='default')
        if response.stop_reason == 'refusal':
            raise PermanentExtractionError('Claude recusou (stop_reason=refusal)')
        served_model = getattr(response, 'model', None) or MODEL
        text = next(b.text for b in response.content if b.type == 'text')
        try:
            result = Extraction.model_validate(_parse_json_response(text))
            return result, result.model_dump_json(), served_model
        except (ValueError, pydantic.ValidationError) as e:
            last_err = e
            # include the assistant turn so the model sees its own invalid
            # response before the correction request
            messages = [{'role': 'user', 'content': content},
                        {'role': 'assistant', 'content': text},
                        {'role': 'user', 'content':
                         f'A resposta anterior era inválida ({e}). Responde de '
                         f'novo, apenas com o objeto JSON válido.'}]
    raise RuntimeError(f'Resposta JSON inválida após repetição: {last_err}')
