"""html normalization + link extraction utilities.

we aggressively simplify noisy html so downstream markdown conversion and
text/anchor extraction behave well:
* strip crap (nav/footer/scripts/svg)
* promote data-* attrs into real attrs + style map (helps markdown lib)
* unwrap useless layout div nesting
* convert inline-only div/span blocks to paragraphs
* group stray inline runs into paragraphs
* normalize anchors (spacing, chip separation, absolute href/src)
* prune empty inline tags and insert lightweight separators between short chips

why: raw browser / pdf->html dumps are full of nested containers, duplicated
spacing, and anchor edge cases that confuse diffing + markdown. we reshape once
up front so every caller gets consistent structure.
"""

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag, NavigableString

logger = logging.getLogger(__name__)

# constants
INLINE_TAGS = {
    "a",
    "span",
    "strong",
    "b",
    "em",
    "i",
    "small",
    "img",
    "code",
    "kbd",
    "samp",
    "var",
    "sup",
    "sub",
    "mark",
    "abbr",
    "cite",
    "time",
    "u",
    "s",
    "del",
    "ins",
    "br",
}

BLOCK_CONTAINER_TAGS = {
    "body",
    "div",
    "section",
    "article",
    "main",
    "aside",
    "header",
    "footer",
    "nav",
    "li",
    "td",
    "th",
}

UNWRAP_PASSES = 5
_WHITESPACE_RE = re.compile(r"\s+")


def parse_style_attribute(style: str):
    """parse a style="`style`" string to a dict preserving raw values.

    tolerate empty / malformed declarations; ignore segments without a colon.
    kept tiny + forgiving because upstream html can be messy.
    """
    style_dict: dict[str, str] = {}
    if not style:
        return style_dict
    for declaration in style.split(";"):
        if ":" in declaration:
            prop, value = declaration.split(":", 1)
            style_dict[prop.strip()] = value.strip()
    return style_dict


def style_to_str(style_map: dict[str, str]):
    """serialize a style dict back to a compact css declaration string."""
    # reserialize style dict back to a string
    return "; ".join(f"{k}:{v}" for k, v in style_map.items())


def _has_only_inline_children(el: Tag) -> bool:
    for ch in el.children:
        if isinstance(ch, NavigableString):
            continue
        name = getattr(ch, "name", None)
        if not name:
            continue
        if name not in INLINE_TAGS:
            return False
    return True


def _is_inline_node(n):
    if isinstance(n, NavigableString):
        return True
    name = getattr(n, "name", None)
    return bool(name and name in INLINE_TAGS)


def _has_visible(n):
    if isinstance(n, NavigableString):
        return bool(str(n).strip())
    name = getattr(n, "name", None)
    if not name:
        return False
    if name in {"img", "a", "strong", "em", "b", "i", "code", "kbd"}:
        return True
    return bool(getattr(n, "get_text", lambda **_: "")(strip=True))


def _parse_data_attributes_to_attrs_and_style(soup: BeautifulSoup):
    for el in soup.find_all(True):
        data_keys = [
            k
            for k in list(el.attrs.keys())
            if isinstance(k, str) and k.startswith("data-")
        ]
        if not data_keys:
            continue
        existing_style = el.get("style", "") or ""
        style_map = parse_style_attribute(existing_style)
        for dk in data_keys:
            new_name = dk[5:]
            val = el.attrs.get(dk)
            if isinstance(val, list):
                val_str = " ".join(str(x) for x in val)
            else:
                val_str = "" if val is None else str(val)
            del el.attrs[dk]
            # avoid creating attributes that shadow Tag methods or
            # otherwise conflict with the Tag object's attribute access
            # (e.g. `new_tag`). If there's a potential collision, keep
            # the value under the original data-... attribute name so
            # we don't break Tag behavior.
            if new_name:
                # normalize a candidate attribute name for attribute lookup
                candidate = new_name.replace("-", "_")
                collision = False
                # if the Tag class exposes the attribute/method, treat as collision
                if hasattr(Tag, candidate) or hasattr(el, candidate):
                    collision = True
                if not collision and new_name not in el.attrs:
                    el.attrs[new_name] = val_str
                else:
                    # restore under a safe data- key to preserve the value
                    el.attrs[f"data-{new_name}"] = val_str
            if new_name and new_name not in style_map:
                style_map[new_name] = val_str
        if style_map:
            el.attrs["style"] = style_to_str(style_map)


def _remove_chrome_and_scripts(soup: BeautifulSoup):
    # remove common layout elements and svg/script noise
    for tag in soup.select("nav, footer, header, aside, script, svg"):
        tag.decompose()


