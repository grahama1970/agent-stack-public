import copy
import json
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from validate_action_catalog import validate, eligible_action_ids

def test_catalog_is_valid_and_deterministic():
    path=Path(__file__).parents[1]/'fixtures/action_catalog.json'
    first=validate(path)
    second=validate(path)
    assert first == second
    data=json.loads(path.read_text())
    assert set(data) == {'schema', 'catalog_id', 'version', 'root_id', 'nodes'}
    assert len(data['nodes']) == 34
    assert len([n for n in data['nodes'] if n['kind']=='action']) == 30
    assert sum(n['kind'] == 'category' for n in data['nodes']) == 3
    assert data['nodes'][0]['id'] == data['root_id']


def test_catalog_rejects_same_team_duplicate_name_and_unknown_outcome(tmp_path):
    path=Path(__file__).parents[1]/'fixtures/action_catalog.json'
    data=json.loads(path.read_text())
    duplicate=copy.deepcopy(data)
    duplicate['nodes'][-1]['name'] = duplicate['nodes'][0]['name']
    bad=tmp_path/'duplicate.json'; bad.write_text(json.dumps(duplicate))
    (tmp_path/'classification_codes.json').write_text((path.parent/'classification_codes.json').read_text())
    with pytest.raises(AssertionError): validate(bad)

    unknown=copy.deepcopy(data)
    unknown['nodes'][2]['outcome_codes'] = ['NOT_A_BATTLE_CODE']
    bad=tmp_path/'unknown.json'; bad.write_text(json.dumps(unknown))
    with pytest.raises(AssertionError): validate(bad)


def test_catalog_rejects_root_segment_in_top_level_category_path(tmp_path):
    path=Path(__file__).parents[1]/'fixtures/action_catalog.json'
    data=json.loads(path.read_text())
    category=next(n for n in data['nodes'] if n['kind'] == 'category')
    category['family_path'] = ['root', category['id'].split('.')[-1]]
    bad=tmp_path/'root-segment.json'; bad.write_text(json.dumps(data))
    (tmp_path/'classification_codes.json').write_text((path.parent/'classification_codes.json').read_text())
    with pytest.raises(AssertionError): validate(bad)


def test_catalog_rejects_missing_category_segment_in_descendant_path(tmp_path):
    path=Path(__file__).parents[1]/'fixtures/action_catalog.json'
    data=json.loads(path.read_text())
    action=next(n for n in data['nodes'] if n['kind'] == 'action')
    action['family_path'] = [action['id'].split('.')[-1]]
    bad=tmp_path/'missing-category-segment.json'; bad.write_text(json.dumps(data))
    (tmp_path/'classification_codes.json').write_text((path.parent/'classification_codes.json').read_text())
    with pytest.raises(AssertionError): validate(bad)


def test_projection_excludes_root_and_categories():
    data=json.loads((Path(__file__).parents[1]/'fixtures/action_catalog.json').read_text())
    ids=eligible_action_ids(data)
    assert ids == sorted(n['id'] for n in data['nodes'] if n['kind']=='action')
    assert not set(ids) & {n['id'] for n in data['nodes'] if n['kind'] != 'action'}


def test_catalog_rejects_unbounded_leaf(tmp_path):
    path=Path(__file__).parents[1]/'fixtures/action_catalog.json'; data=json.loads(path.read_text())
    del next(n for n in data['nodes'] if n['kind']=='action')['arguments']['properties']['target_id']['maxLength']
    bad=tmp_path/'unbounded.json'; bad.write_text(json.dumps(data)); (tmp_path/'classification_codes.json').write_text((path.parent/'classification_codes.json').read_text())
    with pytest.raises(AssertionError): validate(bad)


def test_catalog_actions_are_typed_tau_judge_bound_and_graph_is_closed():
    data=json.loads((Path(__file__).parents[1]/'fixtures/action_catalog.json').read_text())
    actions=[n for n in data['nodes'] if n['kind']=='action']
    assert all(n['arguments']['type'] == 'object' for n in actions)
    assert all(n['evidence']['required'] and n['runtime']['executor'] == 'tau' for n in actions)
    assert all(n['judge'] == {'replay_required': True, 'authority': 'judge'} for n in actions)
    ids={n['id'] for n in data['nodes']}
    assert all(n['parent_id'] in ids or n['parent_id'] is None for n in data['nodes'])
