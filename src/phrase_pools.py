"""Never-repeat spoken pools. CallState tracks what this call already used."""

from __future__ import annotations

import random
from collections.abc import MutableSequence, Sequence

# Stage 1 — 0ms, before the tool network request.
STAGE_1 = (
    "righto, let's have a look for ya",
    "let me just have a look for ya",
    "righto, pulling up the diary",
    "two ticks, I'll pull that up",
    "won't be a sec — let me have a squiz",
)

# Stage 2 — ~1200ms.
STAGE_2 = (
    "doo doo doo",
    "dum de dum",
    "nup... nup... hang on",
    "mm, c'moooon, load",
    "right, where are ya...",
)

# Stage 3 — ~3000ms.
STAGE_3 = (
    "bear with me, it's having a think about it",
    "sorry, this booking system's older than I am",
    "it's having a bit of a think — hang on a tick",
    "bear with me, diary's being slow",
)

# Stage 4 — ~6000ms conversational fill.
STAGE_4 = (
    "still here, just waiting on the screen",
    "yeah it's being a bit of a shocker this morning",
    "won't be long, I can see it spinning",
    "hang on a tick, it's nearly there",
)

# Stage 5 is a real take_message fallback, not only a line — but she still speaks.
STAGE_5 = (
    "I'm not gonna sit here in silence — I'll take a message and get the team to ring you",
    "diary's stuck, so I'll leave a note for the team to call you back rather than guess a time",
)

# ERROR path — 429 / timeout. Never invent availability.
STAGE_ERROR = (
    "this thing's having a sook — bear with me",
    "yeah the screen's not playing, hang on",
    "sorry, it's thrown a wobbly, two secs",
)

# EMPTY / UNKNOWN path — do not say chockers.
STAGE_EMPTY = (
    "nup, not getting a clean look at that yet",
    "hmm, I'm not seeing a clear run of times",
    "yeah I don't wanna guess, let me stay on it",
)

STAGE_POOLS: dict[int, tuple[str, ...]] = {
    1: STAGE_1,
    2: STAGE_2,
    3: STAGE_3,
    4: STAGE_4,
    5: STAGE_5,
}

ACKS = (
    "no worries",
    "no dramas",
    "not a problem",
    "too easy",
    "all good",
    "you're right",
    "sweet",
)

OPENINGS = (
    "Morning, {branch}, Ava speaking!",
    "{branch}, this is Ava — how ya going?",
    "Good morning! You're through to {branch}, Ava here.",
    "{branch}, Ava — what can I do for ya?",
)

CLOSINGS = (
    "No dramas at all. Take care, see ya then!",
    "All good. Catch ya then!",
    "Too easy. Have a good one!",
    "Beautiful. Take care!",
)

BARGE_IN_RESUME = (
    "sorry, go on",
    "yep yep, sorry",
)

BACKCHANNELS = (
    "mm",
    "yep",
    "oh no",
    "geez",
    "mm-hmm",
    "aww",
    "ohh",
    "right",
)

PAIN_BACKCHANNELS = (
    "oh no",
    "aww",
    "geez",
    "ohh",
    "you poor thing",
)

STOCK_POOLS: dict[str, tuple[str, ...]] = {
    "stage_1": STAGE_1,
    "stage_2": STAGE_2,
    "stage_3": STAGE_3,
    "stage_4": STAGE_4,
    "stage_5": STAGE_5,
    "stage_error": STAGE_ERROR,
    "stage_empty": STAGE_EMPTY,
    "ack": ACKS,
    "opening": OPENINGS,
    "closing": CLOSINGS,
    "barge_in_resume": BARGE_IN_RESUME,
    "backchannel": BACKCHANNELS,
    "pain_backchannel": PAIN_BACKCHANNELS,
}


def pick_from_pool(
    used: MutableSequence[str],
    pool: Sequence[str],
    rng: random.Random | None = None,
) -> str:
    """Return a line that has not been used this call. Wrap only when the pool is exhausted."""
    if not pool:
        raise ValueError("empty phrase pool")
    chooser = rng or random.Random()
    unused = [line for line in pool if line not in used]
    if not unused:
        used.clear()
        unused = list(pool)
    choice = chooser.choice(list(unused))
    used.append(choice)
    return choice
