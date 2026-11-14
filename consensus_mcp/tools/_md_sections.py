"""Shared markdown section parser for spec md files (G3 / canonical-001).

Used by repo.get_section (T9) and repo.set_section (T10) to enforce intra-file
scope. Parser is pure / deterministic / round-trip safe:

    parse(text) -> SectionMap
    reconstruct(SectionMap) == text   (byte-identical)

Section ID namespace
--------------------
  - "frontmatter"  : YAML between the leading "---" line and the next "---" line
                     (exclusive of the "---" markers themselves). Returned text
                     is the raw bytes between the markers, verbatim.
  - "section_N"    : the body of the Nth top-level heading "## N. <title>".
                     section_text starts at the heading line and ends at the
                     line before the next "## " heading at the same level
                     (or EOF). Trailing newlines are preserved.

Subsections (### N.M) are part of their parent section. The parser does not
expose them as separate section_ids in v1.0.

Parser invariants
-----------------
  - Code-fence aware: lines inside ``` ... ``` blocks are NEVER treated as
    section headings, even if they happen to start with "## ".
  - Frontmatter aware: a leading "---" delimiter is detected only when it is
    the first non-empty line of the file (covers spec md convention).
  - Content between the closing "---" of frontmatter and the first "## "
    heading (preamble: H1 title, blank lines, etc.) is preserved verbatim
    in the SectionMap as the hidden field "_preamble". It has no section_id
    and is NOT writable via set_section.
  - The "---" delimiters themselves are stored as "_frontmatter_open" and
    "_frontmatter_close" so reconstruction is byte-exact.

Why round-trip safety matters
-----------------------------
repo.set_section enforces "only the requested section changed" by:
  1. Parsing the file pre-write.
  2. Building new_text by reconstructing with one section replaced.
  3. Parsing new_text again.
  4. Comparing pre-parse and post-parse dicts; refusing if any non-target
     section's text or set of section_ids changed.

If new_section_text contains a "## N." line that the parser interprets as a
new heading, the post-parse dict will have a different shape than the pre-parse
dict, and the gate fires. This closes claude-rev-048 / canonical-001.

ASCII only. No emoji. No external dependencies beyond stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Dict, List, Tuple


# ## N. ...  (top-level numbered heading; N is one or more digits)
_HEADING_RE = re.compile(r"^##\s+(\d+)\.\s")
# ``` or ~~~ at start of line (any info string after) - fence open/close marker.
# We accept both backtick and tilde fences per CommonMark, but spec md uses ```.
_FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class SectionMap:
    """Result of parse(). Round-trips via reconstruct().

    Public access:
      - sections: dict[section_id, section_text]
      - section_ids(): ordered list of public section IDs
      - get(section_id): str
      - replace(section_id, new_text) -> new SectionMap

    Hidden fields preserve verbatim bytes between sections so reconstruction
    is byte-identical:
      - _has_frontmatter: bool
      - _frontmatter_open: "---\\n" or ""
      - _frontmatter_close: "---\\n" or ""
      - _preamble: text between frontmatter close and first section_N heading
      - _section_order: order in which section_N IDs appear (preserves source order)
    """

    sections: Dict[str, str] = field(default_factory=dict)
    _has_frontmatter: bool = False
    _frontmatter_open: str = ""
    _frontmatter_close: str = ""
    _preamble: str = ""
    _section_order: List[str] = field(default_factory=list)

    def section_ids(self) -> List[str]:
        """Return public section IDs in source order. 'frontmatter' first if present."""
        out: List[str] = []
        if "frontmatter" in self.sections:
            out.append("frontmatter")
        out.extend(self._section_order)
        return out

    def get(self, section_id: str) -> str:
        return self.sections[section_id]

    def replace(self, section_id: str, new_text: str) -> "SectionMap":
        """Return a new SectionMap with section_id's text replaced.

        Preserves ordering, frontmatter markers, and preamble verbatim.
        Raises KeyError if section_id not in current map.
        """
        if section_id not in self.sections:
            raise KeyError(section_id)
        new_sections = dict(self.sections)
        new_sections[section_id] = new_text
        return SectionMap(
            sections=new_sections,
            _has_frontmatter=self._has_frontmatter,
            _frontmatter_open=self._frontmatter_open,
            _frontmatter_close=self._frontmatter_close,
            _preamble=self._preamble,
            _section_order=list(self._section_order),
        )


def parse(text: str) -> SectionMap:
    """Parse spec md text into SectionMap. See module docstring for semantics."""
    lines = text.splitlines(keepends=True)
    i = 0
    n = len(lines)

    has_frontmatter = False
    fm_open = ""
    fm_close = ""
    fm_body_lines: List[str] = []

    # ---- Step 1: detect leading frontmatter ----
    # Convention: file starts with a line "---\n" (or "---" at EOF). Any blank
    # lines before the first "---" disqualify (we do not auto-skip).
    if n > 0 and lines[0].rstrip("\r\n") == "---":
        # Scan for closing "---".
        j = 1
        while j < n and lines[j].rstrip("\r\n") != "---":
            j += 1
        if j < n:
            # Found closing.
            has_frontmatter = True
            fm_open = lines[0]
            fm_close = lines[j]
            fm_body_lines = lines[1:j]
            i = j + 1
        # If no closing "---" was found, treat as no frontmatter (file is malformed
        # but we do not raise; parser stays defensive).

    # ---- Step 2: collect preamble (text from after frontmatter to first "## N." heading) ----
    preamble_lines: List[str] = []
    section_order: List[str] = []
    sections: Dict[str, str] = {}
    if has_frontmatter:
        sections["frontmatter"] = "".join(fm_body_lines)

    in_fence = False
    fence_marker: str = ""
    section_starts: List[Tuple[int, str]] = []  # list of (line_index, section_id)

    while i < n:
        line = lines[i]
        # Fence tracking - only toggle in body context, not inside frontmatter
        # (frontmatter has been consumed already).
        if not in_fence:
            m_fence = _FENCE_RE.match(line)
            if m_fence:
                in_fence = True
                fence_marker = m_fence.group(1)
                # Continue scanning; fences don't open or close sections.
            m_h = _HEADING_RE.match(line) if not in_fence else None
            if m_h:
                num = m_h.group(1)
                section_id = f"section_{num}"
                section_starts.append((i, section_id))
                # Once we see the first heading, preamble collection ends.
                # All subsequent lines are owned by sections.
                break
            preamble_lines.append(line)
        else:
            # Inside fence: skip heading detection, but watch for fence close.
            if line.startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            preamble_lines.append(line)
        i += 1

    preamble = "".join(preamble_lines)

    # ---- Step 3: scan from first heading onward, splitting by "## N." ----
    if section_starts:
        # We already pushed (i, section_id) into section_starts when we broke out.
        # Continue scanning to find subsequent heading boundaries.
        in_fence = False
        fence_marker = ""
        k = section_starts[0][0]
        while k < n:
            line = lines[k]
            if not in_fence:
                m_fence = _FENCE_RE.match(line)
                if m_fence:
                    in_fence = True
                    fence_marker = m_fence.group(1)
                else:
                    if k != section_starts[0][0]:
                        m_h = _HEADING_RE.match(line)
                        if m_h:
                            num = m_h.group(1)
                            section_starts.append((k, f"section_{num}"))
            else:
                if line.startswith(fence_marker):
                    in_fence = False
                    fence_marker = ""
            k += 1

        # Slice each section's text from start (inclusive) to next section start (exclusive).
        for idx, (start_line, sec_id) in enumerate(section_starts):
            end_line = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else n
            sec_text = "".join(lines[start_line:end_line])
            sections[sec_id] = sec_text
            section_order.append(sec_id)

    return SectionMap(
        sections=sections,
        _has_frontmatter=has_frontmatter,
        _frontmatter_open=fm_open,
        _frontmatter_close=fm_close,
        _preamble=preamble,
        _section_order=section_order,
    )


def reconstruct(smap: SectionMap) -> str:
    """Reconstruct full text from SectionMap. Inverse of parse() under round-trip.

    Order:
      1. _frontmatter_open (e.g., "---\\n")
      2. sections["frontmatter"] (raw body)
      3. _frontmatter_close (e.g., "---\\n")
      4. _preamble (verbatim text between frontmatter close and first ##)
      5. each section in _section_order, concatenated
    """
    out: List[str] = []
    if smap._has_frontmatter:
        out.append(smap._frontmatter_open)
        out.append(smap.sections.get("frontmatter", ""))
        out.append(smap._frontmatter_close)
    out.append(smap._preamble)
    for sec_id in smap._section_order:
        out.append(smap.sections.get(sec_id, ""))
    return "".join(out)
