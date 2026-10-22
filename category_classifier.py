"""
Shared 6-category classifier for the nationwide scrapers (imoti.net, alo.bg,
homes.bg, imot.bg, olx.bg, bazar.bg, imoti.bg). Every listing must end up in
exactly one of the 6 categories the frontend already uses as its type-filter
buckets (index.html's TYPE_FILTER_BUCKETS / sync_to_supabase.py's
CATEGORY_TO_BUCKET) - flat, house, land, garage, shop, business - with no
"uncategorized" bucket in scraper output. sales.bcpea.org already achieves
this via an exact controlled vocabulary (BCPEA_RAW_TYPES in
sync_to_supabase.py), since that portal's own listings carry a precise,
fixed Bulgarian type string. The other 7 portals don't expose a type field
that reliably, so this scores keyword matches across three independent
signals - title, description, and URL - instead of trusting any one alone
(a title might just be an address, a description might mention a nearby
garage without the listing itself being one; agreement across two or more
signals is real evidence, a single stray word in one field is not).

Extends geo_utils.CATEGORY_KEYWORDS (4 rough buckets, apartment-default,
built for Sofia-only apartment-search URLs where that default was accurate)
and BCPEA_RAW_TYPES (6 precise buckets, but an exact-match vocabulary that
only bcpea.org's own controlled type field can use) into one shared
keyword-scoring vocabulary usable against any portal's free-text fields,
now that nationwide, multi-category scraping means "default to apartment"
is no longer a safe assumption.
"""

import re

