"""pdf helpers: turn pdf bytes into simplified html ready for markdown.

flow:
1. extract per-page html via pymupdf (fitz) when available.
2. fix broken data uris (whitespace inside base64) + ensure stable page ids.
3. fallback to rasterized page <img> when text layer missing (optional).
4. consolidate fragmented positioned <p> runs into merged paragraphs while
    preserving ordering by top coordinate.
5. detect ad-hoc bullet paragraphs (pdf->html often emits each bullet as a
    separate absolutely positioned <p>) and rebuild semantic <ul><li> groups,
    including cases where bullets and text are split or inline separators.
6. run shared html.normalize_html pipeline for final cleanup.

goal: produce deterministic, minimal html structure so downstream markdown
conversion is consistent even for visually complex pdfs.
note: really simple. use a better library if you want better conversions
"""

import logging
import re
import base64
from dataclasses import dataclass
import fitz
from bs4 import BeautifulSoup, Tag
from .html import normalize_html


logger = logging.getLogger(__name__)

DATA_URI_PATTERN = re.compile(r'src="(data:image/[^;]+;base64,)\s*(.*?)"', flags=re.DOTALL)
BULLET_PATTERN = re.compile(r'^[\u2022\u25E6\u2219*\-–]\s+')  # • ◦ ∙ * - –
TOP_PATTERN = re.compile(r'top:(\d+(?:\.\d+)?)pt')


def fix_base64(match: re.Match):
    # collapse whitespace inside large base64 blobs to avoid broken images
    prefix = match.group(1)
    data = re.sub(r"\s+", "", match.group(2))
    return f'src="{prefix}{data}"'


@dataclass
class TextElement:
    text: str
    top: float
    left: float
    font_family: str
    font_size: str
    color: str
    line_height: str
    tag: Tag | None


def _get_int(value: str):
    match = re.search(r'\d+', value)
    return int(match.group()) if match else 0


def _extract_position(style_dict: dict[str, str]):
    top = _get_int(style_dict.get("top", "0"))
    left = _get_int(style_dict.get("left", "0"))
    return top, left


def _parse_style_attribute(style: str):
    style_dict: dict[str, str] = {}
    if not style:
        return style_dict
    for declaration in style.split(";"):
        if ":" in declaration:
            prop, value = declaration.split(":", 1)
            style_dict[prop.strip()] = value.strip()
    return style_dict


def _should_merge_elements(elem1: TextElement, elem2: TextElement):
    # same basic font styling
    if (
        elem1.font_family != elem2.font_family or
        elem1.font_size != elem2.font_size or
        elem1.color != elem2.color
    ):
        return False
    # naive vertical proximity check
    vertical_distance = elem2.top - elem1.top
    max_vertical = (_get_int(elem1.line_height) + _get_int(elem2.line_height)) * .75
    if vertical_distance > max_vertical:
        return False
    return True


