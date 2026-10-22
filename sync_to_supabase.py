"""
Computes the same cross-portal-deduped view index.html's groupListings()
computes client-side (ported 1:1 from index.html: same CYR_TO_LAT
transliteration table, same normalizeArea/areasMatch/pricesMatch rules, same
two-pass no-bridging-through-sqm-less-listings union-find, same
BCPEA_TYPE_LOOKUP/BG_CITIES type-bucket and city-key logic - verified against
the real algorithm in index.html, not reimplemented from scratch), and
upserts it into Supabase (listing_sources + merged_listings) so the frontend
can query just what it needs server-side instead of shipping every
scraper's whole JSON file to the browser on every page load.

A merged listing's id is a deterministic hash of its sorted member
(portal, source_id) pairs - NOT "whichever source currently scores highest"
(today's in-browser approach), which drifts day to day as days_on_market
changes and silently breaks bookmarked #/listing/<id> links. A group's id
only changes when its actual membership changes.

Reads the 8 already-committed data/leads_*.json files (homes.bg's own is
gzip-compressed, leads_homes.json.gz, since 2026-09-25 - see geo_utils.py's
"Compressed on-disk JSON storage" comment; load_all_listings() below is
transparently gzip-aware via load_json_any()) - the scrapers themselves
are unchanged, JSON stays the source of truth and safety net. This script
only writes to Supabase.

Auth: SUPABASE_URL and SUPABASE_SECRET_KEY must be set as environment
variables (GitHub Actions repo secrets in production). The secret key
bypasses Row Level Security entirely (see supabase/schema.sql, which has no
write policies at all), so it must never reach the browser or be written to
disk/committed - only ever passed in via the environment.
"""

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests

from geo_utils import (
    BG_CITIES, BG_CITY_BY_NAME, LATIN_CITY_TO_KEY,
    _looks_like_title_echo,
    bcpea_settlement_from_title, bcpea_type_match, city_key_from_name,
    city_key_from_name_prefix, cyr_city_key_from_text, latin_city_key_from_text,
    listing_city_key, load_json_any,
)

DATA_DIR = Path(__file__).parent / "data"

# homes.bg -> "leads_homes.json.gz" (2026-09-25 addendum), alo.bg/imot.bg/
# olx.bg/bazar.bg -> "leads_*.json.gz" (2026-09-26, same GH001 wall hit/
# about to hit - see geo_utils.py's "Compressed on-disk JSON storage"
# comment). load_all_listings() below reads every portal through
# load_json_any(), which is gzip-aware by extension - the full `photos`
# array (and every other field) survives this unchanged; only the on-disk
# bytes shrink. imoti.net/imoti.bg/bcpea.org are untouched, still plain
# .json - their record counts haven't jumped the same way.
PORTAL_FILES = {
    "imoti.net": "leads.json",
    "alo.bg": "leads_alo.json.gz",
    "homes.bg": "leads_homes.json.gz",
    "imot.bg": "leads_imot.json.gz",
    "olx.bg": "leads_olx.json.gz",
    "bazar.bg": "leads_bazar.json.gz",
    "imoti.bg": "leads_imoti_bg.json",
    "sales.bcpea.org": "leads_bcpea.json",
}

# --- Cross-portal duplicate detection - ported 1:1 from index.html --------
# (index.html:1163-1303 - see that file's own comments for the reasoning
# behind every rule here; this is a straight port, not a redesign.)

CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sht", "ъ": "a", "ь": "", "ю": "yu", "я": "ya",
}


def transliterate(s):
    return "".join(CYR_TO_LAT.get(c, c) for c in s)


# Kept 1:1 with index.html's own AREA_PREFIX_RE/normalizeArea() - see that
# copy's own comment for the full story (backlog item 18 only stripped
# кв./жк./v; this also strips с./село, гр./град, and в.з. - all three
# live-confirmed missing at nationwide scale, e.g. "гр.Червен Бряг" vs bare
# "Червен бряг" normalizing to two different area keys for the same real
# Pleven-oblast town). Same "dot alone is proof enough, bare word needs a
# real trailing space" safety rule, same reason: real neighborhood names
# like "Градска Част" merely start with the same letters as "град" and
# must not be truncated by a bare-word match with no word boundary.
AREA_PREFIX_RE = re.compile(r"^(?:v\.z\.|s\.|gr\.)\s*|^(?:vz|v|kv\.?|zh\.?k?\.?|grad|gr|s)\s+")


def normalize_area(area):
    if not area:
        return ""
    s = transliterate(area.lower().strip())
    for _ in range(3):
        s = AREA_PREFIX_RE.sub("", s).strip()
    return s


def areas_match(a1, a2):
    n1, n2 = normalize_area(a1), normalize_area(a2)
    return bool(n1) and n1 == n2


def prices_match(p1, p2):
    if not p1 or not p2:
        return False
    # A flat 1 EUR tolerance was missing real cross-posted duplicates: BGN
    # (Bulgaria's currency, pegged to EUR at a fixed rate) to EUR display
    # conversion rounds slightly differently portal to portal, and a
    # listing scraped a few hours apart can catch one portal just before a
    # price update and another just after - both produce the exact same
    # real listing showing two slightly different EUR prices. Live-sampled:
    # cross-portal pairs that already match on area+sqm have a median price
    # gap of 0.75% when they differ at all - 0.5% is a conservative cut
    # that catches genuine rounding/timing drift without reaching into the
    # long tail (some pairs differ by 10-20%) that's more likely two
    # actually-different apartments coincidentally sharing an area+sqm.
    tolerance = max(1, round(max(p1, p2) * 0.005))
    return abs(p1 - p2) <= tolerance


# prices_match()'s tolerance is now relative (0.5% of price), so a fixed
# +/-1-whole-euro bucket search radius no longer covers it at any real
# price (0.5% of EUR200,000 is EUR1,000) - bucketing by price directly and
# scanning a wider absolute radius would need a radius that scales per
# listing, which is expensive to search efficiently. Bucketing in LOG space
# instead makes tolerance-width constant regardless of price magnitude: a
# 0.5% price change is *always* the same fixed distance in log space, so
# checking the same fixed handful of neighboring buckets (dk in a small
# fixed range) finds every match at every price level in one pass.
_PRICE_LOG_BUCKET_WIDTH = math.log(1 + 0.005)


def price_bucket_key(price):
    return round(math.log(max(price or 0, 1)) / _PRICE_LOG_BUCKET_WIDTH)


# Live-confirmed false merge (user-reported, spot-checked on the production
# site): "Двустаен, 174m2, жк. Надежда 4, София" merged a homes.bg listing
# (174 sqm, EUR173,200) with a bazar.bg listing (EUR174,000) that is a
# DIFFERENT real apartment - its own description literally says "Площ: 71
# кв.м." (71 sqm), but bazar.bg's structured `sqm` field was null for that
# listing, so group_listings() below never saw a real sqm for it at all and
# ran it through the without_sqm/solo_sqmless price-only attach pass
# instead (see that pass's own comments) - price+area+room-count proximity
# alone, no per-listing unit discriminator, exactly the "sqm-less second
# pass" risk docs/decisions.md already disclosed (18.31% accepted
# false-negative-adjacent regression, concentrated in exactly this kind of
# busy housing-complex/жк. area with many similar-priced units).
#
# bazar.bg (and alo.bg, which has the same gap) often has no structured sqm
# field, but a lot of its listing text still states the real area in plain
# words as part of a copy-pasted agency mini-spec block embedded in the
# description ("...бл. 429Площ: 71 кв.м.Етаж: 4..."). Recovering that number
# for classification purposes only lets more listings go through the
# strict, already-tuned with_sqm pass (exact area/city + price +/-0.5% +
# sqm +/-1) instead of falling into the risky sqm-less one - a precision
# improvement, not a rework of either pass. Deliberately narrow: only the
# literal "Площ:" label (the same structured-field-style text the confirmed
# false-merge case itself used), not free-form prose like "с площ от X
# кв.м" elsewhere in a description, which more often describes a balcony,
# plot or unrelated structure rather than the unit's own living area.
#
# Missy's review caught a real bug in the first version of this regex: a
# bare `search()` for "площ:" also matches inside "Чиста площ:", "Обща
# площ:", "Разгъната застроена площ:", etc. - standard Bulgarian
# real-estate labels for net/usable vs. gross/built area, which routinely
# differ 10-20%+ for the *same* physical unit. Feeding one of those into
# the same +/-1-sqm strict pass as a portal's own structured field would
# compare two different measurements as if they were the same one - a real
# correctness bug, not cosmetic (confirmed: 128 of the original 356
# extractable listings, 36%, were actually one of these qualified
# variants, not a bare "Площ:"). Fixed by walking every "площ:" occurrence
# in the text (finditer, not a single search) and skipping any whose
# immediately preceding word starts with one of _QUALIFIED_AREA_PREFIXES -
# only a bare, unqualified "Площ:" (or one preceded by unrelated text, e.g.
# "...Етаж: 4.Площ: 87 кв.м.") is trusted, matching the exact confirmed
# real case's own text.
#
# Re-measured against the real committed data with this narrowed regex (see
# docs/backlog.md item 75 for the full writeup): of 71,414 sqm-less
# listings in a mixed sqm/sqm-less merged group (baseline, unmodified
# grouping), 270 have an extractable bare "Площ:" figure, and 95 of those
# (down from the first version's inflated 124, which counted qualified
# variants as agreeing/disagreeing when they were never comparable in the
# first place) disagree by >20% with their group - all 95 are resolved
# after this fix (0 remain). Also caught by the same review, and disclosed
# honestly rather than hidden: among the 175 listings whose extracted sqm
# already agreed with their baseline group (i.e. were presumably genuine,
# correct matches before this fix), 51 (29.1%) lose their real-sqm partner
# entirely after this change and now show as a standalone listing instead
# of a consolidated one - a real recall cost, not just a precision gain.
# This happens because moving a listing into the with_sqm pass also
# subjects it to that pass's own transitive sqm_range/price_range span
# caps (see union() below) - protections the lenient sqm-less attach loop
# never enforced at all - so a recovered value that's individually within
# +/-1 sqm of one real member can still fail to join a group whose already-
# established span leaves no more room. Not addressed in this change (a
# narrower "refuse-only" variant - using the recovered sqm just to block a
# conflicting attachment rather than to route the listing through the full
# with_sqm pass - would likely avoid most of this cost, but is a bigger
# change to the matching logic than this task's scope allows and is not
# implemented here).
_DESCRIPTION_SQM_RE = re.compile(
    r"площ:\s*(\d+(?:[.,]\d+)?)\s*(?:кв\.?\s*\.?\s*м\.?|m2|m²)",
    re.IGNORECASE,
)

# The word immediately preceding "площ:" is checked against these - net
# ("чиста"/"полезна"), gross/built ("обща"/"застроена") and "разгъната"
# (spread-out, as in "разгъната застроена площ") area qualifiers all name a
# different, non-comparable measurement from the unit's own plain living
# area.
_QUALIFIED_AREA_PREFIXES = ("чист", "общ", "застроен", "разгъна", "полезн")

_WORD_BEFORE_MATCH_RE = re.compile(r"([A-Za-zА-Яа-яЁё]+)\s*$")


def extract_description_sqm(description):
    if not description:
        return None
    for m in _DESCRIPTION_SQM_RE.finditer(description):
        preceding_word_match = _WORD_BEFORE_MATCH_RE.search(description[:m.start()])
        if preceding_word_match and preceding_word_match.group(1).lower().startswith(
            _QUALIFIED_AREA_PREFIXES
        ):
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        # Sanity bound only to reject obvious mis-parses (e.g. a stray
        # decimal fragment) - not a claim about plausible apartment sizes,
        # since the same label text also appears on houses/plots in the
        # same data.
        if 5 <= value <= 2000:
            return round(value)
    return None


def _effective_sqm(l):
    # The listing's own structured sqm field when present; otherwise a
    # value recovered from its description text (see
    # extract_description_sqm() above) purely so group_listings() can
    # decide which of its two matching passes a listing is eligible for.
    # This is intentionally NOT written back onto the listing itself - the
    # merged row's own `sqm` field (built later in build_rows()) still only
    # ever comes from a listing's real structured field, so this has no
    # effect on what's displayed or on price_per_sqm.
    return l.get("sqm") or extract_description_sqm(l.get("description"))


