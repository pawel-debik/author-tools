import sublime
import sublime_plugin

import datetime
import hashlib
import html
import json
import os
import re
import threading
import urllib.error
import urllib.request

from .ProseAnalysis import (
    LAST_RAW,
    PACKAGE_NAME,
    STRUCTURE_MODE,
    all_paragraphs,
    clean_paragraph_text,
    colour_mode,
    get_api_key,
    paragraphs_in,
    sidecar_location,
    view_is_applicable
)


# ------------------------------------------------------------------------------
# SCENE STRUCTURE (STORY GRID)
# ------------------------------------------------------------------------------
#
# "Author Tools: Analyse Scene Structure" sends the whole chapter (or the
# selected paragraphs) to a chat model, which splits it into scenes and
# finds each scene's Five Commandments:
#
#     inciting incident, progressive complications, turning point, crisis,
#     climax, resolution
#
# plus the value that shifts across the scene (e.g. safety + to −).
#
# The paragraphs are numbered here, not by the model, and every answer is
# checked against the text: numbers out of range, pivots outside their
# scene, pivots out of order, and quoted opening words that don't match the
# paragraph they point at. The model gets it wrong often enough that the
# card shows what was corrected.
#
# Shown when "Colour Marks By…" is on "Scene structure" (the last stop of
# cmd+shift+2), in place of Jev's marks:
#
#     inciting incident, turning point,   the paragraph outlined in its
#     crisis, climax                      colour, with an icon in the gutter
#     complications, resolution           the span tinted in its colour
#     labels                              on the right of each scene's first
#                                         paragraph and of each pivot
#     hovering over the gutter            the model's reasons for that
#                                         paragraph; click for the scene card
#
# Only on request: nothing is sent while you write. One analysis per
# manuscript; analysing again replaces it.
#
# This is separate from the paragraph analysis: it uses OpenRouter's normal
# chat endpoint and its own model, and shares only the API key.
# ------------------------------------------------------------------------------

SETTINGS_FILE = "AuthorTools.sublime-settings"

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-6-luna"

# OpenRouter provider tags to use, in order, and no others: Azure's EU
# servers. Empty lets OpenRouter choose.
DEFAULT_PROVIDERS = ["azure/eu"]

# Just left of "Colours: KAV".
STATUS_KEY = "zx_author_tools_structure"

# View settings, which survive a plugin reload: the checked analysis, and
# the region keys currently drawn.
STRUCTURE_SETTING = "author_tools_structure"
DRAWN_KEYS_SETTING = "author_tools_structure_keys"

PARAGRAPH_KEY_PREFIX = "author_tools_structure_p"
DRAW_KEY_PREFIX = "author_tools_structure_draw_"

POINT_KEYS = ("inciting_incident", "turning_point", "crisis", "climax")
SPAN_KEYS = ("progressive_complications", "resolution")

# Also the order the card lists them in.
COMMANDMENTS = (
    "inciting_incident", "progressive_complications", "turning_point",
    "crisis", "climax", "resolution"
)

COMMANDMENT_NAMES = {
    "inciting_incident": "Inciting incident",
    "progressive_complications": "Complications",
    "turning_point": "Turning point",
    "crisis": "Crisis",
    "climax": "Climax",
    "resolution": "Resolution"
}

DEFAULT_SCOPES = {
    "scene": "comment",
    "inciting_incident": "region.yellowish",
    "progressive_complications": "region.orangish",
    "turning_point": "region.redish",
    "crisis": "region.purplish",
    "climax": "region.pinkish",
    "resolution": "region.greenish"
}

TYPE_WORDS = {
    "causal": "causal",
    "coincidental": "coincidental",
    "action": "action",
    "revelation": "revelation",
    "best_bad_choice": "best bad choice",
    "irreconcilable_goods": "irreconcilable goods"
}

DEFAULT_GUTTER_ICON = "bookmark"

STATUSES = ("clear", "weak", "missing")
CONFIDENCES = ("high", "medium", "low")
CHARGES = ("++", "+", "-", "--")

# "***", "* * *", "---", "~": scene breaks, not paragraphs.
SCENE_BREAK_RE = re.compile(r"^[\s*\-_~#•·=]+$")

MAX_FIELD_CHARACTERS = 400

SIDECAR_SUFFIX = ".structure.author-tools.json"
SIDECAR_VERSION = 1

PREVIEW_CHARACTERS = 80

REDRAW_DELAY_MS = 800

# View ids with a request under way.
BUSY = set()

# View ids whose saved structure has been looked for this session.
RESTORED = set()

