from dataclasses import replace

from meta_coder.projects import create_project
from meta_coder.web import TEMPLATES, _home_view


def test_home_search_and_sort_before_pagination(tmp_path):
    projects = []
    for index in range(24):
        project = create_project(f"Study {index:02}", root=tmp_path)
        projects.append(replace(project, created_at=f"2026-09-{index + 1:02}T00:00:00Z"))
        if index % 2 == 0:
            (project.sources_dir / "paper.PDF").write_bytes(b"%PDF-1.4\n")
    page = _home_view(projects, q=" STUDY 1 ", sort="name", page=1)
    assert page["total_projects"] == 24
    assert page["matching_count"] == 10
    assert page["total_pages"] == 1
    assert [project.name for project in page["projects"]] == [f"Study {index}" for index in range(10, 20)]
    assert (page["first_project"], page["last_project"]) == (1, 10)
    assert page["pdf_counts"][projects[10].project_id] == 1
    assert page["pdf_counts"][projects[11].project_id] == 0
    assert _home_view(projects, sort="oldest")["projects"][0].name == "Study 00"
    assert _home_view(projects)["projects"][0].name == "Study 23"


def test_home_clamps_pages_and_handles_empty_search(tmp_path):
    project = create_project('Memory & attention', root=tmp_path)
    assert _home_view([project], page=-4)["page"] == 1
    assert _home_view([project], page=999)["page"] == 1
    defaults = _home_view([project], sort="invalid")
    assert defaults["sort"] == "newest"
    empty = _home_view([project], q="missing", page=999)
    assert empty["page"] == 1 and empty["projects"] == []
    assert empty["first_project"] == empty["last_project"] == 0
    html = TEMPLATES.env.get_template('home.html').render(token='test', **empty)
    assert 'No matching projects' in html
    assert 'Clear search' in html
    assert 'No projects yet' not in html


def test_home_page_links_preserve_search_and_sort(tmp_path):
    import asyncio
    from html import unescape
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit

    from starlette.requests import Request
    from meta_coder.web import Runtime, create_app

    for index in range(12):
        create_project(f'Memory & attention {index:02}', root=tmp_path)
    app = create_app(token='test', projects_root=tmp_path)
    home = next(route.endpoint for route in app.routes if route.path == '/test/')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/test/', 'headers': [], 'query_string': b''})
    with patch.object(Runtime, 'has_any_api_key', return_value=False):
        response = asyncio.run(home(request, q='Memory & attention', sort='name', page=1))
    import re
    html = response.body.decode()
    next_url = unescape(re.search(r'href="([^"]+)">Next</a>', html).group(1))
    assert parse_qs(urlsplit(next_url).query) == {
        'q': ['Memory & attention'], 'sort': ['name'], 'page': ['2'],
    }
    assert html.count('class="row project-home-row"') == 10
    assert '1–10 of 12 projects' in html