# Longest/most specific phrases first within each category isn't needed
# here (unlike BCPEA_TYPE_LOOKUP's startswith-prefix matching) since these
# are substring searches against free text, not a single controlled-
# vocabulary string - "апартамент" and "тристаен апартамент" both just
# need to match "flat" once, not compete for the longest prefix.
CATEGORY_KEYWORDS = {
    "garage": [
        "гараж", "паркомясто", "паркоместа", "паркинг място", "гаражна клетка",
        "garazh", "parkomyasto", "parkomesta", "garage",
    ],
    "shop": [
        "магазин", "заведение", "ресторант", "кафене", "витрина за", "търговски обект",
        "магазини", "magazin", "zavedenie", "shop", "store",
        # "restaurant"/"tyrgovski obekt": imoti.net's own English listing
        # text for these (e.g. title "Restaurant", URL slug
        # "tyrgovski-obekt") - confirmed live against real committed data
        # (docs/backlog.md item 5) that neither matched anything here
        # before, since every other shop keyword is Bulgarian/Cyrillic or a
        # different Latin transliteration. "tyrgovski" (not the full
        # phrase) also catches the URL's hyphenated "tyrgovski-obekt" form.
        "restaurant", "tyrgovski",
    ],
    "business": [
        "офис", "склад", "хале", "производствен", "производство", "фабрика",
        "хотел", "бензиностанция", "газстанция", "автомивка", "индустриален имот",
        "бизнес имот", "търговски имот", "инвестиционен имот", "сграда за офиси",
        "ofis", "sklad", "hotel", "office", "warehouse",
        # "industrial property"/"commercial property": imoti.net's own
        # English titles for these - confirmed live these didn't match any
        # existing keyword (all Cyrillic-only for this nuance, or
        # "промишлен"/"promishlen" wasn't listed either). Deliberately the
        # full phrase, not a bare "industrial"/"commercial" - a real,
        # confirmed-live false positive: "Industrial Zone"/"Промишлена
        # зона" ("Industrialna Zona"/"Promishlena Zona") is a genuinely
        # common Bulgarian district name (Burgas, Haskovo, Yambol, Vratsa,
        # Plovdiv all have one), so a bare "industrial"/"promishlen"
        # keyword wrongly matched ordinary flats/houses/studios located IN
        # that district as if "industrial" described the property itself
        # (e.g. "House, 21 м2 ... Industrial zone - South" is a real
        # house, not a business property) - see docs/backlog.md item 5.
        # "promishlen-imot" (the real URL slug, hyphenated) is similarly
        # exact rather than bare "promishlen", which is a substring of
        # "promishlena" (the district-name spelling) and would have the
        # same collision via the URL signal.
        "industrial property", "commercial property", "promishlen-imot",
    ],
    "land": [
        "парцел", "земеделска земя", "земеделски земи", "земеделски имот", "урегулиран поземлен имот",
        "имот за строеж", "терен", "нива", "дворно място",
        "парцел с къща", "parcel", "teren", "niva", "plot",
        # "упи" (a common abbreviation for "урегулиран поземлен имот" -
        # "regulated land plot") is handled by _UPI_RE below, not as a plain
        # substring here - see that regex's own comment for why.
        # "поземлен имот" ("land property/plot") - a very common, generic
        # Bulgarian land-listing phrase, distinct from the already-present
        # "земеделски имот" (specifically AGRICULTURAL land). Missing before
        # (Missy's PR #264 review, 2026-09-23): real land-plot listings
        # whose title/description literally open with this exact phrase
        # (e.g. "код 62942. Поземлен имот 3800м2 на 20 метра от къщи...",
        # "Голям поземлен имот за продажба, с ПУП и проект за шест къщи...")
        # had NO land keyword match at all, so the listing's own genuine
        # subject never even entered scoring - "къщи" (mentioned only as
        # neighboring/planned-development context, never the property being
        # sold) won by default. A 2-word phrase, so no substring-collision
        # risk the way single-word additions need checking for.
        #
        # NOT handled as a plain substring here, though (Missy's PR #264
        # THIRD review, 2026-09-23) - moved to its own guarded regex,
        # _ZEMYA_IMOT_RE below, because "поземлен имот" is also standard
        # Bulgarian cadastral-registry BOILERPLATE that routinely appears
        # inside a completely unrelated building's own listing (flat/house/
        # shop/business), describing the LAND PARCEL UNDERNEATH that
        # building, not the property being sold - confirmed live,
        # imotibg_515292 ("Търговско помещение..." - a 460m² commercial
        # food-service space) got wrongly flipped flat->land purely because
        # its description happens to contain "...построена в поземлен имот
        # с идентификатор № 67338.516.1...", boilerplate identifying the
        # underlying cadastral parcel, never the listing's own subject. That
        # exact "поземлен имот с идентификатор" shape is near-universal
        # cadastral phrasing (confirmed live across a 15-record sample of
        # every listing matching it on all 6 non-bcpea portals - bcpea.org
        # uses its own controlled BCPEA_RAW_TYPES vocabulary, not this
        # keyword scorer, so it's excluded from this guard's own scope) -
        # but genuine land-for-sale listings ALSO routinely cite their own
        # parcel's cadastral identifier as part of describing the very land
        # being sold ("Продава се атрактивен поземлен имот с идентификатор
        # ..."), so a blanket ban on the phrase would just trade one false
        # positive for false negatives against real land listings that
        # happen to state their own identifier. Re-verified against the
        # SAME 15-record sample after adding the guard below: all 11 other
        # genuine land listings and both genuine business listings in that
        # sample carry at least one OTHER, unguarded land/business keyword
        # match of their own (parcel size phrasing, "земеделск-", the
        # already-separate "урегулиран поземлен имот" phrase, etc.) so
        # excluding just the "с идентификатор"-suffixed boilerplate shape
        # from counting as land evidence doesn't cost them their correct
        # classification - only imotibg_515292, whose ENTIRE land evidence
        # was this one boilerplate phrase, changes.
        # "ниви" (plural of "нива") - the exact same feminine noun -а/-и
        # pluralization gap "къщи"/"вили" already needed fixing for below,
        # just never previously found on the land side: "нива" is a literal
        # PREFIX-plus-ending-swap of "ниви", so the singular substring never
        # matches the plural (e.g. "Продавам ниви, граничащи с къщи..." had
        # no land match at all before this). Handled as its own \b-bounded
        # regex (_NIVI_RE below), NOT a plain CATEGORY_KEYWORDS substring -
        # confirmed live it would otherwise be a substring of unrelated
        # words like "денивелация"/"денивилация" (a real construction term,
        # "ground leveling") and "лениви" ("lazy") if added without a
        # word-boundary guard.
        # "agricultural land"/"development land": imoti.net's own English
        # titles for these; "zemedelski" catches its URL slug
        # ("zemedelski-imot") the same way "parcel" already catches
        # imoti.net's "parcel" URL slug - confirmed live neither matched
        # before (docs/backlog.md item 5).
        "agricultural land", "development land", "zemedelski",
    ],
    "house": [
        "къща", "вила", "етаж от къща", "таунхаус", "еднофамилна къща",
        "жилищна сграда", "къща с двор", "селска къща",
        # "къщи"/"вили" (plural of "къща"/"вила") used to live here as plain
        # substrings but are now handled by _KASHTI_RE/_VILI_RE below
        # instead - see those regexes' own comments for why (both a
        # substring-collision bug and a land-vs-house context bug Missy's
        # PR #264 review, 2026-09-23, found live in already-committed data).
        "kashta", "kyshta", "vila", "taunhaus", "house", "villa",
    ],
    "flat": [
        "апартамент", "едностаен", "двустаен", "тристаен", "четиристаен",
        "многостаен", "мезонет", "гарсониера", "ателие таван", "студио",
        "апартаменти", "стаи",
        "apartament", "apartamenti", "ednostaen", "dvustaen", "tristaen",
        "mezonet", "garsoniera", "studio", "flat", "apartment",
    ],
}

# Digit-prefixed room-count shorthand ("2-стаен", "3 стаен", "1-стаен
# апартамент") is an extremely common way Bulgarian listings state "flat"
# without ever spelling out "едно/дву/три/четиристаен" or the word
# "апартамент" itself - a plain substring list can't express this, so it's
# handled as its own regex rather than a CATEGORY_KEYWORDS entry.
_ROOM_COUNT_RE = re.compile(r"\d\s*-?\s*стаен")