DEFAULT_INSTRUCTIONS = """\
You are a developmental editor trained in Shawn Coyne's Story Grid method. \
Analyse the chapter you are given for the Five Commandments of Storytelling.

The chapter's paragraphs are numbered [P1], [P2], ... Refer to paragraphs \
only by these numbers. Whenever you give a paragraph number, also give the \
paragraph's first 6-8 words exactly as written, so the numbering can be \
checked.

## Step 1: Find the scenes
A chapter may contain one or more scenes. A new scene usually starts with a \
change of time, place, point-of-view character, or after a scene break. \
Most of all, a scene is one unit of conflict that turns a single value. \
Don't merge separate scenes, and don't split one scene just because the \
location changes briefly. Together the scenes should cover every paragraph.

## Step 2: For each scene, identify the Five Commandments
- Inciting incident: the event that upsets the balance of the protagonist's \
life in this scene and starts the conflict. It can be causal (someone acts) \
or coincidental (chance). One paragraph.
- Progressive complications: the obstacles that make achieving the goal \
harder, in escalating order. A span of paragraphs.
- Turning point: the one complication that makes it impossible to go on as \
before, so the character has to choose. It can be an action or a \
revelation. One paragraph.
- Crisis: the dilemma the turning point forces: a best bad choice (two \
negatives) or irreconcilable goods (two positives). It may be implicit. If \
so, give the paragraph it is implied in and put the question into words. \
One paragraph.
- Climax: the character's choice and the action taken on it. One paragraph.
- Resolution: the consequences of that choice, how things settle. A span of \
one or more paragraphs, possibly short.

Also identify:
- Value shift: the life value at stake in the scene (e.g. safety/danger, \
trust/betrayal, love/hate). Give its charge at the start and at the end \
(+ or -, or ++ / -- for a double shift). A scene with no shift is a \
significant finding.
- Protagonist of the scene: who wants something here, and what.

## Rules
- Order must hold within each scene: inciting incident < turning point <= \
crisis <= climax <= resolution.
- Do not invent a commandment to complete the set. If one is missing, weak, \
or only off-page, mark it as "missing" or "weak" and explain. Finding a \
missing element is more useful to the writer than a forced one.
- Base every call on what is on the page, not on what the writer probably \
intended.
- Keep each justification to one sentence. Refer to specific events, not \
generalities.
- Do not rewrite or suggest prose.

## Output
Reply with a single JSON object and nothing else: no introduction, no \
commentary, no markdown fences.

{
  "paragraph_count": <number>,
  "scenes": [
    {
      "scene": 1,
      "paragraphs": { "start": <n>, "end": <n> },
      "summary": "<one sentence>",
      "protagonist": "<name>: <what they want in this scene>",
      "value": { "name": "<value>", "start": "+|-|++|--", "end": "+|-|++|--" },
      "inciting_incident": { "paragraph": <n>, "opening_words": "<first 6-8 words>", "type": "causal|coincidental", "status": "clear|weak|missing", "confidence": "high|medium|low", "why": "<one sentence>" },
      "progressive_complications": { "start": <n>, "end": <n>, "status": "clear|weak|missing", "confidence": "high|medium|low", "why": "<one sentence naming the escalating obstacles>" },
      "turning_point": { "paragraph": <n>, "opening_words": "<...>", "type": "action|revelation", "status": "clear|weak|missing", "confidence": "high|medium|low", "why": "<one sentence>" },
      "crisis": { "paragraph": <n>, "opening_words": "<...>", "type": "best_bad_choice|irreconcilable_goods", "question": "<the dilemma as a question>", "explicit": true|false, "status": "clear|weak|missing", "confidence": "high|medium|low", "why": "<one sentence>" },
      "climax": { "paragraph": <n>, "opening_words": "<...>", "status": "clear|weak|missing", "confidence": "high|medium|low", "why": "<one sentence>" },
      "resolution": { "start": <n>, "end": <n>, "status": "clear|weak|missing", "confidence": "high|medium|low", "why": "<one sentence>" }
    }
  ],
  "notes": "<optional: one or two sentences on the chapter's overall structure, or empty string>"
}

For a missing element, set the paragraph fields to null and explain in "why"."""


def get_settings():
    return sublime.load_settings(SETTINGS_FILE)


def text_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def preview(text):
    text = " ".join(text.split())

    if len(text) > PREVIEW_CHARACTERS:
        text = text[:PREVIEW_CHARACTERS - 1] + "…"

    return text


# ------------------------------------------------------------------------------
# THE PARAGRAPHS
# ------------------------------------------------------------------------------

def structure_paragraphs(view, selections=None):
    """
    [(region, text), ...] for the paragraphs to number: all of them, or
    those touched by `selections`. Headings and scene breaks are skipped;
    short paragraphs ("No.") are kept, because they can be a climax.
    """
    if selections:
        regions = paragraphs_in(view, selections)
    else:
        regions = all_paragraphs(view)

    found = []

    for region in regions:
        text = clean_paragraph_text(view.substr(region))

        if text and not SCENE_BREAK_RE.match(text):
            found.append((region, text))

    return found


def numbered_chapter(texts):
    parts = [
        "The chapter has {} paragraphs, numbered [P1] to [P{}].".format(
            len(texts), len(texts)
        )
    ]

    for number, text in enumerate(texts, 1):
        parts.append("[P{}] {}".format(number, text))

    return "\n\n".join(parts)


# ------------------------------------------------------------------------------
# THE REQUEST
# ------------------------------------------------------------------------------

def read_request_config():
    settings = get_settings()

    return {
        "api_key": get_api_key(),
        "endpoint": settings.get("structure_endpoint", DEFAULT_ENDPOINT),
        "model": settings.get("structure_model", DEFAULT_MODEL),
        "temperature": settings.get("structure_temperature", None),
        "providers": settings.get("structure_providers", DEFAULT_PROVIDERS),
        "reasoning_effort": settings.get("structure_reasoning_effort", "medium"),
        "timeout_seconds": float(settings.get("structure_timeout_seconds", 300)),
        "instructions": settings.get("structure_instructions") or DEFAULT_INSTRUCTIONS
    }