def group_listings(all_listings):
    # City is a hard blocking condition on every merge decision below, not
    # a scoring input - a live report found a bazar.bg listing genuinely in
    # Veliko Tarnovo merged with a homes.bg/alo.bg pair genuinely in
    # Dobrich, all three sharing the generic area name "Център" ("center" -
    # identical text in every Bulgarian town, no city of its own) at a
    # coincidentally matching price. areas_match()/normalize_area() only
    # compare area text, never the actual city, so nothing before this fix
    # could ever catch that. Computed once per listing up front (not
    # per-comparison) since it's a pure function of already-scraped fields.
    # A listing whose city can't be resolved at all is excluded from every
    # bucket below and never merges with anything - the same "leave
    # unclassified rather than guess" rule listing_city_key() itself
    # already follows, extended to matching: a generic area name like
    # "Център" must never contribute to a match without a real, known city
    # agreeing on both sides, and an unknown city can't agree with anything.
    city_keys = {id(l): listing_city_key(l) for l in all_listings}

    # effective_sqm - see _effective_sqm() above - is what actually decides
    # which pass a listing goes through below; every sqm comparison in this
    # function (sqm_range, the with_sqm pairwise check) reads it instead of
    # the raw l["sqm"] field for that reason. It equals l["sqm"] whenever
    # that's set, so this changes nothing for the vast majority of listings.
    effective_sqm = {id(l): _effective_sqm(l) for l in all_listings}

    with_sqm = [l for l in all_listings if effective_sqm[id(l)]]
    without_sqm = [l for l in all_listings if not effective_sqm[id(l)]]

    # Bucketing by price alone puts every listing near a common round price
    # (e.g. exactly 100,000 EUR - very common in this market) into one
    # enormous bucket regardless of area, since the 0.5% tolerance band
    # covers a lot of listings at popular price points - live-measured,
    # some single price buckets held 1,000+ listings, making the O(bucket²)
    # pairwise comparison pass effectively hang. areas_match() already
    # requires an exact normalized-string match, not a fuzzy one, so
    # co-bucketing by (price bucket, normalized area, city) loses no matches
    # a plain price bucket would have found - it only pre-applies filters
    # every surviving pair already had to pass anyway (city now among
    # them), and area names are far more differentiating than price,
    # keeping real buckets small.
    price_buckets = {}
    for l in with_sqm:
        na = normalize_area(l.get("area"))
        city_key = city_keys[id(l)]
        if not na or not city_key:
            continue
        key = (price_bucket_key(l.get("price_eur")), na, city_key)
        price_buckets.setdefault(key, []).append(l)

    parent = {id(l): l for l in with_sqm}
    # Tracks each group's current [min_sqm, max_sqm] span, keyed by the
    # current root's id - a candidate union is only allowed if it keeps the
    # group's overall span within the same +/-1 sqm tolerance every
    # individual pairwise match already enforces. Without this, a chain of
    # listings each 1 sqm apart from a neighbor (95-96, 96-97, 97-98)
    # transitively unions into one group spanning 3 sqm - a real, live-
    # found false-merge bug: a Varna new-construction development selling
    # ~39 distinct units at the same round price with sqm varying by a
    # couple square meters was collapsing into a single "merged" listing.
    sqm_range = {id(l): (effective_sqm[id(l)], effective_sqm[id(l)]) for l in with_sqm}
    # Same transitive-drift risk as sqm, same fix: without tracking the
    # group's own price span, a chain of pairwise-tolerable price hops
    # (each within prices_match() of a neighbor) can drift the group's
    # overall min-to-max price spread well past that same tolerance.
    price_range = {id(l): (l["price_eur"], l["price_eur"]) for l in with_sqm}
    # Backlog item 64 (handoff from item 61, Placy's allocation investigation, PR #312):
    # a group's own set of member portals, keyed by the current root's id -
    # a candidate union is refused outright if the two groups being merged
    # already share a portal. Real-data-confirmed root cause: with no real
    # unit/floor/address discriminator in the scraped data (verified - see
    # docs/decisions.md for what was actually checked: no portal reliably
    # extracts a unit/apartment number, "area" is the only address-like
    # text any portal has and it's already the matching key itself, and
    # every portal's lat/lng is itself a Nominatim geocode of that same
    # area+city text - not a real per-listing coordinate - so it carries no
    # independent building-level signal and using it as a discriminator
    # would just be re-deriving a fake precision from the same area string
    # already being matched on), a mass development selling many
    # near-identical-sized/priced units is indistinguishable from one unit
    # cross-posted across portals UNLESS this one structural fact is used:
    # a single portal never lists the same real property twice in a way
    # that should count as "cross-posted" - two listings from the same
    # portal in what would become one merged group is proof they're
    # different real properties, not proof of heavier cross-posting.
    # Unlike sqm_range/price_range above (which cap a candidate union's
    # resulting *span*), this is a hard refuse, not a tolerance: portal
    # membership is exact, not fuzzy, so there is no "close enough" case to
    # allow. This directly targets the confirmed root cause - 13,814 of
    # 59,028 real multi-member groups contained more than one same-portal
    # listing before this fix, which is structurally impossible for
    # genuine cross-posting.
    #
    # Keyed by (portal, url), not portal alone - a real, separate,
    # pre-existing data bug found while quantifying this fix's impact
    # (docs/decisions.md has the full writeup) made the portal-alone
    # version of this check actively harmful: homes.bg's own
    # leads_homes.json.gz stores 43,838 real listings TWICE, once under a
    # plain numeric id and once under an "as"-prefixed id, both with the
    # exact same url/price/sqm/title - one real listing under two ids, not
    # two apartments. That pair sharing a portal is not proof of a
    # development; the two homes.bg *ids* just refer to one real listing,
    # confirmed by their identical url. A plain "same portal" check
    # couldn't tell that apart from a genuine second unit and refused
    # perfectly good cross-portal matches over it - measured on the real
    # data: a portal-only version of this guard broke 21.56% of the
    # cross-portal pairs already correctly grouped before this fix ever
    # ran, almost entirely concentrated in exactly this bug's footprint
    # (homes.bg has real listing density high enough that some price/area
    # bucket hits one of these 43,838 pairs constantly). Comparing by
    # (portal, url) together fixes that without discarding or guessing
    # which of a same-url pair is "correct" (this task's earlier attempt at
    # that - picking whichever of a same-url pair had more populated
    # fields - was tried and measured *worse*, 30.78% broken, because
    # richness has no relationship to which of the two prices is current):
    # two same-portal listings that share a url are the same real listing
    # by definition and are simply never a conflict; two that don't are
    # still refused exactly as before.
    portal_urls = {id(l): {l["portal"]: {l.get("url")}} for l in with_sqm}

    def _portal_conflict(pu_a, pu_b):
        for portal in pu_a.keys() & pu_b.keys():
            combined = pu_a[portal] | pu_b[portal]
            # None (a missing url) can never be confirmed identical to
            # anything, including another None - stay conservative (treat
            # as a conflict) rather than assume two url-less listings from
            # the same portal are the same real one.
            if None in combined or len(combined) > 1:
                return True
        return False

    def find(x):
        while parent[id(x)] is not x:
            parent[id(x)] = parent[id(parent[id(x)])]
            x = parent[id(x)]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra is rb:
            return
        if _portal_conflict(portal_urls[id(ra)], portal_urls[id(rb)]):
            return
        lo_sqm = min(sqm_range[id(ra)][0], sqm_range[id(rb)][0])
        hi_sqm = max(sqm_range[id(ra)][1], sqm_range[id(rb)][1])
        if hi_sqm - lo_sqm > 1:
            return
        lo_price = min(price_range[id(ra)][0], price_range[id(rb)][0])
        hi_price = max(price_range[id(ra)][1], price_range[id(rb)][1])
        if not prices_match(lo_price, hi_price):
            return
        parent[id(ra)] = rb
        sqm_range[id(rb)] = (lo_sqm, hi_sqm)
        price_range[id(rb)] = (lo_price, hi_price)
        merged_portal_urls = dict(portal_urls[id(rb)])
        for portal, urls in portal_urls[id(ra)].items():
            merged_portal_urls[portal] = merged_portal_urls.get(portal, set()) | urls
        portal_urls[id(rb)] = merged_portal_urls

    for l in with_sqm:
        na = normalize_area(l.get("area"))
        city_key = city_keys[id(l)]
        if not na or not city_key:
            continue
        key = price_bucket_key(l.get("price_eur"))
        # +/-2 buckets of margin around the 1-bucket-wide tolerance itself,
        # to absorb rounding at a bucket edge (two prices genuinely within
        # tolerance can still land in adjacent buckets if one rounds down
        # and the other rounds up right at the boundary).
        for dk in range(-2, 3):
            bucket = price_buckets.get((key + dk, na, city_key))
            if not bucket:
                continue
            for other in bucket:
                if other is l or l["portal"] == other["portal"]:
                    continue
                if (
                    prices_match(l.get("price_eur"), other.get("price_eur"))
                    and abs(effective_sqm[id(l)] - effective_sqm[id(other)]) <= 1
                ):
                    union(l, other)

    groups_by_root = {}
    for l in with_sqm:
        root = find(l)
        groups_by_root.setdefault(id(root), []).append(l)
    groups = list(groups_by_root.values())

    # Same (price bucket, area, city) co-partitioning as above, and for the
    # same reason - a plain price bucket collects every group near a
    # popular round price regardless of area/city, which is both slow and
    # pointless since areas_match() (called below via the group's
    # representative member) requires an exact area match anyway, and every
    # member of a with_sqm group already shares one city_key by
    # construction (city_keys[id(l)] gated every union() candidate above).
    group_buckets = {}
    # Same transitive-drift risk as the with_sqm union step, same fix:
    # checking a new sqm-less listing against only the group's first
    # member (not the group's actual current price range) let a group's
    # overall spread creep past prices_match()'s own tolerance one
    # attachment at a time - live-found, a 7-member group reached a 0.995%
    # spread (should be ~0.5%) this way.
    group_price_range = {}
    for g in groups:
        na = normalize_area(g[0].get("area"))
        group_city_key = city_keys[id(g[0])]
        if not na or not group_city_key:
            continue
        key = (price_bucket_key(g[0].get("price_eur")), na, group_city_key)
        group_buckets.setdefault(key, []).append(g)
        prices = [m["price_eur"] for m in g if m.get("price_eur")]
        group_price_range[id(g)] = (min(prices), max(prices))

    # Backlog item 64: this loop's own "first candidate group that
    # satisfies prices_match()" tie-break, and the with_sqm portal-conflict
    # guard above, were both tried in a version keyed on portal alone and
    # in a version that picked candidates by price-distance or refused
    # whenever more than one candidate qualified - both alternatives were
    # measured to break MORE already-correct real cross-portal matches than
    # this unchanged first-match rule does (see docs/decisions.md's
    # false-negative measurements for the numbers). This loop is
    # deliberately left exactly as it was before this backlog item -
    # unrelated to the with_sqm fix above, which only needed the
    # (portal, url) refinement to stop miscounting a same-url homes.bg
    # duplicate as a second real unit.
    solo_sqmless = []
    for l in without_sqm:
        na = normalize_area(l.get("area"))
        city_key = city_keys[id(l)]
        key = price_bucket_key(l.get("price_eur"))
        attached = False
        if na and city_key and l.get("price_eur"):
            for dk in range(-2, 3):
                candidates = group_buckets.get((key + dk, na, city_key))
                if not candidates:
                    continue
                for group in candidates:
                    if any(m["portal"] == l["portal"] for m in group):
                        continue
                    lo, hi = group_price_range[id(group)]
                    new_lo, new_hi = min(lo, l["price_eur"]), max(hi, l["price_eur"])
                    if prices_match(new_lo, new_hi):
                        group.append(l)
                        group_price_range[id(group)] = (new_lo, new_hi)
                        attached = True
                        break
                if attached:
                    break
        if not attached:
            solo_sqmless.append([l])

    return groups + solo_sqmless


# --- Type buckets + city keys - ported 1:1 from index.html ----------------
#
# City-key derivation (BG_CITIES, BCPEA_TYPE_LOOKUP, city_key_from_name(),
# latin/cyr title matching, listing_city_key() and friends) now lives in
# geo_utils.py, not here - every scraper's own compute_leads() needs the
# same logic for its area-average calculation (previously city-blind,
# grouping e.g. every "Център" together regardless of which town it's
# actually in - the same root cause as the cross-portal merge bug
# group_listings() below now guards against), and a scraper can't import
# this module (it would be backwards - this module already imports every
# scraper). Moving it to the shared geo_utils.py both fixes that and
# leaves exactly one implementation instead of two that could drift.

# "apartment"/"commercial" are classify_category()'s old 4-value output
# (geo_utils.py), still produced by portals not yet migrated to the
# nationwide expansion's category_classifier.py, which outputs bucket
# names directly (flat/garage/shop/business already match a bucket key,
# hence the identity entries) - both resolve through this one lookup
# during the portal-by-portal migration.
CATEGORY_TO_BUCKET = {
    "apartment": "flat", "house": "house", "land": "land", "commercial": "business",
    "flat": "flat", "garage": "garage", "shop": "shop", "business": "business",
}


def type_filter_bucket(l):
    # bcpea never goes through CATEGORY_TO_BUCKET/its own raw "category"
    # column below - that field is known-bad for this one portal (see
    # classify_category()'s docstring in geo_utils.py and the comment where
    # scraper_bcpea.py sets it): bcpea's auctions cover every property type,
    # so most of its own non-apartment listings get silently mislabeled
    # "apartment" there. bcpea_type_match() reads the same title through its
    # own precise controlled vocabulary instead, which is actually reliable.
    if l.get("portal") == "sales.bcpea.org":
        match = bcpea_type_match(l.get("title"))
        return match[1] if match else "other"
    return CATEGORY_TO_BUCKET.get(l.get("category"), "other")


