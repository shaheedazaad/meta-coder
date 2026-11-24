"""Model suggestions for the extractor form, from each provider's catalog."""
import io
import json
import urllib.error

import pytest

from meta_coder import gemini, openai_compatible, openrouter, providers
from meta_coder.extraction import ProviderError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def serve(monkeypatch, module, pages, seen=None):
    pages = list(pages)

    def urlopen(request, timeout):
        if seen is not None:
            seen.append(request)
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return Response(page if isinstance(page, bytes) else json.dumps(page).encode())

    monkeypatch.setattr(module.urllib.request, 'urlopen', urlopen)


def test_gemini_lists_generate_content_models_across_pages(monkeypatch):
    seen = []
    serve(monkeypatch, gemini, [
        {'models': [{'name': 'models/gemini-b', 'supportedGenerationMethods': ['generateContent']},
                    {'name': 'models/embed', 'supportedGenerationMethods': ['embedContent']}],
         'nextPageToken': 'next'},
        {'models': [{'name': 'models/gemini-a', 'supportedGenerationMethods': ['generateContent']}, {}]},
    ], seen)
    assert providers.list_models('gemini', api_key='secret') == ['gemini-a', 'gemini-b']
    assert 'secret' not in seen[0].full_url and seen[0].get_header('X-goog-api-key') == 'secret'
    assert 'pageToken=next' in seen[1].full_url


@pytest.mark.parametrize(('failure', 'message'), [
    (urllib.error.HTTPError('u', 403, 'Forbidden', {}, None), r'\(403\)'),
    (urllib.error.URLError('offline secret'), r'offline \[redacted\]'),
    (b'not json', 'Could not load Gemini models'),
])
def test_gemini_listing_failures_are_redacted(monkeypatch, failure, message):
    serve(monkeypatch, gemini, [failure])
    with pytest.raises(ProviderError, match=message):
        gemini.list_model_ids(api_key='secret')


def test_gemini_listing_needs_a_key():
    with pytest.raises(ProviderError, match='Enter an API key'):
        gemini.list_model_ids(api_key='')


def test_endpoint_lists_its_optional_catalog(monkeypatch):
    seen = []
    serve(monkeypatch, openai_compatible, [{'data': [{'id': 'b'}, {'id': 'a'}, {'no': 'id'}, 'junk']}, ['not', 'a', 'dict']], seen)
    assert providers.list_models('openai_compatible', api_key='k', base_url='http://localhost/v1/') == ['a', 'b']
    assert seen[0].full_url == 'http://localhost/v1/models'
    assert seen[0].get_header('Authorization') == 'Bearer k'
    assert openai_compatible.list_model_ids(base_url='http://localhost/v1') == []
    assert seen[1].get_header('Authorization') is None


@pytest.mark.parametrize(('failure', 'message'), [
    (urllib.error.HTTPError('u', 404, 'Not found', {}, None), r'\(404\)'),
    (urllib.error.URLError('refused k'), r'refused \[redacted\]'),
])
def test_endpoint_listing_failures_are_redacted(monkeypatch, failure, message):
    serve(monkeypatch, openai_compatible, [failure])
    with pytest.raises(ProviderError, match=message):
        openai_compatible.list_model_ids(api_key='k', base_url='http://localhost/v1')


def test_endpoint_listing_needs_a_valid_base_url():
    with pytest.raises(ProviderError, match='API base URL'):
        openai_compatible.list_model_ids(base_url='')


def test_openrouter_listing_and_unknown_provider(monkeypatch):
    monkeypatch.setattr(openrouter, 'list_models', lambda **kwargs: [{'id': 'z/b'}, {'id': 'a/b'}, {'name': 'no id'}])
    assert providers.list_models('openrouter') == ['a/b', 'z/b']
    with pytest.raises(ValueError, match='Unknown provider'):
        providers.list_models('bogus')
