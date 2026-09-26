"""
Canonical institution ids and name matching.

Every institution has ONE canonical id, taken from the data that introduces it: ASSIST agreement
files ("chabot", "csueb"), the AP campus list ("berkeley", "los_angeles") and the college catalogs.
Any spelling a person or a dropdown might use resolves to that id:

    "California State University, East Bay" | "Cal State East Bay" | "CSU East Bay" | "CSUEB" -> csueb
    "Chabot College" | "Chabot" | "chabot"                                                     -> chabot

Aliases are derived from each official name by rule (name_variants), so a new ASSIST file needs
no code change. Matching ignores case, accents and punctuation. A name nobody registered resolves
to a private slug of itself, so it only matches the same spelling.
"""

from __future__ import annotations

import re
import unicodedata

STOP_WORDS = {"of", "the", "at", "and"}


def normalize(text: str) -> str:
    """'California State University, East Bay' -> 'california state university east bay'."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold().replace("&", " and ")))


def _initials(words: list[str]) -> str:
    return "".join(w[0] for w in words if w not in STOP_WORDS)


def name_variants(name: str) -> set[str]:
    """Everyday spellings of an official name, normalized:
    'California State University, East Bay' -> cal state east bay, csu east bay, csueb, ...
    'UC Berkeley' -> university of california berkeley, ucb    'Chabot College' -> chabot"""
    n = normalize(name)
    words = n.split()
    out = {n}
    if m := re.fullmatch(r"(?:california state university|cal state university|cal state|csu) (.+)", n):
        campus = m[1]
        out |= {f"california state university {campus}", f"cal state university {campus}",
                f"cal state {campus}", f"csu {campus}", "csu" + _initials(campus.split()),
                f"{campus} state", f"{campus} state university"}
    if m := re.fullmatch(r"(?:university of california|uc) (.+)", n):
        campus = m[1]
        out |= {f"university of california {campus}", f"uc {campus}", "uc" + _initials(campus.split())}
    if len(words) > 1 and words[-1] == "college":
        out.add(" ".join(words[:-1]))                           # "Chabot College" -> "chabot"
        if len(words) > 2 and words[-2] == "community":
            out.add(" ".join(words[:-2]))
    if sum(w not in STOP_WORDS for w in words) >= 3:
        out.add(_initials(words))                               # "San Jose State University" -> "sjsu"
    return out


class InstitutionRegistry:
    def __init__(self) -> None:
        self._exact: dict[str, str] = {}             # normalized id / official name -> id
        self._variants: dict[str, set[str]] = {}     # normalized variant -> ids that produce it
        self._names: dict[str, str] = {}             # id -> official name

    def register(self, inst_id: str, name: str | None = None) -> str:
        """Record an institution. The first registration of an id or name wins."""
        self._exact.setdefault(normalize(inst_id), inst_id)
        if name:
            self._names.setdefault(inst_id, name)
            self._exact.setdefault(normalize(name), inst_id)
            for v in name_variants(name):
                self._variants.setdefault(v, set()).add(inst_id)
        return inst_id

    def resolve(self, text: str) -> str:
        """Canonical id for any spelling. A variant two institutions share is ambiguous and not used."""
        key = normalize(text)
        if key in self._exact:
            return self._exact[key]
        ids = self._variants.get(key, set())
        return next(iter(ids)) if len(ids) == 1 else "~" + key.replace(" ", "-")

    def same(self, a: str, b: str) -> bool:
        return self.resolve(a) == self.resolve(b)

    def name(self, inst_id: str) -> str | None:
        return self._names.get(inst_id)


REGISTRY = InstitutionRegistry()