# --- Oblast (province) keys, mirrored 1:1 in index.html -------------------
# 28 official Bulgarian oblasts. Sofia city (the capital, a single-city
# oblast of its own) and Sofia Province (the separate oblast that surrounds
# but excludes the capital) are two different entries - a listing whose
# city_key is "sofia" belongs to "sofia_grad" below, never plain "sofia",
# which would be ambiguous between the two.
BG_OBLASTS = [
    ("sofia_grad", "София-град"), ("sofia", "Софийска област"), ("plovdiv", "Пловдив"),
    ("varna", "Варна"), ("burgas", "Бургас"), ("ruse", "Русе"), ("stara_zagora", "Стара Загора"),
    ("pleven", "Плевен"), ("sliven", "Сливен"), ("dobrich", "Добрич"), ("shumen", "Шумен"),
    ("pernik", "Перник"), ("haskovo", "Хасково"), ("yambol", "Ямбол"), ("pazardzhik", "Пазарджик"),
    ("blagoevgrad", "Благоевград"), ("veliko_tarnovo", "Велико Търново"), ("vratsa", "Враца"),
    ("gabrovo", "Габрово"), ("vidin", "Видин"), ("kyustendil", "Кюстендил"),
    ("kardzhali", "Кърджали"), ("montana", "Монтана"), ("lovech", "Ловеч"),
    ("silistra", "Силистра"), ("razgrad", "Разград"), ("smolyan", "Смолян"),
    ("targovishte", "Търговище"),
]
BG_OBLAST_BY_NAME = {name: key for key, name in BG_OBLASTS}

# Every one of the 30 BG_CITIES sits inside exactly one of the 28 oblasts -
# this is the primary, highest-confidence signal: a listing that already
# resolved to a city_key gets its oblast for free, no extra text matching
# needed. Asenovgrad/Dupnitsa/Kazanlak/Dimitrovgrad/Svishtov are towns
# within a larger city's own province, not oblast centers themselves.
CITY_KEY_TO_OBLAST = {
    "sofia": "sofia_grad", "plovdiv": "plovdiv", "varna": "varna", "burgas": "burgas",
    "ruse": "ruse", "stara_zagora": "stara_zagora", "pleven": "pleven", "sliven": "sliven",
    "dobrich": "dobrich", "shumen": "shumen", "pernik": "pernik", "haskovo": "haskovo",
    "yambol": "yambol", "pazardzhik": "pazardzhik", "blagoevgrad": "blagoevgrad",
    "veliko_tarnovo": "veliko_tarnovo", "vratsa": "vratsa", "gabrovo": "gabrovo",
    "vidin": "vidin", "asenovgrad": "plovdiv", "kazanlak": "stara_zagora",
    "kyustendil": "kyustendil", "kardzhali": "kardzhali", "montana": "montana",
    "dimitrovgrad": "haskovo", "targovishte": "targovishte", "lovech": "lovech",
    "silistra": "silistra", "dupnitsa": "kyustendil", "svishtov": "veliko_tarnovo",
}


def oblast_key_from_name(name):
    if not name:
        return None
    return BG_OBLAST_BY_NAME.get(name.strip())


# Longest names first so "Стара Загора" doesn't prefix-match as a shorter
# name that happens to also be a prefix (none currently are, but keeps the
# general principle safe as oblasts get added).
BG_OBLAST_PREFIX_RE = re.compile(
    r"^(" + "|".join(re.escape(name) for _, name in sorted(BG_OBLASTS, key=lambda o: -len(o[1]))) + r")\b"
)


def oblast_key_from_name_prefix(name):
    if not name:
        return None
    match = BG_OBLAST_PREFIX_RE.match(name.strip())
    return BG_OBLAST_BY_NAME.get(match.group(1)) if match else None


# olx.bg's own scraper (scraper_olx.py) slices its crawl by all 28 oblasts
# and falls back to writing the oblast's own display name straight into
# "city"/"area" whenever no more specific city/village line is found on a
# card - that fallback text is itself already an exact oblast name, so the
# same exact/prefix matching used for the city/area fields above recovers
# real oblast-level data other portals never supply at all.
LATIN_OBLAST_TO_KEY = {name: CITY_KEY_TO_OBLAST[ck] for name, ck in LATIN_CITY_TO_KEY.items() if ck in CITY_KEY_TO_OBLAST}
LATIN_OBLAST_TO_KEY["razgrad"] = "razgrad"
LATIN_OBLAST_TO_KEY["smolyan"] = "smolyan"
LATIN_OBLAST_RE = re.compile(
    r"\b(" + "|".join(sorted((k.replace(" ", r"\s+") for k in LATIN_OBLAST_TO_KEY), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def latin_oblast_key_from_text(text):
    if not text:
        return None
    match = LATIN_OBLAST_RE.search(text)
    if not match:
        return None
    normalized = re.sub(r"\s+", " ", match.group(1).lower())
    return LATIN_OBLAST_TO_KEY.get(normalized)


# Unlike the city version (which requires a "гр. " prefix to avoid matching
# a city name that's actually part of someone else's area/neighborhood
# name), oblast names are distinctive enough multi-syllable proper nouns
# that a plain whole-text search is safe - used as the last-resort fallback
# after every more specific signal above has failed.
CYR_OBLAST_ANY_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for _, name in sorted(BG_OBLASTS, key=lambda o: -len(o[1]))) + r")\b"
)


def cyr_oblast_key_from_text(text):
    if not text:
        return None
    match = CYR_OBLAST_ANY_RE.search(text)
    return BG_OBLAST_BY_NAME.get(match.group(1)) if match else None


# --- Geo (lat/lng) oblast lookup - the authoritative signal when present --
# Real oblast boundary polygons (28 features, NUTS3-coded, sourced from
# yurukov/Bulgaria-geocoding - a maintained public dataset already used for
# Bulgarian civic-tech dashboards). This is the only way to actually
# distinguish Sofia Province from Sofia-grad: they share the same name
# ("София") in every portal's own text, so no text-matching rule can ever
# tell them apart - only a point-in-polygon test against their real,
# very-differently-shaped boundaries can. Point-in-ring uses the standard
# ray-casting algorithm; GeoJSON winding order puts the first ring as the
# outer boundary and any further rings as holes (a handful of oblasts -
# Sliven, Gabrovo, Burgas, Stara Zagora, Pernik - have real enclave
# geometry, not a data artifact, confirmed against the source polygons).
_OBLAST_BOUNDARIES_PATH = Path(__file__).parent / "data" / "bg_oblast_boundaries.json"


def _bbox(ring):
    lngs = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lngs), min(lats), max(lngs), max(lats)


def _load_oblast_boundaries():
    if not _OBLAST_BOUNDARIES_PATH.exists():
        return []
    raw = json.loads(_OBLAST_BOUNDARIES_PATH.read_text(encoding="utf-8"))
    boundaries = []
    for entry in raw:
        polygons = []
        for poly in entry["polygons"]:
            polygons.append({
                "exterior": poly["exterior"],
                "exterior_bbox": _bbox(poly["exterior"]),
                "holes": poly["holes"],
            })
        boundaries.append({"key": entry["key"], "polygons": polygons})
    return boundaries


OBLAST_BOUNDARIES = _load_oblast_boundaries()


