"""Toxicity checks for the complaint (input) and the recommendation (output).

A word and phrase list, not a classifier: it needs no model download, costs no LLM call
(the free tier allows about 4 a minute and the agents need them), and every decision can be
explained by the term that matched. The trade-off is recall: misspelt or novel abuse is
missed. See docs/decisions.md D-025.

Categories and what the input guardrail does with each:
- profanity, insult: masked ("f***") before the complaint reaches the model. An angry
  customer can still have a valid dispute, so the run goes on.
- threat: violence against staff or property. The run is not sent to the agents; it is
  escalated so an analyst (and, if needed, security) handles it.
- self_harm: the customer may be at risk. Escalated the same way, with a welfare reason.
On output any category is a violation (guardrails/output.py).

Fraud vocabulary is deliberately absent: "scam", "fraud", "fraudster", "cheated", "stolen"
and "hacked" are how customers describe the very events the service triages.
"""

import re
from dataclasses import dataclass, field

# Words matched on word boundaries, case-insensitive. Hindi/Hinglish abuse is included
# because the customers are in India.
_PROFANITY = [
    "fuck", "fucking", "fucked", "fucker", "motherfucker", "shit", "shitty", "bullshit",
    "bitch", "bastard", "asshole",
    "bhenchod", "behenchod", "madarchod", "chutiya", "chutiye", "bhosdike", "gaandu",
    "harami", "kamina", "kamine", "saala", "saale",
]
_INSULT = [
    "idiot", "idiots", "stupid", "moron", "morons", "imbecile", "useless idiots",
    "retard", "retarded", "scum",
]
_THREAT = [
    # A violent verb aimed at someone. "I will finish my KYC" and "I'll beat the deadline" are
    # not threats; "I will kill myself" is self-harm, not a threat.
    r"\b(?:i|we)(?:'ll| will| am going to| are going to| shall)\s+(?:kill|hurt|beat up|"
    r"attack|shoot|stab)\s+(?:you|him|her|them|everyone|someone|the (?:manager|staff|"
    r"officer|agent)s?|your (?:manager|staff|officers?|agents?|employees?))\b",
    r"\bkill\s+(?:you|him|her|them|the manager|your staff)\b",
    r"\b(?:burn|blow up|bomb|set fire to)\s+(?:down\s+)?(?:the|your|this)\s+(?:bank|branch|"
    r"office|atm)\b",
    r"\byou(?:'ll| will) (?:regret|pay for) (?:this|it)\b.*\b(?:hurt|kill|harm|violence)\b",
]
_SELF_HARM = [
    r"\b(?:kill|hurt|harm) myself\b", r"\b(?:end|take) my (?:own )?life\b",
    r"\bcommit suicide\b", r"\bsuicid(?:e|al)\b", r"\bno reason to live\b",
]


def _words(terms: list[str]) -> re.Pattern[str]:
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def _phrases(patterns: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


CATEGORIES: dict[str, re.Pattern[str]] = {
    "profanity": _words(_PROFANITY),
    "insult": _words(_INSULT),
    "threat": _phrases(_THREAT),
    "self_harm": _phrases(_SELF_HARM),
}
MASKED_CATEGORIES = ("profanity", "insult")
BLOCKING_CATEGORIES = ("threat", "self_harm")


@dataclass(frozen=True)
class ToxicityCheck:
    flagged: bool
    categories: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)  # what matched, lower-cased
    masked_text: str = ""

    @property
    def blocking(self) -> list[str]:
        return [c for c in self.categories if c in BLOCKING_CATEGORIES]


def _mask(match: re.Match[str]) -> str:
    word = match.group(0)
    return word[0] + "*" * (len(word) - 1)


def check_toxicity(text: str) -> ToxicityCheck:
    """Which categories occur in text, and the text with profanity and insults masked."""
    categories, terms = [], []
    for name, pattern in CATEGORIES.items():
        found = [m.group(0).lower() for m in pattern.finditer(text)]
        if found:
            categories.append(name)
            terms.extend(dict.fromkeys(found))
    masked = text
    for name in MASKED_CATEGORIES:
        if name in categories:
            masked = CATEGORIES[name].sub(_mask, masked)
    return ToxicityCheck(flagged=bool(categories), categories=categories, terms=terms,
                         masked_text=masked)
