"""Seed validation rules. No database needed."""

import shutil
import textwrap

import pytest

from _env import FIELD_SEEDS_DIR
from seed_fields import SeedError, load_seed

OTHER_HEADER = """
area: other
kinds:
  - {id: general, label: "Something else", template: other}
"""


def _seed_dir(tmp_path, other_yaml: str):
    """The real seed files, with other.yaml replaced by the given content."""
    for path in FIELD_SEEDS_DIR.glob("*.yaml"):
        shutil.copy(path, tmp_path / path.name)
    (tmp_path / "other.yaml").write_text(OTHER_HEADER + textwrap.dedent(other_yaml), encoding="utf-8")
    return tmp_path


def test_real_seed_is_valid():
    seed = load_seed()
    assert {a["id"] for a in seed.areas if a["is_launch"]} == {"beauty", "fashion", "gadgets", "food"}


def test_adding_a_field_works(tmp_path):
    d = _seed_dir(tmp_path, """
        fields:
          - key: colour
            label: Colour
            type: single
            role: soft
            options:
              - {value: red, label: Red, synonyms: [crimson]}
        assign:
          - {field: price_paid, step: details}
          - {field: colour, step: details}
    """)
    seed = load_seed(d)
    assert seed.fields["colour"]["facetable"] is True


@pytest.mark.parametrize("field_yaml, expected", [
    ("""
     - key: colour
       label: Colour
       type: single
       role: soft
       options:
         - {value: red, label: Red}
         - {value: red, label: Also red}
     """, "duplicate value"),
    ("""
     - key: colour
       label: Colour
       type: single
       role: soft
       options:
         - {value: red, label: Red, synonyms: [crimson]}
         - {value: maroon, label: Maroon, synonyms: [crimson]}
     """, "used by both"),
    ("""
     - key: colour
       label: Colour
       type: single
       role: soft
       options:
         - {value: red, label: Red, synonyms: [maroon]}
         - {value: maroon, label: Maroon}
     """, "equals another option's value"),
    ("""
     - key: colour
       label: Colour
       type: single
       role: hard
       facet: false
       options:
         - {value: red, label: Red}
     """, "must be facetable"),
    ("""
     - key: colour
       label: Colour
       type: single
       role: hard
       personal: true
       options:
         - {value: red, label: Red}
     """, "personal fields"),
    ("""
     - key: colour
       label: Colour
       type: single
       role: soft
     """, "need options"),
    ("""
     - key: colour
       label: Colour
       type: int
       role: soft
       min: 10
       max: 1
     """, "min is greater than max"),
    ("""
     - key: colour
       label: Colour
       type: text
       role: text
       options:
         - {value: red, label: Red}
     """, "cannot have options"),
    ("""
     - key: price_paid
       label: Again
       type: money
       role: hard
     """, "already defined"),
])
def test_bad_fields_are_rejected(tmp_path, field_yaml, expected):
    body = "fields:\n" + textwrap.indent(textwrap.dedent(field_yaml).strip("\n"), "  ") + """
assign:
  - {field: price_paid, step: details}
  - {field: colour, step: details}
"""
    d = _seed_dir(tmp_path, body)
    with pytest.raises(SeedError, match=expected):
        load_seed(d)


@pytest.mark.parametrize("assign_line, expected", [
    ("{field: no_such_field, step: details}", "unknown field"),
    ("{field: rating, step: somewhere}", "step must be one of"),
    ("{field: rating, step: details, kinds: [nope]}", "unknown kind"),
    ("{field: rating, step: details, template: place, kinds: [general]}", "is a other, not a place"),
    ("{field: price_paid, step: verdict}", "assigned twice"),
    ("{field: rating, step: details, config: {price_bands: [500, 300]}}", "price_bands"),
])
def test_bad_assignments_are_rejected(tmp_path, assign_line, expected):
    d = _seed_dir(tmp_path, f"""
        assign:
          - {{field: price_paid, step: details}}
          - {assign_line}
    """)
    with pytest.raises(SeedError, match=expected):
        load_seed(d)


def test_unassigned_field_is_rejected(tmp_path):
    d = _seed_dir(tmp_path, """
        fields:
          - {key: orphan, label: Orphan, type: text, role: text}
        assign:
          - {field: price_paid, step: details}
    """)
    with pytest.raises(SeedError, match="never assigned"):
        load_seed(d)


def test_option_scoped_to_unknown_kind_is_rejected(tmp_path):
    d = _seed_dir(tmp_path, """
        fields:
          - key: colour
            label: Colour
            type: single
            role: soft
            options:
              - {value: red, label: Red, kinds: [nope]}
        assign:
          - {field: price_paid, step: details}
          - {field: colour, step: details}
    """)
    with pytest.raises(SeedError, match="unknown kind 'nope'"):
        load_seed(d)
