from app.kb_grounding import ground_field, ground_knowledge_base


# ---------------------------------------------------------------------------
# Isolated claim-type checks
# ---------------------------------------------------------------------------


def test_grounded_sentence_survives_unchanged():
    text = "UK delivery is a flat £4.50, and free on orders over £35."
    cleaned, notes = ground_field("shipping", text, text)
    assert cleaned == text
    assert notes == []


def test_ungrounded_price_is_removed():
    cleaned, notes = ground_field("shipping", "Standard delivery costs £6.99.", "We ship within the UK.")
    assert cleaned == ""
    assert any('price "£6.99"' in n for n in notes)


def test_ungrounded_percentage_is_removed():
    cleaned, notes = ground_field("returns", "We charge a 15% restocking fee.", "Returns accepted within 30 days.")
    assert cleaned == ""
    assert any('percentage "15%"' in n for n in notes)


def test_ungrounded_carrier_is_removed():
    cleaned, notes = ground_field(
        "shipping", "We ship everything via DPD.", "We aim to dispatch orders within 2 working days."
    )
    assert cleaned == ""
    assert any('carrier "DPD"' in n for n in notes)


def test_day_range_hyphen_form_matches_source_written_with_to():
    """A real source commonly writes "3 to 5 working days" while drafted
    prose commonly writes "3-5 working days" for the exact SAME fact — this
    is a textual-form difference, not a different claim, and must not be
    flagged.
    """
    cleaned, notes = ground_field(
        "shipping", "Standard delivery takes 3-5 working days.", "Delivery usually takes 3 to 5 working days."
    )
    assert cleaned == "Standard delivery takes 3-5 working days."
    assert notes == []


def test_day_range_with_different_numbers_is_still_caught():
    """The real regression: drafted "2-3 working days" when the source
    actually says "3 to 5 working days" — different numbers, must fail
    even though both are hyphen-vs-"to" range claims about delivery time.
    """
    cleaned, notes = ground_field(
        "shipping", "Standard delivery takes 2-3 working days.", "Delivery usually takes 3 to 5 working days."
    )
    assert cleaned == ""
    assert any('time range "2-3 working days"' in n for n in notes)


def test_multiple_day_figures_in_one_source_sentence_are_all_recognised():
    """Regression, caught live on 2026-10-28 (Hawthorn Handmade): a single
    real source sentence states TWO distinct day figures at once — "...2-5
    working days, though they can occasionally take up to 7 days" — and a
    drafted sentence repeating both got the "7 days" half wrongly flagged
    as ungrounded, because the best-matching-sentence search stopped at
    the FIRST day-figure match found in the winning sentence (the "5" from
    "2-5") without checking whether a LATER match in that same sentence
    (the "7") was the one actually being asked about. Every digit figure
    in the winning sentence must be checked, not just the first one found.
    """
    source = (
        "UK orders are sent 2nd class, or 1st class at busy times or if upgraded, and normally arrive "
        "in 2-5 working days, though they can occasionally take up to 7 days. International orders vary "
        "from 3-7 working days for most of Europe to 5-14 working days for the US and everywhere else."
    )
    drafted = (
        "Orders normally arrive in 2-5 working days, though they can occasionally take up to 7 days. "
        "International orders take 3-7 working days for Europe and 5-14 working days for the US."
    )
    cleaned, notes = ground_field("shipping", drafted, source)
    assert notes == []
    assert cleaned == drafted


def test_fact_split_across_two_adjacent_source_sentences_is_recognised():
    """Regression, caught live on 2026-10-28 (Babyblooms): the real source
    states a fact across two sentences split by a full stop — "...fixed
    cost of $29.50 USD. Orders placed before 3.00pm (GMT) Mon-Fri will be
    shipped same day with estimated delivery within 7-10 working days."
    — and a natural paraphrase merged both into ONE drafted sentence.
    Scored sentence-by-sentence alone, the FIRST half (price/USA/service
    name) scored higher on shared vocabulary than the SECOND half (which
    actually has the day figure), so the single-best-sentence search
    picked the wrong half and wrongly flagged a real, correct day figure
    as ungrounded. A 2-sentence window fixes this without breaking the
    "1-2 vs 1-3 for two different facts" discrimination (see
    test_day_range_with_different_numbers_is_still_caught).
    """
    source = (
        "Please visit www.example.com. Express International Delivery is available for USA at a fixed "
        "cost of $29.50 USD. Orders placed before 3.00pm (GMT) Mon-Fri will be shipped same day with "
        "estimated delivery within 7-10 working days. EU Express Delivery costs £19.95."
    )
    drafted = (
        "For the USA, visit www.example.com — Express International Delivery is available at a fixed "
        "cost of $29.50 USD, with orders placed before 3.00pm (GMT) Mon-Fri shipped same day and "
        "estimated delivery within 7-10 working days."
    )
    cleaned, notes = ground_field("shipping", drafted, source)
    assert notes == []
    assert cleaned == drafted


