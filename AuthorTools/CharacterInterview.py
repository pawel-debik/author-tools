import sublime
import sublime_plugin

import html
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from .ProseAnalysis import get_api_key


# ------------------------------------------------------------------------------
# CHARACTER INTERVIEWS
# ------------------------------------------------------------------------------
#
# Select a passage, run "Author Tools: Ask Character…", pick a character and a
# question, and the character answers in their own voice:
#
#     Author Tools: Ask Character…         a new question about the selection
#     Author Tools: Ask Follow-up…         carry on the last conversation
#
# The story folder (the "story_folder" setting) holds a "characters" folder
# with one Markdown file per character (an optional header with name and
# aliases, then free prose), and optionally a story_so_far.md that tells
# every character where the story stands. Without story_folder, the example
# folder in this package is used; without either, the commands are hidden.
#
# Portraits sit beside the character file, with the same name:
#
#     Mara Voss.md
#     Mara Voss.png            the default portrait
#     Mara Voss.happy.png      one per mood, any word you like
#     Mara Voss.angry.jpg
#
# When there are mood portraits, the character is asked to end each answer
# with one of those moods, and the popup shows the matching portrait (or the
# default one when the mood is missing or has no portrait).
#
# This is separate from the paragraph analysis: it uses OpenRouter's normal
# chat endpoint and a chat model, and shares only the API key.
# ------------------------------------------------------------------------------

SETTINGS_FILE = "AuthorTools.sublime-settings"

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

MARKDOWN_SYNTAX = "Packages/Markdown/Markdown.sublime-syntax"

# Marks the interview tab, so the paragraph analysis leaves it alone.
INTERVIEW_SETTING = "author_tools_interview"

# Shown in the status bar of the view the question came from.
STATUS_KEY = "zx_author_tools_character"

# Used when "story_folder" isn't set. Delete it to hide the feature.
EXAMPLE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example")

CHARACTERS_FOLDER = "characters"
STORY_SO_FAR_FILE = "story_so_far.md"

CHARACTER_EXTENSIONS = (".md", ".markdown", ".txt")

# What minihtml can show.
PORTRAIT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif")

# The line the character ends an answer with, when there are mood portraits.
MOOD_RE = re.compile(r"^[ \t]*\[mood:[ \t]*([^\]\n]*)\][ \t]*$\n?", re.I | re.M)

MOOD_INSTRUCTIONS = """\
# Your mood

End every answer with one more line that says how you feel while giving it, \
as exactly one of these words: {moods}. Write it like this, on its own line:

[mood: {example}]"""

OWN_QUESTION = "Type your own question…"

DEFAULT_QUESTIONS = [
    "Would you really do this?",
    "How does this make you feel?",
    "What are you not saying here?",
    "What would you do instead?"
]

# {name} is replaced with the character's name.
DEFAULT_INSTRUCTIONS = """\
You are {name}. Stay in character completely.

The author of the story you live in will show you a passage from it, a \
moment from your own life, and ask you about it. Answer as {name}: in the \
first person, in your own voice and manner of speaking, with your own \
opinions, moods and blind spots.

Be honest. If the passage has you do or say something you never would, say \
so, and say what you would do instead. If it touches something you would \
rather not talk about, you may dodge the question the way you would in \
person, but let it show.

You only know what you would know at this point in the story. Never talk \
about writing, prose, chapters, readers or characters, and never say you are \
an AI. Just talk: no stage directions, no *actions* between asterisks, no \
headings or lists. Keep it to one or two short paragraphs unless you are \
asked for more."""

FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


# ------------------------------------------------------------------------------
# CONVERSATIONS
# ------------------------------------------------------------------------------
#
# INTERVIEWS[window.id()] = {
#     "character": {"name": ..., ...},
#     "messages": [system, user, assistant, user, ...],
#     "busy": False,
# }
#
# Only the latest conversation per window is kept. The system message holds
# the character file and story_so_far as they were when the conversation
# started, so follow-ups don't read the files again.
# ------------------------------------------------------------------------------

