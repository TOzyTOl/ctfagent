"""Technique notes: the method-level memory, stored as files beside a skill.

A case card remembers one challenge's route; a note remembers a *method*, so it
transfers across wordings. Notes live in the skill directory as
``learned-YYYYMMDD-<hash>.md`` so ``read_skill_doc`` can open them, and this
module owns the parts a file cannot do on its own:

* **ranking** — the note block used to be "newest eight", which is the same
  failure as injecting every case card: after a hundred solves the index is
  mostly noise, and the one note that matches is crowded out by date. Notes are
  now ranked on the same informative tokens the case library uses (generic words
  and the library's own boilerplate removed) against the challenge text.
* **usage accounting** — being listed is exposure, not use. A note is *used*
  when its own vocabulary shows up in what the run wrote, and it is credited
  only on runs it was shown to. Without this the index can only grow louder,
  exactly like the cards did (``caselib.used_case_ids``).

The counters live in an HTML comment at the top of the note, so the file stays
readable and hand-editable: losing the counters costs ranking signal, never the
note itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import caselib
from .textmatch import tokens  # noqa: F401 - used by direction_boilerplate

#: Written by ``orchestrator._install_learned_note`` and updated here.
_USAGE = re.compile(r"^<!-- usage (\{.*?\}) -->\n?", re.S)
#: The provenance line the installer puts first; skipped when reading a title.
_COMMENT = re.compile(r"^<!--.*?-->\s*$", re.S)

#: How many notes the prompt index may carry. Each one costs a line on every
#: dispatch of the direction; two matched notes beat eight dated ones.
MAX_INDEX = 4
#: Fill the index to this many entries with the newest notes when nothing
#: matches. A note nobody has ever been shown cannot prove itself.
MIN_INDEX = 2


@dataclass
class Note:
    path: Path
    title: str = ""
    body: str = ""
    used: int = 0
    helped: int = 0
    harmed: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    def signature(self, boilerplate: frozenset[str] = frozenset()) -> set[str]:
        """The note's own informative vocabulary, for match and for credit."""
        return caselib.informative(self.title + " " + self.body, boilerplate)


def parse(path: Path) -> Note:
    text = path.read_text(encoding="utf-8", errors="replace")
    usage: dict[str, int] = {}
    match = _USAGE.match(text)
    if match:
        try:
            raw = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            raw = {}
        if isinstance(raw, dict):
            usage = {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}
        text = text[match.end():]
    title = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or _COMMENT.match(line):
            continue
        if line.startswith("#"):
            title = line.lstrip("# ").strip()
        break
    return Note(
        path=path,
        title=title,
        body=text,
        used=usage.get("used", 0),
        helped=usage.get("helped", 0),
        harmed=usage.get("harmed", 0),
        usage=usage,
    )


def render(note: Note, usage: dict[str, int]) -> str:
    return f"<!-- usage {json.dumps(usage, sort_keys=True)} -->\n" + note.body


def write_usage(path: Path, *, used: int = 0, helped: int = 0, harmed: int = 0) -> bool:
    """Merge counters into a note, leaving its prose untouched.

    Best effort: a note deleted between the read and the write is not an error
    (the operator may be curating the library while a run closes).
    """
    try:
        note = parse(path)
    except OSError:
        return False
    merged = dict(note.usage)
    for key, delta in (("used", used), ("helped", helped), ("harmed", harmed)):
        if delta:
            merged[key] = merged.get(key, 0) + delta
    if not merged:
        return False
    try:
        path.write_text(render(note, merged), encoding="utf-8", newline="\n")
    except OSError:
        return False
    return True


def index(skill_dir: Path) -> list[Note]:
    if not skill_dir.is_dir():
        return []
    out: list[Note] = []
    for path in sorted(skill_dir.glob("learned-*.md")):
        if not path.is_file():
            continue
        try:
            out.append(parse(path))
        except OSError:
            continue
    return out


def _score(note: Note, topic: set[str], boilerplate: frozenset[str]) -> float:
    shared = note.signature(boilerplate) & topic
    score = float(len(shared))
    decided = note.helped + note.harmed
    if decided >= 2:
        # Gentle, like the cards: one bad run is noise, not a verdict.
        score += 1.5 * ((note.helped / decided) - 0.5)
    return score


