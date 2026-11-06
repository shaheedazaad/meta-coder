"""Render production templates into isolated DOM-test fixtures (no HTTP server)."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from meta_coder import web
from meta_coder.app_settings import AppSettings, save_app_settings
from meta_coder.manual import parse_coding_manual
from meta_coder.projects import create_project, write_manual


def render():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'META_CODER_HOME': directory}):
        root = Path(directory) / 'projects'
        project = create_project('DOM test project', root=root)
        write_manual(project, parse_coding_manual('effect_definition: Treatment versus control\neffects:\n  estimate: {type: number}\n  group:\n    type: string\n    levels:\n      - {value: treatment}\n      - {value: control}\n'))
        save_app_settings(AppSettings(manual_generator_provider='openai_compatible', manual_generator_model='local', openai_base_url='http://localhost:8000/v1'))
        with patch.object(web.credentials, 'saved_key_configured', return_value=False), patch.object(web.credentials, 'keyring_available', return_value=False):
            app = web.create_app(token='test', projects_root=root)
            with TestClient(app, base_url='http://localhost') as client:
                pages = {'project': f'/test/projects/{project.project_id}', 'home': '/test/', 'settings': '/test/settings'}
                fixtures = {name: {'url': 'http://localhost' + path, 'html': client.get(path).text} for name, path in pages.items()}
                rejected = client.post(pages['project'] + '/manual', data={'manual_json': json.dumps({'name': 'rejected', 'effect_definition': '', 'effects': []})})
                assert rejected.status_code == 400
                fixtures['rejected'] = {'url': fixtures['project']['url'], 'html': rejected.text}
                return fixtures


if __name__ == '__main__':
    print(json.dumps(render()))