INTERVIEWS = {}


def get_settings():
    return sublime.load_settings(SETTINGS_FILE)


# ------------------------------------------------------------------------------
# FINDING THE FILES
# ------------------------------------------------------------------------------

def configured_story_folder():
    """
    The "story_folder" setting, expanded, or "" when it isn't set.
    """
    folder = get_settings().get("story_folder", "")

    if not isinstance(folder, str) or not folder.strip():
        return ""

    return os.path.expandvars(os.path.expanduser(folder.strip()))


def story_folder():
    """
    The folder with the characters and story_so_far.md: "story_folder" from
    the settings, or else the example folder in this package. None when
    neither is there, which hides the feature.

    A story_folder that is set but missing is still returned, so the
    commands can say what's wrong rather than silently disappear.
    """
    folder = configured_story_folder()

    if folder:
        return folder

    if os.path.isdir(EXAMPLE_FOLDER):
        return EXAMPLE_FOLDER

    return None


def feature_available():
    return story_folder() is not None


def read_text(path):
    with open(path, encoding="utf-8-sig") as file:
        return file.read()


def read_character(path):
    """
    ---
    name: Mara Voss
    aliases: [Mara, the Captain]
    ---
    Free prose...

    The header is optional; without it the file name is the name.
    """
    text = read_text(path)
    name = os.path.splitext(os.path.basename(path))[0]
    aliases = []

    match = FRONT_MATTER_RE.match(text)

    if match:
        text = text[match.end():]

        for line in match.group(1).splitlines():
            key, colon, value = line.partition(":")

            if not colon:
                continue

            key = key.strip().lower()
            value = value.strip()

            if key == "name" and value:
                name = value.strip("\"'")
            elif key == "aliases":
                aliases = [
                    alias.strip().strip("\"'")
                    for alias in value.strip("[]").split(",")
                    if alias.strip()
                ]

    default_portrait, portraits = find_portraits(path)

    return {
        "name": name,
        "aliases": aliases,
        "profile": text.strip(),
        "portrait": default_portrait,
        "portraits": portraits
    }


def find_portraits(path):
    """
    The default portrait (or None) and {mood: path} for the images beside
    the character file that share its name: "Mara Voss.png" and
    "Mara Voss.happy.png" for "Mara Voss.md".
    """
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    default_portrait = None
    portraits = {}

    for entry in sorted(os.listdir(folder), key=str.lower):
        base, extension = os.path.splitext(entry)

        if extension.lower() not in PORTRAIT_EXTENSIONS:
            continue

        base = base.lower()
        image = os.path.join(folder, entry)

        if base == stem:
            default_portrait = default_portrait or image
        elif base.startswith(stem + "."):
            mood = base[len(stem) + 1:].strip()

            if mood:
                portraits.setdefault(mood, image)

    return default_portrait, portraits


def load_characters(folder):
    characters = []

    for entry in sorted(os.listdir(folder), key=str.lower):
        path = os.path.join(folder, entry)

        if (
            entry.startswith(".")
            or not os.path.isfile(path)
            or os.path.splitext(entry)[1].lower() not in CHARACTER_EXTENSIONS
        ):
            continue

        try:
            characters.append(read_character(path))
        except (OSError, UnicodeDecodeError) as exc:
            print("Author Tools: could not read {}: {}".format(path, exc))

    return characters


def is_mentioned(character, text):
    """
    Whether the full name, the first name or an alias appears in the text.
    """
    names = [character["name"]] + character["aliases"]
    names.append(character["name"].split()[0] if character["name"].split() else "")

    for name in names:
        if name and re.search(r"\b" + re.escape(name) + r"\b", text):
            return True

    return False


# ------------------------------------------------------------------------------
# THE REQUEST
# ------------------------------------------------------------------------------