def test_fact_split_across_three_adjacent_source_sentences_is_recognised():
    """Regression, caught live on 2026-10-28 (Dorwest): a real FAQ answer
    spreads one fact across THREE sentences ("Occasionally goods may be
    lost in the post and we will send a replacement order when this
    occurs. However, Royal Mail does not accept that a delivery is lost
    until 7 working days following dispatch. We therefore only send
    replacements after this period has elapsed."), and a natural
    paraphrase merged all three into one drafted sentence. The 2-sentence
    window fix wasn't enough — the digit-bearing clause is the MIDDLE of
    three sentences, so neither adjacent pair alone had enough shared
    vocabulary with the merged draft to win outright. Fixed by accepting
    ANY positively-scoring window's match, not just a single global best.
    """
    source = (
        "Occasionally goods may be lost in the post and we will send a replacement order when this occurs. "
        "However, Royal Mail does not accept that a delivery is lost until 7 working days following dispatch. "
        "We therefore only send replacements after this period has elapsed."
    )
    drafted = (
        "We will send a replacement order if goods are lost in the post, but Royal Mail does not accept a "
        "delivery is lost until 7 working days following dispatch, so replacements are only sent after this "
        "period has elapsed."
    )
    cleaned, notes = ground_field("FAQ answer", drafted, source)
    assert notes == []
    assert cleaned == drafted


def test_short_generic_claim_near_multiple_real_figures_is_accepted():
    """Regression, caught live on 2026-10-28 (Oh My Melt): a short, fully
    generic drafted sentence ("We dispatch within 1-2 working days.") sits
    right next to TWO OTHER real day-figures for different sub-facts
    ("...arrive in 4-7 working days...Royal Mail then takes over...3-5
    working days"). Every word in the claim sentence is generic
    (dispatch/working/days), so there's no vocabulary to disambiguate
    which of the three nearby figures it's quoting — but "1-2" genuinely
    IS one of the three real figures stated, just picked arbitrarily
    wrong by a naive "first window wins" tie-break. The fallback for a
    fully-generic claim must accept a match from ANY zero-scoring window,
    not just whichever happens to be first.
    """
    source = (
        "Orders usually arrive in 4-7 working days. As a small team that hand-pours everything with "
        "care, we dispatch within 1-2 working days. Royal Mail then takes over, with standard delivery "
        "usually taking 3-5 working days."
    )
    drafted = "We dispatch within 1-2 working days."
    cleaned, notes = ground_field("shipping", drafted, source)
    assert notes == []
    assert cleaned == drafted


def test_short_generic_claim_with_no_matching_figure_anywhere_is_still_rejected():
    """Sanity check against over-permissiveness: the fallback above must
    still reject a genuinely invented figure that doesn't match ANY real
    figure in the source, even when the claim sentence is just as short
    and generic as the accepted case above.
    """
    source = "Orders usually arrive in 4-7 working days. We dispatch within 1-2 working days."
    drafted = "We dispatch within 9-14 working days."
    cleaned, notes = ground_field("shipping", drafted, source)
    assert notes != []
    assert cleaned == ""


def test_en_dash_in_drafted_range_is_normalised_not_flagged():
    cleaned, notes = ground_field(
        "shipping", "Dispatch is usually 1–2 working days.", "We dispatch orders within 1 to 2 working days."
    )
    assert cleaned == "Dispatch is usually 1–2 working days."
    assert notes == []


def test_unconditional_scope_phrase_without_source_support_is_removed():
    cleaned, notes = ground_field(
        "shipping",
        "Subscribers get free delivery on every subscription box regardless of value.",
        "Free UK delivery on orders over £35.",
    )
    assert cleaned == ""
    assert any("unconditional claim" in n for n in notes)


def test_sentence_with_one_bad_claim_drops_the_whole_sentence_not_just_the_number():
    """A sentence mixing one grounded fact with one ungrounded claim is
    removed in full — a half-sentence like "we ship via , usually working
    days" would read as broken, and the grounded £4.50 fact can be
    re-asserted in a future draft once it's not bundled with an invented
    carrier name.
    """
    cleaned, notes = ground_field(
        "shipping",
        "Standard Delivery costs £4.50, sent via Royal Mail Tracked 48.",
        "UK delivery is a flat £4.50.",
    )
    assert cleaned == ""
    assert any('carrier "Royal Mail"' in n for n in notes)
    assert any('named service "Tracked 48"' in n for n in notes)


# ---------------------------------------------------------------------------
# Regression: the exact real Wag & Whistle failure (2026-10-27)
# ---------------------------------------------------------------------------

