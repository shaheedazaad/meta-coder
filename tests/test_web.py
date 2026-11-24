import asyncio
import time
from pathlib import Path
from unittest.mock import patch

from meta_coder.pdf_matching import apply_source_pdf_matches, score_pair as real_score_pair
from meta_coder.projects import create_project
from meta_coder.web import Runtime, _project_view, create_app


def test_view_raw_output_renders_stored_response(tmp_path):
    project = create_project("Raw Output Test", root=tmp_path)
    (project.raw_dir / "response.json").write_text(
        '{"answer": "Stored model response"}', encoding="utf-8"
    )
    app = create_app(token="test-token", projects_root=tmp_path)

    async def get_raw_output():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": f"/test-token/projects/{project.project_id}/raw/response.json",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"localhost")],
                "server": ("localhost", 80),
            },
            receive,
            send,
        )
        return messages

    messages = asyncio.run(get_raw_output())
    response = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages).decode()
    assert response["status"] == 200
    assert "Raw model output" in body
    assert "Stored model response" in body
    assert f"/test-token/projects/{project.project_id}?tab=results" in body


def _project_with_unmatched_rows(tmp_path: Path):
    root = tmp_path / "projects"
    project = create_project("Test Project", root=root)
    project.coding_sheet_path.write_text(
        "row_id,source_pdf,locator,authors,year\n"
        "r1,smith2020.pdf,Exp 1,Smith,2020\n",
        encoding="utf-8",
    )
    (project.sources_dir / "orphan.pdf").write_bytes(b"%PDF-1.4\n")
    return project


def _await_scan(runtime: Runtime, project_id: str) -> None:
    for _ in range(500):
        if not runtime.pdf_scanner.is_running(project_id):
            return
        time.sleep(0.01)
    raise AssertionError("scan never finished")


def test_pair_scores_are_cached_across_repeated_project_views(tmp_path):
    project = _project_with_unmatched_rows(tmp_path)
    runtime = Runtime(projects_root=project.path.parent)

    # First render kicks off the background PDF scan (orphan.pdf isn't in
    # the signal cache yet) rather than computing suggestions itself.
    _project_view(runtime, project)
    _await_scan(runtime, project.project_id)

    with patch("meta_coder.pdf_matching.score_pair", side_effect=real_score_pair) as mock_score:
        _project_view(runtime, project)
        _project_view(runtime, project)
        assert mock_score.call_count == 1


def test_pair_score_cache_persists_across_runtime_restarts(tmp_path):
    project = _project_with_unmatched_rows(tmp_path)
    first_runtime = Runtime(projects_root=project.path.parent)

    _project_view(first_runtime, project)
    _await_scan(first_runtime, project.project_id)
    _project_view(first_runtime, project)  # scores once and writes the disk cache

    restarted_runtime = Runtime(projects_root=project.path.parent)
    with patch("meta_coder.pdf_matching.score_pair", side_effect=AssertionError("should use disk cache")):
        _project_view(restarted_runtime, project)


def test_pair_scores_compute_only_for_a_new_pdf(tmp_path):
    project = _project_with_unmatched_rows(tmp_path)
    runtime = Runtime(projects_root=project.path.parent)

    _project_view(runtime, project)
    _await_scan(runtime, project.project_id)

    with patch("meta_coder.pdf_matching.score_pair", side_effect=real_score_pair) as mock_score:
        _project_view(runtime, project)
        assert mock_score.call_count == 1

        (project.sources_dir / "another_orphan.pdf").write_bytes(b"%PDF-1.4\n")
        _project_view(runtime, project)  # kicks off a scan for the new file
        _await_scan(runtime, project.project_id)

        _project_view(runtime, project)
        assert mock_score.call_count == 2


def test_accepting_a_match_reuses_remaining_pair_scores(tmp_path):
    root = tmp_path / "projects"
    project = create_project("Test Project", root=root)
    project.coding_sheet_path.write_text(
        "row_id,source_pdf,locator,authors,year\n"
        "r1,smith.pdf,Exp 1,Smith,2020\n"
        "r2,jones.pdf,Exp 1,Jones,2019\n",
        encoding="utf-8",
    )
    (project.sources_dir / "Smith_2020.pdf").write_bytes(b"%PDF-1.4\n")
    (project.sources_dir / "Jones_2019.pdf").write_bytes(b"%PDF-1.4\n")
    runtime = Runtime(projects_root=root)

    _project_view(runtime, project)
    _await_scan(runtime, project.project_id)
    _project_view(runtime, project)  # calculate all four pair scores once
    apply_source_pdf_matches(project.coding_sheet_path, {"smith.pdf": "Smith_2020.pdf"})

    with patch("meta_coder.pdf_matching.score_pair", side_effect=AssertionError("should use pair cache")):
        _project_view(runtime, project)
