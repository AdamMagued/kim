"""Regression guards for GeminiProvider._convert_schema_json.

Each test asserts REAL current behavior observed in HEAD so that future
refactors cannot silently break the JSON Schema → Gemini Schema translation.
"""

import pytest
from orchestrator.providers.gemini import GeminiProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return GeminiProvider({"api_key": "test-key"})


# ---------------------------------------------------------------------------
# 1. $ref fallback
# ---------------------------------------------------------------------------


def test_ref_falls_back_to_object(provider):
    """Unresolvable $ref must silently collapse to OBJECT."""
    result = provider._convert_schema_json({"$ref": "#/definitions/Foo"})
    assert result == {"type": "OBJECT"}


def test_ref_falls_back_to_object_with_description(provider):
    """$ref fallback preserves a sibling description field."""
    result = provider._convert_schema_json(
        {"$ref": "#/definitions/Foo", "description": "A foo value"}
    )
    assert result == {"type": "OBJECT", "description": "A foo value"}


def test_ref_does_not_include_extra_keys(provider):
    """$ref output must not leak other sibling keys (e.g. 'title')."""
    result = provider._convert_schema_json({"$ref": "#/x", "title": "X"})
    assert "title" not in result
    assert result["type"] == "OBJECT"


# ---------------------------------------------------------------------------
# 2. anyOf T | null → single type + nullable
# ---------------------------------------------------------------------------


def test_anyof_t_or_null_becomes_nullable(provider):
    """anyOf [string, null] must collapse to STRING with nullable=True."""
    result = provider._convert_schema_json(
        {"anyOf": [{"type": "string"}, {"type": "null"}]}
    )
    assert result.get("type") == "STRING"
    assert result.get("nullable") is True
    assert "anyOf" not in result


def test_anyof_null_or_t_order_independent(provider):
    """null entry may appear first; result is still STRING + nullable."""
    result = provider._convert_schema_json(
        {"anyOf": [{"type": "null"}, {"type": "integer"}]}
    )
    assert result.get("type") == "INTEGER"
    assert result.get("nullable") is True


def test_anyof_single_non_null_no_nullable_flag(provider):
    """anyOf with one non-null entry and no null → no nullable key emitted."""
    result = provider._convert_schema_json({"anyOf": [{"type": "boolean"}]})
    assert result.get("type") == "BOOLEAN"
    assert "nullable" not in result


def test_anyof_description_propagated(provider):
    """Top-level description is copied into the collapsed anyOf result."""
    result = provider._convert_schema_json(
        {
            "description": "An optional name",
            "anyOf": [{"type": "string"}, {"type": "null"}],
        }
    )
    assert result.get("description") == "An optional name"
    assert result.get("nullable") is True


# ---------------------------------------------------------------------------
# 3. type list with null / multi-real anyOf
# ---------------------------------------------------------------------------


def test_type_list_with_null_becomes_nullable(provider):
    """{'type': ['string', 'null']} → STRING + nullable=True."""
    result = provider._convert_schema_json({"type": ["string", "null"]})
    assert result.get("type") == "STRING"
    assert result.get("nullable") is True


def test_type_list_null_first(provider):
    """Null may appear first in the list; string is still selected."""
    result = provider._convert_schema_json({"type": ["null", "string"]})
    assert result.get("type") == "STRING"
    assert result.get("nullable") is True


def test_multi_real_anyof_emits_gemini_anyof(provider):
    """anyOf with two non-null real types must emit a Gemini anyOf list."""
    result = provider._convert_schema_json(
        {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    )
    assert "anyOf" in result
    types = [s["type"] for s in result["anyOf"]]
    assert "STRING" in types
    assert "INTEGER" in types
    assert "nullable" not in result


def test_multi_real_anyof_with_null_is_nullable(provider):
    """anyOf with two real types + null → anyOf list + nullable=True."""
    result = provider._convert_schema_json(
        {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    )
    assert "anyOf" in result
    assert result.get("nullable") is True
    # null entry must not appear inside the inner anyOf list
    inner_types = [s.get("type") for s in result["anyOf"]]
    assert "null" not in inner_types
    assert None not in inner_types  # no unconverted entries


# ---------------------------------------------------------------------------
# 4. allOf merges properties/required (deduped); enum/format passthrough
# ---------------------------------------------------------------------------


def test_allof_merges_properties(provider):
    """allOf sub-schemas have their properties merged into one OBJECT."""
    schema = {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ]
    }
    result = provider._convert_schema_json(schema)
    assert result["type"] == "OBJECT"
    assert "a" in result["properties"]
    assert "b" in result["properties"]
    assert result["properties"]["a"]["type"] == "STRING"
    assert result["properties"]["b"]["type"] == "INTEGER"


def test_allof_required_deduped(provider):
    """required fields from all allOf sub-schemas are merged and deduped."""
    schema = {
        "allOf": [
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            {
                "type": "object",
                "properties": {"y": {"type": "string"}},
                "required": ["x", "y"],
            },
        ]
    }
    result = provider._convert_schema_json(schema)
    assert set(result["required"]) == {"x", "y"}
    # 'x' must not appear twice
    assert result["required"].count("x") == 1


def test_allof_description_propagated(provider):
    """Top-level description on allOf is preserved in the merged output."""
    schema = {
        "description": "merged object",
        "allOf": [{"type": "object", "properties": {"z": {"type": "boolean"}}}],
    }
    result = provider._convert_schema_json(schema)
    assert result.get("description") == "merged object"


def test_enum_carried_through_on_string(provider):
    """enum values are stringified and included in the Gemini output."""
    result = provider._convert_schema_json(
        {"type": "string", "enum": ["alpha", "beta", "gamma"]}
    )
    assert result.get("type") == "STRING"
    assert result.get("enum") == ["alpha", "beta", "gamma"]


def test_enum_values_stringified(provider):
    """enum values that are not strings must be cast to str."""
    result = provider._convert_schema_json({"type": "integer", "enum": [1, 2, 3]})
    assert result.get("enum") == ["1", "2", "3"]


def test_format_carried_through(provider):
    """format is passed straight through to the Gemini schema."""
    result = provider._convert_schema_json({"type": "string", "format": "date-time"})
    assert result.get("type") == "STRING"
    assert result.get("format") == "date-time"
