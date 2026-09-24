import sublime
import sublime_plugin

import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request


# ------------------------------------------------------------------------------
# BASIC CONSTANTS
# ------------------------------------------------------------------------------

SETTINGS_FILE = "AuthorTools.sublime-settings"

# The name under which our text is stored in Sublime's bottom status bar.
#
# Sublime orders plugin status entries alphabetically by key. The "zz_" prefix
# places ours LAST, so a narrow window truncates KAV rather than pushing other
# entries (such as a word count) out of view.
STATUS_KEY = "zz_author_tools"

# "Colours: KAV", just left of the analysis.
COLOUR_STATUS_KEY = "zy_author_tools_colours"

DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"

# The value shipped in the default settings. Treated as "no key configured".
API_KEY_PLACEHOLDER = "YOUR_OPENROUTER_API_KEY"

# Also the fixed left-to-right display order.
LABELS = ("kinetic", "audio", "visual", "scent")

DISPLAY_NAMES = {
    "kinetic": "Kinetic",
    "audio": "Audio",
    "visual": "Visual",
    "scent": "Scent"
}

SHORT_NAMES = {
    "kinetic": "K",
    "audio": "A",
    "visual": "V",
    "scent": "S"
}

# Markdown ATX headings such as "# Chapter 3" or "## Scene".
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")


# ------------------------------------------------------------------------------
# GLOBAL IN-MEMORY CACHE
# ------------------------------------------------------------------------------
#
# Key: SHA-1 of (request payload + exact paragraph text).
#
# Caching by text rather than position means a paragraph that merely moves
# (because you inserted text above it) keeps its result, while a paragraph you
# edit gets a new hash and will be analysed again when you next enter it.
# ------------------------------------------------------------------------------

CACHE = {}

# Hashes currently being sent to the API, to prevent duplicate requests.
IN_FLIGHT = set()

CACHE_LOCK = threading.Lock()

# Most recent raw response body, for the "Show Last Raw Response" command.
LAST_RAW = {"text": None}


# ------------------------------------------------------------------------------
# PER-VIEW STATE
# ------------------------------------------------------------------------------
#
# VIEW_STATE[view.id()] = {
#     "paragraph_start": 500,       # where the active paragraph begins
#     "paragraph_hash": "abc...",   # what the active paragraph contains
#     "change_count": 42,           # buffer change counter at last event
#     "token": 7,                   # bumped on every paragraph change
# }
#
# The paragraph is identified by its START offset, not (start, end): typing
# inside a paragraph moves its end, and must not look like navigation.
#
# change_count lets us tell caret movement caused by an EDIT (typing, undo,
# paste) apart from caret movement caused by NAVIGATION (click, arrows,
# search). Only navigation triggers analysis.
#
# token implements the debounce and stale-response protection: a pending
# request only proceeds if the token hasn't changed since it was scheduled.
# ------------------------------------------------------------------------------

VIEW_STATE = {}
VIEW_STATE_LOCK = threading.Lock()


# ------------------------------------------------------------------------------
# SETTINGS HELPERS
# ------------------------------------------------------------------------------

def get_settings():
    return sublime.load_settings(SETTINGS_FILE)


def plugin_enabled():
    return bool(get_settings().get("enabled", True))


def get_api_key():
    """
    Retrieve the OpenRouter API key.

    Priority:

        1. "api_key" in the USER copy of AuthorTools.sublime-settings
        2. OPENROUTER_API_KEY environment variable

    On macOS, Sublime launched from the Dock/Finder does NOT see variables
    exported in ~/.zshrc, so the settings file is the reliable option there.
    """
    key = get_settings().get("api_key", "")

    if isinstance(key, str):
        key = key.strip()
    else:
        key = ""

    if key and key != API_KEY_PLACEHOLDER:
        return key

    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def view_is_applicable(view):
    """
    Only analyse Markdown manuscripts.

    Without this, code, config files and even .env files would be sent to
    OpenRouter. Widgets (find panel, command palette input, console) are
    always skipped.
    """
    if view is None or not view.is_valid():
        return False

    if view.settings().get("is_widget"):
        return False

    extensions = get_settings().get("file_extensions", [".md"])
    extensions = [ext.lower() for ext in extensions]

    file_name = view.file_name()

    if file_name:
        return os.path.splitext(file_name)[1].lower() in extensions

    # Unsaved buffer: fall back to the syntax.
    syntax = view.syntax()

    return syntax is not None and "markdown" in syntax.name.lower()


# ------------------------------------------------------------------------------
# PARAGRAPH EXTRACTION
# ------------------------------------------------------------------------------

def line_is_blank(view, line_region):
    return not view.substr(line_region).strip()


def find_paragraph_region(view, point):
    """
    Find the paragraph containing `point`.

    A paragraph is one or more non-blank lines bounded by blank lines or by
    the beginning/end of the file. We walk outward from the caret's line, so
    the rest of the manuscript is never scanned.

    Sublime normalises CRLF to LF in the buffer, and soft wrapping does not
    create buffer lines, so neither affects this.
    """
    if view.size() == 0:
        return None

    point = max(0, min(point, view.size()))

    current_line = view.line(point)

    if line_is_blank(view, current_line):
        return None

    paragraph_start = current_line.begin()
    paragraph_end = current_line.end()

    # Walk up.
    scan_point = current_line.begin()

    while scan_point > 0:
        previous_line = view.line(scan_point - 1)

        if line_is_blank(view, previous_line):
            break

        paragraph_start = previous_line.begin()
        scan_point = previous_line.begin()

    # Walk down.
    scan_point = current_line.end()

    while scan_point < view.size():
        next_start = view.full_line(scan_point).end()

        if next_start >= view.size():
            break

        next_line = view.line(next_start)

        if line_is_blank(view, next_line):
            break

        paragraph_end = next_line.end()
        scan_point = next_line.end()

    return sublime.Region(paragraph_start, paragraph_end)


def clean_paragraph_text(text):
    """
    Remove Markdown heading lines, so "# Chapter 3" is never analysed.
    """
    lines = [
        line for line in text.splitlines()
        if not HEADING_RE.match(line)
    ]

    return "\n".join(lines).strip()


def current_target(view):
    """
    Decide what to analyse: the first selection if there is one, otherwise
    the paragraph under the caret. Extra cursors are ignored.

    Returns (target, region, text, is_selection):

        target        identity used to detect "moved somewhere new":
                      ("paragraph", start) or ("selection", begin, end),
                      or None on a blank line
        region        the paragraph or selection region, or None
        text          cleaned text; "" for a heading-only paragraph
        is_selection  True when analysing a selection
    """
    selections = view.sel()

    if len(selections) == 0:
        return None, None, None, False

    first = selections[0]

    if not first.empty() and get_settings().get("analyse_selections", False):
        region = sublime.Region(first.begin(), first.end())
        text = clean_paragraph_text(view.substr(region))

        return ("selection", region.begin(), region.end()), region, text, True

    region = find_paragraph_region(view, first.begin())

    if region is None:
        return None, None, None, False

    text = clean_paragraph_text(view.substr(region))

    return ("paragraph", region.begin()), region, text, False


def make_paragraph_hash(text):
    """
    Stable digest for caching. Python's hash() is not stable across sessions.
    """
    # Includes the full question, so changing the instructions or
    # criteria never serves results produced by an older question.
    namespace = json.dumps(
        build_request_payload("", get_settings().get("model", DEFAULT_MODEL)),
        sort_keys=True
    )

    return hashlib.sha1(
        (namespace + "\n" + text).encode("utf-8")
    ).hexdigest()


# ------------------------------------------------------------------------------
# JEV QUESTION
# ------------------------------------------------------------------------------

# How strongly each sense is present. Jev answers with a number from 0
# (first entry) to 4 (last entry), interpolated by its confidence.
SCORE_SCALE = ["absent", "faint", "noticeable", "strong", "dominant"]
SCORE_MAX = float(len(SCORE_SCALE) - 1)

SENSE_DESCRIPTIONS = {
    "kinetic": (
        "movement, bodily action, touch, pressure, temperature, pain, "
        "balance or other bodily sensation"
    ),
    "audio": (
        "sound: voices, speech as heard sound, music, noises, impacts, "
        "silence, rhythm or volume"
    ),
    "visual": (
        "sight: colour, light, darkness, shape, appearance, distance, "
        "position, scenery or visual motion"
    ),
    "scent": (
        "smell or aroma: fragrance, odour, smoke, perfume, decay, food aromas"
    )
}


