import html
import json
import mimetypes
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional


# General
def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# Audio
def _guess_content_type(filename: str, fallback: Optional[str] = None) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return fallback or guessed or "application/octet-stream"


def _guess_audio_content_type(filename: str, fallback: Optional[str] = None) -> str:
    if fallback:
        return fallback
    content_type = _guess_content_type(filename)
    if Path(filename).suffix.lower() == ".webm" and content_type == "video/webm":
        return "audio/webm"
    return content_type


def _is_audio_content_type(content_type: str) -> bool:
    return (content_type or "").lower().startswith("audio/")


# Reactions
REACTION_ALIASES = {
    "like": "👍",
    "thumbs up": "👍",
    "thumbsup": "👍",
    "+1": "👍",
    "haha": "😂",
    "laugh": "😂",
    "laughing": "😂",
    "lol": "😂",
    "heart": "❤️",
    "love": "❤️",
    "cry": "😢",
    "crying": "😢",
    "tears": "😢",
    "sad": "😢",
    "angry": "😡",
    "mad": "😡",
    "surprised": "😮",
    "wow": "😮",
    "open mouth": "😮",
    "thumbs down": "👎",
    "thumbsdown": "👎",
    "-1": "👎",
    "white check mark": "✅",
    "heavy check mark": "✅",
    "check": "✅",
    "checkmark": "✅",
    "eyes": "👀",
    "ghost": "👻",
    "ninja": "🥷",
    "rocket": "🚀",
    "fire": "🔥",
    "tada": "🎉",
    "party popper": "🎉",
    "raised hands": "🙌",
    "pray": "🙏",
    "clap": "👏",
    "100": "💯",
    "thinking face": "🤔",
    "thinking": "🤔",
    "smile": "🙂",
    "slightly smiling face": "🙂",
    "grinning": "😀",
    "heart eyes": "😍",
    "ok hand": "👌",
    "wave": "👋",
    "musical note": "🎵",
    "notes": "🎵",
    "music": "🎵",
    "headphones": "🎧",
    "audio": "🎧",
}


def _reaction_key(value: str) -> str:
    text = str(value or "").strip().lower().strip(":")
    if text in {"+1", "-1"}:
        return text
    text = text.replace("_", " ").replace("-", " ")
    return " ".join(text.split())


_DEFAULT_REACTION = "🥷"


def normalize_reaction_type(reaction: str) -> str:
    """Map a reaction (shortcode name or unicode emoji) to a Graph reactionType.

    Slack-style names (``"ghost"``, ``"thumbsup"``) are translated to the
    matching emoji. A literal emoji is forwarded as-is. An unmapped shortcode
    name (e.g. ``"ninja"``) falls back to a safe default rather than sending an
    invalid reactionType to Microsoft Graph.
    """
    text = str(reaction or "").strip()
    if not text:
        return "✅"
    key = _reaction_key(text)
    if key in REACTION_ALIASES:
        return REACTION_ALIASES[key]
    # Already a literal emoji / symbol char → forward unchanged.
    if any(ord(ch) > 0x2600 for ch in text):
        return text
    # Unmapped shortcode name → safe fallback (never an invalid reactionType).
    return _DEFAULT_REACTION


# File uploads
def _drive_id_from_item(item: dict[str, Any]) -> Optional[str]:
    parent = item.get("parentReference") if isinstance(item, dict) else {}
    if isinstance(parent, dict):
        drive_id = _str_or_none(parent.get("driveId"))
        if drive_id:
            return drive_id
    return _str_or_none(item.get("driveId") or item.get("drive_id"))


def _safe_upload_name(filename: str) -> str:
    name = Path(filename or "attachment").name.strip()
    return name or "attachment"


