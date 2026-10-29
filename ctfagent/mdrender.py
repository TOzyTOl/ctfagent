"""The Markdown subset the agent writes, rendered on the server.

The chat is drawn twice: once live by ``renderMarkdown`` in ``app.js`` while a
turn streams, and once by this module whenever the page is rendered from
storage (a reload, clicking 停止, any of the SSE events that re-render the
document). Those two must produce the *same* markup, or a message changes shape
the moment the page reloads — which is exactly the bug this module exists to
close: the stored path used to print the raw text, so ``##`` and ``` fences
appeared as characters and every paragraph collapsed into one block.

Keep the two implementations in step. The rules, in this order:

  * front matter      ``---`` on the first line -> ``<dl class="meta-strip">``
  * fenced code       ```` ``` ````; an info string (```` ```python ````) becomes
                      ``class="lang-python"``, sanitised to ``[A-Za-z0-9+#._-]``
                      so a fence can never write into the attribute
  * tables            a run of lines that start and end with ``|``
  * headings          ``#{1,6}``, clamped to h3 like the browser does
  * horizontal rule   three or more ``-``, ``*`` or ``_``
  * blockquote        ``>``
  * lists             ``-``/``*``/``+`` and ``1.``/``1)``
  * everything else   one ``<p>`` per line

Inline: ``code``, **bold**, *italic*, [text](https://...). Raw HTML in the
source is escaped first and never passes through, so a message cannot inject
markup — the same guarantee the browser renderer gives.
"""

from __future__ import annotations

import re
from html import escape

from markupsafe import Markup

_FENCE = re.compile(r"^\s*```")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_QUOTE = re.compile(r"^\s*>\s?")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FRONT_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(^|[^*])\*([^*]+)\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?:[^)]+)\)")


def _unquote(value: str) -> str:
    """Mirror the browser's ``/^["']|["']$/g``: one quote off each end."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        value = value[1:]
    if value[-1:] in ("'", '"'):
        value = value[:-1]
    return value


def _inline(text: str) -> str:
    """Escape, then apply the four inline rules in the browser's order."""
    s = escape(text, quote=False)
    s = _CODE.sub(r"<code>\1</code>", s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITALIC.sub(r"\1<em>\2</em>", s)
    s = _LINK.sub(r'<a href="\2" rel="noopener">\1</a>', s)
    return s


def _front_matter(lines: list[str]) -> tuple[str, list[str]]:
    if not lines or lines[0] != "---":
        return "", lines
    try:
        end = lines.index("---", 1)
    except ValueError:
        return "", lines
    pairs = []
    for line in lines[1:end]:
        m = _FRONT_KEY.match(line)
        if m:
            pairs.append((m.group(1), _unquote(m.group(2))))
    if not pairs:
        return "", lines[end + 1 :]
    out = ['<dl class="meta-strip">']
    for key, value in pairs:
        out.append(f"<dt>{escape(key, quote=False)}</dt><dd>{escape(value, quote=False)}</dd>")
    out.append("</dl>")
    return "\n".join(out), lines[end + 1 :]


def _fence_lang(line: str) -> str:
    """The info string on a fence, reduced to something safe for a class name.

    A fence is `` ```python `` or just `` ``` ``; whatever follows belongs to the
    author, so it is filtered to ``[A-Za-z0-9+#._-]`` (which keeps ``c++``,
    ``c#``, ``objective-c`` and ``.yml``) rather than escaped and pasted in.
    """
    rest = line.lstrip().lstrip("`").strip()
    rest = rest.split()[0] if rest else ""
    lang = re.sub(r"[^A-Za-z0-9+#._-]", "", rest)[:24]
    # The value is already safe, but a class that starts with a digit or is
    # exactly "." would still be a broken selector; those fences get no colour.
    return lang if re.match(r"^[A-Za-z][A-Za-z0-9+#._-]*$", lang) else ""


def render(src: str) -> Markup:
    """Render the agent's Markdown to HTML safe to drop into a template."""
    lines = str(src).replace("\r\n", "\n").split("\n")
    out: list[str] = []
    strip, lines = _front_matter(lines)
    if strip:
        out.append(strip)

    in_code = False
    code_buf: list[str] = []
    code_lang = ""
    list_type: str | None = None
    table_buf: list[str] = []

    def flush_code() -> None:
        # The language rides on the ``<pre>`` as ``class="lang-x"``. It is only a
        # hint for the highlighter in app.js and for the label on the copy bar;
        # nothing about the text depends on it, so an unknown or absent language
        # simply gets no colour.
        attr = f' class="lang-{code_lang}"' if code_lang else ""
        out.append(f"<pre{attr}><code>" + escape("\n".join(code_buf), quote=False) + "</code></pre>")

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            out.append("</ul>" if list_type == "ul" else "</ol>")
            list_type = None

    def flush_table() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        rows = [r for r in table_buf if not _TABLE_RULE.match(r)]
        out.append("<table>")
        for idx, row in enumerate(rows):
            cells = row.strip()
            if cells.startswith("|"):
                cells = cells[1:]
            if cells.endswith("|"):
                cells = cells[:-1]
            tag = "th" if idx == 0 else "td"
            rendered = "".join(f"<{tag}>{_inline(c.strip())}</{tag}>" for c in cells.split("|"))
            out.append(f"<tr>{rendered}</tr>")
        out.append("</table>")
        table_buf = []

    for line in lines:
        if _FENCE.match(line):
            if in_code:
                flush_code()
                code_buf = []
                code_lang = ""
                in_code = False
            else:
                close_list()
                flush_table()
                code_lang = _fence_lang(line)
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        if _TABLE_ROW.match(line):
            table_buf.append(line)
            continue
        flush_table()

        heading = _HEADING.match(line)
        if heading:
            close_list()
            level = min(len(heading.group(1)), 3)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        if _HR.match(line):
            close_list()
            out.append("<hr>")
            continue
        if _QUOTE.match(line):
            close_list()
            out.append("<blockquote>" + _inline(_QUOTE.sub("", line, count=1)) + "</blockquote>")
            continue
        ul = _UL.match(line)
        if ul:
            if list_type != "ul":
                close_list()
                out.append("<ul>")
                list_type = "ul"
            out.append("<li>" + _inline(ul.group(1)) + "</li>")
            continue
        ol = _OL.match(line)
        if ol:
            if list_type != "ol":
                close_list()
                out.append("<ol>")
                list_type = "ol"
            out.append("<li>" + _inline(ol.group(1)) + "</li>")
            continue
        if not line.strip():
            close_list()
            continue
        close_list()
        out.append("<p>" + _inline(line) + "</p>")

    if in_code and code_buf:
        flush_code()
    close_list()
    flush_table()
    return Markup("\n".join(out))


def markdown_filter(src: object) -> Markup:
    """Jinja filter. Non-strings (None, numbers) render as nothing."""
    if src is None:
        return Markup("")
    return render(str(src))
