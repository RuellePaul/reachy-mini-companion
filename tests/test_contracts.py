"""The contracts themselves: one definition, examples kept in sync with the dashboard's copy."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, get_args, get_origin

import pytest
from pydantic import TypeAdapter, ValidationError

from mac_server import contracts as c
from robot_agent import animation_names as anim
from scripts import contract_examples
from scripts.export_contract_examples import OUT, render


def test_examples_file_is_current() -> None:
    assert json.loads(OUT.read_text(encoding='utf-8')) == json.loads(render()), (
        'run scripts/export_contract_examples.py'
    )


@pytest.mark.parametrize(
    ('name', 'model'),
    [
        ('SettingsView', c.SettingsView),
        ('RobotEvent', c.RobotEvent),
        ('SessionSnapshot', c.SessionSnapshot),
        ('DashboardState', c.DashboardState),
        ('Volume', c.Volume),
        ('WsMessage', c.WsMessage),
    ],
)
def test_examples_validate(name: str, model: object) -> None:
    adapter = TypeAdapter(model)
    for example in json.loads(render())[name]:
        assert adapter.dump_python(adapter.validate_python(example), mode='json') == example


@pytest.mark.parametrize(('name', 'key'), [('RobotEvent', 'kind'), ('WsMessage', 'channel')])
def test_every_member_of_a_union_has_an_example(name: str, key: str) -> None:
    members = get_args(get_args(getattr(c, name))[0])  # Annotated[A | B | ..., Field(discriminator=key)]
    values = {get_args(member.model_fields[key].annotation)[0] for member in members}
    assert {example[key] for example in contract_examples.examples()[name]} == values


def _literals(annotation: object) -> set[str]:
    if get_origin(annotation) is Literal:
        return set(get_args(annotation))
    return set().union(*(_literals(arg) for arg in get_args(annotation)))


def test_the_values_the_event_table_lists_are_the_contracts() -> None:
    """docs/CONTRACTS.md's robot events: a field written `state` (on · off) or `reason`: a · b lists its Literal."""
    doc = (Path(__file__).resolve().parents[1] / 'docs' / 'CONTRACTS.md').read_text(encoding='utf-8')
    table = doc.split('\n## Robot events', 1)[1].split('\n## ', 1)[0]
    models = {get_args(m.model_fields['kind'].annotation)[0]: m for m in get_args(get_args(c.RobotEvent)[0])}
    listed: dict[tuple[str, str], set[str]] = {}
    for row in (r.split('|') for r in table.splitlines() if r.startswith('| `')):
        for field, inside, after in re.findall(r'`(\w+)`(?: \(([^)]*·[^)]*)\)|: ([\w ·]*·[\w ·]*))', row[2]):
            listed |= {
                (kind, field): {v.strip() for v in (inside or after).split('·')}
                for kind in re.findall(r'`(\w+)`', row[1])
            }
    assert {('session_end', 'reason'), ('manette', 'state'), ('manette', 'why'), ('scene', 'state')} <= set(listed)
    assert listed == {(kind, field): _literals(models[kind].model_fields[field].annotation) for kind, field in listed}


def test_patch_carries_every_setting_and_no_rule() -> None:
    assert set(c.SettingsPatch.model_fields) == set(c.Settings.model_fields)
    for field in c.SettingsPatch.model_fields.values():
        assert field.default is None
        assert not field.metadata


def test_default_name_stays_default() -> None:
    merged = c.merge_settings(c.Settings(), c.SettingsPatch(name='Default'))
    assert merged.name == 'Default'
    assert c.Settings.model_validate(merged.model_dump()).name == 'Default'


@pytest.mark.parametrize(
    'patch',
    [
        {'voice': 'Nobody'},
        {'idle_animation': 'expressive'},
        {'talking_animation': 'calm2'},
        {'idle_sleep_minutes': -1},
        {'idle_sleep_minutes': 61},
        {'name': '   '},
        {'name': 'x' * 41},
        {'personality': 'x' * 1501},
    ],
)
def test_rules_apply_through_a_patch(patch: dict) -> None:
    with pytest.raises(ValidationError):
        c.merge_settings(c.Settings(), c.SettingsPatch(**patch))


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        c.SettingsPatch.model_validate({'temperature': 0.2})
    with pytest.raises(ValidationError):
        TypeAdapter(c.RobotEvent).validate_python(
            {'kind': 'tool_call', 'session_id': 's', 'call_id': 'c', 'name': 'x', 'extra': 1}
        )


def test_animation_names_are_consistent() -> None:
    assert set(anim.VARIANTS) == set(anim.MODES) == set(anim.DEFAULTS)
    for mode in anim.MODES:
        assert 3 <= len(anim.VARIANTS[mode]) <= 8
        assert anim.DEFAULTS[mode] in anim.VARIANTS[mode]