# Digit-glued word boundary (Missy's PR #264 THIRD review, 2026-09-23,
# non-blocking): Python's \b treats ASCII digits and Cyrillic letters as the
# SAME \w class, so a plain \b-bounded regex (e.g. \bкъщи\b) silently fails
# to match when the word is glued directly to a preceding digit with no
# space - a real, live Bulgarian listing-title shorthand ("Продава 2къщи в
# с.Соволяно общ.Кюстендил", confirmed live, olx_9ECK4, 2026-09-23: wrongly
# flat/low instead of house). Bounding against LETTERS specifically
# (Cyrillic or Latin) rather than \w's broader digit-inclusive class treats
# a digit (or punctuation/whitespace/start-of-string) as a valid boundary
# on either side, while a genuine same-alphabet mid-word collision
# ("автокъщи", "вкъщи", "павилион", "групи", "принципи") is still correctly
# rejected exactly as before - confirmed against every existing collision
# case in tests/test_category_classifier_*.py. Same low-prevalence
# tradeoff already accepted for УПИ in round 1 (docs/backlog.md), just
# actually fixed now that it's cheap to do for all four \b-bounded land/
# house regexes at once via one shared helper rather than four separate
# near-identical patterns.
def _letter_bounded(word):
    return re.compile(r"(?<![а-яa-z])" + word + r"(?![а-яa-z])", re.IGNORECASE)


# "упи" ("урегулиран поземлен имот" - "regulated land plot"), a real, very
# common Bulgarian land-listing title word (confirmed live: olx.bg alone had
# 154+ genuine land listings misclassified over this, e.g. "УПИ до къщи в
# Първенец", "УПИ в село Мезек"). Previously a plain CATEGORY_KEYWORDS
# substring padded with a leading+trailing space (" упи ") to avoid a false
# substring match inside an unrelated longer word (e.g. "групи",
# "принципи") - but that padding requires a character before "упи" too,
# which a title OPENING with "УПИ" (extremely common - it's often the very
# first word of a land listing's title) never has, so the padded keyword
# silently never matched the single most common real-world phrasing. A
# proper letter-boundary regex (confirmed live to still correctly reject
# "групи"/"принцип" while matching "УПИ" at the very start of a string,
# mid-title, glued to a preceding digit, or followed by a comma) fixes this
# the same way _ROOM_COUNT_RE already handles "flat"'s own similar shape
# below.
_UPI_RE = _letter_bounded("упи")

# "ниви" (plural of "нива" - "field/plot") - see CATEGORY_KEYWORDS["land"]'s
# own comment above for why this needs its own letter-bounded regex rather
# than a plain substring (the same feminine -а/-и pluralization gap "къщи"/
# "вили" needed below, on the land side this time) and why the boundary
# specifically matters here (rejects "денивелация"/"денивилация", "лениви"
# - confirmed live in this project's own stored description text,
# 2026-09-23).
_NIVI_RE = _letter_bounded("ниви")

# "поземлен имот" ("land property/plot") - see CATEGORY_KEYWORDS["land"]'s
# own comment above for the full explanation: this phrase is both a genuine
# land-listing's own subject-naming AND standard Bulgarian cadastral-
# registry boilerplate describing the parcel underneath an unrelated
# building (almost always immediately followed by "с идентификатор" and a
# cadastral number, e.g. "...построена в поземлен имот с идентификатор №
# 67338.516.1..."). The negative lookahead excludes only that specific
# boilerplate shape - "поземлен имот" directly followed by "с
# идентификатор" (allowing for the whitespace/comma variants confirmed live
# in this project's own stored text, e.g. a stray non-breaking space) -
# while still matching every other real phrasing of "поземлен имот",
# including genuine land listings that cite their OWN parcel's identifier
# using a different construction (no "с" - e.g. "поземлен имот
# идентификатор 70247.76.41", confirmed live as olx_9Mx3k, a genuine 5.7
# decare agricultural land sale) or that mention "с идентификатор" nowhere
# at all (the overwhelming majority of real land listings using this
# phrase).
_ZEMYA_IMOT_RE = re.compile(r"поземлен имот(?!\s*,?\s*с идентификатор)", re.IGNORECASE)

# Proximity/distance markers ("от" - from, "до" - near/to, "близо до" -
# close to, "в близост до" - in the vicinity of, "граничещ(а/и)" -
# bordering, "съседен/съседни/съседна" - neighboring, "покрай" - alongside)
# - the actual Bulgarian idiom this whole land-vs-house "къщи"/"вили"
# context problem is built around (see _KASHTI_RE/_VILI_RE's own comment
# above: "20 метра от къщи", "до вили", "граничеща с ... къщи" are all real,
# sampled examples already documented there). Used by
# _demote_context_only_house_signals' Part B below (Missy's PR #264 THIRD
# review, 2026-09-23) to require that a signal's plural-only house mention
# actually READS like a locational reference to a NEARBY/neighboring house
# before that signal is eligible to have its verdict overridden by
# borrowing from an unrelated sibling signal - as opposed to a title like
# "Продавам две къщи в село Раковски" ("Selling two houses...") where
# "къщи" is the direct, unmarked object of a sale verb, not preceded by any
# of these markers, and is genuinely the ad's own real subject.
_HOUSE_PROXIMITY_MARKER_RE = re.compile(
    r"\b(от|до|близо\s+до|в\s+близост\s+до|граничещ\w*|съседе?н\w*|покрай)\b",
    re.IGNORECASE,
)