# Telescoping, after David Farland: how far the "camera" is zoomed out.
# Asked as a score from 0 (deep inside a character) to 4 (panoramic), so
# it fits in the same request as the senses.
TELESCOPE_KEY = "telescope"

TELESCOPE_SCALE = [
    "inside a character's mind",
    "close up, through a character's eyes",
    "mid-range",
    "wide view",
    "panoramic overview"
]

TELESCOPE_INSTRUCTIONS = (
    "Analyze the narrative distance (zoom level) of the prose in `paragraph`. "
    "**Zoomed In:** Deeply focused on a character's internal thoughts, feelings, "
    "or close-up sensory details. "
    "**Mid-Range:** Active dialogue, immediate physical actions, or character interactions. "
    "**Zoomed Out:** Broad descriptions of landscapes, settings, buildings, crowds, "
    "or summaries of passing time."
)


# Showing vs telling: 0 is summarised or explained, 4 is dramatised.
SHOWING_KEY = "showing"

SHOWING_SCALE = [
    "told: summary or explanation",
    "mostly told",
    "a mix of showing and telling",
    "mostly shown",
    "shown: dramatised in the moment"
]

SHOWING_INSTRUCTIONS = (
    "How much of the fiction prose in `paragraph` is shown rather than "
    "told? Showing dramatises events in the moment through action, dialogue "
    "and concrete sensory detail, and lets the reader draw conclusions. "
    "Telling summarises, explains, or names emotions and traits directly "
    "(\"she was angry\", \"he was a kind man\")."
)

# Tension: 0 is none, 4 is intense.
TENSION_KEY = "tension"

TENSION_SCALE = ["none", "low", "moderate", "high", "intense"]

TENSION_INSTRUCTIONS = (
    "How much tension does the fiction prose in `paragraph` create for the "
    "reader? Tension comes from unresolved conflict, threat, danger, "
    "uncertainty, opposing desires, or a question the reader wants answered."
)

# Questions asked alongside the senses, each switched by its own setting.
# Their answers are optional: a missing one is simply not shown.
EXTRA_QUESTIONS = {
    TELESCOPE_KEY: {
        "setting": "show_telescoping",
        "instructions": TELESCOPE_INSTRUCTIONS,
        "criteria": TELESCOPE_SCALE
    },
    SHOWING_KEY: {
        "setting": "show_showing",
        "instructions": SHOWING_INSTRUCTIONS,
        "criteria": SHOWING_SCALE
    },
    TENSION_KEY: {
        "setting": "show_tension",
        "instructions": TENSION_INSTRUCTIONS,
        "criteria": TENSION_SCALE
    }
}


# Mood: a "choice" question, so Jev picks one option and gives a
# probability for each. Options can be replaced with the "moods" setting.
MOOD_KEY = "mood"

DEFAULT_MOODS = {
    "ominous": "foreboding, dread, menace, a sense that something bad is coming",
    "fearful": "fear, panic, terror, horror, being hunted or trapped",
    "angry": "rage, resentment, hostility, frustration, confrontation",
    "melancholy": "sadness, grief, loss, longing, regret",
    "bleak": "despair, hopelessness, desolation, cruelty",
    "tender": "warmth, love, affection, intimacy, comfort",
    "joyful": "happiness, delight, triumph, celebration, relief",
    "playful": "humour, wit, teasing, lightness, absurdity",
    "wondrous": "awe, marvel, mystery, beauty, the sublime",
    "calm": "peace, stillness, quiet routine, reflection, a neutral tone"
}

MOOD_INSTRUCTIONS = (
    "Which mood does the fiction prose in `paragraph` create for the reader?"
)


def mood_enabled():
    return bool(get_settings().get("show_mood", True))


def mood_options():
    moods = get_settings().get("moods")

    if isinstance(moods, dict) and moods:
        return moods

    return DEFAULT_MOODS


def question_enabled(key):
    return bool(get_settings().get(EXTRA_QUESTIONS[key]["setting"], True))


def enabled_extra_questions():
    return [key for key in EXTRA_QUESTIONS if question_enabled(key)]


def telescoping_enabled():
    return question_enabled(TELESCOPE_KEY)


def build_request_payload(paragraph, model):
    """
    Construct the OpenRouter Decisions request: one "score" question per
    sense, all in a single request.

    Why not one "choice" question? A choice question asks which single sense
    DOMINATES, and its probabilities are Jev's certainty about that pick.
    For most paragraphs Jev is certain, so it showed 100% for one sense and
    0% for the rest. Rating each sense separately measures how much of EACH
    sense is present, which is what the status bar should show.

    These descriptions are operational definitions inspired by David
    Farland's KAV concept, not a claim about his exact taxonomy.
    """
    questions = {
        label: {
            "type": "score",
            "instructions": (
                "How strongly does the fiction prose in `paragraph` "
                "invite the reader to experience {}?".format(description)
            ),
            "criteria": SCORE_SCALE
        }
        for label, description in SENSE_DESCRIPTIONS.items()
    }

    for key in enabled_extra_questions():
        questions[key] = {
            "type": "score",
            "instructions": EXTRA_QUESTIONS[key]["instructions"],
            "criteria": EXTRA_QUESTIONS[key]["criteria"]
        }

    if mood_enabled():
        questions[MOOD_KEY] = {
            "type": "choice",
            "instructions": MOOD_INSTRUCTIONS,
            "criteria": mood_options()
        }

    return {
        "model": model,

        "state": {
            "paragraph": paragraph
        },

        "questions": questions
    }


# ------------------------------------------------------------------------------
# FETCHING RESULTS
# ------------------------------------------------------------------------------
#
# The fetcher runs on a background thread and receives a plain `config` dict
# that was read from settings beforehand.
#
# It returns (scores, raw_response_text), where scores maps each sense to
# a number from 0 (absent) to SCORE_MAX (dominant), plus a 0 to SCORE_MAX
# score for each enabled extra question (telescoping, showing, tension), and
# MOOD_KEY (an option name) when mood is on.
# ------------------------------------------------------------------------------

