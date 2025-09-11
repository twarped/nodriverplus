"""markdown helpers.

turn normalized html into stable markdown with minimal surprises.
we run our own cleaning pass first (normalize_html) then lean on
html_to_markdown with a small custom style->emphasis shim so inline
bold/italic coming from style="" survive.

post-processing tightens spacing, fixes wrapped link labels, normalizes
alt text, escapes stray $ (avoid accidental math), and collapses excessive
blank lines. goal: deterministic diffable output.
"""

import re
from html import unescape
import logging
from typing import Optional

from bs4 import BeautifulSoup, Tag
from html_to_markdown import convert_to_markdown

from .html import parse_style_attribute, normalize_html

logger = logging.getLogger("nodriverplus.utils.markdown")


def _style_to_emphasis_converter(*, 
        tag: Tag | dict, 
        text: str, 
        convert_as_inline: bool | None = None
    ):
    # tag may be a bs4.Tag or a dict depending on converter caller
    style = ""
    if isinstance(tag, Tag):
        style = tag.get("style") or ""
    elif isinstance(tag, dict):
        style = tag.get("style") or ""

    if not style:
        return text

    smap = parse_style_attribute(style)
    smap = {str(k).lower(): str(v).strip().lower() for k, v in smap.items()}

    fw = smap.get("font-weight", "")
    fs = smap.get("font-style", "")
    td = smap.get("text-decoration", "")

    out = text
    if td and "line-through" in td:
        out = f"~~{out}~~"
    if fs and "italic" in fs:
        out = f"*{out}*"
    if fw and (
        "bold" in fw or "semibold" in fw or (fw.isdigit() and int(fw) >= 600)
    ):
        out = f"**{out}**"
    return out


def html_to_markdown(html: str, base_url: Optional[str] = None):
    """convert html to markdown after normalization.

    :param html: raw or already-normalized html (we still run normalize_html to be safe)
    :param base_url: propagate to normalization so relative links get absolute forms.
    :return: stable markdown string.
    :rtype: str
    """
    soup = BeautifulSoup(html or "", "html.parser")

    # normalize the html before continuing
    norm_html = normalize_html(str(soup), base_url)

    md = convert_to_markdown(
        str(BeautifulSoup(norm_html, "html.parser").body or BeautifulSoup(norm_html, "html.parser")),
        extract_metadata=True,
        heading_style="atx",
        bullets="*+-",
        wrap=False,
        custom_converters={
            "span": _style_to_emphasis_converter,
            "div": _style_to_emphasis_converter,
        },
    )

    md = unescape(md).strip()
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    md = re.sub(r"\)\s*\[", ")\n\n[", md)
    md = re.sub(r"^[ \t]*[*+-][ \t]+(#{1,6}[ \t].*)$", r"\1", md, flags=re.MULTILINE)
    md = re.sub(r"^[ \t]*[*+-][ \t]*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"(?m)^(#{1,6}\s*\d+\.)\s*(\S)", r"\1 \2", md)

    def _join_wrapped_link_label(m: re.Match[str]) -> str:
        left = re.sub(r"\s+", " ", m.group(1)).strip()
        right = re.sub(r"\s+", " ", m.group(2)).strip()
        url = m.group(3).strip()
        return f"[{left} {right}]({url})"

    md = re.sub(r"\[([^\]]*?)\n+([^\]]*?)\]\(([^)]+)\)", _join_wrapped_link_label, md)

    def _normalize_link_text(m: re.Match[str]) -> str:
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        url = m.group(2).strip()
        return f"[{text}]({url})"

    def _normalize_image_alt(m: re.Match[str]) -> str:
        alt = re.sub(r"\s+", " ", m.group(1)).strip()
        url = m.group(2).strip()
        return f"![{alt}]({url})"

    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _normalize_link_text, md, flags=re.DOTALL)
    md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _normalize_image_alt, md, flags=re.DOTALL)
    md = re.sub(r"(?<!\\)\$", r"\\$", md)

    # collapse runs of 3+ blank lines into exactly two
    md = re.sub(r"(?:[ \t]*\n){3,}", "\n\n", md)

    return md


__all__ = ["html_to_markdown", "_style_to_emphasis_converter"]