def _consolidate_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    page_divs = soup.find_all("div", id=lambda x: x and x.startswith("page"))
    if not page_divs:
        return html
    for page_div in page_divs:
        # work only with direct child <p> tags so we don't descend into lists or tables
        children = [c for c in page_div.children if isinstance(c, Tag)]
        p_tags = [c for c in children if c.name == "p"]
        others = [c for c in children if c.name != "p"]
        if not p_tags:
            continue  # nothing to consolidate

        # extract a simplified list of text elements with positional metadata
        elements: list[TextElement] = []
        for p_tag in p_tags:
            style_dict = _parse_style_attribute(p_tag.get("style", ""))
            top, left = _extract_position(style_dict)
            span = p_tag.find("span")
            if span and span.get("style"):
                span_style = _parse_style_attribute(span.get("style", ""))
                font_family = span_style.get("font-family", "")
                font_size = span_style.get("font-size", "")
                color = span_style.get("color", "")
            else:
                font_family = style_dict.get("font-family", "")
                font_size = style_dict.get("font-size", "")
                color = style_dict.get("color", "")
            line_height = style_dict.get("line-height", "")
            text = p_tag.get_text(strip=True)
            if not text:
                # skip empty layout nodes
                continue
            elements.append(TextElement(
                text=text,
                top=top,
                left=left,
                font_family=font_family,
                font_size=font_size,
                color=color,
                line_height=line_height,
                tag=p_tag,
            ))
        if not elements:
            continue

        merged: list[TextElement] = []
        current = elements[0]
        for elem in elements[1:]:
            if _should_merge_elements(current, elem):
                # join text with a space, update positional metadata to the later element
                current.text = f"{current.text} {elem.text}"
                current.top = elem.top
                current.left = elem.left
                current.line_height = elem.line_height
            else:
                merged.append(current)
                current = elem
        merged.append(current)

        # build items list (top, tag) for merged paragraphs and existing non-p children, then reattach ordered
        items: list[tuple[float, Tag]] = []
        for elem in merged:
            p_tag = soup.new_tag("p")
            p_tag["style"] = f"top:{elem.top}pt;left:{elem.left}pt;line-height:{elem.line_height}"
            span_tag = soup.new_tag("span")
            span_tag["style"] = f"font-family:{elem.font_family};font-size:{elem.font_size};color:{elem.color}"
            span_tag.string = elem.text
            p_tag.append(span_tag)
            items.append((float(elem.top), p_tag))
        # include other existing elements (tables, lists, images) preserving their position via top style if present
        for other in others:
            style = other.get("style", "")
            m = re.search(r'top:(\d+(?:\.\d+)?)pt', style)
            top_val = float(m.group(1)) if m else 1e9  # push to end if no position
            items.append((top_val, other))
        # sort by top coordinate, stable for equal tops
        items.sort(key=lambda x: x[0])
        # clear page_div and reattach
        for c in list(page_div.contents):
            c.extract()
        for _, tag in items:
            page_div.append(tag)
    return str(soup)


def _fix_consolidated_html(html: str):
    # convert pseudo bullet paragraphs into <ul><li> groups
    soup = BeautifulSoup(html, "html.parser")
    # operate per page div so lists don't span pages
    pages = soup.find_all("div", id=lambda x: x and x.startswith("page")) or [soup.body or soup]
    for page in pages:
        # snapshot children to allow safe replacement while iterating
        children = list(page.children)
        i = 0
        while i < len(children):
            node = children[i]
            if not isinstance(node, Tag) or node.name != "p":
                i += 1
                continue
            text = node.get_text(strip=True)
            # handle paragraphs that look like bullets
            # normal case: paragraph starts with a bullet marker + space (e.g. "• item")
            if not BULLET_PATTERN.match(text):
                # also handle the case where the bullet is its own paragraph (e.g. a lone "•")
                BULLET_ONLY_PATTERN = re.compile(r'^[\u2022\u25E6\u2219*\-–]\s*$')
                if not BULLET_ONLY_PATTERN.match(text):
                    i += 1
                    continue
                # fall through to paired-bullet handling below
            # start collecting bullets. Two scenarios:
            # 1) paragraphs that begin with a bullet marker and the item text in the same <p>
            # 2) a lone-bullet <p> followed by a separate indented <p> carrying the item text
            ul = soup.new_tag("ul")
            j = i
            # scenario A: p's that start with a bullet marker + space
            if BULLET_PATTERN.match(text):
                while j < len(children):
                    n2 = children[j]
                    if not isinstance(n2, Tag) or n2.name != "p":
                        break
                    t2 = n2.get_text(strip=True)
                    if not BULLET_PATTERN.match(t2):
                        break
                    # strip leading bullet marker + space
                    li_text = BULLET_PATTERN.sub("", t2).strip()
                    if li_text:  # avoid empty li
                        li = soup.new_tag("li")
                        li.string = li_text
                        ul.append(li)
                    j += 1
            else:
                # scenario B: lone-bullet paragraphs paired with the following content paragraph
                while j < len(children):
                    n2 = children[j]
                    if not isinstance(n2, Tag) or n2.name != "p":
                        break
                    t2 = n2.get_text(strip=True)
                    # expect a lone bullet marker here
                    if not re.match(r'^[\u2022\u25E6\u2219*\-–]\s*$', t2):
                        break
                    # next node should exist and be a <p> with the item text
                    next_idx = j + 1
                    if next_idx >= len(children):
                        break
                    content_node = children[next_idx]
                    if not isinstance(content_node, Tag) or content_node.name != "p":
                        break
                    content_text = content_node.get_text(strip=True)
                    if content_text:
                        li = soup.new_tag("li")
                        li.string = content_text
                        ul.append(li)
                    # advance past the pair (bullet + content)
                    j = next_idx + 1
            # only replace if we actually built >0 items and more than one bullet (avoid false positives)
            if len(ul.find_all("li")) > 0:
                # remove original bullet paragraphs
                for k in range(i, j):
                    if isinstance(children[k], Tag):
                        children[k].extract()
                # insert ul at correct position
                if i < len(page.contents):
                    page.insert(i, ul)
                else:
                    page.append(ul)
                # refresh children snapshot after mutation
                children = list(page.children)
                i += 1
            else:
                i = j

        # pass for paragraphs that hold multiple inline bullets inside one span
        # example: one long paragraph with '... sentence. • next item ... • next ...'
        paras = list(page.find_all("p"))
        for p_tag in paras:
            # skip if already inside a list that we created
            if p_tag.find_parent("ul"):
                continue
            raw = p_tag.get_text(" ", strip=True)
            if raw.count("•") < 1:
                continue
            # split on bullet markers (keep first segment before first bullet as an item)
            parts = re.split(r'\s*•\s+', raw)
            parts = [x.strip() for x in parts if x.strip()]
            if len(parts) < 2:
                continue  # nothing meaningful to split
            ul = soup.new_tag("ul")
            for part in parts:
                li = soup.new_tag("li")
                li.string = part
                ul.append(li)
            p_tag.replace_with(ul)

    # second pass: split inline bullet separators inside single list items
    # pattern: turn sentence separators like ". • " or ". – " into individual <li>
    for ul in soup.find_all("ul"):
        # snapshot because we'll mutate
        li_nodes = list(ul.find_all("li", recursive=False))
        for li in li_nodes:
            # work only on simple text nodes
            text = li.get_text(strip=True)
            if not text:
                continue
            # normalize dash separators following punctuation into a bullet marker
            # so: ". – " -> ". • " then we split on bullet markers
            norm = re.sub(r'([.;:])\s+[–-]\s+', r'\1 • ', text)
            # also normalize multiple spaces
            norm = re.sub(r'\s+', ' ', norm)
            # split on bullet markers that are not at start (start already handled earlier)
            parts = [p.strip() for p in norm.split(' • ') if p.strip()]
            if len(parts) > 1:
                # replace original li with multiple lis
                ref = li
                for idx, part in enumerate(parts):
                    new_li = soup.new_tag("li")
                    new_li.string = part
                    if idx == 0:
                        ref.replace_with(new_li)
                        ref = new_li
                    else:
                        ref.insert_after(new_li)
                        ref = new_li
    return str(soup)


