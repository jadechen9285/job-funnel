import html
import re

EMPTY_DESCRIPTION = "No description provided."

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def clean_html(raw_html: str | None) -> str:
    """Converts a (possibly entity-escaped) HTML job description into plain text.

    Some ATS APIs (e.g. Greenhouse) return HTML whose tags are themselves escaped
    (`&lt;p&gt;`), so we must unescape BEFORE stripping tags.
    """
    if not raw_html:
        return EMPTY_DESCRIPTION
    text = html.unescape(raw_html)
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)  # second pass for double-encoded entities like &amp;nbsp;
    text = " ".join(text.split())
    return text or EMPTY_DESCRIPTION