def _has_house_proximity_context(text, house_match_start):
    """True if one of _HOUSE_PROXIMITY_MARKER_RE's markers appears in the
    ~40 characters immediately before `text[house_match_start]` - i.e. the
    plural "къщи"/"вили" match at that position reads as a locational
    reference to nearby/neighboring houses, not the ad's own direct
    object."""
    window = text[max(0, house_match_start - 40):house_match_start]
    return bool(_HOUSE_PROXIMITY_MARKER_RE.search(window))

# "къщи"/"вили" (plural of "къща"/"вила" - "house(s)"/"villa(s)") - Missy's
# PR #264 review (2026-09-23) found two real, live bugs in how these were
# added as plain CATEGORY_KEYWORDS substrings:
#
# 1. SUBSTRING COLLISION: "вили" (4 letters, no word-boundary guard) is
#    itself a literal substring of "павилион" ("pavilion" - a small
#    commercial kiosk/structure, completely unrelated to houses) and
#    several other unrelated Cyrillic words confirmed live in this
#    project's own stored text - "привилидж"/"цивилизация" (mid-word, the
#    "-вили-" letters just happen to appear) and any verb ending in
#    "-вили" ("направили", "предоставили", "подготвили" - a very common
#    Bulgarian past-tense plural verb suffix). 9 of 13 real "павилион"
#    listings were reclassified "house" over this, 4 at "high" confidence.
#    "къщи" had the same latent risk, confirmed live too:
#    "автокъща"/"автокъщи" ("auto-house" - a real, common Bulgarian term
#    for a CAR DEALERSHIP, not a house) and "вкъщи" ("at home", an adverb,
#    not the property type) both contain "къщи" as a bare substring.
#    Fixed the same way _UPI_RE already fixed this exact class of bug for
#    "упи" - a proper \b word-boundary regex (Python's \b is Cyrillic-aware
#    by default), confirmed live to still correctly reject every one of the
#    above collisions while still matching "къщи"/"вили" as their own
#    standalone words (start of string, mid-title, followed by a comma).
#
# 2. LAND-VS-HOUSE CONTEXT: even where "къщи"/"вили" correctly match their
#    own word and nothing else, a genuine LAND-plot listing routinely
#    mentions neighboring or future-planned houses as location CONTEXT, not
#    as the property actually being sold - e.g. "Поземлен имот 3800м2 на 20
#    метра от къщи" (a land plot 20m FROM houses), "Парцел 430м2 до вили"
#    (a plot NEAR villas), "проект за шест къщi" (a land plot with an
#    approved PROJECT to build six houses on it - still land, not a house,
#    until built). 190 olx.bg + 7 alo.bg + 1 imoti.bg real records were
#    affected, 53 of the olx.bg ones at "high" confidence. See
#    _demote_context_only_house_signals below for the fix - a related but
#    distinct problem from _resolve_subject_over_amenity (that function
#    resolves an AMENITY-class winner losing to a SUBJECT-class category
#    that leads the title; this is two SUBJECT-class categories - land and
#    house are both real property types a listing can genuinely BE - where
#    one side's only evidence is an inherently context-prone plural word).
#
#    Also letter-bounded (not \b-bounded) for the same digit-glue reason
#    _UPI_RE/_NIVI_RE above are - see _letter_bounded's own comment.
_KASHTI_RE = _letter_bounded("къщи")
_VILI_RE = _letter_bounded("вили")

# Tiebreak order only (when two categories score exactly equal) - most
# specific/least-ambiguous categories first, "flat" last since it's also
# the no-match fallback and shouldn't win a tie against a real signal.
CATEGORY_ORDER = ["garage", "shop", "business", "land", "house", "flat"]

_KEYWORD_RE_CACHE = {
    cat: [re.compile(re.escape(kw)) for kw in kws]
    for cat, kws in CATEGORY_KEYWORDS.items()
}