def build_system_prompt(character, story_so_far):
    instructions = get_settings().get("character_instructions") or DEFAULT_INSTRUCTIONS
    parts = [
        instructions.replace("{name}", character["name"]),
        "# Who you are\n\n" + character["profile"]
    ]

    if story_so_far:
        parts.append("# Your story so far\n\n" + story_so_far)

    moods = sorted(character["portraits"])

    if moods:
        parts.append(MOOD_INSTRUCTIONS.format(moods=", ".join(moods), example=moods[0]))

    return "\n\n".join(parts)


def split_mood(reply):
    """
    The answer without its [mood: ...] line, and the mood (lowercase, or ""
    when there is none). The last mood line wins.
    """
    moods = MOOD_RE.findall(reply)
    text = MOOD_RE.sub("", reply).strip()

    return text, (moods[-1].strip().lower() if moods else "")


def portrait_for(character, mood):
    """
    The portrait for the mood, else the default portrait, else None.
    """
    return character["portraits"].get(mood) or character["portrait"]


def build_first_question(passage, question):
    return 'Here is a passage from your story:\n\n"""\n{}\n"""\n\n{}'.format(
        passage, question
    )


def read_request_config():
    settings = get_settings()

    return {
        "api_key": get_api_key(),
        "endpoint": settings.get("character_endpoint", DEFAULT_ENDPOINT),
        "model": settings.get("character_model", DEFAULT_MODEL),
        "temperature": settings.get("character_temperature", 0.8),
        "timeout_seconds": float(settings.get("character_timeout_seconds", 90))
    }


def fetch_reply(messages, config):
    if not config["api_key"]:
        raise RuntimeError(
            "No OpenRouter API key configured. Set api_key in your User "
            "AuthorTools.sublime-settings."
        )

    payload = {"model": config["model"], "messages": messages}

    if config["temperature"] is not None:
        payload["temperature"] = config["temperature"]

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

        raise RuntimeError(
            "OpenRouter HTTP {}: {}".format(exc.code, error_body[:1000])
        )

    except urllib.error.URLError as exc:
        raise RuntimeError("Could not reach OpenRouter: {}".format(exc))

    except OSError as exc:
        # Timeouts while reading the body.
        raise RuntimeError("OpenRouter didn't answer: {}".format(exc))

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
        raise RuntimeError("The character gave an empty answer.")

    return reply.strip()


def send(window, view, question_text, passage=None):
    """
    Send the conversation (whose last message is the new question) and show
    the answer. `passage` is set for the first question only.
    """
    interview = INTERVIEWS[window.id()]
    interview["busy"] = True
    name = interview["character"]["name"]
    messages = list(interview["messages"])
    config = read_request_config()
    point = view.sel()[-1].end() if len(view.sel()) else 0

    view.set_status(STATUS_KEY, "{} is thinking…".format(name))

    def worker():
        try:
            reply = fetch_reply(messages, config)
            error = None
        except Exception as exc:
            reply = None
            error = str(exc)

        sublime.set_timeout(lambda: finish(reply, error), 0)

    def finish(reply, error):
        view.erase_status(STATUS_KEY)
        interview["busy"] = False

        if error:
            # Take the unanswered question back out, so it can be asked again.
            # Without an answer to the first question there's nothing to
            # follow up on.
            if passage is not None:
                INTERVIEWS.pop(window.id(), None)
            else:
                interview["messages"].pop()
            sublime.error_message("Author Tools: {} couldn't answer.\n\n{}".format(
                name, error
            ))
            return

        # The history keeps the mood line, so the character keeps adding it.
        interview["messages"].append({"role": "assistant", "content": reply})
        text, mood = split_mood(reply)
        portrait = portrait_for(interview["character"], mood)
        show_reply(window, view, point, name, question_text, text, passage, portrait)

    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------------------
# SHOWING THE ANSWER
# ------------------------------------------------------------------------------

def display_modes():
    modes = get_settings().get("character_reply_display", ["popup", "tab"])

    if isinstance(modes, str):
        modes = [modes]

    return modes


def show_reply(window, view, point, name, question, reply, passage, portrait):
    modes = display_modes()

    if "tab" in modes:
        append_to_interview(window, view, name, question, reply, passage)

    if "popup" in modes and view.is_valid():
        show_popup(view, point, name, question, reply, portrait)


