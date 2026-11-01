import pytest

from job_funnel.text import EMPTY_DESCRIPTION, clean_html


def test_entity_escaped_html_is_fully_stripped():
    raw = "&lt;div class=&quot;x&quot;&gt;&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;&lt;/div&gt;"
    assert clean_html(raw) == "Hello world"


def test_plain_html_is_stripped():
    assert clean_html("<p>Hello</p><ul><li>one</li><li>two</li></ul>") == "Hello one two"


def test_double_encoded_entities_are_decoded():
    assert clean_html("Tom &amp;amp; Jerry&amp;nbsp;rock") == "Tom & Jerry rock"


def test_script_and_style_content_removed():
    raw = "<style>.a{color:red}</style><p>Keep</p><script>alert(1)</script>"
    assert clean_html(raw) == "Keep"


def test_whitespace_collapsed():
    assert clean_html("<p>a\n\n   b\t c</p>") == "a b c"


@pytest.mark.parametrize("raw", [None, "", "<p> </p>"])
def test_empty_input_returns_placeholder(raw):
    assert clean_html(raw) == EMPTY_DESCRIPTION