# garage/<anything> tiebreak fix (docs/decisions.md 2026-09-23 "Ready" entry,
# .claude/agents/ready.md): CATEGORY_ORDER puts "garage" first, so it used to
# win every tie unconditionally - including the dominant real-world case,
# confirmed against a random sample of the affected pool, where "гараж"/
# "паркомясто" (garage/parking space) is mentioned as an attached AMENITY
# inside a flat/house/shop/business listing's own title ("Тристаен
# апартамент ... с ПАРКОМЯСТО", "Етаж от къща с гараж и паркомясто"), not the
# listing's own subject. A genuine garage-for-sale listing's title is instead
# templated to OPEN with the word itself ("Garage, 14 м2 Sofia, ...") - the
# garage keyword is the very first thing mentioned, nothing precedes it.
#
# Bulgarian listing titles are conventionally subject-first, amenities-
# appended, so whichever tied category's own keyword appears LEFTMOST in the
# title is its real subject; this resolves a garage/X tie by that position
# instead of by CATEGORY_ORDER, but only when every tied category actually
# has its own match inside the title (the strongest, purpose-written signal -
# see SIGNAL_WEIGHTS - so a positional comparison across categories is
# apples-to-apples) and only when garage itself isn't the leftmost (i.e.
# doesn't override anything when garage genuinely is the subject, e.g. a real
# garage listing that happens to also mention "near the apartments" later -
# "гараж" still precedes "апартаменти" there, so garage still wins). Anywhere
# this can't be determined (title missing, or a tied category's score came
# only from url/description) falls through to the original CATEGORY_ORDER
# behavior unchanged.
#
# Deliberately scoped to ties that include "garage" specifically - the one
# root-caused, quantified pattern (2,049 of 2,516 low-confidence
# garage-tagged listings as of 2026-09-23; see backfill_garage_tiebreak.py) -
# rather than rewriting the tiebreak for every category combination, most of
# which haven't been individually audited against real data.
def _title_match_positions(categories, title):
    """Maps each of `categories` to the character index of its earliest own
    keyword match inside `title` (case-insensitive), leaving out any
    category that has no match in `title` itself (only url/description
    contributed to its share of the tied score)."""
    if not title:
        return {}
    text = title.lower()
    positions = {}
    for cat in categories:
        idxs = [m.start() for m in (p.search(text) for p in _KEYWORD_RE_CACHE[cat]) if m]
        if cat == "flat":
            room_count_match = _ROOM_COUNT_RE.search(text)
            if room_count_match:
                idxs.append(room_count_match.start())
        if cat == "land":
            upi_match = _UPI_RE.search(text)
            if upi_match:
                idxs.append(upi_match.start())
            nivi_match = _NIVI_RE.search(text)
            if nivi_match:
                idxs.append(nivi_match.start())
            zemya_imot_match = _ZEMYA_IMOT_RE.search(text)
            if zemya_imot_match:
                idxs.append(zemya_imot_match.start())
        if cat == "house":
            for house_re in (_KASHTI_RE, _VILI_RE):
                house_match = house_re.search(text)
                if house_match:
                    idxs.append(house_match.start())
        if idxs:
            positions[cat] = min(idxs)
    return positions


def _resolve_garage_tie(winners, title):
    """Given a set of tied `winners` that includes "garage", returns the
    category that should actually win (see the module comment above this
    function), or None to leave CATEGORY_ORDER's existing fallback alone."""
    positions = _title_match_positions(winners, title)
    if len(positions) != len(winners):
        return None
    leftmost = min(positions, key=positions.get)
    if positions[leftmost] == positions["garage"]:
        # Exact tie in title position (or garage genuinely is leftmost) -
        # inconclusive either way, so don't override.
        return None
    return leftmost


# Second root-caused pattern (docs/decisions.md 2026-09-23 "Ready's second
# assignment" entry), generalizing the garage-tie fix above rather than
# duplicating it: the tiebreak fix above only fires on an exact score TIE,
# but the same "amenity word mentioned after the real subject" shape also
# produces an outright (non-tied) win for the wrong category whenever the
# SAME amenity word appears in both the title AND the url - e.g. a listing
# whose url is portal-generated as a transliterated copy of its own title
# (confirmed live for olx.bg/alo.bg: "-s-garazh-" in the url is just
# "с гараж" from the title, not independent taxonomy evidence the way
# imoti.net's own category-path url segments are). That double-counts the
# SAME piece of real-world evidence as if it were two independent signals
# (title weight 3 + url weight 2 = 5), letting an attached-amenity category
# outscore the listing's real, title-leading subject (weight 3 alone)
# outright - no tie ever occurs, so _resolve_garage_tie above never even
# runs. Confirmed live, e.g. a real olx.bg house listing "Продавам
# двуетажна къща ... с гараж в с.Тополово" (subject "къща" leads the title,
# "гараж" is an appended amenity) scored business... er, garage=5 (title +
# url) outright beating house=3 (title only, since olx.bg's url spells
# "къща" as "kascha", not a spelling already in CATEGORY_KEYWORDS["house"]),
# with "high" confidence (title+url did genuinely "agree" - just on the
# wrong, amenity word, not the listing's real subject).
#
# Generalizes (rather than just patching the one missing "kascha" spelling,
# which would leave the same shape of bug for the next missing
# transliteration) the exact same "Bulgarian titles are subject-first,
# amenities-appended" position reasoning the garage tiebreak already
# established and validated against real data - just applied to WINNER vs.
# the title's own leftmost SUBJECT-class match, regardless of whether the
# raw scores happened to land on an exact tie:
#   - AMENITY_CATEGORIES = {"garage", "shop", "business"} - categories this
#     project's own real-data sampling (both the original tiebreak fix and
#     this one) has repeatedly found mentioned as an attached amenity of a
#     listing whose real subject is something else, never the reverse.
#   - SUBJECT_CATEGORIES = {"flat", "house", "land"} - the categories real
#     listings are titled AS, leading the sentence.
# Only overrides when the raw winner is itself amenity-class, and only when
# that winner ALSO has its own match inside the title (an apples-to-apples
# comparison, same discipline as _resolve_garage_tie - if the winner's
# whole score came from url/description alone, there's no title position to
# compare a subject match against, so this leaves the existing tie/
# CATEGORY_ORDER fallback alone rather than guessing).
AMENITY_CATEGORIES = {"garage", "shop", "business"}
SUBJECT_CATEGORIES = {"flat", "house", "land"}