def _unwrap_layout_divs(soup: BeautifulSoup):
    # unwrap divs that are just single nested divs used for layout
    for _ in range(UNWRAP_PASSES):
        for div in soup.find_all("div"):
            children = list(div.find_all(recursive=False))
            text_content = div.get_text(strip=True, separator="")

            is_layout_div = False
            if len(children) == 1 and children[0].name == "div":
                child_text = children[0].get_text(strip=True, separator="")
                if not text_content or text_content == child_text:
                    is_layout_div = True

            if is_layout_div:
                children[0].unwrap()


def _convert_inline_blocks_to_paragraphs(soup: BeautifulSoup):
    # convert divs/spans that contain only inline content to <p>
    for div in soup.find_all("div"):
        if div.get_text(strip=True) and _has_only_inline_children(div):
            div.name = "p"

    for sp in soup.find_all("span"):
        if (
            sp.parent
            and getattr(sp.parent, "name", "") in BLOCK_CONTAINER_TAGS
            and sp.get_text(strip=True)
            and _has_only_inline_children(sp)
        ):
            sp.name = "p"


def _group_inline_runs_into_paragraphs(soup: BeautifulSoup):
    # group sequences of inline nodes inside block containers into <p>
    for container in soup.find_all(BLOCK_CONTAINER_TAGS):
        children = list(container.children)
        run = []

        def flush_run(before: object | None):
            nonlocal run
            if not run:
                return
            if not any(_has_visible(n) for n in run):
                run = []
                return
            p = soup.new_tag("p")
            if before is None:
                container.append(p)
            else:
                run[0].insert_before(p)
            for n in run:
                p.append(n)
            run = []

        for ch in children:
            if _is_inline_node(ch):
                run.append(ch)
            else:
                flush_run(before=ch)
        flush_run(before=None)


def _separate_anchor_chips(p_tag: Tag):
    cur = p_tag.contents[0] if p_tag.contents else None
    while cur is not None:
        next_sib = getattr(cur, "next_sibling", None)
        probe = next_sib
        while isinstance(probe, NavigableString) and not str(probe).strip():
            probe = getattr(probe, "next_sibling", None)
        if (
            getattr(cur, "name", None) == "a"
            and getattr(probe, "name", None) == "a"
        ):
            # avoid calling instance attribute new_tag which may be
            # shadowed by an attribute named 'new_tag' on the Tag
            # (some HTML can include data- attributes that promote to
            # attributes and override Tag methods). Create a fresh
            # BeautifulSoup and produce a br tag from it.
            br = BeautifulSoup("", "html.parser").new_tag("br")
            cur.insert_after(br)
            cur = probe
        else:
            cur = next_sib


def _normalize_anchors(soup: BeautifulSoup):
    # normalize anchor contents and ensure spacing around anchors
    for a_tag in soup.find_all("a"):
        for br in a_tag.find_all("br"):
            br.replace_with(" ")

        link_text = _WHITESPACE_RE.sub(" ", a_tag.get_text(" ", strip=True))

        for child in list(a_tag.children):
            if getattr(child, "name", None) != "img":
                child.extract()

        if a_tag.find("img"):
            a_tag.append(" " + link_text)
        else:
            a_tag.string = link_text

        prev_sib = a_tag.previous_sibling
        if isinstance(prev_sib, NavigableString):
            if prev_sib and not str(prev_sib).endswith((" ", "\n")):
                a_tag.insert_before(" ")
        next_sib = a_tag.next_sibling
        if isinstance(next_sib, NavigableString):
            if next_sib and not str(next_sib).startswith((" ", "\n")):
                a_tag.insert_after(" ")


def _prune_empty_inlines(soup: BeautifulSoup):
    # remove inline tags that have no visible content (except img, br, a)
    for el in soup.find_all(True):
        name = getattr(el, "name", None)
        if not name:
            continue
        if name in INLINE_TAGS and name not in {"img", "br", "a"}:
            if not el.get_text(strip=True) and not any(
                getattr(ch, "name", None) for ch in el.children
            ):
                el.decompose()


def _ensure_inline_boundary_spaces(p_tag: Tag):
    def _node_text(n) -> str:
        if isinstance(n, NavigableString):
            return str(n)
        nm = getattr(n, "name", None)
        if nm == "br":
            return "\n"
        return getattr(n, "get_text", lambda **_: "")(strip=False)

    i = 0
    while i < len(p_tag.contents) - 1:
        left = p_tag.contents[i]
        right = p_tag.contents[i + 1]
        if (
            getattr(left, "name", None) == "br"
            or getattr(right, "name", None) == "br"
        ):
            i += 1
            continue
        lt = _node_text(left)
        rt = _node_text(right)
        if not lt.strip():
            i += 1
            continue
        if not rt.strip():
            i += 1
            continue
        has_ws = lt.endswith((" ", "\n", "\t")) or rt.startswith((" ", "\n", "\t"))
        lt_last = lt.rstrip()[-1:] if lt.rstrip() else ""
        rt_first = rt.lstrip()[:1] if rt.lstrip() else ""
        avoid = lt_last == "(" or rt_first in {
            ")",
            ",",
            ".",
            ";",
            ":",
            "!",
            "?",
        }
        if not has_ws and not avoid:
            right.insert_before(" ")
            i += 2
            continue
        i += 1


