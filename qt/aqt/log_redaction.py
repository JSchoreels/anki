# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Best-effort redaction for Anki-managed Python logs."""

import re

_MASK = "<redacted>"

# Match JSON, Python repr, query strings and ordinary key=value diagnostics.
# Quoted values can contain spaces, delimiters and escaped quotes.
_KEY = r"""(?:
        pass(?:word|wd)|pwd|token|secret
        |(?:access|refresh|auth|api|id)[_-]?token|(?:client|api)[_-]?secret
        |(?:x[_-]?)?api[_-]?key|sync[_-]?key|hkey
        |(?:proxy-)?authorization
    )"""
_CREDENTIAL = re.compile(
    rf"""(?ix)
    (?P<label>
        (?P<query>[?&]){_KEY}=
        |--{_KEY}[ \t]+
        |(?<![^\W_]){_KEY}['"]?[ \t]*[:=][ \t]*
    )
    (?:
        (?P<quote>['"])(?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])*(?:(?P=quote)|(?=\r?$))
        |(?(query)[^&#\s]*|[^\r\n]+)
    )
    """,
    re.MULTILINE,
)
_URL_LOGIN = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^/\s?#@]+@")
_BEARER = re.compile(r"(?i)(\bbearer[ \t]+)[a-z0-9._~+/=-]+")
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_EMAIL = re.compile(r"(?<![\w.%+'-])[\w.%+'-]+(?:@|%40)[\w-]+(?:\.[\w-]+)+", re.I)


def redact_log_text(text: str) -> str:
    text = _URL_LOGIN.sub(r"\g<1>" + _MASK + "@", text)
    text = _CREDENTIAL.sub(r"\g<label>\g<quote>" + _MASK + r"\g<quote>", text)
    text = _BEARER.sub(r"\g<1>" + _MASK, text)
    text = _TOKEN.sub(_MASK, text)
    return _EMAIL.sub(_MASK, text)