def fetch_structure(texts, config):
    """
    Send the numbered chapter; returns the model's reply text.
    """
    if not config["api_key"]:
        raise RuntimeError(
            "No OpenRouter API key configured. Set api_key in your User "
            "AuthorTools.sublime-settings."
        )

    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": config["instructions"]},
            {"role": "user", "content": numbered_chapter(texts)}
        ]
    }

    if config["temperature"] is not None:
        payload["temperature"] = config["temperature"]

    # Only these providers; without a fallback to others, so the chapter
    # never leaves the region they are in.
    if config["providers"]:
        payload["provider"] = {
            "order": list(config["providers"]),
            "allow_fallbacks": False
        }

    # Ignored by models that don't reason.
    if config["reasoning_effort"]:
        payload["reasoning"] = {"effort": config["reasoning_effort"]}

    request = urllib.request.Request(
        config["endpoint"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + config["api_key"],
            "Content-Type": "application/json",
            "Accept": "application/json",
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

        if exc.code == 429:
            raise RuntimeError(
                "OpenRouter's rate limit was reached (free models allow a "
                "limited number of requests per minute and per day). Try "
                "again later.\n\n{}".format(error_body[:500])
            )

        raise RuntimeError(
            "OpenRouter HTTP {}: {}".format(exc.code, error_body[:1000])
        )

    except urllib.error.URLError as exc:
        raise RuntimeError("Could not reach OpenRouter: {}".format(exc))

    except OSError as exc:
        # Timeouts while reading the body.
        raise RuntimeError(
            "OpenRouter didn't answer within {:.0f} seconds "
            "(structure_timeout_seconds): {}".format(config["timeout_seconds"], exc)
        )

    LAST_RAW["text"] = raw_response

    try:
        data = json.loads(raw_response)
    except ValueError:
        raise RuntimeError(
            "OpenRouter returned invalid JSON: {}".format(raw_response[:500])
        )

    # Errors from the model's provider can arrive with HTTP 200.
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("OpenRouter error: {}".format(data["error"]))

    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            "Unexpected OpenRouter response: {}".format(raw_response[:500])
        )

    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError(
            "The model gave an empty answer. Reasoning models sometimes use "
            "up their whole answer thinking; try again, or lower "
            "structure_reasoning_effort."
        )

    return reply


def parse_reply(reply):
    """
    The JSON object in the reply, even when it comes in a ```json fence or
    with a sentence before it.
    """
    start = reply.find("{")
    end = reply.rfind("}")

    if start < 0 or end <= start:
        raise RuntimeError(
            "The model didn't answer with JSON:\n\n{}".format(reply[:500])
        )

    try:
        data = json.loads(reply[start:end + 1])
    except ValueError as exc:
        raise RuntimeError(
            "The model's JSON couldn't be read ({}):\n\n{}".format(exc, reply[:500])
        )

    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise RuntimeError(
            "The model's answer has no scenes:\n\n{}".format(reply[:500])
        )

    return data


# ------------------------------------------------------------------------------
# CHECKING THE ANSWER
# ------------------------------------------------------------------------------
#
# Everything the model says is checked against the numbered paragraphs.
# What can be repaired is repaired, and noted in the scene's "warnings";
# what can't is dropped, also with a warning. The result only holds plain
# JSON values, so it can go in the view settings and the sidecar as is.
# ------------------------------------------------------------------------------

def to_int(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group()) if match else None

    return None


def to_text(value):
    if not isinstance(value, str):
        return ""

    value = " ".join(value.split())

    if len(value) > MAX_FIELD_CHARACTERS:
        value = value[:MAX_FIELD_CHARACTERS - 1] + "…"

    return value


def to_choice(value, choices, default):
    value = value.strip().lower().replace(" ", "_") if isinstance(value, str) else ""
    return value if value in choices else default


def to_charge(value):
    if not isinstance(value, str):
        return ""

    # "−" and "–" from models that like typography.
    value = value.strip().replace("−", "-").replace("–", "-").replace("—", "-")

    return value if value in CHARGES else ""


def word_list(text):
    return re.findall(r"[^\W_]+", text.lower())


def opening_matches(opening_words, text):
    """
    Whether the quoted opening words are how `text` begins. Only the first
    four words are compared, so a quote that trails off still counts.
    """
    quoted = word_list(opening_words)[:4]

    if not quoted:
        return None

    return word_list(text)[:len(quoted)] == quoted


def check_point(raw, name, scene, texts, warnings):
    """
    One of the four one-paragraph commandments.
    """
    raw = raw if isinstance(raw, dict) else {}

    point = {
        "paragraph": to_int(raw.get("paragraph")),
        "status": to_choice(raw.get("status"), STATUSES, "clear"),
        "confidence": to_choice(raw.get("confidence"), CONFIDENCES, ""),
        "type": to_choice(raw.get("type"), TYPE_WORDS, ""),
        "why": to_text(raw.get("why"))
    }

    if name == "crisis":
        point["question"] = to_text(raw.get("question"))
        point["explicit"] = raw.get("explicit") if isinstance(raw.get("explicit"), bool) else None

    number = point["paragraph"]

    if number is None or point["status"] == "missing":
        point["paragraph"] = None
        point["status"] = "missing"
        return point

    opening_words = to_text(raw.get("opening_words"))

    if not 1 <= number <= len(texts):
        warnings.append("{}: P{} doesn't exist".format(COMMANDMENT_NAMES[name], number))
        point["paragraph"] = None
        point["status"] = "missing"
        return point

    # The quote decides when the number and the quote disagree: models
    # count badly and quote well.
    if opening_words and opening_matches(opening_words, texts[number - 1]) is False:
        found = [
            candidate for candidate in range(scene["start"], scene["end"] + 1)
            if opening_matches(opening_words, texts[candidate - 1])
        ]

        if len(found) == 1:
            warnings.append("{}: said P{}, but the quote is from P{}".format(
                COMMANDMENT_NAMES[name], number, found[0]
            ))
            number = found[0]
        else:
            warnings.append("{}: the quote doesn't match P{}".format(
                COMMANDMENT_NAMES[name], number
            ))

    if not scene["start"] <= number <= scene["end"]:
        warnings.append("{}: P{} is outside the scene (P{}–P{})".format(
            COMMANDMENT_NAMES[name], number, scene["start"], scene["end"]
        ))
        point["paragraph"] = None
        point["status"] = "missing"
        return point

    point["paragraph"] = number
    return point


def check_span(raw, name, scene, warnings):
    """
    Complications or resolution: a range of paragraphs within the scene.
    """
    raw = raw if isinstance(raw, dict) else {}

    span = {
        "start": to_int(raw.get("start")),
        "end": to_int(raw.get("end")),
        "status": to_choice(raw.get("status"), STATUSES, "clear"),
        "confidence": to_choice(raw.get("confidence"), CONFIDENCES, ""),
        "why": to_text(raw.get("why"))
    }

    start, end = span["start"], span["end"]

    if start is None and end is not None:
        start = end
    if end is None and start is not None:
        end = start

    if start is None or span["status"] == "missing":
        span.update(start=None, end=None, status="missing")
        return span

    if start > end:
        start, end = end, start

    clamped_start = max(start, scene["start"])
    clamped_end = min(end, scene["end"])

    if clamped_start > clamped_end:
        warnings.append("{}: P{}–P{} is outside the scene".format(
            COMMANDMENT_NAMES[name], start, end
        ))
        span.update(start=None, end=None, status="missing")
        return span

    if (clamped_start, clamped_end) != (start, end):
        warnings.append("{}: P{}–P{} trimmed to the scene".format(
            COMMANDMENT_NAMES[name], start, end
        ))

    span["start"], span["end"] = clamped_start, clamped_end
    return span


def check_order(scene, warnings):
    """
    inciting incident < turning point <= crisis <= climax <= resolution.
    Out of order is shown, not repaired: it may be the model, or the scene.
    """
    positions = []

    for name in ("inciting_incident", "turning_point", "crisis", "climax"):
        number = scene[name]["paragraph"]

        if number is not None:
            positions.append((name, number))

    if scene["resolution"]["start"] is not None:
        positions.append(("resolution", scene["resolution"]["start"]))

    for (first, a), (second, b) in zip(positions, positions[1:]):
        if b < a or (first == "inciting_incident" and b == a):
            warnings.append("{} (P{}) comes before the {} (P{})".format(
                COMMANDMENT_NAMES[second], b, COMMANDMENT_NAMES[first].lower(), a
            ))


def check_result(data, texts):
    """
    The model's answer, checked against the paragraphs it was given.
    """
    count = len(texts)
    notes = []
    scenes = []

    reported = to_int(data.get("paragraph_count"))

    if reported is not None and reported != count:
        notes.append("The model counted {} paragraphs; there are {}.".format(
            reported, count
        ))

    raw_scenes = [raw for raw in data["scenes"] if isinstance(raw, dict)]
    ranges = []

    for raw in raw_scenes:
        paragraphs = raw.get("paragraphs") if isinstance(raw.get("paragraphs"), dict) else {}
        start, end = to_int(paragraphs.get("start")), to_int(paragraphs.get("end"))

        if start is None or end is None:
            notes.append("A scene without paragraph numbers was left out.")
            continue

        if start > end:
            start, end = end, start

        start, end = max(1, start), min(count, end)

        if start > end:
            notes.append("A scene outside the chapter was left out.")
            continue

        ranges.append((start, end, raw))

    ranges.sort(key=lambda item: item[0])
    previous_end = 0

    for start, end, raw in ranges:
        # Overlapping scenes: the later one starts after the earlier one.
        if start <= previous_end:
            start = previous_end + 1

            if start > end:
                notes.append("A scene inside another one was left out.")
                continue

        value = raw.get("value") if isinstance(raw.get("value"), dict) else {}

        scene = {
            "start": start,
            "end": end,
            "summary": to_text(raw.get("summary")),
            "protagonist": to_text(raw.get("protagonist")),
            "value": {
                "name": to_text(value.get("name")),
                "start": to_charge(value.get("start")),
                "end": to_charge(value.get("end"))
            }
        }

        warnings = []

        for name in POINT_KEYS:
            scene[name] = check_point(raw.get(name), name, scene, texts, warnings)

        for name in SPAN_KEYS:
            scene[name] = check_span(raw.get(name), name, scene, warnings)

        check_order(scene, warnings)

        scene["warnings"] = warnings
        scenes.append(scene)
        previous_end = end

    if scenes:
        covered = sum(scene["end"] - scene["start"] + 1 for scene in scenes)

        if covered < count:
            notes.append("{} of {} paragraphs are in no scene.".format(
                count - covered, count
            ))

    model_notes = to_text(data.get("notes"))

    return {
        "scenes": scenes,
        "notes": model_notes,
        "checks": notes
    }


# ------------------------------------------------------------------------------
# REMEMBERING WHERE THE PARAGRAPHS ARE
# ------------------------------------------------------------------------------
#
# Each numbered paragraph is a hidden region, so Sublime moves it along with
# edits. The analysis keeps each paragraph's hash, so an edited paragraph
# can be shown as "edited since the analysis", and so the analysis can be
# matched to the text again after reopening the file.
# ------------------------------------------------------------------------------

def paragraph_key(number):
    return PARAGRAPH_KEY_PREFIX + str(number)


def track_paragraphs(view, regions):
    """
    `regions[i]` is paragraph i + 1, or None when it can't be found.
    """
    erase_tracked_paragraphs(view)

    for number, region in enumerate(regions, 1):
        if region is not None:
            view.add_regions(paragraph_key(number), [region], "", "", sublime.HIDDEN)


def erase_tracked_paragraphs(view):
    analysis = view.settings().get(STRUCTURE_SETTING)

    if not analysis:
        return

    for number in range(1, len(analysis["paragraphs"]) + 1):
        view.erase_regions(paragraph_key(number))


def tracked_region(view, number):
    regions = view.get_regions(paragraph_key(number))

    if not regions or regions[0].empty():
        return None

    return regions[0]


def is_tracking(view, analysis):
    return any(
        view.get_regions(paragraph_key(number))
        for number in range(1, len(analysis["paragraphs"]) + 1)
    )


def match_paragraphs(stored_hashes, current):
    """
    Find the analysed paragraphs in the current text: [region or None] for
    each stored hash. `current` is [(region, text)].

    Unchanged paragraphs are found by their hash, in order. An edited
    paragraph is found by its place: when the paragraphs around a run of
    unmatched ones are found, and as many current paragraphs sit between
    them, those are taken to be the edited ones.
    """
    current_hashes = [text_hash(text) for _, text in current]
    matches = [None] * len(stored_hashes)
    used = set()
    cursor = 0

    for index, stored in enumerate(stored_hashes):
        for position in range(cursor, len(current_hashes)):
            if position not in used and current_hashes[position] == stored:
                matches[index] = position
                used.add(position)
                cursor = position + 1
                break

    index = 0

    while index < len(matches):
        if matches[index] is not None:
            index += 1
            continue

        run_start = index

        while index < len(matches) and matches[index] is None:
            index += 1

        before = matches[run_start - 1] if run_start > 0 else -1
        after = matches[index] if index < len(matches) else len(current)

        if after - before - 1 == index - run_start:
            for offset in range(index - run_start):
                matches[run_start + offset] = before + 1 + offset

    return [current[position][0] if position is not None else None for position in matches]


def paragraph_states(view, analysis):
    """
    [(region or None, edited), ...] for each analysed paragraph, now.
    """
    states = []

    for number, stored in enumerate(analysis["paragraphs"], 1):
        region = tracked_region(view, number)

        if region is None:
            states.append((None, True))
            continue

        text = clean_paragraph_text(view.substr(region))
        states.append((region, text_hash(text) != stored["hash"]))

    return states


# ------------------------------------------------------------------------------
# DRAWING
# ------------------------------------------------------------------------------

def structure_scopes():
    scopes = dict(DEFAULT_SCOPES)
    scopes.update(get_settings().get("structure_scopes", {}))
    return scopes


def structure_shown():
    """
    In the "Scene structure" colour mode, unless all colours are off.
    """
    return (
        get_settings().get("show_gutter_marks", True)
        and colour_mode() == STRUCTURE_MODE
    )


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


def structure_legend():
    """
    For "Colour Marks By…": "yellow inciting incident, orange
    complications, ...".
    """
    scopes = structure_scopes()

    return ", ".join(
        "{} {}".format(COLOUR_WORDS.get(scopes[name], scopes[name]), COMMANDMENT_NAMES[name].lower())
        for name in COMMANDMENTS
    )


def value_shift(scene):
    """
    "safety + → −", or "" when the model gave no value.
    """
    value = scene["value"]

    if not value["name"]:
        return ""

    def charge(sign):
        return sign.replace("-", "−") if sign else "?"

    return "{} {} → {}".format(value["name"], charge(value["start"]), charge(value["end"]))


def scene_label(index, scene, edited):
    parts = ["Scene {}".format(index + 1)]
    shift = value_shift(scene)

    if shift:
        parts.append(shift)

    value = scene["value"]

    if value["start"] and value["start"] == value["end"]:
        parts.append("no shift")

    missing = [
        COMMANDMENT_NAMES[name].lower() for name in COMMANDMENTS
        if scene[name]["status"] == "missing"
    ]

    if missing:
        parts.append("no " + ", ".join(missing))

    if scene["warnings"]:
        parts.append("⚠")

    if edited:
        parts.append("edited")

    return " · ".join(parts)


def commandment_label(name, entry):
    label = COMMANDMENT_NAMES[name]

    if entry["status"] == "weak":
        label += " (weak)"

    return label


def span_region(states, start, end):
    regions = [states[number - 1][0] for number in range(start, end + 1)]
    regions = [region for region in regions if region is not None]

    if not regions:
        return None

    return sublime.Region(regions[0].begin(), regions[-1].end())


def redraw_structure(view):
    """
    Draw the saved analysis of this view, or nothing.
    """
    if not view.is_valid():
        return

    settings = view.settings()
    analysis = settings.get(STRUCTURE_SETTING)
    drawn = []

    if analysis and structure_shown():
        drawn = draw_analysis(view, analysis)

    # Erased after drawing the new ones, so a redraw doesn't flicker.
    for key in settings.get(DRAWN_KEYS_SETTING) or []:
        if key not in drawn:
            view.erase_regions(key)

    settings.set(DRAWN_KEYS_SETTING, drawn)


def draw_analysis(view, analysis):
    scopes = structure_scopes()
    icon = get_settings().get("structure_gutter_icon", DEFAULT_GUTTER_ICON)
    states = paragraph_states(view, analysis)

    # (scope, flags, icon) -> regions; a region set has one scope and style.
    shapes = {}

    # paragraph number -> [(priority, scope, label, scene index)]
    labels = {}

    def add_label(number, priority, scope, text, scene_index):
        labels.setdefault(number, []).append((priority, scope, text, scene_index))

    for index, scene in enumerate(analysis["result"]["scenes"]):
        edited = any(states[number - 1][1] for number in range(scene["start"], scene["end"] + 1))

        add_label(scene["start"], 0, scopes["scene"], scene_label(index, scene, edited), index)

        for name in SPAN_KEYS:
            entry = scene[name]

            if entry["start"] is None:
                continue

            region = span_region(states, entry["start"], entry["end"])

            if region is not None:
                shapes.setdefault((scopes[name], sublime.DRAW_NO_OUTLINE, ""), []).append(region)

        for name in POINT_KEYS:
            entry = scene[name]
            number = entry["paragraph"]

            if number is None or states[number - 1][0] is None:
                continue

            shapes.setdefault((scopes[name], sublime.DRAW_NO_FILL, icon), []).append(
                states[number - 1][0]
            )
            add_label(number, 2, scopes[name], commandment_label(name, entry), index)

    drawn = []

    for (scope, flags, shape_icon), regions in shapes.items():
        key = "{}{}_{}".format(DRAW_KEY_PREFIX, scope, flags)
        view.add_regions(key, regions, scope, shape_icon, flags)
        drawn.append(key)

    drawn.extend(draw_labels(view, states, labels))

    return drawn


def draw_labels(view, states, labels):
    """
    One annotation per labelled paragraph, on its first line, coloured like
    its most important commandment. Clicking a label opens the scene's card.
    """
    groups = {}

    for number, items in labels.items():
        region = states[number - 1][0]

        if region is None:
            continue

        # Coloured like the pivot, if any.
        items.sort(key=lambda item: item[0])
        scope = items[-1][1]
        links = " · ".join(
            '<a href="scene:{}:{}">{}</a>'.format(scene_index, number, html.escape(text))
            for _, _, text, scene_index in items
        )

        group = groups.setdefault(scope, ([], []))
        group[0].append(view.line(region.begin()))
        group[1].append(
            '<body id="author-tools-structure"><style>a {{ text-decoration: none; }}'
            '</style>{}</body>'.format(links)
        )

    drawn = []

    for scope, (regions, annotations) in groups.items():
        key = "{}label_{}".format(DRAW_KEY_PREFIX, scope)
        view.add_regions(
            key,
            regions,
            scope,
            "",
            sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
            annotations=annotations,
            on_navigate=lambda href, view=view: on_label_clicked(view, href)
        )
        drawn.append(key)

    return drawn


def clear_structure(view):
    """
    Forget this view's analysis and erase everything drawn for it.
    """
    settings = view.settings()

    for key in settings.get(DRAWN_KEYS_SETTING) or []:
        view.erase_regions(key)

    erase_tracked_paragraphs(view)

    for name in (STRUCTURE_SETTING, DRAWN_KEYS_SETTING):
        settings.erase(name)


def redraw_all_structures():
    for open_window in sublime.windows():
        for open_view in open_window.views():
            if open_view.settings().get(STRUCTURE_SETTING):
                redraw_structure(open_view)


# ------------------------------------------------------------------------------
# THE CARD
# ------------------------------------------------------------------------------

CARD_CSS = """
body {
    margin: 0;
    padding: 0.6rem 0.8rem;
}
.title {
    font-weight: bold;
}
.row {
    display: block;
    margin: 0.35rem 0 0 0;
}
.name {
    font-weight: bold;
}
.rule {
    display: block;
    border-top: 1px solid color(var(--foreground) alpha(0.25));
    margin: 0.5rem 0 0.2rem 0;
}
.muted {
    color: color(var(--foreground) alpha(0.6));
}
.warning {
    color: var(--yellowish);
}
a {
    text-decoration: none;
}
"""


def paragraph_link(number, end=None):
    if end is not None and end != number:
        return '<a href="goto:{0}">P{0}</a>–<a href="goto:{1}">P{1}</a>'.format(number, end)

    return '<a href="goto:{0}">P{0}</a>'.format(number)


def entry_html(name, entry):
    heading = '<span class="name">{}</span>'.format(COMMANDMENT_NAMES[name])

    if name in POINT_KEYS and entry["paragraph"] is not None:
        heading += " " + paragraph_link(entry["paragraph"])
    elif name in SPAN_KEYS and entry["start"] is not None:
        heading += " " + paragraph_link(entry["start"], entry["end"])

    details = []

    if entry["status"] != "clear":
        details.append(entry["status"])

    if entry.get("type"):
        details.append(TYPE_WORDS[entry["type"]])

    if name == "crisis" and entry.get("explicit") is False:
        details.append("implicit")

    if entry["confidence"]:
        details.append(entry["confidence"] + " confidence")

    if details:
        heading += ' <span class="muted">{}</span>'.format(html.escape(", ".join(details)))

    lines = [heading]

    if entry.get("question"):
        lines.append("<i>{}</i>".format(html.escape(entry["question"])))

    if entry["why"]:
        lines.append(html.escape(entry["why"]))

    return '<div class="row">{}</div>'.format("<br>".join(lines))


def build_card_html(view, analysis, index):
    scene = analysis["result"]["scenes"][index]
    states = paragraph_states(view, analysis)
    edited = sum(
        1 for number in range(scene["start"], scene["end"] + 1) if states[number - 1][1]
    )

    title = "Scene {} of {} · {}".format(
        index + 1, len(analysis["result"]["scenes"]),
        paragraph_link(scene["start"], scene["end"])
    )

    rows = ['<div class="title">{}</div>'.format(title)]

    if scene["summary"]:
        rows.append('<div class="row">{}</div>'.format(html.escape(scene["summary"])))

    facts = []

    if scene["protagonist"]:
        facts.append("<b>Wants</b> " + html.escape(scene["protagonist"]))

    shift = value_shift(scene)

    if shift:
        value = scene["value"]
        no_shift = value["start"] and value["start"] == value["end"]
        facts.append("<b>Value</b> " + html.escape(shift) + (
            ' <span class="warning">no shift</span>' if no_shift else ""
        ))

    if facts:
        rows.append('<div class="row">{}</div>'.format("<br>".join(facts)))

    rows.append('<div class="rule"></div>')

    for name in COMMANDMENTS:
        rows.append(entry_html(name, scene[name]))

    footer = []

    for warning in scene["warnings"]:
        footer.append('<span class="warning">⚠ {}</span>'.format(html.escape(warning)))

    if edited:
        footer.append('<span class="muted">{} paragraph{} edited since the analysis</span>'.format(
            edited, "" if edited == 1 else "s"
        ))

    footer.append('<span class="muted">{} · {}</span>'.format(
        html.escape(analysis.get("model", "")), html.escape(analysis.get("analysed_at", ""))
    ))

    rows.append('<div class="rule"></div>')
    rows.append('<div class="row">{}</div>'.format("<br>".join(footer)))

    return '<body id="author-tools-structure-card"><style>{}</style>{}</body>'.format(
        CARD_CSS, "".join(rows)
    )


def build_overview_html(analysis):
    """
    Every scene in one card, for when the caret is in none of them.
    """
    result = analysis["result"]
    rows = ['<div class="title">{} scene{}</div>'.format(
        len(result["scenes"]), "" if len(result["scenes"]) == 1 else "s"
    )]

    for index, scene in enumerate(result["scenes"]):
        line = '<a href="scene:{}">Scene {}</a> {}'.format(
            index, index + 1, paragraph_link(scene["start"], scene["end"])
        )
        shift = value_shift(scene)

        if shift:
            line += " · " + html.escape(shift)

        if scene["summary"]:
            line += '<br><span class="muted">{}</span>'.format(html.escape(scene["summary"]))

        rows.append('<div class="row">{}</div>'.format(line))

    notes = [html.escape(result["notes"])] if result["notes"] else []
    notes += ['<span class="warning">⚠ {}</span>'.format(html.escape(check)) for check in result["checks"]]

    if notes:
        rows.append('<div class="rule"></div>')
        rows.append('<div class="row">{}</div>'.format("<br>".join(notes)))

    return '<body id="author-tools-structure-card"><style>{}</style>{}</body>'.format(
        CARD_CSS, "".join(rows)
    )


def paragraph_at(view, analysis, point):
    """
    The number of the analysed paragraph at `point`, or None.
    """
    for number in range(1, len(analysis["paragraphs"]) + 1):
        region = tracked_region(view, number)

        if region is not None and region.begin() <= point <= region.end():
            return number

    return None


def scene_of(analysis, number):
    for index, scene in enumerate(analysis["result"]["scenes"]):
        if scene["start"] <= number <= scene["end"]:
            return index

    return None


def paragraph_roles(scene, number):
    """
    The commandments paragraph `number` plays in `scene`: pivots first,
    then the spans it is part of.
    """
    roles = [
        name for name in POINT_KEYS if scene[name]["paragraph"] == number
    ]
    roles += [
        name for name in SPAN_KEYS
        if scene[name]["start"] is not None
        and scene[name]["start"] <= number <= scene[name]["end"]
    ]

    return roles


def build_hover_html(view, analysis, number):
    """
    The small card for hovering over the gutter: what this paragraph does
    in its scene and why, then the scene in one line.
    """
    index = scene_of(analysis, number)
    scene = analysis["result"]["scenes"][index]
    states = paragraph_states(view, analysis)
    rows = []

    for name in paragraph_roles(scene, number):
        rows.append(entry_html(name, scene[name]))

    if not rows:
        rows.append('<div class="row muted">No commandment in this paragraph</div>')

    if states[number - 1][1]:
        rows.append('<div class="row muted">Edited since the analysis</div>')

    line = '<a href="scene:{}">Scene {}</a>'.format(index, index + 1)
    shift = value_shift(scene)

    if shift:
        line += " · " + html.escape(shift)

    if scene["warnings"]:
        line += ' <span class="warning">⚠ {}</span>'.format(len(scene["warnings"]))

    if scene["summary"]:
        line += '<br><span class="muted">{}</span>'.format(html.escape(scene["summary"]))

    rows.append('<div class="rule"></div>')
    rows.append('<div class="row">{}</div>'.format(line))

    return '<body id="author-tools-structure-card"><style>{}</style>{}</body>'.format(
        CARD_CSS, "".join(rows)
    )


def show_hover_card(view, point):
    analysis = view.settings().get(STRUCTURE_SETTING)

    if not analysis:
        return

    number = paragraph_at(view, analysis, point)

    if number is None or scene_of(analysis, number) is None:
        return

    # Anchored at the left edge of the screen row, like Jev's card.
    _, row_y = view.text_to_layout(point)
    row_start = view.layout_to_text((0.0, row_y))

    view.show_popup(
        build_hover_html(view, analysis, number),
        flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
        location=row_start,
        max_width=480,
        on_navigate=lambda href: on_card_link(view, href)
    )


def show_card(view, point, index=None):
    """
    The card of scene `index`, or else of the scene at `point`, or else the
    overview.
    """
    analysis = view.settings().get(STRUCTURE_SETTING)

    if not analysis:
        sublime.status_message(
            "Author Tools: no scene structure yet (Analyse Scene Structure)"
        )
        return

    scenes = analysis["result"]["scenes"]

    if index is None:
        states = paragraph_states(view, analysis)

        for candidate, scene in enumerate(scenes):
            region = span_region(states, scene["start"], scene["end"])

            if region is not None and region.begin() <= point <= region.end():
                index = candidate
                break

    if index is not None and 0 <= index < len(scenes):
        content = build_card_html(view, analysis, index)
    else:
        content = build_overview_html(analysis)

    view.show_popup(
        content,
        location=point,
        max_width=560,
        max_height=600,
        on_navigate=lambda href: on_card_link(view, href)
    )


def show_scene_card(view, index, number=None):
    """
    The card of scene `index`, beside paragraph `number` (its first
    paragraph by default).
    """
    analysis = view.settings().get(STRUCTURE_SETTING)

    if not analysis or not 0 <= index < len(analysis["result"]["scenes"]):
        return

    region = tracked_region(view, number or analysis["result"]["scenes"][index]["start"])

    if region is not None:
        show_card(view, region.begin(), index)


def on_label_clicked(view, href):
    kind, _, value = href.partition(":")

    if kind == "scene" and view.is_valid():
        index, _, number = value.partition(":")
        show_scene_card(view, int(index), int(number) if number else None)


def on_card_link(view, href):
    kind, _, value = href.partition(":")

    if kind == "goto":
        region = tracked_region(view, int(value))

        if region is not None:
            view.hide_popup()
            view.sel().clear()
            view.sel().add(sublime.Region(region.begin()))
            view.show_at_center(region.begin())

    elif kind == "scene":
        show_scene_card(view, int(value))


# ------------------------------------------------------------------------------
# SAVED STRUCTURE
# ------------------------------------------------------------------------------
#
# Beside the manuscript, like the paragraph scores, unless "save_scores"
# says otherwise:
#
#     chapter-12.md.structure.author-tools.json
#
# It holds the checked analysis and a hash of each numbered paragraph, so
# reopening the file finds the paragraphs again, even after edits.
# ------------------------------------------------------------------------------

def sidecar_path(file_name):
    location = sidecar_location()

    if location == "beside_manuscript":
        return file_name + SIDECAR_SUFFIX

    if location == "sublime_cache":
        digest = hashlib.sha1(
            os.path.abspath(file_name).encode("utf-8")
        ).hexdigest()

        return os.path.join(
            sublime.cache_path(), PACKAGE_NAME, "structure", digest + ".json"
        )

    return None


def save_analysis(view, analysis):
    file_name = view.file_name()
    path = sidecar_path(file_name) if file_name else None

    if path is None:
        return

    data = {
        "about": (
            "Scene structure saved by Author Tools for {}. Safe to delete: "
            "analyse the chapter again to get it back."
        ).format(os.path.basename(file_name)),
        "version": SIDECAR_VERSION
    }
    data.update(analysis)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # Written beside the real file and then swapped in, so a crash
        # halfway never leaves a broken sidecar.
        temporary = path + ".tmp"

        with open(temporary, "w", encoding="utf-8") as sidecar:
            json.dump(data, sidecar, ensure_ascii=False, indent=1)

        os.replace(temporary, path)

    except OSError as exc:
        print("[Author Tools] could not save the structure to {}: {}".format(path, exc))


def load_analysis(file_name):
    path = sidecar_path(file_name)

    if not path or not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as sidecar:
            data = json.load(sidecar)

        if data.get("version") != SIDECAR_VERSION:
            return None

        return {
            "model": data.get("model", ""),
            "analysed_at": data.get("analysed_at", ""),
            "paragraphs": data["paragraphs"],
            "result": data["result"]
        }

    except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
        print("[Author Tools] could not read {}: {}".format(path, exc))
        return None


def delete_saved_analysis(view):
    file_name = view.file_name()
    path = sidecar_path(file_name) if file_name else None

    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            print("[Author Tools] could not delete {}: {}".format(path, exc))


def restore_structure(view):
    """
    The first time a view is seen: draw what it already tracks (after a
    plugin reload), or find a saved analysis's paragraphs in the text.
    """
    RESTORED.add(view.id())

    settings = view.settings()
    analysis = settings.get(STRUCTURE_SETTING)

    if analysis and is_tracking(view, analysis):
        redraw_structure(view)
        return

    # View settings outlive a restart (in the session), regions don't: the
    # sidecar is the better copy, when there is one.
    if view.file_name():
        analysis = load_analysis(view.file_name()) or analysis

    if not analysis:
        return

    regions = match_paragraphs(
        [stored["hash"] for stored in analysis["paragraphs"]],
        structure_paragraphs(view)
    )

    if not any(regions):
        return

    settings.set(STRUCTURE_SETTING, analysis)
    track_paragraphs(view, regions)
    redraw_structure(view)


# ------------------------------------------------------------------------------
# ANALYSING
# ------------------------------------------------------------------------------

def start_analysis(view):
    settings = get_settings()
    selections = [region for region in view.sel() if not region.empty()]
    paragraphs = structure_paragraphs(view, selections)

    if len(paragraphs) < 2:
        sublime.status_message("Author Tools: not enough paragraphs to analyse")
        return

    texts = [text for _, text in paragraphs]
    characters = sum(len(text) for text in texts)
    limit = int(settings.get("structure_max_characters", 80000))

    if characters > limit:
        sublime.status_message(
            "Author Tools: too long to analyse ({:,} characters, the limit is "
            "{:,}: structure_max_characters). Select one chapter.".format(
                characters, limit
            )
        )
        return

    config = read_request_config()
    view_id = view.id()
    change_count = view.change_count()
    BUSY.add(view_id)

    view.set_status(STATUS_KEY, "Structure: analysing {} paragraphs…".format(len(texts)))

    def worker():
        try:
            result = check_result(parse_reply(fetch_structure(texts, config)), texts)
            error = None
        except Exception as exc:
            result = None
            error = str(exc)

        sublime.set_timeout(lambda: finish(result, error), 0)

    def finish(result, error):
        BUSY.discard(view_id)

        if not view.is_valid():
            return

        view.erase_status(STATUS_KEY)

        if error:
            sublime.error_message(
                "Author Tools couldn't analyse the scene structure.\n\n{}\n\n"
                "\"Author Tools: Show Last Raw Response\" shows what came "
                "back.".format(error)
            )
            return

        if not result["scenes"]:
            sublime.error_message(
                "Author Tools: the model found no usable scenes.\n\n{}".format(
                    "\n".join(result["checks"])
                )
            )
            return

        regions = [region for region, _ in paragraphs]

        # Edited while waiting: find the paragraphs again by their text.
        if view.change_count() != change_count:
            regions = match_paragraphs(
                [text_hash(text) for text in texts], structure_paragraphs(view)
            )

        analysis = {
            "model": config["model"],
            "analysed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "paragraphs": [
                {"hash": text_hash(text), "preview": preview(text)} for text in texts
            ],
            "result": result
        }

        clear_structure(view)
        view.settings().set(STRUCTURE_SETTING, analysis)
        track_paragraphs(view, regions)
        save_analysis(view, analysis)

        # Show it: the settings watchers redraw Jev's marks and this.
        settings = get_settings()

        if settings.get("colour_marks_by") != STRUCTURE_MODE or not settings.get("show_gutter_marks", True):
            settings.set("colour_marks_by", STRUCTURE_MODE)
            settings.set("show_gutter_marks", True)
            sublime.save_settings(SETTINGS_FILE)

        redraw_structure(view)

        warnings = sum(len(scene["warnings"]) for scene in result["scenes"])
        warnings += len(result["checks"])
        scenes = len(result["scenes"])

        sublime.status_message("Author Tools: {} scene{}{}".format(
            scenes, "" if scenes == 1 else "s",
            " ({} correction{} or warning{}; see the cards)".format(
                warnings, "" if warnings == 1 else "s", "" if warnings == 1 else "s"
            ) if warnings else ""
        ))

    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------------------