WAG_AND_WHISTLE_BAD_DRAFT_SHIPPING = (
    "We deliver within the United Kingdom only, not internationally. "
    "Orders are packed and dispatched within 1-2 business days. "
    "Standard Delivery costs £4.50, sent via Royal Mail Tracked 48 and usually arriving 2-3 working days "
    "after dispatch. "
    "If you're within a 6-mile radius of Alfreton, Derbyshire, a free Local Delivery option appears at "
    "checkout, usually arriving within 1-2 working days, though it isn't tracked. "
    "Free UK delivery applies automatically on orders of £35 or more, and Join the Pack subscribers get "
    "free delivery on every subscription box regardless of value."
)

WAG_AND_WHISTLE_REAL_SOURCE_TEXT = (
    "Do you ship outside the UK? Not at the moment. We deliver within the United Kingdom only, and we "
    "don't ship internationally. How much is delivery? UK delivery is a flat £4.50, and free on orders "
    "over £35. Standard delivery usually takes 3 to 5 working days from dispatch. How quickly do you "
    "dispatch orders? We pack and send orders within 1 to 2 working days. Do you deliver locally? Yes, "
    "and it's free. If you're within six miles of us in Alfreton you can choose local delivery at "
    "checkout, usually within 1 to 3 working days. We bring it ourselves, so there's no courier and no "
    "tracking."
)


def test_wag_and_whistle_regression_catches_every_real_ungrounded_claim():
    cleaned, notes = ground_field("shipping", WAG_AND_WHISTLE_BAD_DRAFT_SHIPPING, WAG_AND_WHISTLE_REAL_SOURCE_TEXT)

    # The four specific ungrounded claims from the real incident must all
    # be caught:
    assert any("Royal Mail" in n for n in notes), "carrier name must be caught"
    assert any("Tracked 48" in n for n in notes), "named service must be caught"
    assert any("2-3 working days" in n for n in notes), "wrong standard-delivery range must be caught"
    assert any("1-2 working days" in n for n in notes), "wrong local-delivery range must be caught"
    assert any("regardless of value" in n for n in notes), "unconditional subscriber claim must be caught"

    # None of the invented specifics survive into the cleaned text:
    for bad in ("Royal Mail", "Tracked 48", "2-3 working days", "regardless of value"):
        assert bad not in cleaned

    # The two genuinely grounded sentences DO survive:
    assert "We deliver within the United Kingdom only, not internationally." in cleaned
    assert "Orders are packed and dispatched within 1-2 business days." in cleaned


# ---------------------------------------------------------------------------
# ground_knowledge_base — full draft dict + FAQ handling
# ---------------------------------------------------------------------------


def test_ground_knowledge_base_cleans_every_field_and_collects_missing_notes():
    draft = {
        "tone": "Warm.",
        "shipping": WAG_AND_WHISTLE_BAD_DRAFT_SHIPPING,
        "returns": "You can return items within 14 days.",
        "sizing": "",
        "brand_claims": "All treats are grain-free.",
        "faq": [{"question": "How much is delivery?", "answer": "Delivery costs £6.99 via Evri."}],
        "missing": ["Carrier not stated anywhere."],
    }
    scraped_pages = {
        "faq": WAG_AND_WHISTLE_REAL_SOURCE_TEXT + " All treats are grain-free. Returns accepted within 14 days."
    }

    cleaned = ground_knowledge_base(draft, scraped_pages)

    assert "Royal Mail" not in cleaned["shipping"]
    assert cleaned["returns"] == "You can return items within 14 days."
    assert cleaned["brand_claims"] == "All treats are grain-free."
    # The ungrounded FAQ answer (wrong price + wrong carrier) is dropped
    # entirely, since its whole answer had nothing left after grounding.
    assert cleaned["faq"] == []
    # The original pre-existing missing note survives alongside the new ones:
    assert "Carrier not stated anywhere." in cleaned["missing"]
    assert any("Royal Mail" in n for n in cleaned["missing"])
    assert any("FAQ question dropped entirely" in n for n in cleaned["missing"])


def test_ground_knowledge_base_does_not_mutate_the_input_draft():
    draft = {
        "tone": "Warm.",
        "shipping": "We ship via DPD.",
        "returns": "",
        "sizing": "",
        "brand_claims": "",
        "faq": [],
        "missing": [],
    }
    original_shipping = draft["shipping"]

    ground_knowledge_base(draft, {"faq": "We dispatch within 2 working days."})

    assert draft["shipping"] == original_shipping
    assert draft["missing"] == []


def test_ground_knowledge_base_passes_through_a_fully_grounded_draft_unchanged():
    """Sanity check against false positives — a clean, accurate draft must
    not lose any content or gain spurious missing notes.
    """
    source = "UK delivery is a flat £4.50. We pack and send orders within 1 to 2 working days."
    draft = {
        "tone": "Warm.",
        "shipping": "UK delivery is a flat £4.50. We pack and send orders within 1-2 working days.",
        "returns": "",
        "sizing": "",
        "brand_claims": "",
        "faq": [],
        "missing": [],
    }
    cleaned = ground_knowledge_base(draft, {"delivery_info": source})
    assert cleaned["shipping"] == draft["shipping"]
    assert cleaned["missing"] == []
