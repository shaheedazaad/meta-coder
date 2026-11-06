"""Durable, credential-free provenance for AI operations and project exports.

History lives outside output/, so replacing inputs or clearing the working results
never erases earlier requests. Each operation owns its directory; identical input
and application snapshots are stored once in content-addressed blobs.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from importlib.metadata import distributions
import io
import json
import os
from pathlib import Path
import platform
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote
from uuid import uuid4
import zipfile

from . import __version__

_CURRENT: ContextVar[Any] = ContextVar('audit_operation', default=None)
_BLOB_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def json_bytes(value):
    def convert(obj):
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, set):
            return sorted(obj)
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f'Unsupported audit value: {type(obj).__name__}')
    return (json.dumps(value, default=convert, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def application_snapshot():
    package = Path(__file__).resolve().parent
    sources = sorted([*package.glob('*.py'), *package.glob('config/*.yml'),
                      *package.glob('templates/*.html'), *package.glob('static/**/*.js'),
                      *package.glob('static/**/*.css'),
                      *package.glob('static/**/*.txt'),
                      *(p for p in (package / 'documentation').rglob('*') if p.is_file())])
    sources += [p for p in (package.parent / 'pyproject.toml', package.parent / 'pixi.lock') if p.is_file()]
    buffer = io.BytesIO()
    hashes = {}
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sources:
            name = path.relative_to(package.parent).as_posix()
            data = path.read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    metadata = {
        'app_version': __version__,
        'python_version': platform.python_version(),
        'platform': platform.platform(),
        'dependencies': {d.metadata['Name']: d.version for d in distributions() if d.metadata['Name']},
        'source_sha256': hashlib.sha256(json_bytes(hashes)).hexdigest(),
        'source_files': hashes,
    }
    return metadata, buffer.getvalue()


class AuditOperation:
    def __init__(self, project, operation, settings, *, secrets=()):
        self.root = project.path / 'audit'
        self.id = uuid4().hex
        self.path = self.root / 'operations' / self.id
        self.secrets = tuple(value for value in secrets if value)
        self.sequence = 0
        self.data = {
            'schema_version': 1, 'operation_id': self.id, 'operation': operation,
            'project_id': project.project_id, 'project_name': project.name,
            'started_at_utc': utc_now(), 'finished_at_utc': None,
            'status': 'in_progress', 'settings': settings, 'inputs': {},
            'model_version_note': 'Requested model is in settings; provider-reported version is in each exchange, or null when unavailable.',
        }
        metadata, source = application_snapshot()
        self.data['application'] = metadata
        self.data['application_source'] = self.blob(source)
        self.save()
        for name, source_path in [('coding_manual.yml', project.manual_path), ('coding_sheet.csv', project.coding_sheet_path)]:
            if source_path.is_file():
                self.input(name, source_path.read_bytes())

    def scrub(self, data):
        for secret in self.secrets:
            for value in {secret, quote(secret, safe='')}:
                data = data.replace(value.encode(), b'[REDACTED]')
        return data

    def blob(self, data):
        digest = hashlib.sha256(data).hexdigest()
        relative = f'blobs/{digest}'
        target = self.root / relative
        # Workers share immutable blobs. Avoid competing replacements on Windows.
        with _BLOB_LOCK:
            if not target.exists():
                atomic_write(target, data)
        return {'path': 'audit/' + relative, 'sha256': digest, 'bytes': len(data)}

    def input(self, name, data):
        self.data['inputs'][name] = self.blob(data)
        self.save()

    def save(self):
        atomic_write(self.path / 'operation.json', self.scrub(json_bytes(self.data)))

    def finish(self, result=None, error=None, status=None):
        self.data.update(finished_at_utc=utc_now(), status=status or ('error' if error else getattr(result, 'status', 'complete')))
        if error:
            self.data['error'] = str(error)
            self.data['error_type'] = type(error).__name__
        else:
            self.data['result'] = self.blob(self.scrub(json_bytes(result)))
        self.save()


def audited_call(project, operation, function, *, audit_inputs=None, audit_settings=None, **kwargs):
    """Run on the calling worker so nested transports share this operation only."""
    settings = {k: v for k, v in kwargs.items() if k in {
        'provider', 'model', 'timeout_sec', 'service_tier', 'reasoning_effort', 'base_url', 'response_format',
    }}
    settings.update(audit_settings or {})
    audit = AuditOperation(project, operation, settings, secrets=(kwargs.get('api_key', ''),))
    token = _CURRENT.set(audit)
    try:
        for name, data in (audit_inputs or {}).items():
            audit.input(name, data)
        if 'pdf_path' in kwargs and kwargs['pdf_path'].is_file():
            audit.input('source/' + kwargs['pdf_path'].name, kwargs['pdf_path'].read_bytes())
        for name in ('manual', 'rows', 'source', 'document_text', 'notes', 'filenames'):
            if name in kwargs:
                audit.input(name + '.json', json_bytes(kwargs[name]))
        result = function(**kwargs)
        if hasattr(result, 'audit_operation_id'):
            result.audit_operation_id = audit.id
        audit.finish(result=result)
        return result
    except BaseException as exc:
        audit.finish(error=exc)
        raise
    finally:
        _CURRENT.reset(token)


def audited_transport(transport, request, *, timeout, cancel_event):
    """Record the exact outbound body before sending and each inbound envelope.

    Authentication headers are never persisted. Response bytes are unchanged
    except for occurrences of the known API key, including echoed errors.
    """
    audit = _CURRENT.get()
    if audit is None:
        return transport(request, timeout=timeout, cancel_event=cancel_event)
    audit.sequence += 1
    path = audit.path / f'exchange-{audit.sequence:04d}.json'
    url = urlsplit(request.full_url)
    safe_url = urlunsplit((url.scheme, url.netloc, url.path, urlencode([
        (key, '[REDACTED]' if key.lower() in {'key', 'api_key', 'token', 'access_token'} else value)
        for key, value in parse_qsl(url.query, keep_blank_values=True)
    ]), ''))
    exchange = {
        'started_at_utc': utc_now(), 'finished_at_utc': None, 'status': 'in_progress',
        'request': {'method': request.get_method(), 'url': safe_url, 'timeout_sec': timeout,
                    'headers': {'Content-Type': request.get_header('Content-type')},
                    'body': audit.blob(audit.scrub(request.data or b''))},
        'response': None,
    }
    def save():
        atomic_write(path, audit.scrub(json_bytes(exchange)))
    save()  # Fail closed: never send a request whose input could not be saved.
    try:
        raw = transport(request, timeout=timeout, cancel_event=cancel_event)
    except HTTPError as exc:
        raw = exc.read()
        exchange.update(status='http_error', error=str(exc), response={
            'body': audit.blob(audit.scrub(raw)), 'http_status': exc.code,
            'headers': {key: str(value) for key, value in (exc.headers or {}).items()
                        if key.lower() in {'date', 'content-type', 'x-request-id', 'request-id', 'retry-after'}},
        })
        # HTTPError caches read methods; a fresh stream preserves downstream error handling.
        raise HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(raw)) from exc
    except BaseException as exc:
        exchange.update(status='transport_error', error=str(exc), error_type=type(exc).__name__)
        raise
    else:
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = {}
        body = body if isinstance(body, dict) else {}
        exchange.update(status='received', response={
            'body': audit.blob(audit.scrub(raw)), 'http_status': getattr(raw, 'status', None),
            'headers': getattr(raw, 'audit_headers', {}),
            'reported_model': body.get('modelVersion') or body.get('model'),
            'response_id': body.get('responseId') or body.get('id'),
            'system_fingerprint': body.get('system_fingerprint'),
            'provider': body.get('provider'), 'created': body.get('created'),
            'usage': body.get('usageMetadata') or body.get('usage'),
        })
        return raw
    finally:
        exchange['finished_at_utc'] = utc_now()
        save()


def export_manifest(project, files):
    return {
        'schema_version': 1, 'exported_at_utc': utc_now(),
        'exporting_app_version': __version__,
        'project': {'id': project.project_id, 'name': project.name, 'created_at': project.created_at},
        'files': files,
        'audit_history': 'audit/operations contains per-action manifests and per-request exchanges; blob paths are relative to the ZIP root.',
        'limitations': [
            'Runs made before audit recording was added lack full provenance; missing records cannot be reconstructed.',
            'in_progress records indicate an interrupted action or an export taken before completion.',
            'Provider model aliases, unavailable model revisions, server defaults and nondeterminism can prevent identical reruns.',
            'Authentication is excluded; known API keys are redacted if echoed in provider output.',
        ],
    }