def portrait_html(portrait):
    if not portrait or not os.path.isfile(portrait):
        return ""

    size = int(get_settings().get("character_portrait_size", 96))
    # file:///C:/... on Windows, file:///Users/... elsewhere.
    url = "file:///" + urllib.parse.quote(portrait.replace("\\", "/").lstrip("/"), safe="/:")

    return '<div class="portrait"><img src="{}" width="{}" height="{}"></div>'.format(
        html.escape(url, quote=True), size, size
    )


def show_popup(view, point, name, question, reply, portrait):
    paragraphs = "".join(
        "<p>{}</p>".format(html.escape(paragraph.strip()).replace("\n", "<br>"))
        for paragraph in re.split(r"\n\s*\n", reply)
        if paragraph.strip()
    )

    content = """
<body id="author-tools-character">
<style>
    body {{ margin: 0; padding: 0.6rem 0.8rem; }}
    .question {{ color: color(var(--foreground) alpha(0.6)); font-style: italic; }}
    .name {{ font-weight: bold; margin-top: 0.5rem; }}
    p {{ margin: 0.4rem 0 0 0; }}
    .portrait {{ margin-top: 0.5rem; }}
</style>
<div class="question">{question}</div>
{portrait}
<div class="name">{name}</div>
{paragraphs}
</body>
""".format(
        question=html.escape(question),
        portrait=portrait_html(portrait),
        name=html.escape(name),
        paragraphs=paragraphs
    )

    # Stays open until Escape or the caret moves, so it can be read at leisure.
    view.show_popup(content, location=point, max_width=640, max_height=480)


def find_interview_view(window):
    for view in window.views():
        if view.settings().get(INTERVIEW_SETTING):
            return view

    return None


def create_interview_view(window, source_view):
    # ADD_TO_SELECTION opens the tab beside the manuscript.
    view = window.new_file(flags=sublime.ADD_TO_SELECTION)
    view.settings().set(INTERVIEW_SETTING, True)
    view.set_name("Interview")
    view.set_scratch(True)
    view.assign_syntax(MARKDOWN_SYNTAX)
    view.settings().set("word_wrap", True)

    if source_view.is_valid() and source_view != view:
        window.focus_view(source_view)

    return view


def quote(text, limit=600):
    text = text.strip()

    if len(text) > limit:
        text = text[:limit].rstrip() + " …"

    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines())


def append_to_interview(window, source_view, name, question, reply, passage):
    view = find_interview_view(window) or create_interview_view(window, source_view)

    parts = []

    if passage is not None:
        if view.size():
            parts.append("\n---\n\n")

        parts.append("# {}\n\n{}\n\n".format(name, quote(passage)))

    parts.append("**You:** {}\n\n**{}:** {}\n\n".format(question, name, reply))

    view.run_command("append", {
        "characters": "".join(parts),
        "force": True,
        "scroll_to_end": True
    })


# ------------------------------------------------------------------------------
# COMMANDS
# ------------------------------------------------------------------------------

def selected_text(view):
    return "\n\n".join(
        view.substr(region) for region in view.sel() if not region.empty()
    ).strip()


def is_busy(window):
    interview = INTERVIEWS.get(window.id())

    if interview and interview["busy"]:
        sublime.status_message("Author Tools: still waiting for {}".format(
            interview["character"]["name"]
        ))
        return True

    return False


