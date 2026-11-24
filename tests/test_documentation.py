"""Bundled help must work offline, retain the token, and never expose app files."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient

from meta_coder.projects import create_project
from meta_coder.web import create_app


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {'href', 'src'} and value:
                self.urls.append(value)


def test_bundled_documentation_and_contextual_links(tmp_path, monkeypatch):
    monkeypatch.setenv('META_CODER_HOME', str(tmp_path))
    project = create_project('Help test', root=tmp_path / 'projects')
    app = create_app(token='help-token', projects_root=tmp_path / 'projects')
    with TestClient(app, base_url='http://localhost') as client:
        doc_links = set()
        for path in ['/', '/settings', f'/projects/{project.project_id}']:
            response = client.get('/help-token' + path)
            assert response.status_code == 200
            doc_links.update(url for url in Links(response.text).urls if '/docs/' in url)
        assert len(doc_links) == 12
        assert "/help-token/docs/#install-or-update" in doc_links
        # Crawl every guide, image, stylesheet, and script from real UI links.
        seen = set()
        pending = list(doc_links)
        while pending:
            url = pending.pop()
            if url in seen:
                continue
            seen.add(url)
            response = client.get(url)
            assert response.status_code == 200, url
            if 'text/html' in response.headers.get('content-type', ''):
                for link in Links(response.text).urls:
                    target = urlsplit(urljoin(str(response.url), link))
                    if target.netloc == 'localhost':
                        assert target.path.startswith('/help-token/docs/'), target.path
                        pending.append(target.path)
        assert '/help-token/docs/troubleshooting/' in seen
        assert len([url for url in seen if url.endswith('.png')]) == 10
        for path in ['/docs/', '/wrong-token/docs/', '/help-token/docs/missing/', '/help-token/docs/%2e%2e/web.py']:
            assert client.get(path).status_code == 404