def fetch_openrouter(paragraph, config):
    """
    Send one paragraph to OpenRouter/Jev using only the standard library.
    """
    if not config["api_key"]:
        raise RuntimeError(
            "No OpenRouter API key configured. Set api_key in your User "
            "AuthorTools.sublime-settings."
        )

    payload = build_request_payload(paragraph, config["model"])

    request = urllib.request.Request(
        config["endpoint"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + config["api_key"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Optional OpenRouter attribution header.
            "X-OpenRouter-Title": "Author Tools for Sublime Text"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=config["timeout_seconds"]
        ) as response:
            raw_response = response.read().decode("utf-8")

    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""

        LAST_RAW["text"] = error_body

        raise RuntimeError(
            "OpenRouter HTTP {}: {}".format(exc.code, error_body[:1000])
        )

    except urllib.error.URLError as exc:
        raise RuntimeError("Could not reach OpenRouter: {}".format(exc))

    try:
        data = json.loads(raw_response)
    except ValueError:
        raise RuntimeError(
            "OpenRouter returned invalid JSON: {}".format(raw_response[:500])
        )

    # --------------------------------------------------------------------------
    # EXPECTED RESPONSE (verified against typesafe/jev-1.13, September 2026)
    # --------------------------------------------------------------------------
    #
    # {
    #     "answers": {
    #         "visual": {
    #             "type": "score",
    #             "score": 3.94,
    #             "legend": {"0": "absent", ..., "4": "dominant"},
    #             "probabilities": {"0": 0, ..., "4": 0.94},
    #             "confidence": 0.95
    #         },
    #         "kinetic": {...}, "audio": {...}, "scent": {...}
    #     }
    # }
    #
    # Every level is validated. We never fabricate scores if Jev does not
    # return them; use "Author Tools: Show Last Raw Response" to inspect what
    # actually came back.
    # --------------------------------------------------------------------------

    answers = data.get("answers") if isinstance(data, dict) else None

    if not isinstance(answers, dict):
        raise RuntimeError("Unexpected Jev response: no 'answers' object.")

    scores = {}

    for label in LABELS:
        answer = answers.get(label)

        if not isinstance(answer, dict) or "score" not in answer:
            raise RuntimeError(
                "Unexpected Jev response: no score for '{}'.".format(label)
            )

        try:
            value = float(answer["score"])
        except (TypeError, ValueError):
            raise RuntimeError(
                "Jev returned a non-numeric score for '{}'.".format(label)
            )

        scores[label] = max(0.0, min(SCORE_MAX, value))

    # The extra questions are optional, so a missing or odd answer just
    # leaves that score out, not an error.
    for key in config["extra_questions"]:
        answer = answers.get(key)

        if not isinstance(answer, dict):
            continue

        try:
            value = float(answer.get("score"))
        except (TypeError, ValueError):
            continue

        scores[key] = max(0.0, min(SCORE_MAX, value))

    # A choice answer: {"type": "choice", "choice": "ominous",
    # "probabilities": {...}, "confidence": 0.8}. Also optional.
    mood = answers.get(MOOD_KEY)

    if (
        config["moods"]
        and isinstance(mood, dict)
        and mood.get("choice") in config["moods"]
    ):
        scores[MOOD_KEY] = mood["choice"]

    return scores, raw_response


def read_request_config():
    """
    Snapshot everything the background thread needs from settings.
    """
    settings = get_settings()

    return {
        "api_key": get_api_key(),
        "endpoint": settings.get("endpoint", DEFAULT_ENDPOINT),
        "model": settings.get("model", DEFAULT_MODEL),
        "timeout_seconds": float(settings.get("timeout_seconds", 15)),
        "extra_questions": enabled_extra_questions(),
        "moods": list(mood_options()) if mood_enabled() else []
    }


# ------------------------------------------------------------------------------
# STATUS-BAR FORMATTING
# ------------------------------------------------------------------------------

def status_prefix(selection=False):
    """
    "KAV", or "KAV (selection)".
    """
    if selection:
        return "KAV (selection)"

    return "KAV"


def separator():
    """
    The gap between status bar items.

    The status bar is plain text, so pixel spacing isn't possible. The default
    uses Unicode spaces instead: two em spaces plus an en space is roughly
    30px at the macOS status bar font size. Ordinary spaces would be only
    ~4px each.
    """
    return get_settings().get("separator", "\u2003\u2003\u2002")


def with_note(note, selection=False):
    """
    e.g. "KAV    analysing…"
    """
    return status_prefix(selection) + separator() + note


# A digit-wide space, used to right-align percentages ("  3%", " 39%").
FIGURE_SPACE = chr(0x2007)


# Filler for the unused part of a bar. The block characters come from a
# fallback font and are about 0.83em wide in the macOS status bar, while an
# ordinary space is only ~0.25em. An en space (0.5em) plus a three-per-em
# space (0.33em) matches one block closely, so the bar keeps its width.
DEFAULT_BAR_TRACK = chr(0x2002) + chr(0x2004)


def render_bar(fraction, width, track):
    """
    A bar that is always `width` characters long: filled with whole blocks,
    and the rest padded with the `track` character, so the status bar doesn't
    shift sideways as values change. Partial blocks (▎, ▌, ...) are avoided
    because the fallback font draws them shorter than a full block, which
    leaves a visible bump at the end of the bar.

    Any value above zero gets at least one block, so it stays visible.

        render_bar(0.73, 10, "·") -> "███████···"
        render_bar(0.02, 10, "·") -> "█·········"
    """
    fraction = max(0.0, min(1.0, fraction))
    full = int(round(fraction * width))

    if fraction > 0:
        full = max(1, full)

    return "█" * full + track * (width - full)


def format_percent(value):
    """
    Right-aligned to three digits so "3%" and "63%" take the same width.
    """
    return "{:>3}%".format(value).replace(" ", FIGURE_SPACE)


ZOOM_TEXT = {
    "in": ("▼", "zoomed in"),
    "out": ("▲", "zoomed out"),
    None: ("●", "mid-range")
}


def format_zoom(scores, compact=False):
    """
    The telescoping part of the status bar, e.g. "▲ zoomed out", or "" when
    there is no telescoping score.
    """
    if scores.get(TELESCOPE_KEY) is None or not telescoping_enabled():
        return ""

    symbol, words = ZOOM_TEXT[paragraph_zoom(scores)]

    return symbol if compact else "{} {}".format(symbol, words)


# 0-4 scores as four dots, e.g. 2.8 -> "●●●○".
LEVEL_DOTS = 4

LEVEL_ITEMS = (
    # key, status bar name, compact name
    (SHOWING_KEY, "Showing", "Sh"),
    (TENSION_KEY, "Tension", "Te")
)


def format_dots(value):
    filled = int(round(max(0.0, min(SCORE_MAX, value)) * LEVEL_DOTS / SCORE_MAX))

    return "●" * filled + "○" * (LEVEL_DOTS - filled)


def format_levels(scores, compact=False):
    """
    ["Showing ●●●○", "Tension ●●○○", "Mood ominous"], or
    ["Sh3", "Te2", "ominous"] when compact, for the enabled questions that
    have an answer.
    """
    items = []

    for key, name, short in LEVEL_ITEMS:
        value = scores.get(key)

        if value is None or not question_enabled(key):
            continue

        if compact:
            items.append("{}{}".format(short, int(round(value))))
        else:
            items.append("{} {}".format(name, format_dots(value)))

    mood = scores.get(MOOD_KEY)

    if mood and mood_enabled():
        items.append(mood if compact else "Mood {}".format(mood))

    return items


def format_scores(scores, selection=False):
    """
    Each sense's percentage is its share of the paragraph's total sensory
    score, so the four always add up to about 100%.

    Default:  KAV    Kinetic ███           Audio ███           Visual ...
    No bars:  KAV    Kinetic  26%    Audio  33%    Visual  39%    Scent   3%
    Compact:  KAV    K26 A33 V39 S3 ▲ Sh3 Te2 ominous

    The telescoping zoom ("▲ zoomed out"), showing, tension and mood come
    last.
    """
    settings = get_settings()
    compact = bool(settings.get("compact", False))
    zoom = format_zoom(scores, compact)
    extras = ([zoom] if zoom else []) + format_levels(scores, compact)

    total = sum(scores.get(label, 0) for label in LABELS)

    # Abstract prose (reasoning, exposition) scores near zero on every sense.
    # Shares of near-nothing would be noise, so say so instead.
    if total < float(settings.get("min_sensory_total", 0.5)):
        parts = [with_note("little sensory content", selection)] + extras

        return separator().join(parts) + separator()

    probabilities = {
        label: scores.get(label, 0) / total
        for label in LABELS
    }

    labels = list(LABELS)

    if not settings.get("show_scent", True):
        labels.remove("scent")

    # No sorting: every category keeps its position, only the bars change.

    def percent(label):
        return int(round(probabilities.get(label, 0) * 100))

    if compact:
        body = " ".join(
            ["{}{}".format(SHORT_NAMES[label], percent(label)) for label in labels]
            + extras
        )
        return with_note(body, selection)

    show_bars = bool(settings.get("show_bars", True))
    bar_width = int(settings.get("bar_width", 10))
    bar_track = settings.get("bar_track", DEFAULT_BAR_TRACK)

    # Without bars the percentages are the only reading, so always shown.
    show_percentages = (
        not show_bars or bool(settings.get("show_percentages", False))
    )

    parts = [status_prefix(selection)]

    for label in labels:
        item = [DISPLAY_NAMES[label]]

        # A sense at 0% gets no bar at all, not even the empty track.
        if show_bars and percent(label) > 0:
            item.append(
                render_bar(probabilities.get(label, 0), bar_width, bar_track)
            )

        if show_percentages:
            item.append(format_percent(percent(label)))

        parts.append(" ".join(item))

    parts.extend(extras)

    # Trailing gap keeps the last item clear of whatever follows it.
    return separator().join(parts) + separator()


# ------------------------------------------------------------------------------
# GUTTER MARKS
# ------------------------------------------------------------------------------
#
# Every analysed paragraph gets an icon in the left margin. Its colour shows
# one measurement at a time, chosen with "Author Tools: Colour Marks By…":
# the dominant sense (the default), mood, tension, showing or telescoping.
# The colour also tints the paragraph in the minimap, so the balance of a
# whole scene is visible while scrolling.
#
# The "region.*ish" scopes are colours every Sublime Text 4 colour scheme
# defines, adjusted to fit that scheme (light or dark).
#
# With telescoping on, the circle becomes a triangle pointing up for a
# zoomed-out paragraph and down for a zoomed-in one, whatever the colour.
#
# Each analysed paragraph is kept as its own hidden region, with its scores
# in the view's settings, so switching colours needs no new requests.
# Sublime moves regions along with edits, so a paragraph keeps its scores
# when you add or delete text above it, and an edited paragraph keeps its
# colour until it is analysed again. The visible marks are drawn as one
# region set per colour and icon, because a region set has a single scope
# and icon.
# ------------------------------------------------------------------------------

MARK_KEY_PREFIX = "author_tools_mark_"
PARAGRAPH_KEY_PREFIX = "author_tools_paragraph_"

# View settings, which survive a plugin reload: {paragraph id: scores}, the
# next free paragraph id, and the mark region keys currently drawn.
PARAGRAPHS_SETTING = "author_tools_paragraphs"
NEXT_ID_SETTING = "author_tools_next_paragraph_id"
DRAWN_KEYS_SETTING = "author_tools_mark_keys"

# None is the ordinary icon.
ZOOMS = (None, "in", "out")

# White PNGs in icons/ (with @2x and @3x versions), tinted by the scope.
ZOOM_ICONS = {
    "in": "triangle_down",
    "out": "triangle_up"
}

PACKAGE_NAME = __package__ or "AuthorTools"

# Also the order of "Colour Marks By…" and of cycling with {"mode": "next"}.
COLOUR_MODES = ("senses", "mood", "tension", "showing", "telescoping")

COLOUR_MODE_NAMES = {
    "senses": "Dominant sense",
    "mood": "Mood",
    "tension": "Tension",
    "showing": "Showing vs telling",
    "telescoping": "Telescoping"
}

# For the status bar: "Colours: KAV".
COLOUR_MODE_SHORT_NAMES = {
    "senses": "KAV",
    "mood": "Mood",
    "tension": "Tension",
    "showing": "Showing",
    "telescoping": "Telescoping"
}

# The modes coloured by a 0-4 score, with that score's key and the words
# for its two ends.
SCALE_MODES = {
    "tension": (TENSION_KEY, "no tension", "intense"),
    "showing": (SHOWING_KEY, "told", "shown"),
    "telescoping": (TELESCOPE_KEY, "zoomed in", "zoomed out")
}

DEFAULT_MARK_SCOPES = {
    "kinetic": "region.redish",
    "audio": "region.bluish",
    "visual": "region.purplish",
    "scent": "region.orangish"
}

# Ten moods, eight colours: similar moods share one.
DEFAULT_MOOD_SCOPES = {
    "ominous": "region.purplish",
    "fearful": "region.orangish",
    "angry": "region.redish",
    "melancholy": "region.bluish",
    "bleak": "region.bluish",
    "tender": "region.pinkish",
    "joyful": "region.yellowish",
    "playful": "region.yellowish",
    "wondrous": "region.cyanish",
    "calm": "region.greenish"
}

# Moods without a colour (e.g. from your own "moods" list) take one from
# here, by their position in the list.
MOOD_FALLBACK_SCOPES = [
    "region.redish", "region.orangish", "region.yellowish", "region.greenish",
    "region.cyanish", "region.bluish", "region.purplish", "region.pinkish"
]

# Low to high: cool colours for 0, hot for 4.
DEFAULT_SCALE_SCOPES = [
    "region.bluish", "region.greenish", "region.yellowish",
    "region.orangish", "region.redish"
]

COLOUR_WORDS = {
    "region.redish": "red",
    "region.orangish": "orange",
    "region.yellowish": "yellow",
    "region.greenish": "green",
    "region.cyanish": "cyan",
    "region.bluish": "blue",
    "region.purplish": "purple",
    "region.pinkish": "pink"
}


def colour_mode():
    mode = get_settings().get("colour_marks_by", "senses")

    return mode if mode in COLOUR_MODES else "senses"


def dominant_sense(scores):
    """
    The highest-scoring sense, or None for paragraphs with little sensory
    content.
    """
    total = sum(scores.get(label, 0) for label in LABELS)

    if total < float(get_settings().get("min_sensory_total", 0.5)):
        return None

    return max(LABELS, key=lambda label: scores.get(label, 0))


def paragraph_zoom(scores):
    """
    "in", "out", or None for a middle distance (or no telescoping score).
    """
    value = scores.get(TELESCOPE_KEY)

    if value is None or not telescoping_enabled():
        return None

    settings = get_settings()

    if value < float(settings.get("zoomed_in_below", 1.5)):
        return "in"

    if value > float(settings.get("zoomed_out_above", 2.5)):
        return "out"

    return None


def sense_scopes(settings):
    scopes = dict(DEFAULT_MARK_SCOPES)
    scopes.update(settings.get("mark_scopes", {}))
    return scopes


def mood_scope(mood, settings):
    scopes = dict(DEFAULT_MOOD_SCOPES)
    scopes.update(settings.get("mood_scopes", {}))

    if mood in scopes:
        return scopes[mood]

    moods = list(mood_options())

    if mood not in moods:
        return None

    return MOOD_FALLBACK_SCOPES[moods.index(mood) % len(MOOD_FALLBACK_SCOPES)]


def scale_scopes(settings):
    scopes = settings.get("scale_scopes")

    if isinstance(scopes, list) and scopes:
        return scopes

    return DEFAULT_SCALE_SCOPES


def mark_scope(scores, mode, settings):
    """
    The colour for a paragraph in this mode, or None for no mark.
    """
    if mode == "senses":
        dominant = dominant_sense(scores)
        return sense_scopes(settings).get(dominant) if dominant else None

    if mode == "mood":
        mood = scores.get(MOOD_KEY)
        return mood_scope(mood, settings) if mood else None

    value = scores.get(SCALE_MODES[mode][0])

    if value is None:
        return None

    # Any number of colours: 0 gets the first, 4 the last.
    scopes = scale_scopes(settings)
    level = int(value / SCORE_MAX * (len(scopes) - 1) + 0.5)

    return scopes[max(0, min(len(scopes) - 1, level))]


def colour_word(scope):
    return COLOUR_WORDS.get(scope, scope)


def colour_legend(mode):
    """
    e.g. "red kinetic, blue audio, purple visual, orange scent".
    """
    settings = get_settings()

    if mode == "senses":
        scopes = sense_scopes(settings)
        return ", ".join(
            "{} {}".format(colour_word(scopes[label]), label)
            for label in LABELS if label in scopes
        )

    if mode == "mood":
        # Moods sharing a colour are listed together: "blue melancholy/bleak".
        groups = []

        for mood in mood_options():
            word = colour_word(mood_scope(mood, settings))
            group = next((g for g in groups if g[0] == word), None)

            if group is None:
                groups.append((word, [mood]))
            else:
                group[1].append(mood)

        return ", ".join(
            "{} {}".format(word, "/".join(moods)) for word, moods in groups
        )

    _, low, high = SCALE_MODES[mode]
    scopes = scale_scopes(settings)

    return "{} {} … {} {}".format(
        colour_word(scopes[0]), low, colour_word(scopes[-1]), high
    )


def colour_mode_enabled(mode):
    """
    False when the measurement a mode colours by isn't asked for.
    """
    if mode == "mood":
        return mood_enabled()

    if mode in SCALE_MODES:
        return question_enabled(SCALE_MODES[mode][0])

    return True


# How the paragraph itself is drawn besides the gutter icon, when
# "paragraph_tint" is on. The fill covers the text of each line, like a
# selection; how strong it is depends on the colour scheme.
TINT_STYLES = {
    "background": sublime.DRAW_NO_OUTLINE,
    "outline": sublime.DRAW_NO_FILL
}


def mark_flags(settings):
    if settings.get("paragraph_tint", False):
        style = settings.get("paragraph_tint_style", "background")
        return TINT_STYLES.get(style, TINT_STYLES["background"])

    # Only the gutter icon, not a highlight over the text itself.
    return sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE


def mark_icon(zoom, default_icon):
    if zoom is None:
        return default_icon

    return "Packages/{}/icons/{}.png".format(PACKAGE_NAME, ZOOM_ICONS[zoom])


def paragraph_key(paragraph_id):
    return PARAGRAPH_KEY_PREFIX + paragraph_id


def tracked_region(view, paragraph_id):
    """
    Where a remembered paragraph is now, or None once it has been deleted.
    """
    regions = view.get_regions(paragraph_key(paragraph_id))

    if not regions or regions[0].empty():
        return None

    return regions[0]


def update_paragraph_mark(view, region, scores):
    """
    Remember these scores for the paragraph at region, replacing whatever
    it had, and redraw the marks.
    """
    if region is None:
        return

    settings = view.settings()
    paragraphs = dict(settings.get(PARAGRAPHS_SETTING) or {})

    for paragraph_id in list(paragraphs):
        existing = tracked_region(view, paragraph_id)

        if existing is None or existing.intersects(region):
            view.erase_regions(paragraph_key(paragraph_id))
            del paragraphs[paragraph_id]

    next_id = int(settings.get(NEXT_ID_SETTING, 0))
    paragraph_id = str(next_id)

    view.add_regions(paragraph_key(paragraph_id), [region], "", "", sublime.HIDDEN)
    paragraphs[paragraph_id] = scores

    settings.set(NEXT_ID_SETTING, next_id + 1)
    settings.set(PARAGRAPHS_SETTING, paragraphs)

    redraw_marks(view)


def redraw_marks(view):
    """
    Draw the marks for every remembered paragraph in the current colour mode.
    """
    settings = get_settings()
    drawn = []

    if settings.get("show_gutter_marks", True):
        mode = colour_mode()
        icon = settings.get("gutter_icon", "circle")
        groups = {}

        paragraphs = view.settings().get(PARAGRAPHS_SETTING) or {}

        for paragraph_id, scores in paragraphs.items():
            region = tracked_region(view, paragraph_id)
            scope = mark_scope(scores, mode, settings) if region else None

            if scope is not None:
                group = (scope, paragraph_zoom(scores))
                groups.setdefault(group, []).append(region)

        flags = mark_flags(settings)

        for (scope, zoom), regions in groups.items():
            key = MARK_KEY_PREFIX + scope + ("_" + zoom if zoom else "")
            view.add_regions(key, regions, scope, mark_icon(zoom, icon), flags)
            drawn.append(key)

    # Erased after drawing the new marks, so switching doesn't flicker.
    for key in view.settings().get(DRAWN_KEYS_SETTING) or []:
        if key not in drawn:
            view.erase_regions(key)

    view.settings().set(DRAWN_KEYS_SETTING, drawn)

    view.set_status(COLOUR_STATUS_KEY, colour_status(settings))


def colour_status(settings):
    """
    "Colours: KAV", "Colours: Mood + tint", or "Colours: off".
    """
    if not settings.get("show_gutter_marks", True):
        return "Colours: off" + separator()

    text = "Colours: " + COLOUR_MODE_SHORT_NAMES[colour_mode()]

    if settings.get("paragraph_tint", False):
        text += " + tint"

    return text + separator()


def redraw_all_marks():
    if not plugin_enabled():
        return

    for open_window in sublime.windows():
        for open_view in open_window.views():
            if view_is_applicable(open_view):
                redraw_marks(open_view)


def clear_marks(view):
    """
    Remove the marks and forget the remembered paragraphs.
    """
    settings = view.settings()

    for key in settings.get(DRAWN_KEYS_SETTING) or []:
        view.erase_regions(key)

    for paragraph_id in settings.get(PARAGRAPHS_SETTING) or {}:
        view.erase_regions(paragraph_key(paragraph_id))

    for name in (DRAWN_KEYS_SETTING, PARAGRAPHS_SETTING, NEXT_ID_SETTING):
        settings.erase(name)

    view.erase_status(COLOUR_STATUS_KEY)


def set_colour_mode(mode):
    settings = get_settings()
    settings.set("colour_marks_by", mode)
    sublime.save_settings(SETTINGS_FILE)

    redraw_all_marks()

    message = "Author Tools marks: {} - {}".format(
        COLOUR_MODE_NAMES[mode], colour_legend(mode)
    )

    if not colour_mode_enabled(mode):
        message += " (not analysed: switched off in settings)"

    sublime.status_message(message)


def display_result(view, scores):
    """
    Show a result for the view's current target: always in the status bar,
    and in the margin only for paragraphs. Selections never touch the marks,
    because a selection can cover part of a paragraph, or several.
    """
    state = get_state(view)

    status = format_scores(scores, state["is_selection"])
    state["result_status"] = status
    set_status(view, status)

    start = state["paragraph_start"]

    if start is not None and not state["is_selection"]:
        update_paragraph_mark(view, find_paragraph_region(view, start), scores)


# ------------------------------------------------------------------------------
# ECHOES
# ------------------------------------------------------------------------------
#
# A word repeated close to itself, e.g. "dark" three times in two sentences.
# Counted here, not by Jev: finding repeats is exact, instant and free.
#
# Common words ("the", "said", "then"...) never count. Names never count
# either: a word is taken as a name when every occurrence is capitalised.
# ------------------------------------------------------------------------------

# Letters, with inner apostrophes or hyphens: "don't", "half-light".
WORD_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")

ECHO_STOPWORDS = set("""
a about above after again against all almost also although always am an and
another any anyone anything are around as at away back be because been before
being below between both but by came can can't come could couldn't did didn't
do does doesn't doing don't down during each either else even ever every for
from get got had hadn't has hasn't have haven't having he he'd he'll he's her
here hers herself him himself his how however i i'd i'll i'm i've if in into
is isn't it it's its itself just know knew let like made make many may me
might mine more most much must my myself never no nor not nothing now of off
oh on once one only onto or other our ours ourselves out over own perhaps
quite rather really said same say says see seemed she she'd she'll she's
should shouldn't so some something still such than that that's the their
theirs them themselves then there there's these they they'd they'll they're
thing things this those though through to too toward towards under until up
upon us very was wasn't way we we'd we'll we're we've well went were weren't
what when where whether which while who whom whose why will with within
without won't would wouldn't yes yet you you'd you'll you're you've your
yours yourself
""".split())


def find_echoes(text):
    """
    [(word, count), ...], most repeated first: every word that occurs within
    echo_window_words of an earlier occurrence of itself, with the number of
    its occurrences that are part of such a repeat.
    """
    settings = get_settings()
    window = int(settings.get("echo_window_words", 50))
    ignore_names = bool(settings.get("echo_ignore_names", True))
    ignored = set(word.lower() for word in settings.get("echo_ignore", []))

    last_seen = {}
    echoing = {}
    lowercase_seen = set()

    for index, match in enumerate(WORD_RE.finditer(text)):
        original = match.group(0).replace("’", "'")
        word = original.lower()

        if len(word) < 3 or word in ECHO_STOPWORDS or word in ignored:
            continue

        if not original[0].isupper():
            lowercase_seen.add(word)

        previous = last_seen.get(word)

        if previous is not None and index - previous <= window:
            echoing.setdefault(word, set()).update((previous, index))

        last_seen[word] = index

    echoes = [
        (word, len(indexes)) for word, indexes in echoing.items()
        if not ignore_names or word in lowercase_seen
    ]

    return sorted(echoes, key=lambda item: (-item[1], item[0]))


def format_echoes(echoes):
    if not echoes:
        return '<span class="muted">none</span>'

    shown = int(get_settings().get("echo_max_shown", 12))

    text = " · ".join(
        "{} ×{}".format(word, count) for word, count in echoes[:shown]
    )

    if len(echoes) > shown:
        text += ' <span class="muted">+{} more</span>'.format(len(echoes) - shown)

    return text


# ------------------------------------------------------------------------------
# DETAILS CARD
# ------------------------------------------------------------------------------
#
# Hovering over the margin (or "Author Tools: Show Details") shows the word
# count, Jev's scores and echoes for the paragraph there, or for the
# selection when hovering beside selected lines. Popups are HTML, so the bars here are real
# rectangles with the percentage printed inside them.
# ------------------------------------------------------------------------------

CARD_BAR_MAX_PX = 160

# Wide enough for "100%" inside the bar, even when that overstates tiny values.
CARD_BAR_MIN_PX = 38

SHOWING_WORDS = ["told", "mostly told", "mixed", "mostly shown", "shown"]

CARD_CSS = """
body {
    margin: 0;
    padding: 0.6rem 0.8rem;
}
.row {
    display: block;
    margin: 0.2rem 0;
}
.name {
    display: inline-block;
    width: 5.8rem;
}
.bar {
    display: inline-block;
    background-color: var(--foreground);
    color: var(--background);
    padding: 0 0.3rem;
    border-radius: 0.15rem;
}
.zero {
    display: inline-block;
    padding: 0 0.3rem;
}
.rule {
    display: block;
    border-top: 1px solid color(var(--foreground) alpha(0.25));
    margin: 0.4rem 0;
}
.muted {
    color: color(var(--foreground) alpha(0.6));
}
"""


def card_row(name, content):
    return '<div class="row"><span class="name">{}</span>{}</div>'.format(
        name, content
    )


def level_word(value, words):
    return words[int(round(max(0.0, min(SCORE_MAX, value))))]


def build_details_html(scores, text, is_selection=False):
    """
    The card: word count, then Jev's scores (when `scores` isn't None),
    then echoes.
    """
    settings = get_settings()

    word_count = "{:,}".format(len(WORD_RE.findall(text)))

    if is_selection:
        word_count += ' <span class="muted">(selection)</span>'

    rows = [card_row("Word count", word_count)]

    if scores is None:
        rows.append('<div class="row muted">Not analysed yet</div>')
    else:
        rows.append('<div class="rule"></div>')
        rows.extend(score_rows(scores, settings))

    rows.append('<div class="rule"></div>')
    rows.append(card_row("Echoes", format_echoes(find_echoes(text))))

    return '<body id="author-tools-details"><style>{}</style>{}</body>'.format(
        CARD_CSS, "".join(rows)
    )


def score_rows(scores, settings):
    rows = []

    total = sum(scores.get(label, 0) for label in LABELS)

    labels = list(LABELS)

    if not settings.get("show_scent", True):
        labels.remove("scent")

    if total < float(settings.get("min_sensory_total", 0.5)):
        rows.append('<div class="row muted">Little sensory content</div>')
    else:
        for label in labels:
            fraction = scores.get(label, 0) / total
            value = int(round(fraction * 100))

            if value == 0:
                content = '<span class="zero">0%</span>'
            else:
                width = max(CARD_BAR_MIN_PX, int(round(fraction * CARD_BAR_MAX_PX)))
                content = '<span class="bar" style="width: {}px">{}%</span>'.format(
                    width, value
                )

            rows.append(card_row(DISPLAY_NAMES[label], content))

    extra_rows = []

    if scores.get(TELESCOPE_KEY) is not None and telescoping_enabled():
        extra_rows.append(card_row("Zoom", format_zoom(scores)))

    if scores.get(SHOWING_KEY) is not None and question_enabled(SHOWING_KEY):
        value = scores[SHOWING_KEY]
        extra_rows.append(card_row("Showing", "{} <span class=\"muted\">{}</span>".format(
            format_dots(value), level_word(value, SHOWING_WORDS)
        )))

    if scores.get(TENSION_KEY) is not None and question_enabled(TENSION_KEY):
        value = scores[TENSION_KEY]
        extra_rows.append(card_row("Tension", "{} <span class=\"muted\">{}</span>".format(
            format_dots(value), level_word(value, TENSION_SCALE)
        )))

    if scores.get(MOOD_KEY) and mood_enabled():
        extra_rows.append(card_row("Mood", scores[MOOD_KEY]))

    if extra_rows:
        rows.append('<div class="rule"></div>')
        rows.extend(extra_rows)

    return rows


def details_target(view, point, from_command):
    """
    (region, is_selection) for the card: the selection when the command runs
    with text selected, or when hovering beside a selected line; otherwise
    the paragraph at `point`.
    """
    analyse_selections = get_settings().get("analyse_selections", False)

    if (
        analyse_selections
        and len(view.sel()) > 0
        and not view.sel()[0].empty()
    ):
        selection = view.sel()[0]

        beside_selection = (
            view.line(selection.begin()).begin()
            <= point
            <= view.line(selection.end()).end()
        )

        if from_command or beside_selection:
            return sublime.Region(selection.begin(), selection.end()), True

    return find_paragraph_region(view, point), False


def show_details_popup(view, point, from_command=False):
    """
    Show the card for the selection or paragraph at `point`. Word count and
    echoes are counted here; Jev's scores are shown when its current text
    has been analysed.
    """
    region, is_selection = details_target(view, point, from_command)
    text = clean_paragraph_text(view.substr(region)) if region else ""

    if not text:
        if from_command:
            sublime.status_message("Author Tools: no paragraph or selection here")
        return

    with CACHE_LOCK:
        scores = CACHE.get(make_paragraph_hash(text))

    # Sublime draws the popup from `location`, so anchor it at the left edge
    # of the screen row (a wrapped line has several) rather than at the
    # caret or hover point, which may be halfway across the line.
    _, row_y = view.text_to_layout(point)
    row_start = view.layout_to_text((0.0, row_y))

    view.show_popup(
        build_details_html(scores, text, is_selection),
        flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
        location=row_start,
        max_width=400
    )


# ------------------------------------------------------------------------------
# VIEW / STATUS HELPERS
# ------------------------------------------------------------------------------

def set_status(view, text):
    if view and view.is_valid():
        view.set_status(STATUS_KEY, text)


def clear_status(view):
    if view and view.is_valid():
        view.erase_status(STATUS_KEY)


def get_state(view):
    with VIEW_STATE_LOCK:
        state = VIEW_STATE.get(view.id())

        if state is None:
            state = {
                "target": None,
                "is_selection": False,
                "paragraph_start": None,
                "paragraph_hash": None,
                "change_count": view.change_count(),
                "token": 0,
                # The status text of the last result, so an edit can
                # mark exactly that text as "edited".
                "result_status": None,
                # Bumped by every selection scan, which stops the previous
                # one; and the region keys that scan is still tracking.
                "scan_token": 0,
                "scan_keys": []
            }
            VIEW_STATE[view.id()] = state

        return state


def remove_view_state(view):
    with VIEW_STATE_LOCK:
        VIEW_STATE.pop(view.id(), None)


# ------------------------------------------------------------------------------
# MAIN ANALYSIS LOGIC
# ------------------------------------------------------------------------------

def handle_navigation(view, force=False):
    """
    Called when the caret or selection moved WITHOUT an edit.

        1. Find the target: the selection, or the paragraph under the caret.
        2. Same target as before -> do nothing.
        3. Cached -> display immediately.
        4. Otherwise -> wait `delay_ms` (debounce), then request.

    force=True (the manual command) skips the same-paragraph check, the cache
    and the debounce.
    """
    if not plugin_enabled() or not view_is_applicable(view):
        return

    state = get_state(view)
    target, region, text, is_selection = current_target(view)

    if not force and target == state["target"]:
        return

    state["target"] = target
    state["is_selection"] = is_selection
    state["paragraph_start"] = (
        region.begin() if region is not None and not is_selection else None
    )
    state["token"] += 1

    # Blank line: keep whatever is displayed, so the status bar doesn't
    # flicker while you move between paragraphs.
    if region is None:
        state["paragraph_hash"] = None
        return

    settings = get_settings()
    min_characters = int(settings.get("min_characters", 20))
    max_selection = int(settings.get("max_selection_characters", 10000))

    if not text:
        state["paragraph_hash"] = None
        set_status(view, with_note(
            "nothing to analyse" if is_selection else "heading", is_selection
        ))
        return

    if len(text) < min_characters:
        state["paragraph_hash"] = None
        set_status(view, with_note("too short", is_selection))
        return

    # Protects against Select All sending the whole manuscript.
    if is_selection and len(text) > max_selection:
        state["paragraph_hash"] = None
        set_status(view, with_note(
            "selection too long (over {:,} characters)".format(max_selection),
            is_selection
        ))
        return

    paragraph_hash = make_paragraph_hash(text)
    state["paragraph_hash"] = paragraph_hash

    if not force:
        with CACHE_LOCK:
            cached = CACHE.get(paragraph_hash)

        if cached is not None:
            display_result(view, cached)
            return

    set_status(view, with_note("analysing…", is_selection))

    token = state["token"]
    delay_ms = 0 if force else int(get_settings().get("delay_ms", 400))

    sublime.set_timeout_async(
        lambda: start_request(view, token, text, paragraph_hash, force),
        delay_ms
    )


def start_request(view, token, paragraph, paragraph_hash, force):
    """
    Runs after the debounce delay. Proceeds only if the caret is still in the
    same paragraph, so holding the down arrow through fifteen paragraphs does
    not fire fifteen requests.
    """
    if not view.is_valid():
        return

    state = get_state(view)

    if state["token"] != token or state["paragraph_hash"] != paragraph_hash:
        return

    with CACHE_LOCK:
        if not force and paragraph_hash in CACHE:
            cached = CACHE[paragraph_hash]
        else:
            cached = None

        if cached is None:
            # Already being fetched: its result will be displayed when it
            # arrives, because the state hash matches.
            if paragraph_hash in IN_FLIGHT:
                return

            IN_FLIGHT.add(paragraph_hash)

    if cached is not None:
        display_result(view, cached)
        return

    # Network work gets its own thread so it never blocks Sublime's async
    # event thread (which handles other plugins' events too).
    threading.Thread(
        target=analysis_worker,
        args=(view, paragraph, paragraph_hash, read_request_config()),
        daemon=True
    ).start()


def analysis_worker(view, paragraph, paragraph_hash, config):
    try:
        scores, raw = fetch_openrouter(paragraph, config)

        LAST_RAW["text"] = raw

        with CACHE_LOCK:
            CACHE[paragraph_hash] = scores

        sublime.set_timeout_async(
            lambda: finish_analysis_success(view, paragraph_hash, scores),
            0
        )

    except Exception as exc:
        error_message = str(exc)

        sublime.set_timeout_async(
            lambda: finish_analysis_error(view, paragraph_hash, error_message),
            0
        )

    finally:
        with CACHE_LOCK:
            IN_FLIGHT.discard(paragraph_hash)


def result_is_current(view, paragraph_hash):
    """
    Stale-response protection: a result for paragraph A must never be shown
    while the caret is in paragraph B.
    """
    if not view or not view.is_valid():
        return False

    return get_state(view)["paragraph_hash"] == paragraph_hash


def finish_analysis_success(view, paragraph_hash, scores):
    if result_is_current(view, paragraph_hash):
        display_result(view, scores)


def finish_analysis_error(view, paragraph_hash, error_message):
    print("[Author Tools] {}".format(error_message))

    if not result_is_current(view, paragraph_hash):
        return

    short_error = error_message.replace("\n", " ")

    if len(short_error) > 120:
        short_error = short_error[:117] + "..."

    set_status(view, with_note("ERROR: " + short_error, get_state(view)["is_selection"]))


# ------------------------------------------------------------------------------
# SCANNING THE PARAGRAPHS IN A SELECTION
# ------------------------------------------------------------------------------
#
# "Scan Paragraph with Jev" with text selected sends every paragraph the
# selection touches to Jev, one after the other, and marks each one as its
# result arrives. A paragraph whose current text was already analysed is
# taken from the cache instead of sent again.
#
# Each paragraph is tracked as a hidden region while it waits, so it keeps
# its place when you type elsewhere. A paragraph edited before its result
# arrives is left unmarked, because the result describes the old text.
#
# Scanning again, or disabling Author Tools, stops a scan in progress. The
# scan also stops at the first error, so a wrong API key fails only once.
# ------------------------------------------------------------------------------

SCAN_KEY_PREFIX = "author_tools_scan_"


def selected_paragraphs(view):
    """
    The paragraphs touched by any selection, in document order.
    """
    regions = []

    for selection in view.sel():
        point = selection.begin()

        while point < selection.end():
            region = find_paragraph_region(view, point)

            if region is None:
                # A blank line: try the next one.
                point = view.full_line(point).end()
                continue

            if region not in regions:
                regions.append(region)

            point = view.full_line(region.end()).end()

    return sorted(regions, key=lambda region: region.begin())


def stop_scan(view):
    state = get_state(view)
    state["scan_token"] += 1

    for key in state["scan_keys"]:
        view.erase_regions(key)

    state["scan_keys"] = []

    return state["scan_token"]


def scan_is_current(view, token):
    return (
        view.is_valid()
        and plugin_enabled()
        and get_state(view)["scan_token"] == token
    )


def start_paragraph_scan(view):
    settings = get_settings()
    min_characters = int(settings.get("min_characters", 20))
    limit = int(settings.get("max_scan_paragraphs", 30))

    paragraphs = []

    for region in selected_paragraphs(view):
        text = clean_paragraph_text(view.substr(region))

        if len(text) >= min_characters:
            paragraphs.append((region, text))

    if not paragraphs:
        set_status(view, with_note("no paragraphs to scan"))
        return

    if len(paragraphs) > limit:
        set_status(view, with_note(
            "{} paragraphs selected, the limit is {} "
            "(max_scan_paragraphs)".format(len(paragraphs), limit)
        ))
        return

    token = stop_scan(view)
    state = get_state(view)
    items = []

    for index, (region, text) in enumerate(paragraphs):
        key = "{}{}_{}".format(SCAN_KEY_PREFIX, token, index)
        view.add_regions(key, [region], "", "", sublime.HIDDEN)
        state["scan_keys"].append(key)
        items.append((key, text, make_paragraph_hash(text)))

    threading.Thread(
        target=scan_worker,
        args=(view, token, items, read_request_config()),
        daemon=True
    ).start()


def scan_worker(view, token, items, config):
    """
    One request at a time, in document order.
    """
    total = len(items)

    for index, (key, text, paragraph_hash) in enumerate(items):
        if not scan_is_current(view, token):
            return

        with CACHE_LOCK:
            scores = CACHE.get(paragraph_hash)

            if scores is None:
                IN_FLIGHT.add(paragraph_hash)

        if scores is None:
            set_status(view, with_note(
                "scanning {} of {}…".format(index + 1, total)
            ))

            try:
                scores, raw = fetch_openrouter(text, config)
                LAST_RAW["text"] = raw

                with CACHE_LOCK:
                    CACHE[paragraph_hash] = scores

            except Exception as exc:
                error_message = str(exc)
                print("[Author Tools] {}".format(error_message))

                sublime.set_timeout_async(
                    lambda: finish_scan_error(
                        view, token, error_message, index + 1, total
                    ),
                    0
                )
                return

            finally:
                with CACHE_LOCK:
                    IN_FLIGHT.discard(paragraph_hash)

        sublime.set_timeout_async(
            lambda key=key, text=text, paragraph_hash=paragraph_hash,
            scores=scores: mark_scanned(view, key, text, paragraph_hash, scores),
            0
        )

    sublime.set_timeout_async(lambda: finish_scan(view, token, total), 0)


def mark_scanned(view, key, text, paragraph_hash, scores):
    if not view.is_valid():
        return

    regions = view.get_regions(key)
    view.erase_regions(key)

    if regions and clean_paragraph_text(view.substr(regions[0])) == text:
        update_paragraph_mark(view, regions[0], scores)

    # The caret's paragraph may have been waiting for this very result.
    finish_analysis_success(view, paragraph_hash, scores)


def finish_scan(view, token, total):
    if not scan_is_current(view, token):
        return

    stop_scan(view)

    sublime.status_message("Author Tools: scanned {} paragraph{}".format(
        total, "" if total == 1 else "s"
    ))

    # Back to the result for the caret's paragraph.
    get_state(view)["target"] = None
    handle_navigation(view)


def finish_scan_error(view, token, error_message, number, total):
    if not scan_is_current(view, token):
        return

    stop_scan(view)

    short_error = error_message.replace("\n", " ")

    if len(short_error) > 100:
        short_error = short_error[:97] + "..."

    set_status(view, with_note("ERROR: {} (scan stopped at {} of {})".format(
        short_error, number, total
    )))


# ------------------------------------------------------------------------------
# SUBLIME EVENT LISTENER
# ------------------------------------------------------------------------------

SETTINGS_WATCH_KEY = "author_tools_marks"


def plugin_loaded():
    """
    Called by Sublime once the plugin (re)loads.
    """
    # Regions outlive the code that drew them, so erase those from earlier
    # versions (the "telling" underline, marks from when this package was
    # called KAV Status, and the per-sense marks from before colour modes)
    # from open views.
    legacy_keys = ["kav_telling"] + [
        prefix + label + ("_" + zoom if zoom else "")
        for prefix in ("kav_mark_", MARK_KEY_PREFIX)
        for label in LABELS for zoom in ZOOMS
    ]

    for open_window in sublime.windows():
        for open_view in open_window.views():
            for key in legacy_keys:
                open_view.erase_regions(key)

    # Recolour the marks when the settings change, e.g. "colour_marks_by"
    # or "mood_scopes" edited by hand.
    get_settings().add_on_change(SETTINGS_WATCH_KEY, redraw_all_marks)

    window = sublime.active_window()
    view = window.active_view() if window else None

    if view:
        sublime.set_timeout_async(lambda: handle_navigation(view), 0)


def plugin_unloaded():
    get_settings().clear_on_change(SETTINGS_WATCH_KEY)


class AuthorToolsEventListener(sublime_plugin.EventListener):

    def on_selection_modified_async(self, view):
        """
        Fires on every caret change: clicks, arrows, Page Up/Down, search
        results, AND typing.

        If the buffer's change_count moved, this caret change was caused by an
        edit. We then only re-anchor to the paragraph the caret is now in
        (typing never triggers a request). Otherwise it's navigation.
        """
        if not plugin_enabled() or not view_is_applicable(view):
            return

        state = get_state(view)
        change_count = view.change_count()

        if change_count != state["change_count"]:
            state["change_count"] = change_count

            target, region, _, is_selection = current_target(view)
            state["target"] = target
            state["paragraph_start"] = (
                region.begin() if region is not None and not is_selection else None
            )
            return

        handle_navigation(view)

    def on_activated_async(self, view):
        """
        Switching tabs. Edits made elsewhere (e.g. in a second view of the
        same file) are absorbed here so they don't count as navigation.
        """
        if not view_is_applicable(view):
            return

        if not plugin_enabled():
            clear_status(view)
            clear_marks(view)
            return

        get_state(view)["change_count"] = view.change_count()
        redraw_marks(view)
        handle_navigation(view)

    def on_load_async(self, view):
        self.on_activated_async(view)

    def on_modified_async(self, view):
        """
        No API request here - that would mean constant traffic while writing.
        We only flag that the displayed percentages describe the previous
        version of the paragraph.
        """
        if not plugin_enabled() or not view_is_applicable(view):
            return

        current_status = view.get_status(STATUS_KEY)

        if current_status and current_status == get_state(view)["result_status"]:
            # rstrip() removes the trailing gap (Unicode spaces count as
            # whitespace), so "edited" gets the same single gap as the rest.
            set_status(
                view,
                current_status.rstrip() + separator() + "edited" + separator()
            )

    def on_hover(self, view, point, hover_zone):
        """
        Hovering over the margin shows the details card for the paragraph
        there, or for the selection when hovering beside it.
        """
        if hover_zone != sublime.HOVER_GUTTER:
            return

        if not plugin_enabled() or not view_is_applicable(view):
            return

        show_details_popup(view, point)

    def on_close(self, view):
        # Cached results survive: the same text may appear elsewhere.
        remove_view_state(view)


# ------------------------------------------------------------------------------
# COMMANDS (see Default.sublime-commands)
# ------------------------------------------------------------------------------

class AuthorToolsAnalyseCurrentParagraphCommand(sublime_plugin.TextCommand):
    """
    Author Tools: Scan Paragraph with Jev

    Forces a fresh request for the caret's paragraph, ignoring the cache.
    Also the way to analyse a paragraph you've just written without leaving
    it. With text selected, scans every paragraph in the selection instead.
    """

    def run(self, edit):
        view = self.view

        if not view_is_applicable(view):
            sublime.status_message("Author Tools only analyses Markdown files")
            return

        if not plugin_enabled():
            sublime.status_message("Author Tools analysis is disabled")
            return

        if any(not selection.empty() for selection in view.sel()):
            sublime.set_timeout_async(lambda: start_paragraph_scan(view), 0)
        else:
            sublime.set_timeout_async(
                lambda: handle_navigation(view, force=True), 0
            )


class AuthorToolsShowDetailsCommand(sublime_plugin.TextCommand):
    """
    Author Tools: Show Details

    The same card as hovering over the margin, for the selection or else the
    caret's paragraph.
    """

    def run(self, edit):
        view = self.view

        if len(view.sel()) == 0:
            return

        # .b is the caret end of the selection, which is on screen.
        show_details_popup(view, view.sel()[0].b, from_command=True)


class AuthorToolsColourMarksByCommand(sublime_plugin.WindowCommand):
    """
    Author Tools: Colour Marks By…

    Choose what the margin (and minimap) colours show. From a key binding,
    pass {"mode": "next"} to cycle through the modes, or a mode name such
    as {"mode": "mood"}.
    """

    def run(self, mode=None):
        current = colour_mode()

        if mode == "next":
            index = COLOUR_MODES.index(current)
            mode = COLOUR_MODES[(index + 1) % len(COLOUR_MODES)]

        if mode in COLOUR_MODES:
            set_colour_mode(mode)
            return

        items = [
            sublime.QuickPanelItem(
                COLOUR_MODE_NAMES[option],
                details=colour_legend(option),
                annotation=(
                    "current" if option == current
                    else "" if colour_mode_enabled(option)
                    else "switched off"
                )
            )
            for option in COLOUR_MODES
        ]

        def on_select(index):
            if index >= 0:
                set_colour_mode(COLOUR_MODES[index])

        self.window.show_quick_panel(
            items, on_select, selected_index=COLOUR_MODES.index(current)
        )


class AuthorToolsToggleMarksCommand(sublime_plugin.ApplicationCommand):
    """
    Author Tools: Toggle Colours

    Hides or shows the margin marks, and with them the minimap colours and
    the paragraph tint. Analysis carries on, so switching back on shows
    every paragraph analysed in the meantime.
    """

    def run(self):
        settings = get_settings()
        show = not settings.get("show_gutter_marks", True)

        settings.set("show_gutter_marks", show)
        sublime.save_settings(SETTINGS_FILE)

        redraw_all_marks()

        sublime.status_message(
            "Author Tools colours {}".format("on" if show else "off")
        )


class AuthorToolsToggleParagraphTintCommand(sublime_plugin.ApplicationCommand):
    """
    Author Tools: Toggle Paragraph Tint

    Colours the analysed paragraphs themselves, not just the margin, in the
    style set by "paragraph_tint_style".
    """

    def run(self):
        settings = get_settings()
        tint = not settings.get("paragraph_tint", False)

        settings.set("paragraph_tint", tint)
        sublime.save_settings(SETTINGS_FILE)

        redraw_all_marks()

        sublime.status_message(
            "Author Tools paragraph tint {}".format("on" if tint else "off")
        )


class AuthorToolsClearCacheCommand(sublime_plugin.ApplicationCommand):

    def run(self):
        with CACHE_LOCK:
            CACHE.clear()

        sublime.status_message("Author Tools cache cleared")


class AuthorToolsToggleCommand(sublime_plugin.ApplicationCommand):

    def run(self):
        settings = get_settings()
        enabled = bool(settings.get("enabled", True))

        settings.set("enabled", not enabled)
        sublime.save_settings(SETTINGS_FILE)

        window = sublime.active_window()
        view = window.active_view() if window else None

        if view:
            if enabled:
                clear_status(view)
                clear_marks(view)
            else:
                remove_view_state(view)
                sublime.set_timeout_async(lambda: handle_navigation(view), 0)

        sublime.status_message(
            "Author Tools analysis {}".format("disabled" if enabled else "enabled")
        )


class AuthorToolsShowLastResponseCommand(sublime_plugin.WindowCommand):
    """
    Print the most recent raw response to the console. Useful while the
    Decisions API is alpha and its response shape is unverified.
    """

    def run(self):
        raw = LAST_RAW["text"]

        if raw is None:
            print("[Author Tools] No response received yet.")
        else:
            print("[Author Tools] Last raw response:\n" + raw)

        self.window.run_command("show_panel", {"panel": "console"})