class AuthorToolsAskCharacterCommand(sublime_plugin.TextCommand):
    """
    Pick a character, then a question about the selected text.
    """

    def is_visible(self):
        return feature_available()

    def is_enabled(self):
        return feature_available()

    def run(self, edit):
        view = self.view
        window = view.window()

        if window is None or is_busy(window):
            return

        # In the interview tab there is nothing to select; carry on instead.
        if view.settings().get(INTERVIEW_SETTING):
            window.run_command("author_tools_character_follow_up")
            return

        settings = get_settings()
        passage = selected_text(view)

        if not passage:
            sublime.status_message("Author Tools: select the passage to ask about first")
            return

        max_characters = int(settings.get("character_max_characters", 20000))

        if len(passage) > max_characters:
            sublime.status_message(
                "Author Tools: selection too long to ask about "
                "(over {} characters)".format(max_characters)
            )
            return

        root = story_folder()

        if root is None:
            return

        if not os.path.isdir(root):
            sublime.error_message(
                "Author Tools: the story folder in your settings doesn't "
                "exist:\n\n{}\n\nCheck story_folder in \"Preferences: Author "
                "Tools Settings\".".format(root)
            )
            return

        folder = os.path.join(root, CHARACTERS_FOLDER)

        if not os.path.isdir(folder):
            sublime.error_message(
                "Author Tools: no \"{}\" folder in your story folder:\n\n{}"
                "\n\nCreate it, with one Markdown file per character.".format(
                    CHARACTERS_FOLDER, root
                )
            )
            return

        characters = load_characters(folder)

        if not characters:
            sublime.error_message(
                "Author Tools: no character files in\n{}".format(folder)
            )
            return

        # Characters named in the passage first; sorting keeps the rest in order.
        mentioned = [is_mentioned(character, passage) for character in characters]
        order = sorted(range(len(characters)), key=lambda i: not mentioned[i])
        characters = [characters[i] for i in order]

        items = [
            sublime.QuickPanelItem(
                characters[i]["name"],
                annotation="in selection" if mentioned[order[i]] else ""
            )
            for i in range(len(characters))
        ]

        def on_character(index):
            if index >= 0:
                self.choose_question(characters[index], passage)

        window.show_quick_panel(items, on_character, placeholder="Ask which character?")

    def choose_question(self, character, passage):
        window = self.view.window()
        questions = get_settings().get("character_questions", DEFAULT_QUESTIONS)
        items = list(questions) + [OWN_QUESTION]

        def on_question(index):
            if index < 0:
                return

            if index < len(questions):
                self.ask(character, passage, questions[index])
                return

            window.show_input_panel(
                "Ask {}:".format(character["name"]),
                "",
                lambda text: text.strip() and self.ask(character, passage, text.strip()),
                None,
                None
            )

        # Shown after the first panel has closed.
        sublime.set_timeout(
            lambda: window.show_quick_panel(
                items,
                on_question,
                placeholder="Ask {}…".format(character["name"])
            ),
            0
        )

    def ask(self, character, passage, question):
        view = self.view
        window = view.window()

        if window is None or is_busy(window):
            return

        story_so_far = ""
        story_path = os.path.join(story_folder() or "", STORY_SO_FAR_FILE)

        if os.path.isfile(story_path):
            try:
                story_so_far = read_text(story_path).strip()
            except (OSError, UnicodeDecodeError) as exc:
                print("Author Tools: could not read {}: {}".format(story_path, exc))

        INTERVIEWS[window.id()] = {
            "character": character,
            "messages": [
                {"role": "system", "content": build_system_prompt(character, story_so_far)},
                {"role": "user", "content": build_first_question(passage, question)}
            ],
            "busy": False
        }

        send(window, view, question, passage)


class AuthorToolsCharacterFollowUpCommand(sublime_plugin.WindowCommand):
    """
    Ask the last character in this window another question, with the whole
    conversation so far.
    """

    def is_visible(self):
        return feature_available()

    def is_enabled(self):
        return feature_available()

    def run(self):
        window = self.window
        interview = INTERVIEWS.get(window.id())

        if not interview:
            sublime.status_message(
                "Author Tools: ask a character about a passage first"
            )
            return

        if is_busy(window):
            return

        name = interview["character"]["name"]

        def on_done(text):
            text = text.strip()
            view = window.active_view()

            if not text or view is None or is_busy(window):
                return

            interview["messages"].append({"role": "user", "content": text})
            send(window, view, text)

        window.show_input_panel("Ask {} (follow-up):".format(name), "", on_done, None, None)