def rank(notes: list[Note], topic_text: str, boilerplate: frozenset[str] = frozenset()) -> list[Note]:
    """Notes worth listing for this challenge, best first.

    Matched notes come first in score order; the remainder of the index is
    filled with the newest notes (the filenames start with the date, so reverse
    name order is newest first). Capping the *matched* part and not the whole
    index keeps discoverability — an undetected note teaches nothing — while
    keeping the block's cost flat as the library grows.
    """
    if not notes:
        return []
    topic = caselib.informative(topic_text, boilerplate)
    scored = sorted(
        ((_score(note, topic, boilerplate), note) for note in notes),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    picked = [note for score, note in scored if score > 0][:MAX_INDEX]
    if len(picked) < MIN_INDEX:
        matched = {id(note) for note in picked}
        # Newest-but-unproven: notes never shown to anything can never earn a
        # score, so a fill is what gets them their first run. Fewest exposures
        # first, newest among equals — otherwise the same one or two notes ride
        # along forever and the rest are never seen, so never prove themselves.
        newest = sorted(notes, key=lambda n: n.name, reverse=True)
        newest.sort(key=lambda n: n.used)
        for note in newest:
            if len(picked) >= MIN_INDEX:
                break
            if id(note) not in matched:
                picked.append(note)
                matched.add(id(note))
    return picked


def render_index(notes: list[Note]) -> str:
    if not notes:
        return ""
    lines = [f"- {note.name}" + (f"：{note.title}" if note.title else "") for note in notes]
    return (
        "\n\n# 实战沉淀笔记（平台从解出的题蒸馏，与本技能同目录）\n"
        + "\n".join(lines)
        + "\n命中「什么时候用」的特征就先 read_skill_doc 打开对应笔记再动手。\n"
    )


def used_notes(
    paths: list[Path], written_text: str, boilerplate: frozenset[str] = frozenset()
) -> list[Path]:
    """Which of the notes this run was shown did the run actually apply.

    Same rule as ``caselib.used_case_ids``: at least two distinctive tokens of
    the note appear in what the run wrote. A note that was listed but never
    opened, or opened and ignored, is not credited.
    """
    if not paths:
        return []
    written = caselib.informative(written_text, boilerplate)
    out: list[Path] = []
    for path in paths:
        try:
            note = parse(path)
        except OSError:
            continue
        if len(note.signature(boilerplate) & written) >= caselib.MIN_USED_SHARED:
            out.append(path)
    return out


def balance(
    store,
    run_id: int,
    shown: list[Path],
    solved: bool,
    boilerplate: frozenset[str] | None = None,
) -> int:
    """Charge one run's outcome to the notes it used. Once per run.

    ``shown`` is what the run's dispatch index listed; the store decides whether
    this run gets to charge anything at all (``claim_notes_credit``). Notes are
    grouped by directory because each direction has its own boilerplate —
    charging every direction's notes against one direction's boilerplate would
    either admit the note format (``什么时候用``, ``怎么做``) everywhere or reject
    real matches.
    """
    if not shown or not store.claim_notes_credit(run_id):
        return 0
    text = caselib.run_written_text(store, run_id)
    groups: dict[Path, list[Path]] = {}
    for path in shown:
        groups.setdefault(path.parent, []).append(path)
    credited = 0
    for parent, paths in groups.items():
        bl = boilerplate if boilerplate is not None else boilerplate_of(index(parent))
        for path in used_notes(paths, text, bl):
            if write_usage(path, used=1, helped=int(solved), harmed=int(not solved)):
                credited += 1
    return credited


def boilerplate_of(notes: list[Note]) -> frozenset[str]:
    """Tokens shared by most of one direction's notes.

    The same rule ``caselib`` applies to its cards. Without it the note format
    itself (``什么时候用``, ``怎么做``, ``边界``, ``flag``) matches everything.
    """
    if len(notes) < caselib.BOILERPLATE_MIN_CARDS:
        return frozenset()
    counts: dict[str, int] = {}
    for note in notes:
        for token in tokens(note.title + " " + note.body):
            counts[token] = counts.get(token, 0) + 1
    floor = caselib.BOILERPLATE_SHARE * len(notes)
    return frozenset(t for t, n in counts.items() if n >= floor)


def direction_boilerplate(paths: list[Path]) -> frozenset[str]:
    parsed: list[Note] = []
    for path in paths:
        try:
            parsed.append(parse(path))
        except OSError:
            continue
    return boilerplate_of(parsed)