def _insert_chip_separators(p_tag: Tag):
    def _is_chip_tag(n) -> bool:
        name = getattr(n, "name", None)
        if not name:
            return False
        if name not in INLINE_TAGS or name in {"a", "br", "img"}:
            return False
        text = getattr(n, "get_text", lambda **_: "")(strip=True)
        if not text:
            return False
        return len(text) <= 32

    contents = list(p_tag.contents)
    chip_idxs = [i for i, n in enumerate(contents) if _is_chip_tag(n)]
    if len(chip_idxs) < 2:
        return
    for left_i, right_i in zip(chip_idxs, chip_idxs[1:]):
        mids = contents[left_i + 1 : right_i]
        if not mids:
            contents[left_i].insert_after(" | ")
            continue
        only_ws = all(
            isinstance(m, NavigableString) and m.strip() == "" for m in mids
        )
        if not only_ws:
            continue
        replaced = False
        for m in mids:
            if isinstance(m, NavigableString):
                if not replaced:
                    m.replace_with(" | ")
                    replaced = True
                else:
                    m.extract()


def _make_links_absolute(soup: BeautifulSoup, base_url: str):
    for tag, attr in [("a", "href"), ("img", "src")]:
        for el in soup.find_all(tag):
            if original_url := el.get(attr):
                try:
                    absolute_url = urljoin(base_url, original_url)
                    el[attr] = absolute_url
                except Exception:
                    logger.exception("failed joining %s with base %s", original_url, base_url)


def normalize_html(html: str, base_url: str = None):
    """takes unfriendly html and returns cleaned html.

    :param html: the input html to clean
    :param base_url: if provided we early-upgrade relative href/src -> absolute so
    later code (markdown, link dedupe) operates on canonical urls.
    :return: the now beautiful html
    :rtype: str
    """
    soup = BeautifulSoup(html, "html.parser")

    # optionally resolve relative links early in the pipeline
    if base_url:
        _make_links_absolute(soup, base_url)

    _remove_chrome_and_scripts(soup)
    _parse_data_attributes_to_attrs_and_style(soup)
    _unwrap_layout_divs(soup)
    _convert_inline_blocks_to_paragraphs(soup)
    _group_inline_runs_into_paragraphs(soup)

    # separate anchors and normalize them
    for p in soup.find_all("p"):
        _separate_anchor_chips(p)

    _normalize_anchors(soup)
    _prune_empty_inlines(soup)

    for p in soup.find_all("p"):
        _ensure_inline_boundary_spaces(p)
        _insert_chip_separators(p)

    return str(soup)


URL_PATTERN = re.compile(r"https?://[\w./-]+", flags=re.IGNORECASE)

def extract_links(
    html: str,
    base_url: str,
    strip_query = True,
    selectors: list[tuple[str, str]] = [("a", "href")],
):
    """extract absolute links from html ([tag,attribute] + plaintext urls).

    :param selectors: tag/attr pairs to treat as link sources.
    :param strip_query: optionally drop query strings for broader dedupe.
    :return: ordered, de-duped list.
    :rtype: list[str]
    """
    links: list[str] = []

    # if it's xml, extract <loc> tags
    is_xml = html.lstrip().startswith("<?xml")
    if is_xml:
        for match in re.finditer(
            r"<loc>(.*?)</loc>", html, flags=re.IGNORECASE | re.DOTALL
        ):
            links.append(match.group(1).strip())
        return links

    soup = BeautifulSoup(html, "html.parser")
    
    # get the links and make them absolute
    for tag, attr in selectors:
        for el in soup.find_all(tag):
            if original_url := el.get(attr):
                absolute_url = urljoin(base_url, original_url)
                links.append(absolute_url)
                el[attr] = absolute_url

    # get plaintext urls as well
    for link in URL_PATTERN.findall(soup.get_text(" ")):
        links.append(link)

    # dedupe while preserving order
    seen: set[str] = set()
    unique_links = [x for x in links if not (x in seen or seen.add(x))]

    if strip_query:
        unique_links = [url.split("?")[0] for url in unique_links]

    return unique_links