def _resolve_subject_over_amenity(winner, title):
    """Returns the real-subject category that should win instead of an
    amenity-class `winner`, when the title's own leftmost SUBJECT-class
    keyword precedes `winner`'s own title match - or None to leave the
    existing winner (tie-resolved or not) alone."""
    if winner not in AMENITY_CATEGORIES:
        return None
    positions = _title_match_positions(SUBJECT_CATEGORIES | {winner}, title)
    if winner not in positions:
        return None
    subject_positions = {cat: pos for cat, pos in positions.items() if cat in SUBJECT_CATEGORIES}
    if not subject_positions:
        return None
    leftmost_subject = min(subject_positions, key=subject_positions.get)
    if subject_positions[leftmost_subject] < positions[winner]:
        return leftmost_subject
    return None

# Each signal's contribution to a category's score. The URL often encodes
# the portal's own category cleanly in a path segment (e.g. a search or
# listing URL containing ".../garazhi-parkomesta/...") so it counts for
# more than one stray word inside a long free-text description, but less
# than the title, which is short and purpose-written to describe exactly
# what's for sale.
SIGNAL_WEIGHTS = {"title": 3, "url": 2, "description": 1}


def _score_signal(signal_name, text, scores, matched_signals):
    if not text:
        return
    text = text.lower()
    weight = SIGNAL_WEIGHTS[signal_name]
    for cat, patterns in _KEYWORD_RE_CACHE.items():
        matched = any(p.search(text) for p in patterns)
        # "къщи"/"вили" (house) and "ниви" (land) are folded into the SAME
        # per-category match check as their category's other keywords
        # (rather than each being its own separate additive `if` block, the
        # way _ROOM_COUNT_RE/_UPI_RE already were before this fix) so that a
        # signal mentioning BOTH, e.g., "къща" and "къщи" (a real, live
        # pattern: "Продава се... къща... Всяка къща има собствен парцел...
        # аналогични двуетажни КЪЩИ в чернова" - one description, singular
        # AND plural both genuinely describing the same real house listing)
        # only counts as ONE match for "house" in that signal, not two -
        # confirmed live this double-counting was otherwise silently
        # inflating a signal's score by 2x weight instead of a clean 1x,
        # which changed an actual tie-break outcome for several real
        # records during this fix's own verification. (_ROOM_COUNT_RE and
        # _UPI_RE keep their own pre-existing separate-block form below,
        # unchanged - not touched by this fix, not the bug being fixed
        # here, and their own combination is always with OTHER land/flat
        # evidence rather than two forms of the exact same word, so it
        # doesn't carry the same live-confirmed regression risk.)
        if cat == "house" and (_KASHTI_RE.search(text) or _VILI_RE.search(text)):
            matched = True
        if cat == "land" and (_NIVI_RE.search(text) or _ZEMYA_IMOT_RE.search(text)):
            matched = True
        if matched:
            scores[cat] = scores.get(cat, 0) + weight
            matched_signals.setdefault(cat, set()).add(signal_name)
    if _ROOM_COUNT_RE.search(text):
        scores["flat"] = scores.get("flat", 0) + weight
        matched_signals.setdefault("flat", set()).add(signal_name)
    if _UPI_RE.search(text):
        scores["land"] = scores.get("land", 0) + weight
        matched_signals.setdefault("land", set()).add(signal_name)


