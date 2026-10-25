"""Checks on model prose in a final answer.

Two jobs:
  - make the stored answer inert: Markdown links/images, reference definitions, autolinks, HTML tags and
    bare URLs are stripped, so neither the UI nor a report renderer can be made to fetch or follow anything
    the model (or an injected observation) wrote;
  - find the backend ids named in the prose (evt-, finding-, job-, run-) so the orchestrator can reject any
    that do not exist in the run's citable scope. An id needs at least one digit, so ordinary words such as
    "run-time" or "job-related" are not mistaken for ids.
"""

from __future__ import annotations

import re

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_REF_DEF = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*\S.*$", re.M)
_AUTOLINK = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.-]*:|www\.)[^>\s]*>")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_URL = re.compile(r"(?i)\b(?:https?|ftp|javascript|data|file):[^\s<>)\]]+|\bwww\.[^\s<>)\]]+")

ID_KINDS = {
    "event": re.compile(r"\bevt-(?=[0-9A-Za-z_-]*\d)[0-9A-Za-z_-]{1,40}"),
    "finding": re.compile(r"\bfinding-(?=[0-9A-Za-z_-]*\d)[0-9A-Za-z_-]{1,40}"),
    "job": re.compile(r"\bjob-(?=[0-9A-Za-z_-]*\d)[0-9A-Za-z_-]{1,40}"),
    "run": re.compile(r"\brun-(?=[0-9A-Za-z_-]*\d)[0-9A-Za-z_-]{1,40}"),
}


def sanitize_text(text: str) -> tuple[str, bool]:
    """Strip links, images, HTML and URLs. Returns (clean text, whether anything was removed)."""
    out = _COMMENT.sub("", text)
    out = _IMAGE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    out = _REF_LINK.sub(r"\1", out)
    out = _REF_DEF.sub("", out)
    out = _AUTOLINK.sub("", out)
    out = _TAG.sub("", out)
    out = _URL.sub("[url removed]", out)
    return out, out != text


def ids_in(text: str) -> dict[str, set[str]]:
    """Backend ids named in free text, by kind."""
    return {kind: set(rx.findall(text)) for kind, rx in ID_KINDS.items()}


def merge_ids(*groups: dict[str, set[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {k: set() for k in ID_KINDS}
    for g in groups:
        for k, v in g.items():
            out[k] |= v
    return out
