"""utility facade: file type sniffing + pdf/html -> markdown helpers.

exports small focused pieces so callers can:
* detect bytes (pdf vs utf-8 vs unsupported)
* turn pdf bytes into normalized html then markdown
* extract links from html
* promote existing html/bytes to markdown directly (to_markdown)

keep surface tiny + predictable.
"""

from .pdf import pdf_to_html
from .markdown import html_to_markdown
from .html import extract_links
from .urls import fix_url

class ByteType:
    name: str | None
    supported: bool

    def __init__(self, name: str | None, supported: bool):
        self.name = name
        self.supported = supported


def get_type(bytes_: bytes):
    """basic sniff: return a ByteType describing support.

    recognizes pdf magic header and utf-8 decodable text; else unsupported.

    :param bytes_: the byte content to analyze
    """
    if bytes_.startswith(b"%PDF-"):
        return ByteType("pdf", True)
    try:
        bytes_.decode("utf-8")
        return ByteType("utf-8", True)
    except:
        pass
    return ByteType(None, False)


def to_markdown(content: str | bytes, base_url: str | None = None) -> str:
    """promote bytes or html string to markdown.

    pdf bytes -> html via pdf_to_html -> markdown
    utf-8 string -> markdown directly
    raises when bytes type unsupported.

    :param content: raw supported bytes or html string
    :param base_url: for relative link resolution.
    :return: markdown
    :rtype: str
    """
    if isinstance(content, bytes):
        if not get_type(content).supported:
            raise NotImplementedError("file type not supported")
        html = pdf_to_html(content, base_url or "")
    else:
        html = content
    return html_to_markdown(html, base_url)

__all__ = [
    "pdf_to_html",
    "html_to_markdown",
    "extract_links",
    "ByteType",
    "get_type",
    "to_markdown",
    "fix_url",
]