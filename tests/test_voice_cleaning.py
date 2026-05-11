import pytest
from tray.voice import clean_for_speech

def test_clean_for_speech_empty_or_none():
    assert clean_for_speech("") == ""
    assert clean_for_speech(None) == ""

def test_clean_for_speech_code_fences():
    text = "Hello\n```python\nprint('world')\n```\nHow are you?"
    expected = "Hello How are you?"
    assert clean_for_speech(text) == expected

def test_clean_for_speech_inline_code():
    text = "This is some `inline code` right here."
    expected = "This is some inline code right here."
    assert clean_for_speech(text) == expected

def test_clean_for_speech_headings():
    text1 = "# Heading 1\nSome text"
    text2 = "## Heading 2\nMore text"
    text3 = "###### Heading 6\nSmall text"
    assert clean_for_speech(text1) == "Heading 1 Some text"
    assert clean_for_speech(text2) == "Heading 2 More text"
    assert clean_for_speech(text3) == "Heading 6 Small text"

def test_clean_for_speech_bold_and_italic():
    text = "This is *italic*, **bold**, and ***bold italic***."
    expected = "This is italic, bold, and bold italic."
    assert clean_for_speech(text) == expected

    text2 = "This is _italic_, __bold__, and ___bold italic___."
    expected2 = "This is italic, bold, and bold italic."
    assert clean_for_speech(text2) == expected2

def test_clean_for_speech_markdown_links():
    text = "Check out [this link](https://example.com)!"
    expected = "Check out this link!"
    assert clean_for_speech(text) == expected

def test_clean_for_speech_markdown_images():
    text = "Here is an image: ![alt text](https://example.com/image.png)"
    expected = "Here is an image: !alt text"
    assert clean_for_speech(text) == expected

def test_clean_for_speech_brackets_and_braces():
    text = "This is a {JSON} object and an [array]."
    expected = "This is a JSON object and an array."
    assert clean_for_speech(text) == expected

def test_clean_for_speech_excessive_quotes():
    text = 'He said """hello""" and then \'\'\'goodbye\'\'\'.'
    expected = "He said hello and then goodbye."
    assert clean_for_speech(text) == expected

def test_clean_for_speech_collapsing_spaces_and_newlines():
    text = "This   has \n\n too   many \t spaces."
    expected = "This has too many spaces."
    assert clean_for_speech(text) == expected

def test_clean_for_speech_leftover_punctuation():
    text = "Wait , stop . Please."
    expected = "Wait, stop. Please."
    assert clean_for_speech(text) == expected

def test_clean_for_speech_truncation_with_sentence_boundary():
    # Create text longer than 500 characters
    part1 = "A" * 400 + ". "
    part2 = "B" * 150
    text = part1 + part2

    assert len(text) > 500
    cleaned = clean_for_speech(text)

    # It should cut off right after the period which is at index 400
    assert cleaned == "A" * 400 + "."

def test_clean_for_speech_truncation_without_sentence_boundary():
    # Create text longer than 500 chars with no period near the end
    text = "A" * 550
    cleaned = clean_for_speech(text)

    assert len(cleaned) == 503  # 500 chars + "..."
    assert cleaned.endswith("...")
    assert cleaned.startswith("A" * 500)