# EVENTS
# ------------------------------------------------------------------------------

SETTINGS_WATCH_KEY = "author_tools_structure"

# view id -> change count of the last scheduled redraw.
PENDING_REDRAWS = {}


def plugin_loaded():
    get_settings().add_on_change(SETTINGS_WATCH_KEY, redraw_all_structures)

    window = sublime.active_window()
    view = window.active_view() if window else None

    if view and view_is_applicable(view):
        restore_structure(view)


def plugin_unloaded():
    get_settings().clear_on_change(SETTINGS_WATCH_KEY)


class AuthorToolsStructureEventListener(sublime_plugin.EventListener):

    def on_activated_async(self, view):
        if view.id() not in RESTORED and view_is_applicable(view):
            restore_structure(view)

    def on_load_async(self, view):
        self.on_activated_async(view)

    def on_modified_async(self, view):
        """
        Redraw shortly after typing stops, so edited paragraphs and the
        "edited" label catch up. No request is ever sent from here.
        """
        if not view.settings().get(STRUCTURE_SETTING):
            return

        change_count = view.change_count()
        PENDING_REDRAWS[view.id()] = change_count

        def redraw():
            if PENDING_REDRAWS.get(view.id()) == change_count:
                PENDING_REDRAWS.pop(view.id(), None)
                redraw_structure(view)

        sublime.set_timeout(redraw, REDRAW_DELAY_MS)

    def on_hover(self, view, point, hover_zone):
        """
        In the "Scene structure" colour mode, hovering over the gutter shows
        what the paragraph there does in its scene (Jev's card otherwise).
        """
        if hover_zone == sublime.HOVER_GUTTER and structure_shown():
            show_hover_card(view, point)

    def on_close(self, view):
        RESTORED.discard(view.id())
        PENDING_REDRAWS.pop(view.id(), None)