def pdf_to_html(pdf_bytes: bytes, url: str, embed_images = True):
    """convert raw pdf bytes to normalized html.

    :param embed_images: when a page lacks extractable text produce a png snapshot.
    :param url: used for title + relative link base.
    :return: cleaned html (no null bytes) ready for markdown.
    :rtype: str
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts: list[str] = []
    for i, page in enumerate(doc):
        page_num = i + 1
        extracted_html = (page.get_text("html") or "").strip()
        if extracted_html:
            # fix broken data uris + ensure page id increments
            extracted_html = DATA_URI_PATTERN.sub(fix_base64, extracted_html)
            extracted_html = re.sub(r'<div id="page0"', f'<div id="page{i}"', extracted_html, count=1)
            parts.append(extracted_html)
            continue
        if not embed_images:
            continue
        # fallback to image snapshot
        zoom = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=zoom, alpha=False)
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("ascii")
        parts.append(f'<img alt="Page {page_num}" src="data:image/png;base64,{b64}" />')
    filename = url.split("/")[-1]
    meta_html = ""
    meta = doc.metadata or {}
    if meta:
        meta_html = "<section><h3>PDF Metadata</h3><ul>"
        for k, v in meta.items():
            meta_html += f"<li><b>{k}</b>: {v}</li>"
        meta_html += "</ul></section>"
    html = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{filename}</title></head><body>"
        + meta_html
        + "\n".join(parts)
        + "</body></html>"
    )
    consolidated_html = _consolidate_html(html)
    fixed_html = _fix_consolidated_html(consolidated_html)
    normalized_html = normalize_html(fixed_html, url)
    return normalized_html.replace("\x00", "")