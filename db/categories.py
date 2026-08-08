"""One spelling per department.

Home Depot's breadcrumbs do not agree with themselves. The same aisle
arrives as "Outdoor" on one item and "Outdoors" on the next; its doors and
windows arrive under three different roots -- "Doors", "Windows", and
"Doors & Windows" -- and storage arrives both as "Storage" and as
"Storage & Organization". `crumb()` in tools/sweep.py takes the top
breadcrumb verbatim, which is right: guessing at a taxonomy we do not own
would be worse than recording what they said. So the disagreement lands
here, at the point of writing, where it is cheap to settle and settled
once.

It matters because the department chips in the app are built from distinct
values. Two spellings is two chips for one aisle, and switching one off
leaves the other behind -- a filter that does not do what it says.

**Explicit map, not a rule.** Stripping a trailing "s" would fold
"Outdoor" into "Outdoors" and also turn "Tools" into "Tool" and
"Appliances" into "Appliance". No rule separates a plural department from
a plural-looking one. Thirty-odd strings is a list, so this is a list.

**Only variants of one department are merged.** Departments that are
merely adjacent -- Patio and Outdoors, Garage and Storage, Bath and
Plumbing, Building and Lumber & Composites -- stay apart. Collapsing those
would be a taxonomy opinion; this file only fixes spelling.

An unrecognised department passes through unchanged rather than being
forced into a bucket. A new aisle showing up under its own name is the
correct outcome; silently filing it under "Other" would hide it.
"""

# The spellings we keep. A case variant of any of these normalises to the
# form written here, so "OUTDOORS" and "outdoors" cannot become two chips.
CANONICAL = [
    "Appliances", "Auto", "Bath", "Building", "Cleaning", "Decor",
    "Doors & Windows", "Electrical", "Flooring", "Furniture", "Garage",
    "Garden", "Grills", "Hardware", "Holiday", "Ladders", "Lighting",
    "Lumber & Composites", "Outdoors", "Paint", "Patio", "Pet", "Plumbing",
    "Pools", "Safety", "Smart Home", "Storage", "Tools",
]

# Distinct strings that name the same department. Keyed casefolded.
# `db/migrations/005_category_aliases.sql` backfills exactly these, and
# tests/test_categories.py fails if the two ever disagree.
ALIASES = {
    "outdoor": "Outdoors",
    "storage & organization": "Storage",
    "doors": "Doors & Windows",
    "windows": "Doors & Windows",
}

_LOOKUP = {name.casefold(): name for name in CANONICAL}
_LOOKUP.update(ALIASES)


def canonical(category):
    """The one spelling for this department.

    Passes `None` and blank through as `None` so callers can keep using
    `coalesce(excluded.category, product.category)` without a category of
    `""` overwriting a real one.
    """
    if category is None:
        return None
    name = " ".join(str(category).split())
    if not name:
        return None
    return _LOOKUP.get(name.casefold(), name)
