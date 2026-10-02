"""Stdlib-only HTML helpers for catalog extraction."""
from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

_SKIP_TEXT_TAGS = {"script", "style", "noscript", "svg", "template"}
_OPTION_HINTS = ("size", "colour", "color", "variant", "attribute", "option", "fit", "length", "width", "material")
_PLACEHOLDER_OPTIONS = re.compile(r"^(choose|select|pick|--|—|please)", re.I)


class PageParser(HTMLParser):
    """Single pass over a page collecting everything the extractors need."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.meta: dict[str, list[str]] = {}
        self.links: list[str] = []
        self.jsonld_blocks: list[str] = []
        self.canonical: str | None = None
        self.title = ""
        self.selects: list[tuple[str, list[str]]] = []  # (label, option texts)
        self._text: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self._select_label: str | None = None
        self._select_opts: list[str] = []
        self._in_option = False
        self._option_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"].strip())
        elif tag == "link" and "canonical" in a.get("rel", "").lower() and a.get("href"):
            self.canonical = urljoin(self.base_url, a["href"])
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buf = []
        elif tag == "select":
            label = " ".join(a.get(k, "") for k in ("name", "id", "aria-label", "data-option-name")).lower()
            self._select_label = label if any(h in label for h in _OPTION_HINTS) else None
            self._select_opts = []
        elif tag == "option" and self._select_label is not None:
            self._in_option = True
            self._option_buf = []
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TEXT_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld_blocks.append("".join(self._jsonld_buf))
        elif tag == "option" and self._in_option:
            self._in_option = False
            text = " ".join("".join(self._option_buf).split())
            if text and not _PLACEHOLDER_OPTIONS.match(text):
                self._select_opts.append(text)
        elif tag == "select" and self._select_label is not None:
            if self._select_opts:
                self.selects.append((self._select_label, self._select_opts))
            self._select_label = None
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section"):
            self._text.append("\n")

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_title:
            self.title += data
        if self._in_option:
            self._option_buf.append(data)
        if not self._skip_depth:
            self._text.append(data)

    @property
    def text(self) -> str:
        raw = "".join(self._text)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)

    def first_meta(self, *keys: str) -> str | None:
        for k in keys:
            vals = self.meta.get(k)
            if vals:
                return vals[0]
        return None

    def absolute_links(self) -> list[str]:
        out = []
        for href in self.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#", "whatsapp:")):
                continue
            out.append(strip_fragment(urljoin(self.base_url, href)))
        return out


def parse_page(html: str, base_url: str) -> PageParser:
    p = PageParser(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # malformed markup — keep whatever was collected
    return p


def strip_fragment(url: str) -> str:
    s = urlsplit(url)
    return urlunsplit((s.scheme, s.netloc, s.path, s.query, ""))


def html_to_text(html: str | None, limit: int = 2000) -> str:
    if not html:
        return ""
    text = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:limit]


def iter_jsonld(blocks: list[str]):
    """Yield every JSON-LD object (flattening @graph and lists)."""
    for raw in blocks:
        raw = raw.strip().removeprefix("<!--").removesuffix("-->").strip()
        raw = raw.removeprefix("//<![CDATA[").removesuffix("//]]>").strip()
        try:
            data = json.loads(raw)
        except ValueError:
            # Some sites emit raw newlines/tabs inside strings.
            try:
                data = json.loads(re.sub(r"[\n\r\t]", " ", raw))
            except ValueError:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def jsonld_types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t.split("/")[-1]}
    if isinstance(t, list):
        return {str(x).split("/")[-1] for x in t}
    return set()
