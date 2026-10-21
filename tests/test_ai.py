import pytest

from app.ai.drafter import DraftText, extract_json


def test_extract_json_with_preface():
    raw = 'Character count check: P1: 180 chars\n{"title":"A title","body":"A body"}'

    assert DraftText.model_validate_json(extract_json(raw)) == DraftText(
        title="A title", body="A body")


def test_extract_json_with_markdown_fence():
    raw = '```json\n{"title":"A title","body":"A body"}\n```'

    assert extract_json(raw) == '{"title":"A title","body":"A body"}'


def test_extract_json_requires_an_object():
    with pytest.raises(ValueError, match="JSON object"):
        extract_json("Character count check: no JSON returned")
