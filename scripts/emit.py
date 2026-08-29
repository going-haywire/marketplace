"""Writing a channel feed — the one place TOML text is produced.

Hand-formatted rather than handed to `toml.dumps`, for two reasons:

1. **The serialization trap.** tomlkit's `String` is a `str` *subclass*. It
   passes every `isinstance` check and reprs identically, but `toml.dumps` does
   not recognise the subclass, falls back to treating it as a sequence, and
   writes `version = ["0", ".", "0", ".", "3", "8"]`. That is still valid TOML,
   so it parses fine and the corruption surfaces only in a *consumer*, whose
   parser rejects the row. A published first-party feed shipped broken this way
   and nobody noticed. Formatting each value by type means a non-`str` value
   cannot be silently reinterpreted — `_format_value` raises on anything it does
   not recognise.
2. **Comments.** A published feed is read by humans debugging a subscription,
   and `toml.dumps` cannot write a header.

Field order comes from `Haybale._TOML_FIELDS` — the consumer's own serializer —
rather than being restated here, so a generated feed and a studio-written one
stay shape-compatible without anyone remembering to sync two lists.
"""

from __future__ import annotations

from haywire.core.library.haybale import Haybale

#: Written by a *consumer's* refresh into its own cache, never by a publisher.
#: Emitting them would publish another machine's state — and `stale` in
#: particular would arrive at a subscriber pre-set, which is nonsense: whether a
#: row is stale is a fact about that subscriber's last refresh.
_CONSUMER_ONLY_FIELDS = frozenset({"via", "last_seen", "stale"})

#: The publishable fields, in the consumer's own order.
FIELD_ORDER: tuple[str, ...] = tuple(f for f in Haybale._TOML_FIELDS if f not in _CONSUMER_ONLY_FIELDS)


def _format_string(value: str) -> str:
    """A TOML basic string, escaping what basic strings forbid."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _format_value(value: object) -> str:
    """Format one TOML value, by exact type.

    `type(...) is str` rather than `isinstance`: that check is the whole
    defence against the tomlkit trap described in the module docstring. An
    `isinstance` check accepts a tomlkit `String` — which is exactly the value
    that serializes to a list of characters — so it would let the bug through
    while looking correct.
    """
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is str:
        return _format_string(value)
    if type(value) is int:
        return str(value)
    if type(value) is list:
        if not value:
            return "[]"
        return "[" + ", ".join(_format_value(v) for v in value) + "]"
    raise TypeError(
        f"refusing to serialize {type(value).__name__} ({value!r}) into a feed. "
        f"Only exact str/bool/int/list are emitted; a str *subclass* (tomlkit's String) "
        f"would be written as a list of characters and break every consumer. "
        f"Normalise with str(...) before this point."
    )


def emit_row(row: Haybale) -> list[str]:
    """One `[[haybales]]` block as lines.

    `to_dict()` does the omission of empty and default-valued fields, so the
    rule for what appears in a feed is the consumer's rule, not a second one.
    `authors` and `deprecated` serialize to tables and so are written last —
    every bare key after a table header would be parsed *into* that table.
    """
    data = row.to_dict()
    lines = ["[[haybales]]"]

    width = max((len(f) for f in FIELD_ORDER if f in data and f not in ("authors", "deprecated")), default=0)
    for name in FIELD_ORDER:
        if name not in data or name in ("authors", "deprecated"):
            continue
        lines.append(f"{name.ljust(width)} = {_format_value(data[name])}")

    for author in data.get("authors", []):
        lines.append("")
        lines.append("[[haybales.authors]]")
        for key in ("name", "url"):
            if key in author:
                lines.append(f"{key.ljust(4)} = {_format_value(author[key])}")

    return lines


def emit_feed(rows: list[Haybale], *, header: str) -> str:
    """A complete channel feed: a comment header, then inline `[[haybales]]`.

    Inline rather than the two-tier `[[stalls]]`-plus-subdirectory layout: a
    subscribing studio fetches the feed and then fetches every `[[stalls]]` URL
    it lists, so two-tier costs 1 + N HTTP requests per refresh where inline
    costs one. The single-library subscription case two-tier buys is already
    served by an author's own stall.
    """
    parts: list[str] = [header.rstrip("\n"), ""]
    for row in rows:
        parts.extend(emit_row(row))
        parts.append("")
    return "\n".join(parts)
