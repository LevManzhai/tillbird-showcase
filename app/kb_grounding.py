"""Deterministic post-drafting grounding check for app.demo_kb's drafted
knowledge base — no API calls, runs entirely in code.

Confirmed live (2026-10-27): Wag & Whistle's drafted `shipping` text named
"Royal Mail Tracked 48" and stated "2-3 working days" for standard delivery
and "1-2 working days" for local delivery, when the real site names no
carrier at all and actually says "3 to 5 working days" and "1 to 3 working
days" respectively — plus an unstated "regardless of value" qualifier on a
free-delivery claim. The drafting prompt in app.demo_kb already said "only
use facts explicitly stated in the source text" when this was drafted, so
a prompt instruction alone isn't reliable enough on its own — this module
re-checks every number, price, time range, percentage, carrier name and
unconditional-scope qualifier in the drafted text against the raw scraped
source text it was supposedly drafted from, and strips out anything that
isn't actually there.

Removal happens at SENTENCE granularity (not just deleting the matched
number), so the rest of a sentence's real, grounded content survives
intact rather than being left as a grammatically broken fragment — "we
ship via Royal Mail Tracked 48, usually 2-3 working days" loses the whole
sentence, not just the numbers, if any claim in it doesn't check out.
Each removed sentence is reported as an "ungrounded claim removed: ..."
entry in the draft's `missing` list, the same list a human already checks
(printed by scripts/create_demo.py and scripts/batch_demos.py's quality
check) — so a stripped claim gets noticed, not silently dropped.
"""
import re

