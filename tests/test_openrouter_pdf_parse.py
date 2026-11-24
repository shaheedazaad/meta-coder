"""OpenRouter's PDF parser can succeed yet return blank or mostly blank text."""
import json
from unittest.mock import Mock

import pytest

from meta_coder import openrouter, provenance
from meta_coder.coding_sheet import CodingSheetRow
from meta_coder.extraction import ProviderError
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual

TEXT = 'Results showed F(1, 27) = 29.31, p < .001.'
ITEM = {'row_id': 'r1', 'estimate': {'value': 1.5, 'evidence': 'p. 2'}, 'notes': {'value': '', 'evidence': ''}}
FALLBACK = [{'id': 'file-parser', 'pdf': {'engine': 'cloudflare-ai'}}]


def annotated(text, usage=None):
    """A completion carrying the file-parser annotation as OpenRouter returns it."""
    body = {'choices': [{'message': {'content': json.dumps({'effects': [ITEM]}), 'annotations': [
        {'type': 'file', 'file': {'name': 'paper.pdf', 'content': [
            {'type': 'text', 'text': '<file name="paper.pdf">'}, {'type': 'text', 'text': text},
            {'type': 'text', 'text': '</file>'}]}}]}}]}
    if usage is not None:
        body['usage'] = usage
    return body


def parsed(pages, usage=None):
    metadata = '## Metadata\nTitle: A long PDF title that is not page text\n\n## Contents\n'
    return annotated(metadata + ''.join(f'### Page {n}\n\n{page}\n\n' for n, page in enumerate(pages, 1)), usage)


def wire(body):
    return json.dumps(body).encode()


def bodies(transport):
    return [json.loads(call.args[0].data) for call in transport.call_args_list]


def send(monkeypatch, *responses):
    transport = Mock(side_effect=[wire(body) for body in responses])
    monkeypatch.setattr(openrouter, 'cancellable_urlopen', transport)
    return transport


def call_pdf():
    return openrouter._call_openrouter(model='m', api_key='key', prompt='code', pdf_bytes=b'%PDF',
                                       filename='paper.pdf', response_schema={})


@pytest.fixture
def extraction_input(tmp_path):
    manual = parse_coding_manual('effect_definition: Treatment versus control\neffects:\n'
                                 '  estimate: {type: number}\n  notes: {type: string}\n')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'%PDF')
    return {'pdf_path': pdf, 'manual': manual, 'rows': [CodingSheetRow('r1', 'paper.pdf', 'experiment 1')],
            'api_key': 'key', 'model': 'm'}


@pytest.mark.parametrize('pages', [[TEXT] * 12, [TEXT, TEXT, ''], ['Short.'], [TEXT, '']])
def test_good_or_short_parses_are_not_retried(monkeypatch, pages):
    transport = send(monkeypatch, parsed(pages))
    _text, tokens = call_pdf()
    assert transport.call_count == 1
    assert 'plugins' not in bodies(transport)[0]  # OpenRouter's default engine, or native PDF input
    assert 'parse_issue' not in tokens


def test_native_pdf_input_has_no_annotation_and_is_not_retried(monkeypatch):
    transport = send(monkeypatch, {'choices': [{'message': {'content': '{}'}}]})
    assert call_pdf()[0] == '{}'
    assert transport.call_count == 1


@pytest.mark.parametrize('first', [parsed([TEXT] + [''] * 11), parsed(['', '', '']), annotated(''), annotated('## Contents\n')])
def test_blank_or_mostly_blank_parse_is_retried_once_with_the_fallback_parser(monkeypatch, first):
    first['usage'] = {'prompt_tokens': 100, 'completion_tokens': 7}
    transport = send(monkeypatch, first, parsed([TEXT] * 12, {'prompt_tokens': 900, 'completion_tokens': 40}))
    text, tokens = call_pdf()
    assert json.loads(text) == {'effects': [ITEM]}
    first_body, retry_body = bodies(transport)
    assert 'plugins' not in first_body
    assert retry_body == {**first_body, 'plugins': FALLBACK}
    # Both requests are billed, so the reported usage covers both.
    assert (tokens['input_tokens'], tokens['output_tokens']) == (1000, 47)
    assert 'parse_issue' not in tokens