# Messages
def normalize_message(item: dict[str, Any]) -> dict[str, Any]:
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    from_obj = item.get("from") if isinstance(item.get("from"), dict) else {}
    user = from_obj.get("user") if isinstance(from_obj.get("user"), dict) else {}
    app = (
        from_obj.get("application")
        if isinstance(from_obj.get("application"), dict)
        else {}
    )
    raw_content = str(body.get("content") or "")
    content_type = str(body.get("contentType") or "html").lower()
    text = raw_content if content_type == "text" else html_to_text(raw_content)
    attachments = normalized_attachments_from_message(item)
    return {
        "id": str(item.get("id") or ""),
        "created": item.get("createdDateTime")
        or item.get("lastModifiedDateTime")
        or "",
        "from": user.get("displayName") or app.get("displayName") or "Unknown",
        "from_user_id": user.get("id"),
        "from_application_id": app.get("id"),
        "text": text,
        "web_url": item.get("webUrl"),
        "attachments": attachments,
        "files": attachments,
        "raw": item,
    }


def normalized_attachments_from_message(item: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source, field in (
        ("teams_attachment", "attachments"),
        ("teams_file", "files"),
    ):
        raw_items = item.get(field)
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            normalized = normalize_attachment(raw_item, source=source)
            if not normalized:
                continue
            key = (
                str(normalized.get("id") or ""),
                str(normalized.get("name") or ""),
                str(normalized.get("web_url") or normalized.get("content_url") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            attachments.append(normalized)
    return attachments


def normalize_attachment(
    item: dict[str, Any], *, source: str = "teams_attachment"
) -> dict[str, Any]:
    embedded = _maybe_json_object(item.get("content"))
    file_obj = item.get("file") if isinstance(item.get("file"), dict) else {}
    parent = (
        item.get("parentReference")
        if isinstance(item.get("parentReference"), dict)
        else {}
    )

    name = _str_or_none(
        item.get("name")
        or item.get("title")
        or item.get("displayName")
        or embedded.get("name")
        or embedded.get("title")
        or file_obj.get("name")
    )
    content_type = _str_or_none(
        item.get("contentType")
        or item.get("mimetype")
        or embedded.get("contentType")
        or embedded.get("mimeType")
        or file_obj.get("mimeType")
    )
    web_url = _str_or_none(
        item.get("webUrl")
        or item.get("web_url")
        or item.get("permalink")
        or embedded.get("webUrl")
        or embedded.get("web_url")
        or embedded.get("objectUrl")
        or file_obj.get("webUrl")
    )
    content_url = _str_or_none(
        item.get("contentUrl")
        or item.get("content_url")
        or item.get("url_private_download")
        or embedded.get("contentUrl")
        or embedded.get("downloadUrl")
        or embedded.get("@microsoft.graph.downloadUrl")
    )
    thumbnail_url = _str_or_none(
        item.get("thumbnailUrl")
        or item.get("thumbnail_url")
        or embedded.get("thumbnailUrl")
    )
    attachment_id = _str_or_none(
        item.get("id")
        or item.get("fileId")
        or embedded.get("id")
        or embedded.get("uniqueId")
    )
    drive_id = _str_or_none(
        item.get("driveId")
        or item.get("drive_id")
        or embedded.get("driveId")
        or parent.get("driveId")
    )
    size = item.get("size") or embedded.get("size") or file_obj.get("size")

    normalized = {
        "id": attachment_id,
        "name": name,
        "content_type": content_type,
        "content_url": content_url,
        "web_url": web_url,
        "thumbnail_url": thumbnail_url,
        "drive_id": drive_id,
        "size": size if isinstance(size, (int, float)) else None,
        "source": source,
    }
    return {k: v for k, v in normalized.items() if v not in (None, "", {})}


def _maybe_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


## HTML Extractor


def html_to_text(content: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(content or "")
    return parser.text()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    @staticmethod
    def _attr_value(
        attrs: list[tuple[str, Optional[str]]], *names: str
    ) -> Optional[str]:
        lookup = {name.lower(): value for name, value in attrs}
        for name in names:
            value = lookup.get(name.lower())
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"br", "p", "div", "li"}:
            self._newline()
        if tag.lower() == "img":
            alt = self._attr_value(attrs, "alt", "title")
            if alt:
                self.parts.append(alt)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


# Text to HTML


def format_teams_message(message: str, *, is_html: bool = False) -> str:
    return message if is_html else text_to_teams_html(message)


def text_to_teams_html(text: str) -> str:
    """Render agent text as Teams-safe HTML.

    Microsoft Graph Teams messages take HTML bodies, not Slack rich text blocks
    or Markdown blocks. This intentionally supports only the common Markdown
    shapes agents emit, and leaves the output as plain escaped text otherwise.
    """
    lines = (text or "").splitlines()
    if not lines:
        return ""

    blocks: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        blocks.append("<br>".join(_render_inline_markdown(line) for line in paragraph))
        paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code_text = "\n".join(code_lines)
            blocks.append(f"<pre>{html.escape(code_text, quote=False)}</pre>")
            continue

        if _is_markdown_table(lines, index):
            flush_paragraph()
            table_rows = [_split_markdown_table_row(lines[index])]
            index += 2
            while index < len(lines) and "|" in lines[index].strip():
                table_rows.append(_split_markdown_table_row(lines[index]))
                index += 1
            blocks.append(_render_markdown_table_as_pre(table_rows))
            continue

        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_paragraph()
            heading_text = _render_inline_markdown(heading.group(1))
            blocks.append(f"<strong>{heading_text}</strong>")
            index += 1
            continue

        quote = re.match(r"^\s{0,3}>\s?(.*)$", line)
        if quote:
            flush_paragraph()
            quote_lines = []
            while index < len(lines):
                quote_match = re.match(r"^\s{0,3}>\s?(.*)$", lines[index])
                if not quote_match:
                    break
                quote_lines.append(_render_inline_markdown(quote_match.group(1)))
                index += 1
            quote_body = "<br>".join(quote_lines)
            blocks.append(f"<blockquote>{quote_body}</blockquote>")
            continue

        unordered = re.match(r"^\s{0,3}[-*+]\s+(.+)$", line)
        ordered = re.match(r"^\s{0,3}\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            flush_paragraph()
            tag = "ul" if unordered else "ol"
            items = []
            while index < len(lines):
                item_match = (
                    re.match(r"^\s{0,3}[-*+]\s+(.+)$", lines[index])
                    if tag == "ul"
                    else re.match(r"^\s{0,3}\d+[.)]\s+(.+)$", lines[index])
                )
                if not item_match:
                    break
                item_text = _render_inline_markdown(item_match.group(1))
                items.append(f"<li>{item_text}</li>")
                index += 1
            blocks.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    return "<br><br>".join(blocks)


def _render_inline_markdown(text: str) -> str:
    rendered = html.escape(text or "", quote=True)
    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(f"<code>{match.group(1)}</code>")
        return f"\x00CODE{len(code_spans) - 1}\x00"

    rendered = re.sub(r"`([^`\n]+)`", stash_code, rendered)
    rendered = re.sub(
        r"!\[([^\]]*)\]\((https?://[^)\s]+)\)",
        lambda m: (f'<a href="{m.group(2)}">' f"{m.group(1) or m.group(2)}</a>"),
        rendered,
    )
    rendered = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        rendered,
    )
    rendered = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"__([^_\n]+)__", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"~~([^~\n]+)~~", r"<del>\1</del>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    rendered = re.sub(
        r"(?<![\w_])_([^_\n]+)_(?![\w_])",
        r"<em>\1</em>",
        rendered,
    )

    for idx, code in enumerate(code_spans):
        rendered = rendered.replace(f"\x00CODE{idx}\x00", code)
    return rendered


def _is_markdown_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    row = lines[index]
    separator = lines[index + 1]
    if "|" not in row or "|" not in separator:
        return False
    return _is_markdown_table_separator(separator)


def _is_markdown_table_separator(row: str) -> bool:
    cells = _split_markdown_table_row(row)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _split_markdown_table_row(row: str) -> list[str]:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_markdown_table_as_pre(rows: list[list[str]]) -> str:
    widths = [
        max(len(row[col]) for row in rows if col < len(row))
        for col in range(max(len(row) for row in rows))
    ]
    lines = []
    for row_index, row in enumerate(rows):
        padded = [
            (row[col] if col < len(row) else "").ljust(widths[col])
            for col in range(len(widths))
        ]
        lines.append(" | ".join(padded).rstrip())
        if row_index == 0:
            lines.append("-+-".join("-" * width for width in widths).rstrip())
    table_text = "\n".join(lines)
    return f"<pre>{html.escape(table_text, quote=False)}</pre>"