# Carrier names we've seen UK Shopify stores actually use — a name here is
# only ever ACCEPTED if the source text itself names it; this list exists
# purely to recognise a claim worth checking, not to approve any of them.
CARRIER_NAMES = ["Royal Mail", "DPD", "Evri", "Hermes", "UPS", "DHL", "Parcelforce", "Yodel"]
_CARRIER_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in CARRIER_NAMES) + r")\b", re.IGNORECASE)
# Royal Mail's named tracked-service tiers — checked separately from the
# carrier name itself, since a source can name "Royal Mail" without ever
# specifying "Tracked 48" (confirmed live: the real failure case named
# neither, but a store naming the carrier without the specific service
# tier is also a realistic, separately-wrong claim).
_TRACKED_SERVICE_RE = re.compile(r"\btracked\s?(24|48)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"£\s?\d+(?:\.\d{1,2})?")
_PERCENT_RE = re.compile(r"\d+\s?%")
# Separator accepts "to"/"until" as well as a hyphen/en-dash/em-dash, since
# a real page commonly writes "3 to 5 working days" while drafted prose
# commonly writes "3-5 working days" for the exact same fact — this is a
# textual-form difference, not a different claim, so it must not be
# flagged as ungrounded. Two DIFFERENT numbers (drafted "2-3" vs source
# "3-5") still correctly fail, since the digit pairs themselves differ.
_DAY_RANGE_RE = re.compile(
    r"\b(\d+)\s*(?:-|–|—|to|until)\s*(\d+)\s*(?:working|business)?\s*days?\b", re.IGNORECASE
)
_SINGLE_DAY_RE = re.compile(r"\b(\d+)\s*(?:working|business)?\s*days?\b", re.IGNORECASE)
_HOUR_RE = re.compile(r"\b(\d+)\s*hours?\b", re.IGNORECASE)
# Phrases asserting an unconditional scope are exactly the kind of extra
# qualifier a draft can pad in that sounds plausible after a real fact but
# isn't actually stated — confirmed live: "...get free delivery on every
# subscription box regardless of value" when the source never qualified
# its free-delivery claim that way at all.
_UNCONDITIONAL_SCOPE_RE = re.compile(
    r"regardless of (?:value|order value|amount|size|weight)"
    r"|no matter (?:the|how much|what)"
    r"|always free"
    r"|on every (?:single )?(?:order|box|subscription)",
    re.IGNORECASE,
)

FIELDS_TO_GROUND = ["shipping", "returns", "sizing", "brand_claims"]


def _normalise(text: str) -> str:
    """£, whitespace and hyphen/en-dash/em-dash normalised so a literal
    substring/regex check isn't fooled by cosmetic formatting differences
    — never used to treat two genuinely different claims as the same one.
    """
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"£\s+", "£", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _split_sentences(text: str) -> list[str]:
    """Good enough for the short, plain prose draft_knowledge_base produces
    (its own prompt asks for "flowing prose paragraphs... under ~150
    words") — not a general-purpose sentence splitter.
    """
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


class _Claim:
    def __init__(self, label: str, grounded: bool):
        self.label = label
        self.grounded = grounded


# Day-figure claims need more than "does this number appear somewhere in
# the source" — confirmed live: a store's real FAQ states "1 to 2 working
# days" for DISPATCH and, separately, "1 to 3 working days" for LOCAL
# delivery; a drafted claim of "1-2 working days" for local delivery is
# WRONG (it's actually 1-3), but a blind whole-source search for "1 to 2
# working days" would find the real dispatch sentence and wrongly call it
# grounded. These generic words are excluded when comparing a claim
# sentence's own topic words against the words near a candidate match in
# the source, so two sentences that only share boilerplate shipping
# vocabulary ("delivery", "working days", "dispatch") don't count as the
# same topic — they need to share something more specific (e.g. "local",
# "Alfreton", "collection").
_GENERIC_CONTEXT_WORDS = {
    "the", "and", "or", "is", "are", "we", "you", "your", "our", "us", "to", "of", "in", "on", "for", "with",
    "within", "after", "before", "usually", "working", "business", "days", "day", "delivery", "dispatch",
    "dispatched", "arrives", "arriving", "arrive", "orders", "order", "standard", "takes", "take", "isn",
    "though", "tracked", "free", "once", "that", "this", "costs", "cost", "sent", "via", "from", "into",
    "such", "than", "also", "will", "can", "may",
}


def _stem(word: str) -> str:
    """Light, deliberately crude suffix-stripping so "packed"/"pack" or
    "arriving"/"arrival"-style near-matches between a drafted sentence and
    its real source sentence still count as shared vocabulary — confirmed
    live: a draft's "Orders are packed and dispatched..." needed to match
    the source's "We pack and send orders...", and plain exact-string set
    overlap missed that pairing entirely. Not a real stemmer (no attempt
    at correctness beyond this), just enough to stop a verb-tense mismatch
    from making two sentences about the same fact look unrelated.
    """
    for suffix in ("ing", "ed", "es"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    if word.endswith("s") and not word.endswith("ss") and len(word) - 1 >= 3:
        return word[:-1]
    return word


def _context_words(text: str) -> set[str]:
    words = re.findall(r"[a-z]{4,}", text.lower())
    return {_stem(w) for w in words if w not in _GENERIC_CONTEXT_WORDS}


def _candidate_windows(source_sentences: list[str]) -> list[str]:
    """Every individual source sentence, PLUS every run of 2 and 3
    adjacent sentences concatenated — confirmed live, twice: Babyblooms'
    real source states a USA delivery fact across TWO sentences split by
    a full stop ("...fixed cost of $29.50 USD. Orders placed before
    3.00pm (GMT) Mon-Fri will be shipped same day with estimated delivery
    within 7-10 working days."), and Dorwest's real source spreads a lost-
    parcel fact across THREE ("Occasionally goods may be lost in the post
    and we will send a replacement order when this occurs. However, Royal
    Mail does not accept that a delivery is lost until 7 working days
    following dispatch. We therefore only send replacements after this
    period has elapsed."). A natural paraphrase routinely merges 2-3
    adjacent source sentences into one drafted sentence; scored one
    source sentence at a time, the half/third actually containing the
    digit can lose on shared vocabulary to a neighbour that doesn't.
    """
    windows = list(source_sentences)
    for size in (2, 3):
        for i in range(len(source_sentences) - size + 1):
            windows.append(" ".join(source_sentences[i : i + size]))
    return windows


def _day_claim_grounded(day_fact_re, sentence_words: set[str], source_sentences: list[str], target) -> bool:
    """True if ANY candidate window (see _candidate_windows) that shares
    at least one specific (non-generic) word with the claim's own
    sentence contains a `day_fact_re` match whose digits equal `target`
    (a (lo, hi) tuple for a range, or a single digit string for a plain
    figure).

    Deliberately permissive across MULTIPLE positively-scoring windows,
    not just the single highest-scoring one — confirmed live (Dorwest,
    Hawthorn Handmade): a merged/paraphrased drafted sentence can combine
    vocabulary from one source sentence with a digit that actually lives
    in a NEIGHBOURING one, and no single window may dominate the score;
    requiring one global best match was too fragile. Any window scoring
    above zero is accepted as plausibly on-topic, so the exact digit only
    needs to show up in ONE of them.

    Requires at least one shared SPECIFIC word to accept a match from a
    positively-scoring window (not just "the source contains this digit
    pattern somewhere") — that's what distinguishes two different facts
    stated nearby (a store's FAQ states "1 to 2 working days" for dispatch
    and, separately, "1 to 3 working days" for local delivery; a drafted
    LOCAL-delivery claim of "1-2 working days" must still fail, because no
    local-delivery-relevant window contains that exact pair).

    Only when the claim sentence has NO specific vocabulary at ALL (every
    window scores 0 — confirmed live, Oh My Melt: "We dispatch within 1-2
    working days." sits right next to "...arrive in 4-7 working days...
    Royal Mail then takes over...3-5 working days", three different real
    figures for three different sub-facts, none distinguishable by
    vocabulary alone) does this fall back to accepting a match from ANY
    zero-scoring window, not just the first — a short, undistinguishing
    claim is still real quoted text as long as SOME real figure in the
    source has these exact digits, even if which specific fact it's
    quoting can't be told apart. This fallback only ever fires when there
    was nothing to disambiguate with in the first place, so it can't
    override a positive-score mismatch found elsewhere.
    """

    target = tuple(target)

    def matches_target(m) -> bool:
        return m.groups() == target

    any_positive_score = False
    zero_score_match_found = False
    for window in _candidate_windows(source_sentences):
        score = len(sentence_words & _context_words(window))
        matches = list(day_fact_re.finditer(window))
        if not matches:
            continue
        if score > 0:
            any_positive_score = True
            if any(matches_target(m) for m in matches):
                return True
        elif any(matches_target(m) for m in matches):
            zero_score_match_found = True

    # The claim sentence had no specific vocabulary to disambiguate WHICH
    # nearby figure it's about (confirmed live, Oh My Melt: "We dispatch
    # within 1-2 working days." is a short, fully-generic sentence sitting
    # next to "...arrive in 4-7 working days...Royal Mail then takes
    # over...3-5 working days" — three different real figures for three
    # different sub-facts, dispatch/total/carrier, none distinguishable by
    # vocabulary alone). Rather than arbitrarily picking whichever window
    # happens to be first, accept the claim if its digits match ANY
    # zero-score match anywhere — a short, undistinguishing claim is still
    # real quoted text as long as SOME real figure in the source has these
    # exact digits, even if we can't tell which fact it's quoting.
    return not any_positive_score and zero_score_match_found


def _find_claims(sentence: str, source_norm: str, source_sentences: list[str]) -> list[_Claim]:
    claims: list[_Claim] = []
    sentence_words = _context_words(sentence)

    for m in _CARRIER_RE.finditer(sentence):
        claims.append(_Claim(f'carrier "{m.group(1)}"', m.group(1).lower() in source_norm))

    for m in _TRACKED_SERVICE_RE.finditer(sentence):
        claims.append(_Claim(f'named service "{m.group(0)}"', _normalise(m.group(0)) in source_norm))

    for m in _PRICE_RE.finditer(sentence):
        claims.append(_Claim(f'price "{m.group(0)}"', _normalise(m.group(0)) in source_norm))

    for m in _PERCENT_RE.finditer(sentence):
        claims.append(_Claim(f'percentage "{m.group(0)}"', _normalise(m.group(0)) in source_norm))

    consumed_spans = []
    for m in _DAY_RANGE_RE.finditer(sentence):
        lo, hi = m.group(1), m.group(2)
        consumed_spans.append(m.span())
        grounded = _day_claim_grounded(_DAY_RANGE_RE, sentence_words, source_sentences, (lo, hi))
        claims.append(_Claim(f'time range "{m.group(0)}"', grounded))

    for m in _SINGLE_DAY_RE.finditer(sentence):
        if any(start <= m.start() < end for start, end in consumed_spans):
            continue  # already covered by a day-range match above
        n = m.group(1)
        grounded = _day_claim_grounded(_SINGLE_DAY_RE, sentence_words, source_sentences, (n,))
        claims.append(_Claim(f'time figure "{m.group(0)}"', grounded))

    for m in _HOUR_RE.finditer(sentence):
        n = m.group(1)
        grounded = bool(re.search(rf"\b{re.escape(n)}\s*hours?\b", source_norm, re.IGNORECASE))
        claims.append(_Claim(f'time figure "{m.group(0)}"', grounded))

    for m in _UNCONDITIONAL_SCOPE_RE.finditer(sentence):
        claims.append(_Claim(f'unconditional claim "{m.group(0)}"', _normalise(m.group(0)) in source_norm))

    return claims


def ground_field(field_name: str, field_text: str, source_text: str) -> tuple[str, list[str]]:
    """Checks every claim in `field_text` against `source_text`. Returns
    (cleaned_text, removed_notes) — cleaned_text keeps only sentences where
    every claim checked out; removed_notes is a list of human-readable
    "ungrounded claim removed: ..." strings, one per removed claim, naming
    which field and quoting the sentence it was removed from.
    """
    if not field_text:
        return field_text, []

    source_norm = _normalise(source_text)
    source_sentences = _split_sentences(source_norm)
    notes = []
    kept_sentences = []
    for sentence in _split_sentences(field_text):
        claims = _find_claims(sentence, source_norm, source_sentences)
        ungrounded = [c for c in claims if not c.grounded]
        if ungrounded:
            for c in ungrounded:
                notes.append(
                    f'ungrounded claim removed from {field_name}: {c.label} — sentence dropped: '
                    f'"{sentence.strip()}"'
                )
            continue
        kept_sentences.append(sentence)
    return " ".join(kept_sentences), notes


def ground_knowledge_base(draft: dict, scraped_pages: dict) -> dict:
    """Re-checks `draft`'s shipping/returns/sizing/brand_claims text and
    every FAQ answer against the combined raw text of `scraped_pages` (the
    same page dict passed to draft_knowledge_base), stripping any sentence
    containing a claim not actually found there. Returns a new dict with
    the same shape as `draft` — doesn't mutate the input.
    """
    source_text = "\n\n".join(scraped_pages.values())
    cleaned = dict(draft)
    missing = list(draft.get("missing") or [])

    for field in FIELDS_TO_GROUND:
        text = draft.get(field) or ""
        new_text, notes = ground_field(field, text, source_text)
        cleaned[field] = new_text
        missing.extend(notes)

    new_faq = []
    for entry in draft.get("faq") or []:
        question = entry.get("question", "")
        answer = entry.get("answer", "")
        new_answer, notes = ground_field(f'FAQ answer ("{question}")', answer, source_text)
        missing.extend(notes)
        if new_answer:
            new_faq.append({**entry, "answer": new_answer})
        elif answer:
            missing.append(f'FAQ question dropped entirely (its whole answer was ungrounded): "{question}"')
    cleaned["faq"] = new_faq

    cleaned["missing"] = missing
    return cleaned