# Second land-vs-house pattern Missy's PR #264 review found (see
# _KASHTI_RE/_VILI_RE's own comment above, bug class 2) - a genuine LAND
# listing whose only "house" evidence is the ambiguous "къщи"/"вили" plural
# CONTEXT words (neighboring/planned houses, never the listing's own
# subject) can still outscore land outright, since a real land-plot
# description often repeats "къщи" more than once (both as neighboring
# context AND, confusingly, sometimes as an approved development's planned
# unit count - "проект за шест къщи" - still land until built).
#
# Unlike _resolve_subject_over_amenity (which compares an AMENITY-class
# winner against a SUBJECT-class category leading the title), this is a
# SUBJECT-vs-SUBJECT problem: land and house are both real property types a
# listing can genuinely BE.
#
# First cut of this fix used "specificity" alone (demote къщи/вили whenever
# no OTHER house keyword also matched the same signal) - live-verified
# against the ACTUAL full affected population (not just Missy's own
# examples) and found to be WRONG for a real, sizeable class it hadn't been
# checked against: genuine multi-house listings that ALSO mention their own
# attached plot as an amenity, e.g. "Две къщи с АКТ 14 в общ парцел..."
# (two real houses, full room-by-room descriptions in the body text, "с общ
# парцел" - "with a shared parcel" - trailing as the attached-land
# amenity), "Продавам две къщи с парцел" - both wrongly flipped to `land`
# by the first cut, since "къщи" was still each listing's ONLY house
# keyword (no singular "къща" also present) even though it was genuinely
# the listing's own real subject, not context.
#
# Fixed by adding POSITION back in, the same "subject leads,
# amenity/context trails" reasoning this whole file already uses elsewhere
# - but applied PER SIGNAL (title's own text vs land's own title match,
# description's own text vs land's own description match) rather than
# title-only, because the deciding land evidence can legitimately live only
# in the description (e.g. "Имот 630м2 на 100 метра от последните
# къщи..." - title alone never says "Поземлен", only the description
# spells out "Поземлен имот 630м2..." explicitly):
#   Part A (TITLE only): a title is short and reliably subject-first
#     (established precedent elsewhere in this file), so a generic position
#     check there is trustworthy for ANY house keyword, not just the
#     ambiguous plural ones - if land's own title match precedes house's,
#     the title itself settles it directly ("Парцел с вила" - land leads,
#     "вила" singular trails - genuinely land, the same convention the
#     pre-existing "парцел с къща" keyword phrase already encodes). A
#     house-leading, land-trailing title ("Две къщи с... парцел") is
#     explicitly left alone, fixing the regression above.
#   Part A' (DESCRIPTION/URL): a full free-text description is NOT reliably
#     subject-first the way a title is - live-confirmed regression sampling
#     the fix's OWN real effect found a genuine, clean two-signal-agreement
#     house listing (a 4-row-house development) whose description simply
#     happens to OPEN by describing its "10 парцела" before getting to the
#     "4-ри редови къщи" being sold - a naive generic position check there
#     wrongly stripped a correct "high" confidence down to "low". So
#     description/url positions only ever settle it when house's ENTIRE
#     evidence in that one signal is purely the ambiguous "къщи"/"вили"
#     context words (no other, more specific/definitive house keyword also
#     present in that same text) AND land precedes there too - never when a
#     genuine singular "къща"/"вила" (or other definitive house word) is
#     also present in that same description, regardless of word order.
#   Part B: a signal whose house evidence is purely "къщи"/"вили" and has NO
#     land match of its own to compare (so neither Part A nor A' can settle
#     it directly, e.g. the title in the "Имот... от... къщи" case, which
#     never says "Поземлен") borrows the verdict from a SIBLING signal that
#     WAS directly demoted - i.e., only fires when another matched signal
#     independently confirmed "land leads, house trails" within its own
#     text, so this is corroboration from the same ad's OTHER text, never a
#     guess from nothing.
#
#     THIRD failure mode (Missy's PR #264 third review, 2026-09-23): as
#     originally written, Part B let ANY signal - including the TITLE -
#     borrow a demoted sibling's verdict purely because its own house
#     evidence happened to be the ambiguous plural with no land competitor
#     of its own, with no check on whether the title's OWN text actually
#     read as context in the first place. Reproduced live: title "Продавам
#     две къщи в село Раковски" ("Selling two houses in Rakovski village")
#     alone correctly classifies as house/single_signal_only, but adding a
#     description that mentions bordering agricultural land ("Земеделска
#     земя... граничеща с двете къщи, е включена в сделката") flips the
#     WHOLE listing to land - even though nothing about the title's own
#     "две къщи" (a numbered, direct object of "Продавам") was ever
#     ambiguous. This bypasses the very design rationale the rest of this
#     file relies on elsewhere ("titles ARE reliably subject-first").
#
#     Fixed by requiring TITLE specifically to show its OWN internal
#     evidence of being a locational/context reference - one of
#     _HOUSE_PROXIMITY_MARKER_RE's markers ("от", "до", "близо до",
#     "граничещ...", "съседен...", "покрай" - the actual real-world idiom
#     this whole context-vs-subject problem is about, already documented
#     above) appearing shortly before the "къщи"/"вили" match - before
#     it's eligible for Part B borrowing at all. The motivating case this
#     mechanism was built for, olx_9RCOH ("...на 100 метра от последните
#     къщи..."), keeps its "от" marker and is unaffected; a title like
#     "Продавам две къщи..." has no such marker anywhere near "къщи" and so
#     is no longer eligible to be overridden by a sibling signal.
#     Deliberately scoped to the title signal only (Missy's specific,
#     confirmed finding) - url/description eligibility for Part B is left
#     as-is, unchanged from the second review's fix, since no live
#     regression was found there and this file's own established rationale
#     already treats title differently (Part A above already draws the
#     same signal-specific distinction).
# Only applies when "land" has independent evidence somewhere at all
# (scores.get("land", 0) > 0), so a genuine house listing with zero land
# mentions anywhere is never touched by any part.
def _demote_context_only_house_signals(scores, matched_signals, title, description, url):
    """Mutates `scores`/`matched_signals` in place per the rule described
    above - see that comment for the two real, sampled regressions (one
    involving singular house words, one involving a long multi-topic
    description) that shaped the title/description asymmetry here."""
    if scores.get("land", 0) <= 0 or scores.get("house", 0) <= 0:
        return
    house_signals = matched_signals.get("house")
    if not house_signals:
        return
    signal_text = {"title": title, "url": url, "description": description}

    def _earliest(text, cat):
        idxs = [m.start() for m in (p.search(text) for p in _KEYWORD_RE_CACHE[cat]) if m]
        if cat == "house":
            for house_re in (_KASHTI_RE, _VILI_RE):
                m = house_re.search(text)
                if m:
                    idxs.append(m.start())
        if cat == "land":
            for land_re in (_UPI_RE, _NIVI_RE, _ZEMYA_IMOT_RE):
                m = land_re.search(text)
                if m:
                    idxs.append(m.start())
        return min(idxs) if idxs else None

    demoted = set()
    context_only_no_land_competitor = set()
    for signal_name in list(house_signals):
        text = signal_text.get(signal_name)
        if not text:
            continue
        text = text.lower()
        house_pos = _earliest(text, "house")
        if house_pos is None:
            continue
        has_context = bool(_KASHTI_RE.search(text) or _VILI_RE.search(text))
        has_other = any(p.search(text) for p in _KEYWORD_RE_CACHE["house"])
        land_pos = _earliest(text, "land")
        if land_pos is not None:
            if signal_name == "title":
                # Part A: title's own word order settles it directly,
                # regardless of whether the house match was the ambiguous
                # plural or a definitive singular word.
                if land_pos < house_pos:
                    demoted.add(signal_name)
            elif has_context and not has_other:
                # Part A': same idea, but restricted to signals whose
                # house evidence is purely the ambiguous plural context
                # words - never overrides a genuine singular "къща"/"вила"
                # elsewhere in the same free-text signal, regardless of
                # word order (see the "10 парцела... 4-ри редови къщи"
                # regression this restriction fixes, above).
                if land_pos < house_pos:
                    demoted.add(signal_name)
            continue
        # Part B candidate: no land competitor in THIS signal - only
        # eligible for cross-signal corroboration if house's evidence here
        # is purely the ambiguous plural context words.
        if has_context and not has_other:
            if signal_name == "title" and not _has_house_proximity_context(text, house_pos):
                # Third failure mode fix (see Part B's own comment above):
                # the title's own plural mention isn't accompanied by any
                # locational/distance marker, so nothing about the title
                # itself suggests it's context rather than the ad's real
                # subject - not eligible to be overridden by a sibling
                # signal's verdict.
                continue
            context_only_no_land_competitor.add(signal_name)

    if demoted:
        demoted |= context_only_no_land_competitor

    for signal_name in demoted:
        scores["house"] -= SIGNAL_WEIGHTS[signal_name]
        house_signals.discard(signal_name)
    if scores.get("house", 0) <= 0:
        scores.pop("house", None)
        matched_signals.pop("house", None)