# ------------------------------------------------------------------------------
# COMMANDS (see Default.sublime-commands)
# ------------------------------------------------------------------------------

class AuthorToolsAnalyseStructureCommand(sublime_plugin.TextCommand):
    """
    Author Tools: Analyse Scene Structure

    The whole file, or the paragraphs touched by the selection (for a file
    with several chapters). Replaces the previous analysis.
    """

    def run(self, edit):
        view = self.view

        if not view_is_applicable(view):
            sublime.status_message("Author Tools only analyses Markdown files")
            return

        if view.id() in BUSY:
            sublime.status_message("Author Tools: the structure is still being analysed")
            return

        start_analysis(view)


class AuthorToolsShowStructureCommand(sublime_plugin.TextCommand):
    """
    Author Tools: Show Scene Structure

    The card of the scene at the caret, or an overview of all scenes.
    """

    def run(self, edit):
        view = self.view

        if len(view.sel()) == 0:
            return

        show_card(view, view.sel()[0].b)


class AuthorToolsClearStructureCommand(sublime_plugin.TextCommand):
    """
    Author Tools: Clear Scene Structure

    Forgets this manuscript's analysis, and deletes its saved file.
    """

    def is_enabled(self):
        return bool(self.view.settings().get(STRUCTURE_SETTING))

    def run(self, edit):
        view = self.view
        clear_structure(view)
        delete_saved_analysis(view)
        sublime.status_message("Author Tools: scene structure cleared")
