"""Prompt normalisation, tokenisation and rule-based complexity signals.

Patterns live in language packs (autorouter/lang/*.json, plus the user's
<home>/lang/*.json); every pack is active at once, so a prompt in any covered
language is scored. Text is lower-cased and stripped of Latin accents before
matching, so Latin patterns are written without accents.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import config

KEYS = ("strong", "weak", "quick", "multi_file", "follow_up", "stopwords", "advice", "work",
        "quick_verbs", "code_words")
CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f]")
# A file name, backticks, snake_case, camelCase or a path: the prompt is about specific code.
ANCHOR = re.compile(r"`|\w{2,}\.[a-z]{1,5}\b|\w_\w|\b[a-z]+[A-Z]\w*|[/\\]\w")
CJK_RUN = re.compile(CJK.pattern + "+")


def _compile(patterns):
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error:
            continue  # a bad pack pattern must never break routing
    return out


@lru_cache(maxsize=None)
def pack(user_dir: str):
    """Merged, compiled patterns of all language packs."""
    merged = {k: {} for k in KEYS}
    for d in (Path(__file__).parent / "lang", Path(user_dir)):
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for k in KEYS:
                for item in data.get(k, []) if isinstance(data, dict) else []:
                    if isinstance(item, str):
                        merged[k][item] = None  # dict: dedupe across packs, keep order
    m = {k: list(v) for k, v in merged.items()}
    follow = _compile(m["follow_up"])
    return {
        "strong": _compile(m["strong"]), "weak": _compile(m["weak"]),
        "quick": _compile(m["quick"]), "verbs": _compile(m["quick_verbs"]), "code": _compile(m["code_words"]), "advice": _compile(m["advice"]), "work": _compile(m["work"]), "multi": _compile(m["multi_file"]),
        "stop": frozenset(m["stopwords"]),
        # (?![a-z0-9_]) instead of \b: CJK words are not separated by word boundaries
        "follow": re.compile(r"^\s*(?:" + "|".join(p.pattern for p in follow) + r")(?![a-z0-9_]).{0,40}$")
                  if follow else None,
    }


def _pack():
    return pack(str(config.home() / "lang"))


OVERRIDES = {"!quick": "quick", "!builder": "builder",
             "!architect": "architect", "!direct": "direct"}


def normalise(text: str) -> str:
    out = []
    for c in unicodedata.normalize("NFKD", text.lower()):
        if unicodedata.combining(c) and out and out[-1].isascii() and out[-1].isalpha():
            continue  # accent on a Latin letter; marks on kana, Cyrillic... are kept
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def count_words(text: str) -> int:
    """Whitespace words, with CJK counted as about two characters per word."""
    return len(CJK.sub(" ", text).split()) + (len(CJK.findall(text)) + 1) // 2


def tokenize(text: str) -> list[str]:
    """Content words of a prompt, normalised. Used for kNN and learned weights."""
    text = normalise(text)
    stop = _pack()["stop"]
    words = re.findall(r"\w+", CJK.sub(" ", text))
    for run in CJK_RUN.findall(text):  # no spaces in CJK: character bigrams stand in for words
        words += [run[i:i + 2] for i in range(max(1, len(run) - 1))]
    return [w for w in words if (len(w) >= 3 or CJK.match(w)) and w not in stop and not w.isdigit()]


@dataclass
class Signals:
    strong: int = 0
    weak: int = 0
    quick: bool = False
    advice: bool = False  # asks how/why/what is best: needs judgment, never a lookup
    work: bool = False    # refactor / implement / create: more than a small edit
    multi: bool = False
    words: int = 0
    follow_up: bool = False
    override: str | None = None
    reasons: list = field(default_factory=list)


def _count(patterns, text) -> int:
    n = 0
    for p in patterns:
        try:
            if re.search(p, text):
                n += 1
        except re.error:
            continue  # a bad user-supplied pattern must never break routing
    return n


def signals(prompt: str, extra_strong=(), extra_weak=(), extra_quick=()) -> Signals:
    text = normalise(prompt).strip()
    s = Signals(words=count_words(text))
    p = _pack()
    for flag, tier in OVERRIDES.items():
        if text.startswith(flag):
            s.override = tier
            return s
    s.strong = _count(p["strong"] + list(extra_strong), text)
    s.weak = _count(p["weak"] + list(extra_weak), text)
    # Leading verbs like "show" or "list" are a lookup only when the prompt is tiny or points at
    # code; otherwise they ask for a feature ("show an error toast when the upload fails").
    anchored = bool(ANCHOR.search(prompt)) or _count(p["code"], text) > 0 or s.words <= 5
    s.quick = _count(p["quick"] + list(extra_quick), text) > 0 or (anchored and _count(p["verbs"], text) > 0)
    s.advice = _count(p["advice"], text) > 0
    s.work = _count(p["work"], text) > 0
    s.multi = _count(p["multi"], text) > 0
    s.follow_up = bool(p["follow"] and p["follow"].match(text)) and s.words <= 6 and not s.strong
    return s


def base_score(s: Signals) -> tuple[float, list[str]]:
    """Rule-based complexity score. >=3 architect, <=-2 quick, else builder."""
    pts, why = 0.0, []
    if s.strong:
        pts += 3 + min(s.strong - 1, 2)
        why.append(f"{s.strong} strong")
    if s.weak:
        pts += min(s.weak - 1, 3)  # one weak signal is neutral, four reach the top tier
        why.append(f"{s.weak} weak")
    if s.multi:
        pts += 1
        why.append("multi-file")
    if s.words > 120:
        pts += 1
        why.append("long")
    if s.quick and not (s.strong or s.advice or s.work) and s.words <= 20:
        pts -= 3  # a lookup stays simple even if it mentions "auth"
        why.append("lookup/small edit")
    if s.words < 12 and not s.strong and s.weak < 3:
        pts -= 1
        why.append("short")
    return pts, why


def tier_from_score(score: float) -> str:
    if score >= 3:
        return "architect"
    if score <= -2:
        return "quick"
    return "builder"