def classify_listing(title=None, description=None, url=None):
    """Classifies one listing from its title, description, and URL.

    Returns (category, confidence, reason):
      category   - always one of CATEGORY_KEYWORDS' 6 keys, never None/other.
      confidence - "high" or "low".
      reason     - short machine-readable string explaining a "low" verdict
                   (None when confidence is "high") - accumulate these to
                   answer "how many listings needed the low-confidence
                   fallback, and why."
    """
    scores = {}
    matched_signals = {}
    _score_signal("title", title, scores, matched_signals)
    _score_signal("url", url, scores, matched_signals)
    _score_signal("description", description, scores, matched_signals)
    _demote_context_only_house_signals(scores, matched_signals, title, description, url)

    if not scores:
        return "flat", "low", "no_keyword_match"

    best_score = max(scores.values())
    winners = [cat for cat in CATEGORY_ORDER if scores.get(cat) == best_score]
    winner = winners[0]

    # Checked first, ahead of both the tie logic and the single-signal/high-
    # confidence logic below, since it can override either shape of result
    # (an outright amenity-class win OR an amenity-involving tie) - see the
    # module comment above _resolve_subject_over_amenity for why this is
    # checked independently of whether the raw scores happen to tie.
    subject_override = _resolve_subject_over_amenity(winner, title)
    if subject_override is not None:
        reason = "title_subject_override:" + winner + "->" + subject_override
        if len(winners) > 1:
            reason += ",tied_with:" + ",".join(w for w in winners if w != winner)
        return subject_override, "low", reason

    if len(winners) > 1:
        if "garage" in winners:
            resolved = _resolve_garage_tie(winners, title)
            if resolved is not None:
                return resolved, "low", "tied_categories_by_title_position:" + ",".join(winners)
        return winner, "low", "tied_categories:" + ",".join(winners)

    # High confidence requires agreement across more than one independent
    # signal - repeated keyword hits within a single field (e.g. a
    # description that says "апартамент" three times) shouldn't count as
    # stronger evidence than one field alone actually is.
    if len(matched_signals[winner]) < 2:
        return winner, "low", "single_signal_only"

    return winner, "high", None