def _point_in_ring(lng, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_intersect = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < x_intersect:
                inside = not inside
        j = i
    return inside


def _point_to_segment_distance_deg(lng, lat, p1, p2):
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((lng - x1) ** 2 + (lat - y1) ** 2) ** 0.5
    t = ((lng - x1) * dx + (lat - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return ((lng - proj_x) ** 2 + (lat - proj_y) ** 2) ** 0.5


def _point_to_ring_distance_deg(lng, lat, ring):
    n = len(ring)
    best = None
    j = n - 1
    for i in range(n):
        d = _point_to_segment_distance_deg(lng, lat, ring[j], ring[i])
        if best is None or d < best:
            best = d
        j = i
    return best


# Live-investigated (docs/backlog.md item 4, task 4): a real cluster of
# olx.bg listings near Близнаци (Varna's own coastline, e.g. lat/lng
# 43.1032247/27.9233784 - a genuine, live-scraped in-Bulgaria coordinate)
# came back unresolved from the strict point-in-ring test above even
# though they're plainly inside Varna oblast. Traced to the actual edge
# geometry, not a logic bug in _point_in_ring (that algorithm - standard
# ray-casting - was verified correct against the raw ring data): the
# nearest ring edge crossing at this point's own latitude sits at
# lng=27.923306, and the listing's own lng is 27.9233784 - about 8 meters
# further east, outside Varna's own simplified coastline polygon by less
# than the width of a single building. This is coastline-simplification +
# ordinary GPS/geocoding precision noise, not a wrong-oblast bug - the
# yurukov/Bulgaria-geocoding boundary data this project already uses (see
# OBLAST_BOUNDARIES' own comment above) is intentionally simplified for
# file size, and a beachfront-adjacent point is exactly where that
# simplification bites hardest. NEAR_BOUNDARY_TOLERANCE_DEG below adds a
# small, deliberately conservative fallback for this specific shape of
# miss - a point that's just barely OUTSIDE exactly one oblast's own
# boundary, not a name-matching gap and not a substitute for the strict
# test above (still tried first, unconditionally).
#
# ~0.003 degrees is roughly 200-330m in Bulgaria's own latitude range
# (parallels/meridians aren't equal-length in degrees - ~111km/degree
# latitude, ~78-85km/degree longitude across Bulgaria's 41-44N range) -
# comfortably past this real incident's ~8m gap and ordinary consumer-GPS/
# geocoder error, while still far too small to bridge the gap between two
# genuinely different oblasts anywhere they share a real inland border
# (every BG_OBLASTS pair's own shared-border segments are tens of km long,
# not meters) - and even then, only fires when exactly one oblast is
# within tolerance; two oblasts both within tolerance (a real shared
# border) is left unresolved rather than guessed, same "don't guess an
# ambiguous case" discipline as BG_MUNICIPALITY_TO_OBLAST's own "Бяла"
# exclusion.
NEAR_BOUNDARY_TOLERANCE_DEG = 0.003


# backlog item 28, sub-item 5 (Обзор, a real Burgas Black Sea coastal
# town just south of the Varna/Burgas oblast border): the same
# coastline-simplification root cause already fixed for Близнаци above
# (item 4 task 4), but the opposite failure shape - there, a real point
# fell just OUTSIDE the correct oblast's simplified polygon and came back
# unresolved; here, a handful of alo.bg listings' real, accurately-
# geocoded coordinates (e.g. 42.8445, 27.882196 - confirmed against the
# listing's own title text, "...директен достъп до плажа Обзор, област
# Бургас") fall just INSIDE Varna oblast's own simplified polygon by the
# strict point-in-ring test, so NEAR_BOUNDARY_TOLERANCE_DEG's fallback
# below (which only runs when the strict test finds nothing) never even
# gets a chance to run - the strict test already "succeeds", just for the
# wrong oblast. Hand-verified: this point sits ~0.0038deg inside Varna's
# own polygon edge but only ~0.0062deg outside Burgas's - both within
# ordinary coastline-simplification/GPS noise range, and both the
# listing's own city ("Бургас", one of the 30 hand-verified BG_CITIES,
# unambiguously Burgas oblast) and area ("Обзор", a real, unambiguous
# Burgas-oblast settlement - not on the "Бяла"/"Средец" ambiguous-name
# list) independently agree it's Burgas. A single, narrow, coordinate-
# keyed override for these exact confirmed-wrong points - not a general
# "prefer text over geo near any border" rule, which would risk
# regressing every OTHER correctly-resolved near-border geo match
# project-wide (the same reasoning CITY_AREA_OBLAST_OVERRIDE below
# already documents for its own narrow scope).
GEO_OBLAST_OVERRIDE = {
    (42.84397504, 27.88168498): "burgas",
    (42.8445, 27.882196): "burgas",
}


def oblast_key_from_latlng(lat, lng):
    if lat is None or lng is None:
        return None
    override = GEO_OBLAST_OVERRIDE.get((lat, lng))
    if override:
        return override
    for entry in OBLAST_BOUNDARIES:
        for poly in entry["polygons"]:
            min_lng, min_lat, max_lng, max_lat = poly["exterior_bbox"]
            if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
                continue
            if not _point_in_ring(lng, lat, poly["exterior"]):
                continue
            if any(_point_in_ring(lng, lat, hole) for hole in poly["holes"]):
                continue
            return entry["key"]

    near = set()
    for entry in OBLAST_BOUNDARIES:
        for poly in entry["polygons"]:
            min_lng, min_lat, max_lng, max_lat = poly["exterior_bbox"]
            # Widen the bbox check by the tolerance itself - the strict
            # bbox above would otherwise reject a point just outside it,
            # exactly the case this fallback exists for.
            if not (
                min_lng - NEAR_BOUNDARY_TOLERANCE_DEG <= lng <= max_lng + NEAR_BOUNDARY_TOLERANCE_DEG
                and min_lat - NEAR_BOUNDARY_TOLERANCE_DEG <= lat <= max_lat + NEAR_BOUNDARY_TOLERANCE_DEG
            ):
                continue
            if _point_to_ring_distance_deg(lng, lat, poly["exterior"]) <= NEAR_BOUNDARY_TOLERANCE_DEG:
                near.add(entry["key"])
    if len(near) == 1:
        return next(iter(near))
    return None


# Bulgaria's 28 oblasts are subdivided into 265 official municipalities
# ("общини") - a fixed, unambiguous administrative fact (each municipality
# sits in exactly one oblast) - live-sampled as the real cause of most of
# the "Others" bucket for homes.bg/olx.bg/sales.bcpea.org: these portals'
# own "city"/"area" text is usually already the settlement's real
# municipality or village name (e.g. homes.bg's own "city": "Несебър"),
# just not one of the 28 oblast *names* nor one of the 30 BG_CITIES this
# module already tracks - so every earlier check correctly fails to find
# an oblast, even though the municipality itself unambiguously determines
# one. Restricted to municipality names actually confirmed present in the
# real data and independently verified against Bulgaria's official
# administrative division - "Бяла" is deliberately NOT included here even
# though it's common in the data: it's a real, different municipality in
# BOTH Varna and Ruse oblasts, genuinely ambiguous from the name alone,
# the same class of mistake that caused real data corruption earlier this
# project (see verify_geocode_qualifiers.py's docstring) - left
# unclassified rather than guessed. A settlement within Sofia city's own
# municipality (Столична община) - Банкя/Нови Искър/Панчарево/Кремиковци/
# Бистрица/Лозен/Владая/Желява and others - maps to "sofia_grad", not
# "sofia" (Sofia Province is a genuinely separate, surrounding oblast).
BG_MUNICIPALITY_TO_OBLAST = {
    # Burgas oblast
    "Айтос": "burgas", "Камено": "burgas", "Карнобат": "burgas", "Малко Търново": "burgas",
    "Несебър": "burgas", "Поморие": "burgas", "Приморско": "burgas", "Руен": "burgas",
    "Созопол": "burgas", "Сунгурларе": "burgas", "Царево": "burgas",
    "Равда": "burgas", "Кошарица": "burgas", "Синеморец": "burgas", "Резово": "burgas",
    # Varna oblast (Byala deliberately excluded - see docstring above)
    "Аврен": "varna", "Аксаково": "varna", "Белослав": "varna", "Долни Чифлик": "varna",
    "Провадия": "varna", "Суворово": "varna", "Ветрино": "varna", "Вeтринo": "varna",
    "Девня": "varna", "Долен чифлик": "varna", "Вълчи Дол": "varna",
    # Dobrich oblast
    "Балчик": "dobrich", "Генерал Тошево": "dobrich", "Каварна": "dobrich", "Тервел": "dobrich",
    "Крушари": "dobrich", "Шабла": "dobrich", "Кранево": "dobrich", "Рогачево": "dobrich",
    "Оброчище": "dobrich", "Топола": "dobrich", "Българево": "dobrich", "Дуранкулак": "dobrich",
    "Кардам": "dobrich",
    # Sofia Province (Софийска област) - the municipalities, distinct from
    # Sofia CITY's own sub-municipal districts listed separately below.
    "Божурище": "sofia", "Ботевград": "sofia", "Годеч": "sofia", "Горна Малина": "sofia",
    "Долна Баня": "sofia", "Драгоман": "sofia", "Елин Пелин": "sofia", "Етрополе": "sofia",
    "Златица": "sofia", "Ихтиман": "sofia", "Костенец": "sofia", "Костинброд": "sofia",
    "Мирково": "sofia", "Пирдоп": "sofia", "Правец": "sofia", "Самоков": "sofia",
    "Своге": "sofia", "Сливница": "sofia", "Чавдар": "sofia", "Челопеч": "sofia",
    "Антон": "sofia", "Софийска": "sofia",
    # Sofia-grad's own sub-municipal districts (villages/towns administered
    # directly by Sofia's own Столична община, NOT Sofia Province).
    #
    # "Лозен" is deliberately NOT listed here (2026-09-24, Placy - full
    # free-text gazetteer mining audit) even though Sofia-grad genuinely has
    # its own "Лозен" district (район Панчарево) - it's a 4-way real-name
    # collision, not a 2-way one like "Бяла"/"Средец": WebSearch + ekatte.com
    # (the same authoritative EKATTE source this project's own gazetteer is
    # built from) independently confirm THREE more, completely unrelated,
    # real villages also bare-named "Лозен" - EKATTE 44046 (Strazhitsa
    # municipality, Veliko Tarnovo oblast), EKATTE 44053 (Septemvri
    # municipality, Pazardzhik oblast), and EKATTE 44077 (Lyubimets
    # municipality, Haskovo oblast). Confirmed live impact: 42 olx.bg/bcpea
    # records nationwide with city=area="Лозен" (no oblast qualifier
    # captured in either structured field) were ALL silently resolving to
    # sofia_grad - including ones whose own title explicitly names a
    # different oblast ("...Лозен, област Пазарджик...",
    # "...Лозен, област Велико Търново...", "...с. Лозен, Хасково...").
    # Worse, all 41 of the olx.bg ones shared one corrupted cached geocode
    # result for the query "Лозен, Лозен, България" (data/geocode_cache.json)
    # that resolves to a point inside Sofia-grad's own boundary regardless
    # of which real "Лозен" the listing is actually in - see
    # listing_oblast_key()'s geo-priority-over-text design, and the matching
    # data remediation in docs/decisions.md. A listing whose own text
    # explicitly names a different oblast now self-heals via
    # cyr_oblast_key_from_text() once the wrong coordinate is removed;
    # a listing with no such qualifier correctly becomes unresolved rather
    # than silently wrong, per this project's "never guess" rule. A listing
    # with city="София" (not merely area="Лозен") still resolves to
    # sofia_grad correctly and is UNAFFECTED by this exclusion, since
    # listing_city_key()/CITY_KEY_TO_OBLAST already handles that case
    # independently of this table.
    "Банкя": "sofia_grad", "Нови Искър": "sofia_grad", "Панчарево": "sofia_grad",
    "Кремиковци": "sofia_grad", "Бистрица": "sofia_grad",
    "Владая": "sofia_grad", "Желява": "sofia_grad",
    # Kyustendil oblast
    "Бобов дол": "kyustendil", "Бобовдол": "kyustendil", "Бобошево": "kyustendil",
    "Невестино": "kyustendil", "Рила": "kyustendil", "Сапарева баня": "kyustendil",
    "Трекляно": "kyustendil", "Koчериново": "kyustendil", "Кочериново": "kyustendil",
    # Blagoevgrad oblast
    "Банско": "blagoevgrad", "Белица": "blagoevgrad", "Кресна": "blagoevgrad",
    "Петрич": "blagoevgrad", "Разлог": "blagoevgrad", "Сандански": "blagoevgrad",
    "Сатовча": "blagoevgrad", "Симитли": "blagoevgrad", "Струмяни": "blagoevgrad",
    "Якоруда": "blagoevgrad", "Гоце Делчев": "blagoevgrad", "Хаджидимово": "blagoevgrad",
    "Гърмен": "blagoevgrad",
    # Plovdiv oblast
    "Асеновград": "plovdiv", "Брезово": "plovdiv", "Хисаря": "plovdiv", "Калояново": "plovdiv",
    "Карлово": "plovdiv", "Куклен": "plovdiv", "Лъки": "plovdiv", "Марица": "plovdiv",
    "Първомай": "plovdiv", "Перущица": "plovdiv", "Раковски": "plovdiv", "Родопи": "plovdiv",
    "Садово": "plovdiv", "Съединение": "plovdiv", "Стамболийски": "plovdiv", "Сопот": "plovdiv",
    "Калофер": "plovdiv", "Марково": "plovdiv", "Първенец": "plovdiv", "Труд": "plovdiv",
    "Скутаре": "plovdiv", "Крумово": "plovdiv", "Брестовица": "plovdiv", "Тополово": "plovdiv",
    "Ягодово": "plovdiv", "Белащица": "plovdiv", "Граф Игнатиево": "plovdiv",
    # "Куртово Конаре" was WRONGLY listed under Pazardzhik oblast below until
    # 2026-09-24 (Placy, full free-text gazetteer mining audit) - confirmed
    # via ekatte.com (EKATTE 40717): it's a village in Стамболийски
    # municipality (already correctly listed as Plovdiv oblast two lines
    # up), Plovdiv oblast, not Pazardzhik at all. Live impact was
    # coincidentally zero today (every one of its 43 nationwide records -
    # all homes.bg, all currently removed - already has city="Пловдив" or
    # "Стамболийски", both of which independently resolve to Plovdiv oblast
    # via city text BEFORE this table is ever consulted), but the entry
    # itself was simply wrong and would misfire the moment a record with no
    # usable city field but area="Куртово Конаре" showed up.
    "Куртово Конаре": "plovdiv",
    # Pazardzhik oblast
    "Батак": "pazardzhik", "Белово": "pazardzhik", "Брацигово": "pazardzhik",
    "Лесичово": "pazardzhik", "Панагюрище": "pazardzhik", "Пещера": "pazardzhik",
    "Ракитово": "pazardzhik", "Септември": "pazardzhik", "Сърница": "pazardzhik",
    "Стрелча": "pazardzhik", "Велинград": "pazardzhik", "Мало Конаре": "pazardzhik",
    # Veliko Tarnovo oblast
    "Елена": "veliko_tarnovo", "Горна Оряховица": "veliko_tarnovo", "Лясковец": "veliko_tarnovo",
    "Павликени": "veliko_tarnovo", "Полски Тръмбеш": "veliko_tarnovo", "Стражица": "veliko_tarnovo",
    "Сухиндол": "veliko_tarnovo", "Златарица": "veliko_tarnovo", "Арбанаси": "veliko_tarnovo",
    "Драгижево": "veliko_tarnovo", "Хотница": "veliko_tarnovo", "Поликраище": "veliko_tarnovo",
    "Самоводене": "veliko_tarnovo", "Пчелище": "veliko_tarnovo", "Леденик": "veliko_tarnovo",
    "Беляковец": "veliko_tarnovo", "Първомайци": "veliko_tarnovo", "Присово": "veliko_tarnovo",
    # Gabrovo oblast
    "Дряново": "gabrovo", "Севлиево": "gabrovo", "Трявна": "gabrovo",
    # Shumen oblast
    "Велики Преслав": "shumen", "Върбица": "shumen", "Каолиново": "shumen",
    "Каспичан": "shumen", "Никола Козлево": "shumen", "Нови Пазар": "shumen",
    "Смядово": "shumen", "Венец": "shumen",
    # Yambol oblast
    "Болярово": "yambol", "Елхово": "yambol", "Стралджа": "yambol", "Тунджа": "yambol",
    # Stara Zagora oblast
    "Братя Даскалови": "stara_zagora", "Чирпан": "stara_zagora", "Гурково": "stara_zagora",
    "Мъглиж": "stara_zagora", "Николаево": "stara_zagora", "Опан": "stara_zagora",
    "Павел баня": "stara_zagora", "Раднево": "stara_zagora", "Енина": "stara_zagora",
    "Старозагорски бани": "stara_zagora", "Гълъбово": "stara_zagora",
    # Ruse oblast (Byala deliberately excluded - see docstring above)
    "Борово": "ruse", "Две могили": "ruse", "Иваново": "ruse", "Сливо поле": "ruse",
    "Ценово": "ruse", "Ветово": "ruse", "Червена вода": "ruse", "Николово": "ruse",
    "Щръклево": "ruse",
    # Silistra oblast
    "Алфатар": "silistra", "Дулово": "silistra", "Главиница": "silistra",
    "Кайнарджа": "silistra", "Ситово": "silistra", "Тутракан": "silistra", "Калипетрово": "silistra",
    # Razgrad oblast
    "Исперих": "razgrad", "Кубрат": "razgrad", "Лозница": "razgrad", "Самуил": "razgrad",
    "Цар Калоян": "razgrad", "Завет": "razgrad",
    # Targovishte oblast
    "Антоново": "targovishte", "Омуртаг": "targovishte", "Опака": "targovishte",
    "Попово": "targovishte",
    # Pernik oblast
    "Брезник": "pernik", "Земен": "pernik", "Ковачевци": "pernik", "Радомир": "pernik",
    "Трън": "pernik",
    # "Кладница" and "Рударци" were WRONGLY hardcoded to "sofia_grad" above
    # until 2026-09-24 (Placy) - both are real Vitosha-foothill villages
    # close enough to Sofia to be commonly (and, per a genuine 2020s
    # secession petition covered in local press, controversially) mistaken
    # for part of it, but both are administratively, unambiguously part of
    # Pernik municipality/oblast, confirmed via ekatte.com (EKATTE 37174 for
    # Кладница, EKATTE 63152 for Рударци - the same authoritative source
    # this project's own gazetteer is built from), not Sofia's own Столична
    # община. Confirmed live impact via a full free-text gazetteer mining
    # pass: every one of Рударци's 17 nationwide records (100%) and 13/18 of
    # Кладница's (the other 5 already had city="Перник" set explicitly,
    # already correctly resolving via city_key independently of this table)
    # were silently mislabeled sofia_grad - several with their own title
    # explicitly, repeatedly stating "област Перник"/"община Перник". Unlike
    # "Лозен" above, no second, different real "Кладница"/"Рударци"
    # settlement was found anywhere else in Bulgaria (not ambiguous, simply
    # wrong) - a direct correction, not an exclusion.
    "Кладница": "pernik", "Рударци": "pernik",
    # Vidin oblast
    "Белоградчик": "vidin", "Бойница": "vidin", "Брегово": "vidin", "Чупрене": "vidin",
    "Димово": "vidin", "Грамада": "vidin", "Кула": "vidin", "Макреш": "vidin",
    "Ново село": "vidin", "Ружинци": "vidin",
    # Montana oblast
    "Берковица": "montana", "Бойчиновци": "montana", "Брусарци": "montana",
    "Чипровци": "montana", "Георги Дамяново": "montana", "Лом": "montana",
    "Медковец": "montana", "Вълчедръм": "montana", "Вършец": "montana", "Якимово": "montana",
    # Vratsa oblast
    "Бяла Слатина": "vratsa", "Борован": "vratsa", "Козлодуй": "vratsa",
    "Криводол": "vratsa", "Мездра": "vratsa", "Мизия": "vratsa", "Оряхово": "vratsa",
    "Роман": "vratsa", "Хайредин": "vratsa",
    # Lovech oblast
    "Априлци": "lovech", "Летница": "lovech", "Луковит": "lovech", "Тетевен": "lovech",
    "Троян": "lovech", "Угърчин": "lovech", "Ябланица": "lovech", "Рибарица": "lovech",
    "Шипково": "lovech", "Лесидрен": "lovech",
    # Pleven oblast
    "Белене": "pleven", "Долна Митрополия": "pleven", "Долни Дъбник": "pleven",
    "Гулянци": "pleven", "Кнежа": "pleven",
    "Никопол": "pleven", "Пордим": "pleven", "Червен бряг": "pleven",
    # Haskovo oblast
    "Димитровград": "haskovo", "Харманли": "haskovo", "Ивайловград": "haskovo",
    "Любимец": "haskovo", "Маджарово": "haskovo", "Минерални бани": "haskovo",
    "Симеоновград": "haskovo", "Стамболово": "haskovo", "Свиленград": "haskovo",
    "Тополовград": "haskovo",
    # Kardzhali oblast
    "Ардино": "kardzhali", "Черноочене": "kardzhali", "Джебел": "kardzhali",
    "Кирково": "kardzhali", "Крумовград": "kardzhali", "Момчилград": "kardzhali",
    # Smolyan oblast
    "Баните": "smolyan", "Борино": "smolyan", "Чепеларе": "smolyan", "Девин": "smolyan",
    "Доспат": "smolyan", "Мадан": "smolyan", "Неделино": "smolyan", "Рудозем": "smolyan",
    "Златоград": "smolyan", "Проглед": "smolyan",
    # Sliven oblast
    "Котел": "sliven", "Нова Загора": "sliven", "Твърдица": "sliven",
}


# BG_MUNICIPALITY_TO_OBLAST above is municipality-*seat*-only (265 names) -
# structural, not a "just missing a few" gap: most of Bulgaria's ~5,300 real
# settlements sit in a municipality whose SEAT has a different name, so
# they're invisible to it even though their own municipality unambiguously
# determines an oblast (docs/backlog.md item 4, task 2 - live-sampled:
# 1,545 distinct real settlement names unmatched, e.g. Типченица,
# Изворово, Илинденци, Цалапица).
#
# data/bg_settlements_to_oblast.json is the real fix: derived from
# yurukov/Bulgaria-geocoding's settlements.csv + municipalities.csv (the
# SAME maintained public dataset this module already uses for
# OBLAST_BOUNDARIES above - not a new, unvetted source) via
# settlement -> municipality code -> oblast, cross-validated against every
# name already in BG_MUNICIPALITY_TO_OBLAST above (27 of the resulting 28
# municipality-code prefixes derived with ZERO contradictions against this
# table's own hand-verified entries; the 28th, "SOF" = Столична, Sofia-
# grad's own single municipality, is unambiguous by construction - Sofia-
# grad has exactly one). 4,513 distinct settlement names found in the raw
# data; of those, 521 (~11.5%) appear under more than one oblast anywhere
# in Bulgaria and are EXCLUDED from the generated file entirely - same
# "don't guess an ambiguous name" discipline as this table's own "Бяла"
# exclusion above, just applied programmatically instead of by hand. That
# automated rule independently rediscovered "Бяла" (spans 3 oblasts in the
# full settlement-level data, not just the 2 known from municipality
# seats) and "Средец" (spans 3 oblasts on its own, before even accounting
# for the separate Sofia-grad-district collision Missy flagged) with no
# special-casing needed - a good sign the rule is doing the right thing
# generally, not just on the two cases already known about. The 3,784
# names left (well past the 1,545 actually observed missing, so real
# margin) are purely ADDITIVE: any name already decided by hand above
# (checked first in oblast_key_from_municipality() below, unchanged) is
# never overridden by this generated file, even on the rare case both
# would have agreed anyway.
#
# Regeneration: re-run the derivation described above against a fresh
# settlements.csv/municipalities.csv if Bulgaria's own municipality map
# ever changes (rare - the last real changes were years ago) or if a
# future audit finds more names needing the ambiguous-exclusion treatment
# by hand (add them to _MANUALLY_EXCLUDED_SETTLEMENTS below rather than
# editing the generated file directly).
_SETTLEMENTS_TO_OBLAST_PATH = Path(__file__).parent / "data" / "bg_settlements_to_oblast.json"


def _load_settlements_to_oblast():
    if not _SETTLEMENTS_TO_OBLAST_PATH.exists():
        return {}
    return json.loads(_SETTLEMENTS_TO_OBLAST_PATH.read_text(encoding="utf-8"))


# Names the automated ambiguity check above wouldn't catch on its own,
# because one side of the real-world collision isn't a separate EKATTE
# settlement at all (so it never appears in settlements.csv to trigger the
# "spans more than one oblast" exclusion) - specifically, a Sofia-grad
# intra-city district sharing a name with a genuine, different settlement
# elsewhere. "Средец" already excludes itself automatically (a real
# settlement of that name also exists in Stara Zagora/Smolyan, not just
# Burgas), kept here as a documented belt-and-suspenders entry since it's
# the exact case Missy flagged live. Add future finds here, never by
# editing the generated JSON file directly.
_MANUALLY_EXCLUDED_SETTLEMENTS = {"Средец"}

BG_SETTLEMENT_TO_OBLAST = {
    name: key for name, key in _load_settlements_to_oblast().items()
    if name not in _MANUALLY_EXCLUDED_SETTLEMENTS
}


# Portals prefix a settlement-type label onto the name itself
# ("гр.Несебър" - town, "с.Владая" - village, "кв. Виница" - quarter) and
# sometimes leave stray leading punctuation from a malformed split
# (", гр.Несебър") - strip both before the exact-match lookup, the same
# normalization principle city_key_from_name() already applies for its
# own trailing-suffix case.
_MUNICIPALITY_PREFIX_RE = re.compile(r"^[\s,]*(?:гр\.?|с\.?|кв\.?|ж\.?к\.?|в\.?з\.?|м-т)\s*", re.IGNORECASE)


def oblast_key_from_municipality(name):
    if not name:
        return None
    stripped = name.strip()
    normalized = _MUNICIPALITY_PREFIX_RE.sub("", stripped).strip()
    # BG_MUNICIPALITY_TO_OBLAST (hand-verified, including its typo-
    # tolerant variants for real scraper quirks) always wins first -
    # BG_SETTLEMENT_TO_OBLAST (generated, ~3,800 names wide) never
    # overrides an existing decision, only adds coverage beyond it. See
    # BG_SETTLEMENT_TO_OBLAST's own comment for why the two never actually
    # disagree where both happen to cover the same name.
    return (
        BG_MUNICIPALITY_TO_OBLAST.get(stripped)
        or BG_MUNICIPALITY_TO_OBLAST.get(normalized)
        or BG_SETTLEMENT_TO_OBLAST.get(stripped)
        or BG_SETTLEMENT_TO_OBLAST.get(normalized)
    )



# backlog item 20: imot.bg tags every listing's `city` field from which of
# its 25 CITY_SLUGS query pages produced it, not from the card's own text -
# but imot.bg's own `grad-lovech` page itself returns listings physically in
# Червен бряг (Pleven oblast, ~55km from Lovech; a pre-1999 okrug legacy -
# one listing's own URL literally encodes "obshtina-lovech", imot.bg's own
# site data, not a scraper misread; see docs/decisions.md's 2026-09-22
# entry for the full evidence). A general "trust area text over city_key
# whenever they disagree and area resolves via BG_MUNICIPALITY_TO_OBLAST"
# fix was tried and rejected after checking it against real committed
# data: it produces MORE false positives than it fixes. imot.bg's own URLs
# prove several other (city, area) disagreements are a real in-city quarter
# coincidentally sharing a name with a distant municipality seat, not a
# misfiling - e.g. "...grad-vratsa-samuil" (35 currently-ungeocoded
# listings would have flipped Враца->Самуил's real municipality-seat
# oblast, Разград) and "...grad-sliven-novo-selo" (30 listings; Ново село
# is Vidin's municipality seat name, but this is Sliven's own quarter) -
# these two examples alone account for 65 of the 166 total (city, area)
# disagreements the general rule would have touched across the dataset.
# So this is a
# single, exact, evidence-confirmed (city, area) pair override, not a
# general rule - only extend it with the same two-sided confirmation
# (real coordinates AND a portal's own URL text agreeing) demonstrated
# here, never by table membership alone.
#
# 2026-09-23 (Placy investigation into a real undercount report): widened
# from imot.bg-only, exact-raw-string matching to any portal, matched by
# normalize_area() instead of a literal string - live-confirmed the SAME
# "Ловеч"+Червен-бряг pair recurs on imoti.net (own URL:
# ".../lovech/lovech-cherven-brjag/...", city="Ловеч", area="Cherven Bryag"
# in Latin script - normalize_area() maps it to the same "cherven bryag"
# key the Cyrillic imot.bg/imoti.bg records already use) and on imoti.bg
# itself (city="Ловеч", area="Червен бряг") - both missed by the old
# imot.bg-only gate. This isn't the general rule rejected above: it's the
# same one already-confirmed (city, real-settlement) pair, just matched
# portal-independently and spelling-independently rather than needing a
# separate literal entry per portal's own text formatting - the underlying
# fact being encoded ("Ловеч" + something that really is Cherven Bryag
# really is Pleven oblast, regardless of which portal said so or how it
# spelled the town name) doesn't depend on which portal reported it.
CITY_AREA_OBLAST_OVERRIDE = {
    ("Ловеч", "cherven bryag"): "pleven",
}


# 2026-09-27 (Placy, Sofia/Plovdiv count + "map not active" investigation):
# imoti.net's own detail-page HTML apparently embeds at least one other
# "latitude"/"longitude" JSON pair somewhere on the page besides a listing's
# own real pin (a site-wide widget or similar - this sandbox's egress proxy
# still blocks direct access to imoti.net, same block noted throughout this
# file's history, so the exact page element responsible couldn't be
# confirmed directly). extract_coords_imoti_net()'s plain regex search picks
# up whichever "latitude"/"longitude" pair appears first in the raw HTML,
# which is wrong whenever that isn't the listing's own real coordinate.
#
# Live-confirmed against real committed data (not guessed): of imoti.net's
# active listings that have coordinates at all, 1,686 cluster within ~300m
# of one of the two fixed points below (both in central Sofia, near
# Sofia University/NDK) - 1,017 of those 1,686 (60%) have their OWN "city"
# text field naming a DIFFERENT real city entirely (626 Пловдив, 169
# Бургас, 110 Варна, 55 Стара Загора, 26 Враца, and 10 more oblasts with a
# handful each), proving the coordinate isn't real per-listing geocoding
# for at least that many - every one of those 1,017 was resolving to the
# wrong "sofia_grad" oblast in production before this fix, since geo
# coordinates are checked before any text signal below.
#
# Rather than guess the real coordinate (unreachable from this sandbox) or
# blanket-distrust every coordinate anywhere near this point (669 of the
# 1,686 genuinely ARE tagged "София" and must not regress), this only
# distrusts the suspect point when an independent, resolvable city/area
# text signal actively disagrees with "sofia_grad" - the same
# "corroborate before overriding a geo signal, never guess" discipline
# CITY_AREA_OBLAST_OVERRIDE and listing_city_key()'s own field-vs-title
# check already use. Every listing found in either cluster has a non-empty
# "city" field (0 of 1,686 had none), so this check never has to fall back
# to guessing when text is missing - the corroborating signal is always
# available for the listings this actually matters for.
_IMOTI_NET_SUSPECT_COORD_CLUSTERS = [
    (42.6961, 23.3254),
    (42.7010, 23.3210),
]
_IMOTI_NET_SUSPECT_COORD_TOLERANCE_DEG = 0.003


def _imoti_net_coord_is_suspect(lat, lng):
    return any(
        abs(lat - clat) <= _IMOTI_NET_SUSPECT_COORD_TOLERANCE_DEG
        and abs(lng - clng) <= _IMOTI_NET_SUSPECT_COORD_TOLERANCE_DEG
        for clat, clng in _IMOTI_NET_SUSPECT_COORD_CLUSTERS
    )


def _text_oblast_key(l):
    # The same (city, area) text-based oblast lookup listing_oblast_key()
    # itself falls through to lower down - factored out so the suspect-
    # coordinate check above can corroborate against it without duplicating
    # the lookup order.
    for field in ("city", "area"):
        value = l.get(field)
        if value:
            key = (
                oblast_key_from_name(value)
                or oblast_key_from_name_prefix(value)
                or oblast_key_from_municipality(value)
            )
            if key:
                return key
    return None


def listing_oblast_key(l, city_key):
    geo_key = oblast_key_from_latlng(l.get("lat"), l.get("lng"))
    if geo_key:
        if (
            geo_key == "sofia_grad"
            and l.get("portal") == "imoti.net"
            and l.get("lat") is not None
            and l.get("lng") is not None
            and _imoti_net_coord_is_suspect(l["lat"], l["lng"])
        ):
            text_key = _text_oblast_key(l)
            if text_key and text_key != "sofia_grad":
                geo_key = None  # fall through to the text-based checks below instead
        if geo_key:
            return geo_key
    override = CITY_AREA_OBLAST_OVERRIDE.get((l.get("city"), normalize_area(l.get("area"))))
    if override:
        return override
    if city_key:
        key = CITY_KEY_TO_OBLAST.get(city_key)
        if key:
            return key
    if l.get("portal") == "sales.bcpea.org":
        settlement = bcpea_settlement_from_title(l.get("title"))
        if settlement:
            key = oblast_key_from_name(settlement)
            if key:
                return key
            key = oblast_key_from_name_prefix(settlement)
            if key:
                return key
            key = oblast_key_from_municipality(settlement)
            if key:
                return key
        # Last resort, bcpea-only (2026-09-24, Placy): bcpea's own
        # description is real, official auction-notice legal text (unlike
        # every other portal's free-text ad copy), and "Столична община" -
        # Sofia city's own single, official municipality name - is as
        # unambiguous an administrative signal as a coordinate: there is
        # exactly one in all of Bulgaria. Needed because "Лозен" was
        # excluded above as a genuine 4-way name collision (see that
        # comment) - bcpea_92319 ("Лозен", no city field, no coordinates)
        # would otherwise regress from correctly-resolved to unresolved
        # purely as a side effect of that exclusion, even though its own
        # description explicitly, unambiguously says "село Лозен, Столична
        # община – район Панчарево". Checked against every one of the 23
        # active/removed bcpea records whose description mentions "Столична
        # община" nationwide: 22 already independently resolve to
        # sofia_grad via their own settlement text and are unaffected by
        # this fallback (it only ever fires after that lookup already
        # failed); this is the one exception, now fixed instead of silently
        # regressed.
        if settlement and "Столична община" in (l.get("description") or ""):
            return "sofia_grad"
        return None
    for field in ("city", "area"):
        value = l.get(field)
        if value:
            key = oblast_key_from_name(value)
            if key:
                return key
            key = oblast_key_from_name_prefix(value)
            if key:
                return key
            key = oblast_key_from_municipality(value)
            if key:
                return key
    title = l.get("title")
    if title and "," in title:
        last_segment = title.rsplit(",", 1)[1].strip()
        key = oblast_key_from_name(last_segment)
        if key:
            return key
        key = oblast_key_from_name_prefix(last_segment)
        if key:
            return key
    key = latin_oblast_key_from_text(title)
    if key:
        return key
    key = cyr_oblast_key_from_text(title)
    if key:
        return key
    # No known oblast matched (a small town/village not near any of the 30
    # cities, with no oblast name anywhere in its own text either) - left
    # unclassified rather than guessed; the frontend's "Others" bucket
    # covers it, same as an unclassified city_key.
    return None


# --- Load, merge, shape rows -----------------------------------------------

def load_all_listings():
    all_listings = []
    for portal, filename in PORTAL_FILES.items():
        path = DATA_DIR / filename
        if not path.exists():
            print(f"WARNING: {path} not found, skipping {portal}")
            continue
        listings = load_json_any(path)
        for l in listings:
            l.setdefault("portal", portal)
        all_listings.extend(listings)
    return all_listings


def merged_id_for(sources):
    members = sorted(f"{s['portal']}:{s['id']}" for s in sources)
    digest = hashlib.sha256(",".join(members).encode("utf-8")).hexdigest()
    return "m_" + digest[:16]


# Every field a listing_sources row copies straight from a leads_*.json
# entry, confirmed against the real union of keys actually present across
# all 8 committed files (not guessed from scraper source). "id" and
# "portal" are handled separately (source_id / portal columns).
#
# property_type_raw/construction_type/built_year/completion_status/
# floor_number/floor_qualifier/features/has_elevator/furnished/
# has_central_heating/agency_name/agency_website (2026-09-24) are new
# fields from geo_utils.extract_specs_alo()/extract_contact_alo()
# (alo.bg) and, added the same day, geo_utils.extract_specs_bazar()/
# extract_contact_bazar() (bazar.bg - a narrower subset: only
# property_type_raw/construction_type/floor_number/agency_name/
# agency_website, per that portal's own real spec table - see
# extract_specs_bazar()'s own comment) - every other portal's (and, for
# the bazar-only-missing subset, bazar.bg's own) leads_*.json entries
# simply don't have these keys, so s.get(f) below is already the right
# "None for every row this doesn't apply to" behavior, same as e.g.
# site_posted_at already being alo.bg/imoti.net-only. Needs the matching
# `alter table ... add
# column if not exists` migration in supabase/schema.sql applied by hand
# in the Supabase SQL editor before these actually land in the live
# tables - see that file's own comment, and upsert()'s _MISSING_COLUMN_RE
# handling further down, for why a sync still succeeds even before that
# migration is applied (the missing column(s) are stripped and retried,
# not a hard failure).
#
# coords_checked/specs_checked/detail_checked (2026-09-29): NOT content
# fields - these are the backfill scripts' own "has this listing's detail
# page actually been (re)visited" markers (backfill_detail_bazar.py/
# backfill_detail_homes.py/backfill_detail_bcpea.py/backfill_detail_imot.py/
# backfill_detail_imoti_net.py/backfill_detail_olx.py - see each one's own
# module docstring), synced so the frontend's empty-state copy
# (renderSpecsPanel()/renderSellerPanel() in index.html) can tell "this
# listing's own page hasn't been checked yet" apart from "checked, this
# portal's page genuinely doesn't show it" instead of asserting the latter
# for every empty field by default. Real, measured gap this closes: a
# live audit of data/history_bazar.json.gz on 2026-09-29 found 43,185
# bazar.bg listings already marked coords_checked before extract_specs_
# bazar()/extract_contact_bazar() existed (2026-09-24) - only 21.5% of
# those have any specs, vs. 99.2% for listings checked on/after that date,
# and 91.7% of the no-specs ones already have a real description, proving
# their page WAS fetched successfully - the gap is a stale-flag bug (now
# fixed, see backfill_detail_bazar.py's specs_checked tier), not bazar.bg
# "not publishing" the field. alo.bg's own equivalent markers
# (_detail_fetched/_photos_checked/_gallery_specs_rechecked/
# _description_title_echo_rechecked) are deliberately NOT included here -
# their leading underscore is this codebase's existing convention for
# "internal bookkeeping, not meant to leave history_*.json" (see
# scraper_alo.py's own _DETAIL_ONLY_FIELDS) - surfacing an alo.bg-specific
# checked signal to the frontend is a separate follow-up, not done here.
SOURCE_FIELDS = [
    "url", "photo", "photos", "price_eur", "sqm", "area", "title", "description",
    "category", "category_confidence", "lat", "lng", "price_per_sqm", "price_history",
    "price_drop_count", "drop_pct", "days_on_market", "score", "source_status", "removed_at",
    "area_avg_price_per_sqm", "pct_vs_area_avg", "site_updated_at", "site_posted_at",
    "property_type_raw", "construction_type", "built_year", "completion_status",
    "floor_number", "floor_qualifier", "features", "has_elevator", "furnished",
    "has_central_heating", "agency_name", "agency_website",
    "coords_checked", "specs_checked", "detail_checked",
]

# "First seen" date for a listing/group, precomputed server-side (backlog
# item 6 slice-1 regression fix) so the frontend doesn't need price_history
# in the bulk merged_listings list-view fetch just to show it. Mirrors
# index.html's own listingFirstSeenDate() EXACTLY (same price_history[0]
# .date read, same null-if-missing behavior) - just moved here so it can
# be stored as a real first_seen_at column instead. Do not "improve" this
# to e.g. min() across every price_history entry - the point is that this
# produces the identical value the frontend already computed, just earlier.
def first_seen_at_for(row):
    ph = row.get("price_history")
    if isinstance(ph, list) and len(ph) > 0 and isinstance(ph[0], dict) and ph[0].get("date"):
        return ph[0]["date"]
    return None


# merged_listings has no source_status/removed_at columns - a merged group's
# equivalent is the "status" field computed separately (available/sold,
# only true once every member source agrees it's gone), not any one
# source's own status. Sending those two columns to merged_listings gets
# PostgREST's "could not find the column" error since there's no such
# column to write to.
MERGED_FIELDS = [f for f in SOURCE_FIELDS if f not in ("source_status", "removed_at")]

# Fields a merged listing takes from the group's richest-media source
# (`media_best`) rather than its highest-motivation-score source (`best`) -
# see MEDIA_FIELDS' selection logic below (`select_media_best()`) for why:
# `score` (compute_motivation_score() in geo_utils.py) is price-drop/days-
# on-market/price-vs-area-average - it has no relationship at all to how
# many photos a cross-posted source has or whether it has a real
# description, so picking `best`'s photo/photos/description here (as the
# generic MERGED_FIELDS loop below still does for every other field)
# regularly ships a merged listing with a single photo and no description
# from the top-scoring source, while a different cross-posted source for
# the exact same real property sitting right next to it in the same group
# has, say, 8 photos and a full seller-written description. Backlog:
# reported directly by the site owner ("every single listing needs to have
# multiple photos... priority for our listing the one with multiple photos
# and full description").
MEDIA_FIELDS = ("photo", "photos", "description")


def _photo_count(s):
    # "photos" (plural) is the richer, portal-scraped photo-gallery field -
    # confirmed against the real committed leads_*.json(.gz) files (not
    # assumed): where present it's always a flat list of image URL strings
    # (never a list of objects, on any of the 8 portals), and 3 portals
    # (alo.bg, imot.bg, sales.bcpea.org... in fact all 8 except imoti.bg)
    # only populate it for a subset of their own listings - imoti.bg never
    # has the key at all. "photo" (singular) is the one field every portal
    # always has when it has any image at all, so a source with no "photos"
    # list but a real "photo" still counts as having exactly 1 photo, not
    # 0 - that's the literal "original listing with only one photo" case
    # from the bug report, not an absent-data case.
    photos = s.get("photos")
    if isinstance(photos, list) and photos:
        return len(photos)
    return 1 if s.get("photo") else 0


def _has_real_description(s):
    # Reuses geo_utils._looks_like_title_echo() - the same "extracted text
    # is just a substring of the listing's own title, not real prose" check
    # already confirmed against real alo.bg production data (see that
    # function's own comment) - rather than duplicating that logic here.
    # Generalized on purpose: the same failure shape (a "description" field
    # that's actually an echo of the title) is exactly what this fix must
    # not treat as a real description on ANY portal, not just alo.bg.
    desc = s.get("description")
    if not desc or not str(desc).strip():
        return False
    return not _looks_like_title_echo(desc, s.get("title"))


def select_media_best(sorted_sources):
    """Picks which group member's photo/photos/description the merged
    listing should show, independently of `best` (sorted_sources[0], the
    highest motivation-score source used for every other MERGED_FIELDS
    value). Selection order:
      1. Most photos (_photo_count()).
      2. Tiebreak: has a real, non-title-echo description.
      3. Second tiebreak: sorted_sources' own existing motivation-score
         order (so behavior stays deterministic when several sources are
         equally rich on both signals above).
    If no source in the group has more than 1 photo, there is nothing
    richer to prefer on the one signal the user actually complained about
    (a single-photo listing being shown when a multi-photo one existed for
    the same property), so this falls back to `best` unchanged - today's
    behavior - rather than picking a source based on description alone.
    For a group of 1 (the ~majority of listings, which aren't cross-posted
    at all) this is always a complete no-op: `max_photos` is that one
    source's own count, `candidates` is `[sorted_sources[0]]`, so the
    single `return` at the bottom always yields `sorted_sources[0]` -
    identical to `best`.
    """
    max_photos = max(_photo_count(s) for s in sorted_sources)
    if max_photos <= 1:
        return sorted_sources[0]
    # Preserves sorted_sources' motivation-score order at every step below,
    # so the last remaining tiebreak (comparing candidates written to `pool`)
    # is exactly "whichever qualifying source scores highest".
    candidates = [s for s in sorted_sources if _photo_count(s) == max_photos]
    with_description = [s for s in candidates if _has_real_description(s)]
    pool = with_description or candidates
    return pool[0]


def build_rows(all_listings):
    groups = group_listings(all_listings)

    listing_source_rows = []
    merged_rows = []

    for sources in groups:
        sorted_sources = sorted(sources, key=lambda s: s.get("score") or 0, reverse=True)
        status = "sold" if all(s.get("source_status") == "removed" for s in sorted_sources) else "available"
        mid = merged_id_for(sorted_sources)

        for s in sorted_sources:
            row = {"portal": s["portal"], "source_id": s["id"], "merged_id": mid}
            for f in SOURCE_FIELDS:
                row[f] = s.get(f)
            row["type_bucket"] = type_filter_bucket(s)
            city_key = listing_city_key(s)
            row["city_key"] = city_key
            row["oblast_key"] = listing_oblast_key(s, city_key)
            row["area_key"] = normalize_area(s.get("area")) or None
            row["first_seen_at"] = first_seen_at_for(row)
            listing_source_rows.append(row)

        best = sorted_sources[0]
        merged = {
            "id": mid,
            "portal": best["portal"],
            "status": status,
            "member_count": len(sorted_sources),
            "member_portals": sorted({s["portal"] for s in sorted_sources}),
        }
        for f in MERGED_FIELDS:
            merged[f] = best.get(f)
        # photo/photos/description specifically come from whichever group
        # member has the richest media, not from `best` (highest motivation
        # score) like every other field above - see select_media_best()'s
        # own comment. Deliberately overwrites just these 3 keys after the
        # generic MERGED_FIELDS loop above rather than excluding them from
        # it, so this stays a clearly-scoped override, not a second parallel
        # field list to keep in sync with MERGED_FIELDS.
        media_best = select_media_best(sorted_sources)
        for f in MEDIA_FIELDS:
            merged[f] = media_best.get(f)
        merged["type_bucket"] = type_filter_bucket(best)
        city_key = listing_city_key(best)
        merged["city_key"] = city_key
        merged["oblast_key"] = listing_oblast_key(best, city_key)
        merged["area_key"] = normalize_area(best.get("area")) or None
        merged["first_seen_at"] = first_seen_at_for(merged)
        merged_rows.append(merged)

    return dedupe_rows(listing_source_rows, ("portal", "source_id")), dedupe_rows(merged_rows, ("id",))


def dedupe_rows(rows, key_fields):
    # Supabase's upsert is a single INSERT ... ON CONFLICT DO UPDATE per
    # batch - Postgres rejects the whole batch (error 21000, "ON CONFLICT
    # DO UPDATE command cannot affect row a second time") if two rows in
    # it share the same conflict key, so a single duplicate anywhere in
    # ~250k+ rows fails the entire sync. Live-found: a batch of
    # listing_sources rows failed this way. group_listings() deliberately
    # never unions two listings from the same portal (cross-portal
    # matching is the whole point - see its own comments), so if one
    # portal's own leads_*.json ever has two entries whose extracted "id"
    # collides (a rare scraper-side id-extraction bug, not reproduced in
    # the current committed leads_*.json files - whatever caused it was
    # already gone by the next run's fresh data), those two never merge
    # into one group and surface as two separate rows sharing the same
    # (portal, source_id) - exactly this failure. Rather than chase a
    # transient one-bad-day upstream cause across 8+ leads-computation
    # call sites, the dedupe belongs here, at the one place a duplicate
    # actually becomes fatal.
    seen = {}
    dupes = 0
    for row in rows:
        key = tuple(row.get(f) for f in key_fields)
        if key in seen:
            dupes += 1
            continue
        seen[key] = row
    if dupes:
        print(f"WARNING: dropped {dupes} duplicate row(s) sharing a {key_fields} value before upsert - "
              f"kept the first occurrence of each. This means the same key appeared more than once in "
              f"the source data, which shouldn't happen - worth investigating upstream if it recurs.")
    return list(seen.values())


# --- Supabase REST upsert ---------------------------------------------------

BATCH_SIZE = 500
MAX_HTTP_RETRIES = 4
RETRY_BACKOFF_SECONDS = 5

# 2026-09-25: run 36106917335 hit a bare requests.exceptions.ReadTimeout
# (60s) on the very last upsert() request of that run, after successfully
# processing hundreds of thousands of rows over ~37 minutes with no other
# failures - a real, not-permanently-broken call that just needed a bit
# more patience under whatever tail load Supabase was under at that
# moment, the exact same "bad luck once" shape MAX_HTTP_RETRIES/
# RETRY_BACKOFF_SECONDS above already exist to absorb for a Postgres
# statement timeout. It still exhausted all MAX_HTTP_RETRIES attempts and
# raised, because every attempt used the same fixed 60s per-request
# timeout with no extra headroom for a request that specifically times
# out (as opposed to a fast 4xx/5xx failure, which a longer per-request
# wait can't help). Two independent, low-risk changes, not a rewrite of
# the retry logic: a longer per-request timeout (REQUEST_TIMEOUT_SECONDS,
# raised from the old 60-second literal every call site used to hardcode,
# to 120) gives a genuinely slow-but-alive request more room to finish
# before giving up on it at all, and one extra retry attempt specifically for a
# Timeout/ReadTimeout (TIMEOUT_EXTRA_RETRIES) - never for other
# RequestException subclasses, which are already adequately covered by
# MAX_HTTP_RETRIES and shouldn't get quietly more lenient - covers
# exactly the "one more chance for the thing that actually timed out"
# case this incident was.
REQUEST_TIMEOUT_SECONDS = 120
TIMEOUT_EXTRA_RETRIES = 1


def request_with_retries(method, url, **kwargs):
    # A live production sync failed outright on a single Postgres error
    # 57014 ("canceling statement due to statement timeout") on one batch
    # out of 335, with zero retry logic anywhere in this file - that one
    # transient timeout crashed the whole sync via resp.raise_for_status(),
    # which (since this ran before the cleanup step further down in main())
    # also meant the stale-row cleanup this same fix introduces never got a
    # chance to run at all. A statement timeout under momentary load is
    # exactly the kind of thing a short retry absorbs - confirmed the
    # surrounding batches on either side of the one that failed succeeded
    # fine, so this isn't a permanently-broken query, just bad luck once.
    # Every Supabase HTTP call in this file goes through this now, not just
    # upsert() - the same failure mode applies equally to the GET/DELETE
    # calls the cleanup functions make.
    max_attempts = MAX_HTTP_RETRIES + TIMEOUT_EXTRA_RETRIES
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.Timeout as e:
            # See REQUEST_TIMEOUT_SECONDS/TIMEOUT_EXTRA_RETRIES above -
            # a Timeout specifically gets TIMEOUT_EXTRA_RETRIES beyond the
            # normal budget, since it's the one failure mode a longer wait
            # can plausibly fix on its own.
            if attempt == max_attempts:
                raise
            print(f"  request timed out (attempt {attempt}/{max_attempts}): {e} - retrying {url}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        except requests.RequestException as e:
            if attempt == MAX_HTTP_RETRIES:
                raise
            print(f"  request exception (attempt {attempt}/{MAX_HTTP_RETRIES}): {e} - retrying {url}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        if resp.ok or attempt == max_attempts:
            return resp
        print(f"  request failed (attempt {attempt}/{MAX_HTTP_RETRIES}): "
              f"{resp.status_code} {resp.text[:300]} - retrying {url}")
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)


# A real production failure (2026-09-22): backlog item 18 added `area_key`
# to every listing_sources/merged_listings row this script builds, but the
# schema.sql migration that adds the column itself is a manual step in the
# Supabase SQL editor - nothing in this pipeline applies it automatically.
# Every sync since that code shipped failed outright on Postgres/PostgREST's
# own "PGRST204: Could not find the '<col>' column... in the schema cache"
# for a column this script sends but the live table doesn't have yet - and
# since this crashed before the cleanup step further down in main() ever
# ran, a code change with a pending manual migration step was silently
# taking down the ENTIRE sync, not just failing to populate that one new
# column. Detect this specific error, strip the missing column from every
# row for this table (not just the current batch, so later batches don't
# hit the same wall), and retry - the sync stays fully functional either
# side of whenever the migration actually gets applied, and the moment it
# is, this stops triggering (PostgREST's schema cache has the column, no
# more 204) with no code change needed.
_MISSING_COLUMN_RE = re.compile(r"Could not find the '([^']+)' column of '([^']+)' in the schema cache")


def upsert(base_url, headers, table, rows, on_conflict):
    # 2026-10-10 fix (run 38041416146/job 114182255312): the FINAL
    # listing_sources batch of a 443,247-row run (rows 438500-443247,
    # 98.9% already upserted successfully) hit a sustained Postgres 57014
    # ("canceling statement due to statement timeout") through every one
    # of request_with_retries()'s own retry attempts, and the uncaught
    # HTTPError this used to raise unconditionally crashed the whole
    # script - losing the merged_listings upsert below (main() never
    # reached that line) and every cleanup step after it, not just this
    # one batch's ~4,747 rows. This is the write-side sibling of the
    # read-side 57014 handling added for check_portal_counts()/
    # delete_stale_merged_listings() (see StatementTimeoutError's own
    # comment further down, backlog item 76) - same transient-timeout
    # signature, same _is_statement_timeout() check, but a different fix
    # shape because a write is not a read: skipping it does NOT mean
    # "nothing lost, re-check next cycle", it means specific rows
    # genuinely did not land in Supabase this run.
    #
    # Confirmed safe to skip and move on anyway, by actually reading the
    # rest of this file, not assumed: main() calls load_all_listings() /
    # build_rows() fresh from the FULL committed data/*.json(.gz) files on
    # every single run (see load_all_listings()'s own loop over
    # PORTAL_FILES - no incremental delta tracking anywhere in this
    # pipeline), and every row here upserts with
    # "Prefer: resolution=merge-duplicates" on a stable conflict key
    # (on_conflict=portal,source_id / on_conflict=id). That makes this
    # whole function idempotent and self-healing across runs: the very
    # next scheduled sync (6h later via scrape.yml, or 24h via
    # scrape-large.yml - see each workflow's own cron) re-sends this exact
    # row set, including whatever this run's skipped batch(es) contained,
    # along with everything else. A skipped batch leaves those specific
    # rows stale until then, not permanently lost and not in any
    # inconsistent state (PostgREST wraps each batch request in its own
    # transaction - a 57014-aborted batch commits none of its rows, so
    # there's no partial-row corruption to worry about either). This is
    # the exact same "stale a bit longer, self-heals next cycle" safety
    # property the read-side fix relies on, just reasoned through
    # independently here because the write case needed its own check.
    #
    # Scoped to this one function (used by BOTH the confirmed
    # listing_sources call site above and the merged_listings call site
    # right after it in main()) deliberately, not just the observed
    # table: this isn't guessing that merged_listings has the same bug,
    # it's the literal same code path under the same kind of load, so
    # the identical fix covers it for free rather than needing its own
    # separate confirmed occurrence first.
    #
    # Deliberately narrow, like _is_statement_timeout()'s own contract: a
    # GENUINELY different error at this same call site (auth failure, a
    # malformed request, a different Postgres error code) must still
    # fall through to resp.raise_for_status() below and crash loudly -
    # only this one confirmed, specific, transient signature degrades
    # gracefully.
    skipped_batches = 0
    skipped_rows = 0
    i = 0
    while i < len(rows):
        batch = rows[i : i + BATCH_SIZE]
        resp = request_with_retries(
            "POST",
            f"{base_url}/rest/v1/{table}?on_conflict={on_conflict}",
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=batch,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            match = _MISSING_COLUMN_RE.search(resp.text)
            if match and match.group(2) == table:
                missing_col = match.group(1)
                print(f"::warning::{table} is missing column '{missing_col}' on the live Supabase table "
                      f"(pending manual schema.sql migration, see docs/backlog.md) - stripping it from "
                      f"every {table} row and retrying so this sync isn't blocked on that migration landing.")
                for row in rows:
                    row.pop(missing_col, None)
                continue  # retry this same batch index, now without the missing column
            if _is_statement_timeout(resp):
                batch_end = min(i + len(batch), len(rows))
                print(f"::warning::{table}: batch {i}-{batch_end} of {len(rows)} hit a sustained Postgres "
                      f"statement timeout (57014) after every retry attempt - skipping this batch and "
                      f"continuing with the rest of the upsert. These {len(batch)} row(s) did NOT land in "
                      f"Supabase this run; they will be re-upserted (along with everything else) on the "
                      f"next scheduled sync, since this script always rebuilds its full row set from the "
                      f"committed data files rather than tracking incremental deltas.")
                skipped_batches += 1
                skipped_rows += len(batch)
                i += BATCH_SIZE
                continue
            print(f"ERROR upserting into {table} (batch starting at {i}): {resp.status_code} {resp.text[:500]}")
            resp.raise_for_status()
        print(f"  upserted {len(batch)} rows into {table} ({min(i + len(batch), len(rows))}/{len(rows)})")
        i += BATCH_SIZE
    if skipped_batches:
        print(f"::warning::{table}: {skipped_rows} row(s) across {skipped_batches} batch(es) were skipped "
              f"this run due to repeated Postgres statement timeouts (57014) - not a code bug, just "
              f"transient load; the next scheduled sync's full re-upsert will catch them up. See the "
              f"per-batch warning(s) above for exactly which rows.")


# --- Data-loss safety guard --------------------------------------------
# A scraper's own parser can silently break (a site markup change) without
# raising any exception - it just returns 0 or near-0 listings for that
# portal, and nothing upstream of this file (the scraper's own exit code,
# the git commit step) can tell that apart from a genuinely quiet run.
# Without this guard, delete_stale_merged_listings()/delete_stale_listing_
# sources() below would read "not in what I just loaded from disk" as
# "genuinely gone" and delete it from the live tables in this same sync -
# for a portal the size of alo.bg (83,939 listings at the time this guard
# was added), that's an unrecoverable, one-run deletion of real, live data
# over what was actually just a broken scrape. Comparing this run's count
# against what's already live catches that before any DELETE fires: stale
# data sitting in the live tables one cycle longer than necessary is
# always the safer failure than deleting real, current listings a scraper
# just failed to see.
MIN_PORTAL_RATIO = 0.5
MIN_PORTAL_ABSOLUTE = 10


class DataLossGuardTripped(Exception):
    pass


# Two real production incidents, six days apart (run 37198858695/job
# 111426216987, 2026-10-04; run 38019738767/job 114117861396, 2026-10-10 -
# see docs/backlog.md for the full writeup), both crashed this entire
# script with an uncaught requests.exceptions.HTTPError AFTER the real
# upsert work above had already completed successfully - one on
# _fetch_stored_source_ids()'s listing_sources read (inside
# check_portal_counts()), the other on delete_stale_merged_listings()'s own
# merged_listings read. Both are the exact same shape: a keyset-paginated
# verification/cleanup GET (order=X&limit=1000&X=gt.<cursor>) whose final
# retry attempt (request_with_retries() already retries every attempt
# MAX_HTTP_RETRIES+ times) still comes back as Postgres's own 57014
# ("canceling statement due to statement timeout"), not any other error.
# request_with_retries() can't tell this apart from a genuinely broken
# request on its own - it just returns the last response, ok or not - so
# each read loop below checks for this one specific signature itself and
# raises this instead of blindly calling resp.raise_for_status(). main()
# catches StatementTimeoutError narrowly (see its own comment) to skip just
# that one cleanup step for this run rather than crash the whole sync -
# the actual data write already succeeded by the time either of these
# fires, so losing "Sync complete" over a stale-row cleanup timeout is
# disproportionate. Deliberately NOT a catch-all for "any 500" or "any
# HTTPError" at these call sites: a real auth failure (401/403), a
# genuinely malformed request, or some other Postgres error must still
# raise_for_status() and crash loudly exactly as before - only this one
# confirmed, narrow, transient-and-already-twice-recurring failure shape
# degrades gracefully.
class StatementTimeoutError(Exception):
    pass


def _is_statement_timeout(resp):
    """True only for Postgres's own 57014 on a 500 response - never for
    any other status code or error body, so a different real failure at
    the same call site (auth, malformed request, a different Postgres
    error) still falls through to the normal resp.raise_for_status()
    path below and raises loudly instead of being silently treated as
    "safe to skip cleanup for"."""
    if resp.status_code != 500:
        return False
    try:
        return resp.json().get("code") == "57014"
    except ValueError:
        # Not JSON (or empty body) - fall back to a plain substring check
        # of the raw text rather than assuming it can't be this error.
        return '"code":"57014"' in resp.text


def _fetch_stored_source_ids(base_url, headers, portal):
    stored_ids = set()
    cursor = None
    while True:
        query_params = {"select": "source_id", "portal": f"eq.{portal}", "order": "source_id", "limit": 1000}
        if cursor is not None:
            query_params["source_id"] = f"gt.{cursor}"
        resp = request_with_retries(
            "GET", f"{base_url}/rest/v1/listing_sources", headers=headers, params=query_params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if not resp.ok:
            if _is_statement_timeout(resp):
                raise StatementTimeoutError(
                    f"listing_sources read for portal={portal!r} timed out (Postgres 57014) "
                    f"after cursor={cursor!r}"
                )
            resp.raise_for_status()
        rows = resp.json()
        stored_ids.update(r["source_id"] for r in rows)
        if len(rows) < 1000:
            break
        cursor = rows[-1]["source_id"]
    return stored_ids


def check_portal_counts(base_url, headers, current_by_portal):
    """Fetches each portal's currently-live source_ids (so callers doing
    the actual deletion don't have to re-fetch them) and raises
    DataLossGuardTripped if any portal's freshly-scraped count looks like
    a broken scrape rather than a real drop - below MIN_PORTAL_ABSOLUTE in
    absolute terms, or below MIN_PORTAL_RATIO of what's already live.
    A portal with nothing live yet (a first-ever sync) has no baseline to
    protect and always passes."""
    stored_by_portal = {}
    problems = []
    for portal in PORTAL_FILES:
        stored_ids = _fetch_stored_source_ids(base_url, headers, portal)
        stored_by_portal[portal] = stored_ids

        stored_count = len(stored_ids)
        current_count = len(current_by_portal.get(portal, set()))
        if stored_count == 0:
            continue
        if current_count < MIN_PORTAL_ABSOLUTE:
            problems.append(
                f"{portal}: {current_count} listing(s) this run (was {stored_count} live) - "
                f"near-zero, treating as a broken scrape rather than a real drop"
            )
        elif current_count / stored_count < MIN_PORTAL_RATIO:
            problems.append(
                f"{portal}: {current_count} listings this run vs {stored_count} already live "
                f"({current_count / stored_count:.0%}, below the {MIN_PORTAL_RATIO:.0%} floor)"
            )

    if problems:
        raise DataLossGuardTripped(
            "Refusing to delete any stale rows this run - one or more portals produced "
            "implausibly few listings, which looks like a broken scrape, not a real drop:\n  "
            + "\n  ".join(problems)
            + "\nExisting listing_sources/merged_listings rows are untouched. Investigate the "
            "scraper(s) named above before the next scheduled sync - this guard will otherwise "
            "keep blocking cleanup (harmlessly) every run until the count recovers."
        )
    return stored_by_portal


def delete_stale_merged_listings(base_url, headers, current_ids):
    # merged_id changes whenever a group's real membership changes (a dedup
    # fix, a category/city reclassification) - upsert-only syncing leaves
    # the OLD id's row behind forever when that happens, since nothing ever
    # deletes it. Left unchecked across several merged_id-changing fixes
    # this session, that silently grew merged_listings to 244,217 rows
    # against only 167,611 actually current (~76,600 orphaned) - which was
    # enough to push the frontend's deep-page queries in fetchAllRows()
    # past Postgres's statement timeout (error 57014) and take the whole
    # site down ("Could not load listings data.", live-reproduced via
    # probe_site_load.py). Deleting what the current sync didn't touch
    # keeps this table's size tied to the real current listing count,
    # permanently, not just as a one-time cleanup.
    # Keyset pagination (WHERE id > <cursor>), not OFFSET - this table is
    # exactly the one whose OFFSET-paged growth caused the outage this
    # function exists to prevent a repeat of (see this function's own
    # comment); fetching the id list to clean it up must not hit the same
    # depth-scales-with-offset statement-timeout wall against the current,
    # still-bloated table before cleanup has even run.
    stored_ids = set()
    cursor = None
    while True:
        query_params = {"select": "id", "order": "id", "limit": 1000}
        if cursor is not None:
            query_params["id"] = f"gt.{cursor}"
        resp = request_with_retries(
            "GET", f"{base_url}/rest/v1/merged_listings", headers=headers, params=query_params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if not resp.ok:
            if _is_statement_timeout(resp):
                # See StatementTimeoutError's own comment above - this is
                # the exact call site that crashed run 38019738767/job
                # 114117861396 (2026-10-10).
                raise StatementTimeoutError(
                    f"merged_listings read timed out (Postgres 57014) after cursor={cursor!r}"
                )
            resp.raise_for_status()
        rows = resp.json()
        stored_ids.update(r["id"] for r in rows)
        if len(rows) < 1000:
            break
        cursor = rows[-1]["id"]

    stale = list(stored_ids - current_ids)
    if not stale:
        print("  no stale rows in merged_listings")
        return

    for i in range(0, len(stale), BATCH_SIZE):
        batch = stale[i : i + BATCH_SIZE]
        resp = request_with_retries(
            "DELETE",
            f"{base_url}/rest/v1/merged_listings",
            headers=headers,
            params={"id": "in.(" + ",".join(batch) + ")"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            print(f"ERROR deleting stale merged_listings (batch starting at {i}): {resp.status_code} {resp.text[:500]}")
            resp.raise_for_status()
    print(f"  deleted {len(stale)} stale rows from merged_listings")


def delete_stale_listing_sources(base_url, headers, current_by_portal, stored_by_portal):
    # Same orphaned-row problem as merged_listings (see
    # delete_stale_merged_listings()'s comment), for the per-source table -
    # a listing that stops appearing in a portal's own leads_*.json (sold,
    # delisted, or reclassified into a different merged group) otherwise
    # stays in listing_sources forever. Not the table that broke the site
    # this time (it isn't bulk-loaded at all - see index.html's loadData()
    # comment), but the same unbounded growth is still real waste worth
    # cleaning up here while already fixing the sibling table.
    #
    # stored_by_portal is passed in (from check_portal_counts(), called
    # first in main()) rather than re-fetched here - it already had to
    # fetch every portal's live source_ids to run the data-loss guard
    # before any DELETE was allowed to proceed at all, so re-fetching the
    # same data a second time would just be wasted round-trips.
    for portal, current_ids in current_by_portal.items():
        stored_ids = stored_by_portal.get(portal, set())
        stale = list(stored_ids - current_ids)
        if not stale:
            continue
        for i in range(0, len(stale), BATCH_SIZE):
            batch = stale[i : i + BATCH_SIZE]
            resp = request_with_retries(
                "DELETE",
                f"{base_url}/rest/v1/listing_sources",
                headers=headers,
                params={"portal": f"eq.{portal}", "source_id": "in.(" + ",".join(batch) + ")"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if not resp.ok:
                print(f"ERROR deleting stale listing_sources for {portal} (batch starting at {i}): "
                      f"{resp.status_code} {resp.text[:500]}")
                resp.raise_for_status()
        print(f"  deleted {len(stale)} stale rows from listing_sources for {portal}")


def main():
    supabase_url = os.environ.get("SUPABASE_URL")
    secret_key = os.environ.get("SUPABASE_SECRET_KEY")
    if not supabase_url or not secret_key:
        print("SUPABASE_URL and SUPABASE_SECRET_KEY must be set", file=sys.stderr)
        sys.exit(1)

    headers = {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }

    all_listings = load_all_listings()
    print(f"Loaded {len(all_listings)} raw listings across {len(PORTAL_FILES)} portals")

    listing_source_rows, merged_rows = build_rows(all_listings)
    print(f"Computed {len(merged_rows)} merged listings from {len(listing_source_rows)} sources")

    base_url = supabase_url.rstrip("/")
    upsert(base_url, headers, "listing_sources", listing_source_rows, on_conflict="portal,source_id")
    upsert(base_url, headers, "merged_listings", merged_rows, on_conflict="id")

    current_by_portal = {}
    for r in listing_source_rows:
        current_by_portal.setdefault(r["portal"], set()).add(r["source_id"])

    print("Checking this run's per-portal counts against what's already live before cleaning up stale rows...")
    stored_by_portal = None
    try:
        stored_by_portal = check_portal_counts(base_url, headers, current_by_portal)
    except DataLossGuardTripped as e:
        print(f"::error::{e}", file=sys.stderr)
        print(
            "This run's upserts above already completed (new/updated listings are live) - "
            "only the stale-row cleanup was skipped, so nothing was deleted."
        )
        sys.exit(1)
    except StatementTimeoutError as e:
        # See StatementTimeoutError's own comment above
        # (_fetch_stored_source_ids()) - a transient Postgres read timeout
        # on this verification query, after this run's real upsert work
        # above already completed successfully. Degrade gracefully: skip
        # ALL stale-row cleanup for this run only (stored_by_portal stays
        # None, so neither delete_stale_merged_listings() nor
        # delete_stale_listing_sources() below runs - the latter needs
        # stored_by_portal's per-portal id sets, which this failure means
        # we never finished collecting) rather than crash the whole sync.
        # Nothing is deleted; a future run's cleanup will catch up.
        print(
            f"::warning::check_portal_counts() timed out reading stored source_ids from Supabase "
            f"(Postgres statement_timeout, 57014) after this run's real upsert work above already "
            f"completed successfully: {e}. Skipping all stale-row cleanup for this run only - a "
            f"future run's cleanup will catch up. Nothing was deleted."
        )

    if stored_by_portal is not None:
        print("Cleaning up stale rows left behind by earlier syncs...")
        current_merged_ids = {r["id"] for r in merged_rows}
        try:
            delete_stale_merged_listings(base_url, headers, current_merged_ids)
        except StatementTimeoutError as e:
            # Same narrow degrade-gracefully handling as above, but scoped
            # to just this one table: stored_by_portal is already in hand
            # (unaffected by this failure), so delete_stale_listing_sources()
            # below still runs normally - only merged_listings' own cleanup
            # is skipped for this run.
            print(
                f"::warning::delete_stale_merged_listings() timed out reading stored merged_listings "
                f"ids from Supabase (Postgres statement_timeout, 57014): {e}. Skipping merged_listings "
                f"cleanup for this run only - a future run's cleanup will catch up. Nothing was deleted "
                f"from merged_listings."
            )
        delete_stale_listing_sources(base_url, headers, current_by_portal, stored_by_portal)

    print("Sync complete")


if __name__ == "__main__":
    main()
