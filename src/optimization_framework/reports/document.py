"""Preserve source HTML; index review anchors in browser UTF-16 coordinates."""
from __future__ import annotations

from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re


ASSETS = Path(__file__).with_name("assets")
EXCLUDED = {"svg", "script", "style", "input", "button", "select", "textarea", "label", "nav"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class SectionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.sections = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            if self.current is not None:
                raise ValueError("Report sections must not be nested")
            key = attrs.get("id", "")
            if not re.fullmatch(r"[\w-]{1,100}", key) or key in self.sections:
                raise ValueError("Report sections need unique, stable IDs")
            self.current = key
            self.sections[key] = ""
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "section":
            self.current = None
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, value):
        if self.current and not EXCLUDED.intersection(self.stack):
            self.sections[self.current] += value


def sections(source):
    parser = SectionText()
    parser.feed(source)
    if not parser.sections:
        raise ValueError("A report needs at least one <section id=...> with its text")
    return parser.sections


def digest(source):
    return sha256(source.encode("utf-8")).hexdigest()


def utf16_slice(value, start, end):
    encoded = value.encode("utf-16-le")
    if not 0 <= start < end <= len(encoded) // 2:
        raise ValueError("Highlight is outside its source section")
    try:
        return encoded[start * 2:end * 2].decode("utf-16-le")
    except UnicodeError as exc:
        raise ValueError("Highlight splits a Unicode character") from exc


def script_json(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def clean_source(source):
    # Disable source interaction: sorting a table would change anchor positions.
    source = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", source, flags=re.I | re.S)
    source = re.sub(r'<nav\b[^>]*class="toolbar"[^>]*>.*?</nav>', "", source, flags=re.I | re.S)
    return source


def review_html(report, feedback=None, *, workspace_id=None):
    source = clean_source(report["html"])
    config = {key: report[key] for key in ("id", "title", "source_hash", "created_at", "campaign_id", "parent_id")}
    config.update(workspace_id=workspace_id, feedback=feedback, revision=report.get("revision"))
    head, body = re.split(r"<body\b[^>]*>", source, maxsplit=1, flags=re.I)
    body = re.sub(r"</body>\s*</html>\s*$", "", body, flags=re.I)
    controls = (ASSETS / "controls.html").read_text()
    head = re.sub(r"</head>\s*$", "", head, flags=re.I)
    return (head + '<style id="report-review-style">' + (ASSETS / "review.css").read_text() + "</style></head><body>" + controls
            + '<main id="report-content" tabindex="-1">' + body + "</main>"
            + '<script id="report-config" type="application/json">' + script_json(config) + "</script>"
            + '<script id="report-review-script">' + (ASSETS / "review.js").read_text() + "</script></body></html>")


def compact_figures(source):
    figures = {}
    def replace(match):
        key = f"figure-{len(figures) + 1}"
        figures[key] = match[0]
        caption = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", match[0], re.S | re.I)
        return f'<figure data-report-figure="{key}">' + (caption[0] if caption else "") + "</figure>"
    return re.sub(r"<figure\b[^>]*>.*?</figure>", replace, clean_source(source), flags=re.S | re.I), figures


class WriterHTML(HTMLParser):
    """The model writes inert document markup, never executable HTML or CSS."""
    allowed = {"section", "h1", "h2", "h3", "h4", "p", "b", "strong", "i", "em", "sub", "sup", "code", "pre",
               "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th", "blockquote", "div", "span",
               "figure", "figcaption", "a", "br", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.allowed:
            raise ValueError(f"Writer returned unsupported HTML: {tag}")
        safe = []
        for key, value in attrs:
            if key not in {"id", "class", "href", "data-report-figure", "colspan", "rowspan"}:
                raise ValueError(f"Writer returned unsupported HTML attribute: {key}")
            if key == "href" and not (value or "").startswith(("https://", "http://", "#")):
                raise ValueError("Writer returned an unsafe link")
            safe.append(f' {key}="{escape(value or "", quote=True)}"')
        self.output.append("<" + tag + "".join(safe) + ">")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("Writer returned unbalanced document markup")
        self.output.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, value):
        self.output.append(escape(value))


def writer_document(title, body, figures):
    parser = WriterHTML()
    parser.feed(body)
    if parser.stack:
        raise ValueError("Writer returned unfinished document markup")
    safe = "".join(parser.output)
    used = []
    def restore(match):
        key = match[1]
        if key not in figures or key in used:
            raise ValueError("Writer returned an unknown or repeated figure")
        used.append(key)
        return figures[key]
    safe = re.sub(r'<figure data-report-figure="([\w-]+)">.*?</figure>', restore, safe, flags=re.S)
    if set(used) != set(figures):
        raise ValueError("Writer omitted a supplied figure; keep its placeholder in the draft")
    sections(safe)
    style = "body{font:16px/1.6 system-ui;color:#203448;background:#edf1f4;margin:0}section{max-width:980px;background:white;margin:24px auto;padding:40px;break-after:page}table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #ccd4dc;text-align:left}figure{margin:20px 0}svg{width:100%;height:auto}figcaption{font-size:13px;color:#526576}h1,h2{line-height:1.2}@media print{body{background:white}section{margin:0;padding:0}}"
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>{style}</style></head><body>{safe}</body></html>'