def test_retry_usage_counts_whichever_attempts_reported_it(monkeypatch):
    send(monkeypatch, parsed(['', '']), parsed([TEXT, TEXT], {'prompt_tokens': True}))
    _text, tokens = call_pdf()
    assert (tokens['input_tokens'], tokens['output_tokens']) == (None, None)
    send(monkeypatch, parsed(['', ''], {'completion_tokens': 3}), parsed([TEXT, TEXT]))
    assert call_pdf()[1]['output_tokens'] == 3


def test_retry_without_a_parser_annotation_is_accepted(monkeypatch):
    send(monkeypatch, parsed(['', '']), {'choices': [{'message': {'content': '{}'}}]})
    assert call_pdf() == ('{}', {'input_tokens': None, 'output_tokens': None, 'served_model': None, 'finish_reason': None})


def test_pdf_no_parser_can_read_is_an_error_not_an_empty_result(monkeypatch):
    transport = send(monkeypatch, parsed(['', '']), parsed(['', '']))
    with pytest.raises(ProviderError, match='no text for this PDF.*cloudflare-ai') as error:
        call_pdf()
    assert transport.call_count == 2
    assert json.loads(error.value.raw_response) == parsed(['', ''])


def test_still_mostly_blank_parse_is_held_for_review(monkeypatch, extraction_input):
    mostly_blank = parsed([TEXT] + [''] * 11)
    transport = send(monkeypatch, mostly_blank, mostly_blank)
    result = openrouter.extract_pdf_effects(**extraction_input)
    assert transport.call_count == 2
    assert result.status == 'needs_review'
    assert result.error.startswith('PDF parser returned no text for 11 of 12 pages')
    # The coded values are kept for the reviewer.
    assert result.coded_by_row_id['r1']['estimate'] == ITEM['estimate']


def test_blank_parse_extraction_is_an_error(monkeypatch, extraction_input):
    send(monkeypatch, annotated(''), annotated(''))
    result = openrouter.extract_pdf_effects(**extraction_input)
    assert result.status == 'error' and 'no text for this PDF' in result.error


def test_fallback_attempt_goes_through_the_audited_transport(tmp_path, monkeypatch, extraction_input):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path))
    project = create_project('Parse audit', root=tmp_path / 'projects')
    write_manual(project, extraction_input['manual'])
    first, retry = parsed(['', '']), parsed([TEXT, TEXT])
    send(monkeypatch, first, retry)
    result = provenance.audited_call(project, 'extraction', openrouter.extract_pdf_effects, **extraction_input)
    assert result.status == 'ok'
    operation = next((project.path / 'audit/operations').iterdir())
    exchanges = [json.loads(path.read_text()) for path in sorted(operation.glob('exchange-*.json'))]
    assert len(exchanges) == 2
    sent = [json.loads((project.path / item['request']['body']['path']).read_bytes()) for item in exchanges]
    received = [json.loads((project.path / item['response']['body']['path']).read_bytes()) for item in exchanges]
    assert 'plugins' not in sent[0] and sent[1]['plugins'] == FALLBACK
    assert received == [first, retry]


@pytest.mark.parametrize('body', [
    [], {}, {'choices': 'x'}, {'choices': ['x']}, {'choices': [{'message': 'x'}]},
    {'choices': [{'message': {'annotations': 'x'}}]},
    {'choices': [{'message': {'annotations': ['x', {'type': 'url_citation'}, {'type': 'file', 'file': 'x'},
                                              {'type': 'file', 'file': {'content': 'x'}}]}}]},
])
def test_page_chars_need_a_parser_annotation(body):
    assert openrouter.parsed_page_chars(body) is None


def test_page_chars_count_text_by_page():
    assert openrouter.parsed_page_chars(parsed(['', 'a b c'])) == [0, 3]
    # Text before the first heading is metadata even without a Contents heading.
    assert openrouter.parsed_page_chars(annotated('Preamble\n### Page 1\n\n## Page 2\nx y')) == [0, 2]
    assert openrouter.parsed_page_chars(annotated('No headings at all')) == [15]
    odd_parts = annotated('a')
    odd_parts['choices'][0]['message']['annotations'][0]['file']['content'] += [
        'x', {'type': 'image_url'}, {'type': 'text', 'text': 5}]
    assert openrouter.parsed_page_chars(odd_parts) == [1]
