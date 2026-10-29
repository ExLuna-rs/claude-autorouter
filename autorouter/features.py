"""Prompt normalisation, tokenisation and rule-based complexity signals.

Patterns match English and French. Text is lower-cased and stripped of
accents before matching, so patterns are written without accents.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Strong signals justify the top tier on their own.
STRONG = [
    r"\barchitectur", r"\bdesign\b", r"\bconcev", r"\bconcoi", r"\bconception\b",
    r"\baudit", r"\bvuln", r"\bcve\b", r"\bfaille", r"\bpentest", r"\bthreat model",
    r"\bmigrat", r"\bmigre[rz]?\b",
    r"\brefactor\w*\s+(the\s+)?(whole|entire|all)\b",
    r"\brefacto\w*\s+(global|complet|tout|entier)",
    r"\brace condition", r"\bdeadlock", r"\bmemory leak", r"\bfuite memoire",
    r"\bintermittent", r"\baleatoire", r"\brandom(ly)?\s+(crash|fail)",
    r"\bflaky\b",
    r"\bwhy\b.*\b(crash|fail|broken|hang|doesn'?t work)",
    r"\bpourquoi\b.*\b(plante|crash|bug|marche pas|fonctionne pas|bloque)",
    r"\btrade-?offs?\b", r"\bcompar\w*\b.*\b(approach|approche|solution|option|architecture)",
    r"\bcryptograph", r"\bdistributed\b", r"\bdistribue", r"\bscalab",
    r"\broot cause\b", r"\bcause racine\b",
]

# Weak signals add weight but never decide on their own.
WEAK = [
    r"\bsecurit", r"\bsecurity\b", r"\bauthenti", r"\bauth\b", r"\boauth",
    r"\blicen[cs]", r"\bconcurren", r"\bthread", r"\basync\b", r"\bperformance",
    r"\boptimi[sz]", r"\bplan\b", r"\bdebug", r"\bbug\b", r"\bcache\b",
    r"\bdatabase\b", r"\bschema\b", r"\bbase de donnees\b", r"\bapi\b",
]

# Lookups and small edits.
QUICK = [
    r"^\s*(where|which file|ou\b|quel(le)?s? fichier)",
    r"^\s*(find|search|grep|list|show|locate|trouve|cherche|liste|montre|localise)\b",
    r"^\s*(read|open|cat|lis|ouvre|affiche)\b",
    r"^\s*(what is|what's|what does|c'?est quoi|qu'?est-ce que?)\b",
    r"^\s*(how many|combien)\b",
    r"\brename\b", r"\brenomm", r"\btypos?\b", r"\bfaute de frappe",
    r"\bformat(ting)?\b", r"\breformat", r"\bdocstring", r"\bcomment(aire)?s?\b",
    r"\bmissing import", r"\bimport manquant",
]

MULTI_FILE = [
    r"\ball files\b", r"\bevery file\b", r"\bmultiple files\b", r"\bcodebase\b",
    r"\bwhole (repo|project)\b", r"\bentire (repo|project)\b",
    r"\btous les fichiers\b", r"\bplusieurs fichiers\b", r"\bprojet entier\b",
    r"\btout le (repo|projet|code)\b",
]

# Conversational follow-ups: the orchestrator handles these itself.
FOLLOW_UP = re.compile(
    r"^\s*(ok(ay)?|yes|yep|no|nope|go( ahead)?|continue|proceed|thanks?( you)?|"
    r"oui|non|vas-?y|go on|continue|merci|parfait|top|d'?accord|ca marche|"
    r"lgtm|sounds good|do it|fais-?le|c'?est bon)\b.{0,40}$")

OVERRIDES = {"!quick": "quick", "!builder": "builder",
             "!architect": "architect", "!direct": "direct"}

STOPWORDS = set("""
a an the and or but if then else of to in on at by for with from into onto is are was were be been
it its this that these those i you he she we they me my your our their do does did done can could
should would will just so as not no yes please also there here than too very
le la les un une des du de d l et ou mais si alors dans sur au aux par pour avec sans est sont etre
ce cet cette ces je tu il elle nous vous ils elles mon ma mes ton ta tes son sa ses notre votre leur
que qui quoi ne pas plus moins tres aussi y en se sa fait faire peux peut
""".split())


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    """Content words of a prompt, normalised. Used for kNN and learned weights."""
    words = re.findall(r"[a-z0-9_]+", normalise(text))
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()]


@dataclass
class Signals:
    strong: int = 0
    weak: int = 0
    quick: bool = False
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
    s = Signals(words=len(text.split()))
    for flag, tier in OVERRIDES.items():
        if text.startswith(flag):
            s.override = tier
            return s
    s.strong = _count(list(STRONG) + list(extra_strong), text)
    s.weak = _count(list(WEAK) + list(extra_weak), text)
    s.quick = _count(list(QUICK) + list(extra_quick), text) > 0
    s.multi = _count(MULTI_FILE, text) > 0
    s.follow_up = bool(FOLLOW_UP.match(text)) and s.words <= 6 and not s.strong
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
    if s.quick and not s.strong:
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
