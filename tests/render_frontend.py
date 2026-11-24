"""Render production templates into isolated DOM-test fixtures (no HTTP server)."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from meta_coder import extractors, web
from meta_coder.app_settings import AppSettings, save_app_settings
from meta_coder.manual import parse_coding_manual
from meta_coder.extraction import ExtractionResult
from meta_coder.projects import create_project, write_manual
from meta_coder.settings import save_project_extractor_id
from meta_coder.runner import write_raw_result


def render():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'META_CODER_HOME': directory}):
        root = Path(directory) / 'projects'
        project = create_project('DOM test project', root=root)
        write_manual(project, parse_coding_manual('effect_definition: Treatment versus control\neffects:\n  estimate: {type: number}\n  group:\n    type: string\n    levels:\n      - {value: treatment}\n      - {value: control}\n'))
        local = extractors.add_extractor(extractors.validate_new_extractor(
            {'name': 'Local server', 'provider': 'openai_compatible', 'model': 'local', 'base_url': 'http://localhost:8000/v1'}))
        gemini = extractors.add_extractor(extractors.validate_new_extractor(
            {'name': 'Gemini lab', 'provider': 'gemini', 'model': 'gemini-test'}))
        extractors.set_key_saved(gemini.id, True)
        router = extractors.add_extractor(extractors.validate_new_extractor(
            {'name': 'Router', 'provider': 'openrouter', 'model': 'vendor/model', 'openrouter_provider': 'saved/tag',
             'openrouter_allow_fallbacks': 'deny'}))
        save_app_settings(AppSettings(drafting_extractor_id=local.id))
        save_project_extractor_id(project, local.id)
        coded = create_project('DOM results project', root=root)
        write_manual(coded, parse_coding_manual('effect_definition: Treatment versus control\neffects:\n  estimate: {type: number}\n'))
        sheet = ['row_id,source_pdf,locator,authors,year']
        for index, (name, status) in enumerate([('done.pdf', 'ok'), ('failed.pdf', 'error'), ('review.pdf', 'needs_review')]):
            (coded.sources_dir / name).write_bytes(b'%PDF')
            sheet.append(f'r{index},{name},exp,Smith,2024')
            write_raw_result(coded, ExtractionResult(name, status), provider='gemini', model='model')
        coded.coding_sheet_path.write_text('\n'.join(sheet) + '\n', encoding='utf-8')
        with patch.object(web.credentials, 'keyring_available', return_value=False):
            app = web.create_app(token='test', projects_root=root)
            with TestClient(app, base_url='http://localhost') as client:
                pages = {'project': f'/test/projects/{project.project_id}', 'home': '/test/', 'settings': '/test/settings',
                         'results': f'/test/projects/{coded.project_id}?tab=results&run_status=error'}
                fixtures = {name: {'url': 'http://localhost' + path, 'html': client.get(path).text} for name, path in pages.items()}
                rejected = client.post(pages['project'] + '/manual', data={'manual_json': json.dumps({'name': 'rejected', 'effect_definition': '', 'effects': []})})
                assert rejected.status_code == 400
                fixtures['rejected'] = {'url': fixtures['project']['url'], 'html': rejected.text}
                fixtures['ids'] = {'local': local.id, 'gemini': gemini.id, 'router': router.id}
                return fixtures


if __name__ == '__main__':
    print(json.dumps(render()))
