"""Malformed manuals and model responses must fail with actionable diagnostics."""
import pytest
import yaml

from meta_coder import manual, mechanism


@pytest.mark.parametrize(('effects', 'message'), [
    (None, 'at least one field'), ({}, 'at least one field'), ([], 'mapping'),
    ({'': {}}, 'must be named'), ({'x': []}, 'must be a mapping'),
    ({'x': {'type': 'array'}}, 'not supported'),
    ({'x': {'required': 'yes'}}, 'required must be true or false'),
    ({'x': {'evidence_required': 'yes'}}, 'evidence_required must be true or false'),
    ({'x': {'levels': 'a'}}, 'must be a list'),
    ({'x': {'levels': ['a']}}, 'mapping with a `value`'),
    ({'x': {'levels': [{'value': ' '}]}}, 'cannot be empty'),
    ({'x': {'levels': [{'value': 'a'}, {'value': 'a'}]}}, 'duplicate value'),
    ({'x': {'type': 'number', 'levels': [{'value': 'a'}]}}, 'type is not `string`'),
])
def test_invalid_manual_fields(effects, message):
    with pytest.raises(manual.ManualError, match=message):
        manual.parse_coding_manual(yaml.safe_dump({'effect_definition': 'comparison', 'effects': effects}))


@pytest.mark.parametrize('text', ['[a, b]', 'word', '42'])
def test_manual_requires_top_level_mapping(text):
    with pytest.raises(manual.ManualError, match='top-level mapping'):
        manual.parse_coding_manual(text)


def test_yaml_diagnostics_include_location():
    with pytest.raises(manual.ManualError, match='Invalid YAML at line 1, column'):
        manual.parse_coding_manual('bad: [')


def test_yaml_error_without_location(monkeypatch):
    def fail(_text, Loader):
        raise yaml.YAMLError('parser failure')
    monkeypatch.setattr(manual.yaml, 'load', fail)
    with pytest.raises(manual.ManualError, match='Invalid YAML: The YAML could not be parsed'):
        manual.parse_coding_manual('text')


def test_read_manual_and_roundtrip_descriptions(tmp_path):
    path = tmp_path / 'manual.yml'
    with pytest.raises(FileNotFoundError, match='Coding manual not found'):
        manual.read_coding_manual(path)
    path.write_text('description: A study\neffect_definition: comparison\neffects:\n  group:\n    levels:\n      - value: treatment\n      - value: control\n        description: baseline\n')
    parsed = manual.read_coding_manual(path)
    assert parsed.is_complete and parsed.effects['group'].is_categorical
    assert not parsed.effects['notes'].is_categorical
    roundtrip = manual.parse_coding_manual(manual.manual_to_yaml_text(parsed))
    assert roundtrip.description == 'A study'
    assert roundtrip.effects == parsed.effects


@pytest.mark.parametrize(('payload', 'message'), [
    ([], 'Invalid manual data'),
    ({'effects': 'bad'}, 'must be a list'),
    ({'effects': ['bad']}, 'must be an object'),
    ({'effects': [{}]}, 'missing a field name'),
    ({'effects': [{'name': 'a'}, {'name': ' a '}]}, 'duplicate field name'),
])
def test_invalid_editor_payload(payload, message):
    with pytest.raises(manual.ManualError, match=message):
        manual.manual_from_editor_payload(payload)


@pytest.mark.parametrize(('parsed', 'message'), [
    ([], 'not a JSON object'), ({}, 'missing an `effects` array'),
    ({'effects': [None]}, 'non-empty string `row_id`'),
    ({'effects': [{'row_id': ' '}]}, 'non-empty string `row_id`'),
    ({'effects': [{'row_id': 1}]}, 'non-empty string `row_id`'),
])
def test_invalid_response_envelope(parsed, message):
    result = mechanism.validate_response(parsed, {'r1'})
    assert result.needs_review and message in result.error


@pytest.mark.parametrize(('kind', 'value', 'valid'), [
    ('string', 'text', True), ('string', 1, False),
    ('number', 1.5, True), ('number', 1, True), ('number', True, False), ('number', '1', False),
    ('integer', 1, True), ('integer', 1.5, False), ('integer', True, False),
    ('boolean', True, True), ('boolean', False, True), ('boolean', 0, False), ('boolean', 'true', False),
    ('integer', None, True),
    ('number', float('nan'), False), ('number', float('inf'), False), ('number', float('-inf'), False),
    ('number', 10 ** 400, True),
])
def test_response_values_enforce_manual_types(kind, value, valid):
    field = {'value': value, 'evidence': 'p. 1'}
    if value is None:
        field['missing'] = 'not_reported'
    parsed = {'effects': [{'row_id': 'r1', 'field': field}]}
    result = mechanism.validate_response(parsed, {'r1'}, {'field': manual.FieldSpec(type=kind)})
    assert result.ok is valid
    if not valid:
        assert 'invalid field value' in result.error


def test_parsed_nonfinite_json_numbers_are_rejected():
    from meta_coder.extraction import parse_json_response
    for literal in ('NaN', 'Infinity', '-Infinity'):
        parsed, _ = parse_json_response('{"effects": [{"row_id": "r1", "x": {"value": %s}}]}' % literal)
        result = mechanism.validate_response(parsed, {'r1'}, {'x': manual.FieldSpec(type='number', evidence_required=False)})
        assert not result.ok and 'invalid field value' in result.error


@pytest.mark.parametrize('returned', [' r1 ', 'r1 ', 'R1'])
def test_row_ids_must_match_exactly_without_normalisation(returned):
    result = mechanism.validate_response({'effects': [{'row_id': returned}]}, {'r1'})
    assert not result.ok
    assert result.missing_ids == {'r1'} and result.extra_ids == {returned}


def test_numeric_row_id_never_matches_string_request():
    result = mechanism.validate_response({'effects': [{'row_id': 1}]}, {'1'})
    assert not result.ok and 'string `row_id`' in result.error


@pytest.mark.parametrize('field', [{}, {'value': 1}, {'value': 1, 'evidence': 42}, {'value': 1, 'evidence': ' '}])
def test_values_and_nonempty_evidence_are_required(field):
    result = mechanism.validate_response({'effects': [{'row_id': 'r1', 'x': field}]}, {'r1'}, {'x': manual.FieldSpec(type='number')})
    assert not result.ok and 'invalid field value' in result.error


def test_legacy_field_name_set_still_requires_value():
    result = mechanism.validate_response({'effects': [{'row_id': 'r1', 'x': {}}]}, {'r1'}, {'x'})
    assert not result.ok
    result = mechanism.validate_response({'effects': [{'row_id': 'r1', 'x': {'value': 'ok'}}]}, {'r1'}, {'x'})
    assert result.ok


def test_schema_rejects_unsupported_dialect_and_empty_manual():
    empty = manual.CodingManual('empty', None, 'comparison', {})
    with pytest.raises(ValueError, match='Unknown schema dialect'):
        mechanism.build_response_schema(empty, dialect='xml')
    with pytest.raises(ValueError, match='at least one effect field'):
        mechanism.build_response_schema(empty)


def test_optional_manual_sections_allow_absent_or_empty_mappings():
    assert manual._parse_section(None, 'optional', allow_empty=True) == {}
    assert manual._parse_section({}, 'optional', allow_empty=True) == {}
    with pytest.raises(manual.ManualError, match='mapping'):
        manual._parse_section('invalid', 'optional', allow_empty=True)
