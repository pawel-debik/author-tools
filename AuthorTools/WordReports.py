import sublime
import sublime_plugin

import bisect
import html
import re


# ------------------------------------------------------------------------------
# WORD REPORTS
# ------------------------------------------------------------------------------
#
# Word reports for the whole document, counted locally (no Jev, no network):
#
#     Author Tools: Adverbs Report
#     Author Tools: Dialogue Verbs Report
#
# Each opens a list of the words found, most used first. Choosing a word
# lists its occurrences; moving through that list scrolls to each one.
# Escape goes back a step, and from the word list returns to where you were.
# ------------------------------------------------------------------------------

SETTINGS_FILE = "AuthorTools.sublime-settings"

ADVERBS = """
absently abruptly absolutely accidentally actually admiringly affectionately
aggressively agonizingly airily almost angrily annoyingly anxiously
apologetically apparently appreciatively approvingly arrogantly ashamedly
awkwardly badly barely bashfully basically begrudgingly belatedly bitterly
blandly blankly blindly blissfully bluntly boldly boyishly breathlessly
briefly brightly briskly broadly brusquely busily calmly carefully carelessly
casually cautiously certainly cheerfully clearly clumsily coldly
comfortably completely confidently constantly contemptuously contentedly
coolly correctly courageously covertly coyly crazily crossly cruelly
cryptically curiously curtly darkly dearly deceptively decisively deeply
defensively defiantly deftly deliberately delicately delightfully
desperately determinedly devotedly dimly directly disapprovingly
dismissively distantly dizzily doubtfully dramatically dreamily drily
dryly dully eagerly earnestly easily effortlessly elegantly emphatically
encouragingly energetically enormously enthusiastically entirely
especially evenly eventually evidently exactly excitedly exclusively
expectantly extremely faintly fairly faithfully fervently fiercely finally
firmly fleetingly fondly foolishly forcefully formally frankly frantically
freely frenziedly frequently fretfully fully furiously furtively gently
genuinely giddily gingerly gladly gleefully gloomily gracefully graciously
gradually gratefully greatly greedily grimly grudgingly gruffly guiltily
happily harshly hastily heartily heavily helpfully helplessly hesitantly
highly hoarsely honestly hopefully hopelessly hotly hungrily hurriedly
icily idly immediately impatiently incredibly indignantly inevitably
innocently inquisitively insistently instantly intensely intently
irritably jealously jokingly jovially joyfully joyously keenly kindly
knowingly lazily lightly limply literally loftily longingly loosely
loudly lovingly madly meaningfully meekly merely merrily mildly mindlessly
miserably mockingly mostly mysteriously naturally nearly neatly nervously
nicely noisily normally obediently obviously occasionally oddly offhandedly
openly optimistically painfully passionately patiently peacefully
perfectly perhaps persistently playfully pleasantly pointedly politely
positively possibly powerfully precisely presumably probably promptly
properly proudly purely quickly quietly rapidly rarely readily really
reassuringly recklessly regretfully reluctantly remorsefully repeatedly
reproachfully resentfully resignedly respectfully restlessly roughly
rudely ruefully sadly sarcastically savagely scornfully seductively
seemingly selfishly seriously severely sharply sheepishly shrewdly shrilly
shyly silently simply sincerely slightly slowly slyly smoothly smugly
snidely softly solemnly somehow soothingly speedily stealthily steadily
sternly stiffly stubbornly stupidly suddenly sullenly supposedly surely
surprisingly suspiciously sweetly swiftly sympathetically tearfully
tenderly tensely terribly thankfully thoroughly thoughtfully tightly
timidly tiredly totally tragically tremendously triumphantly truly
uncertainly uncomfortably understandingly uneasily unexpectedly
unfortunately unhappily unwillingly urgently utterly vaguely valiantly
vastly vehemently vigorously violently virtually wanly warily warmly
wearily weakly wildly willingly wisely wistfully wryly yearningly
zealously
very quite rather somewhat
""".split()

# Past and present forms, since manuscripts use either tense.
DIALOGUE_VERBS = """
said says asked asks replied replies answered answers told tells
shouted shouts yelled yells screamed screams shrieked shrieks
whispered whispers murmured murmurs mumbled mumbles muttered mutters
hissed hisses growled growls snapped snaps barked barks snarled snarls
exclaimed exclaims cried cries called calls bellowed bellows roared roars
sighed sighs laughed laughs chuckled chuckles giggled giggles snickered
snickers snorted snorts sneered sneers scoffed scoffs spat spits
demanded demands insisted insists begged begs pleaded pleads implored
implores protested protests retorted retorts countered counters
stammered stammers stuttered stutters gasped gasps breathed breathes
croaked croaks rasped rasps grunted grunts groaned groans moaned moans
whined whines wailed wails sobbed sobs whimpered whimpers blurted blurts
announced announces declared declares stated states added adds
continued continues explained explains admitted admits agreed agrees
argued argues warned warns suggested suggests offered offers promised
promises repeated repeats interrupted interrupts conceded concedes
teased teases joked jokes quipped quips purred purrs drawled drawls
thundered thunders squeaked squeaks squealed squeals chirped chirps
huffed huffs grumbled grumbles complained complains lied lies
noted notes observed observes remarked remarks mused muses wondered
wonders inquired inquires enquired enquires queried queries questioned
questions responded responds prompted prompts urged urges ordered orders
commanded commands ventured ventures confessed confesses cooed coos
soothed soothes crooned croons intoned intones chimed chimes
""".split()

# Quote marks that open or close dialogue. A closing ’ only counts when it
# isn't an apostrophe inside a word ("don’t").
QUOTE_RE = re.compile(r"[\"“”«»‘]|’(?![^\W\d_])")

WORD_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")

CONTEXT_CHARACTERS = 40


def get_settings():
    return sublime.load_settings(SETTINGS_FILE)


def word_list(base, extra_setting):
    settings = get_settings()

    words = set(word.lower() for word in base)
    words.update(word.lower() for word in settings.get(extra_setting, []))
    words.difference_update(
        word.lower() for word in settings.get("ignore_words", [])
    )

    return words


# Anything that ends a sentence (or a line) between a verb and a quote mark
# means the verb isn't tagging that dialogue.
SENTENCE_BREAK_RE = re.compile(r"[.!?;:\n]")


def tags_dialogue(text, start, end, quotes, distance):
    """
    Whether the word sits in the same sentence as a quote mark, within
    `distance` characters: ”, she said  or  he said, “  but not
    ” She left. He called the dog.  `quotes` is a sorted list of offsets.
    """
    index = bisect.bisect_left(quotes, start)

    if index > 0:
        between = text[quotes[index - 1] + 1:start]

        if len(between) <= distance and not SENTENCE_BREAK_RE.search(between):
            return True

    if index < len(quotes):
        between = text[end:quotes[index]]

        if len(between) <= distance and not SENTENCE_BREAK_RE.search(between):
            return True

    return False


def find_words(text, words, dialogue_only=False):
    """
    {word: [(start, end), ...]} for every occurrence of the listed words.
    """
    settings = get_settings()
    distance = int(settings.get("quote_distance", 60))

    quotes = (
        [match.start() for match in QUOTE_RE.finditer(text)]
        if dialogue_only else None
    )

    found = {}

    for match in WORD_RE.finditer(text):
        word = match.group(0).lower()

        if word not in words:
            continue

        if dialogue_only and not tags_dialogue(
            text, match.start(), match.end(), quotes, distance
        ):
            continue

        found.setdefault(word, []).append((match.start(), match.end()))

    return found


def context_snippet(text, start, end):
    """
    "…he said <b>quietly</b>, turning away…" on one line.
    """
    before_start = max(0, start - CONTEXT_CHARACTERS)
    after_end = min(len(text), end + CONTEXT_CHARACTERS)

    before = text[before_start:start]
    after = text[end:after_end]

    # Stay within the paragraph.
    before = before.split("\n\n")[-1]
    after = after.split("\n\n")[0]

    def one_line(part):
        return " ".join(part.split())

    return "{}{}<b>{}</b>{}{}".format(
        "…" if before_start > 0 else "",
        html.escape(one_line(before)) + (" " if before[-1:].isspace() else ""),
        html.escape(text[start:end]),
        (" " if after[:1].isspace() else "") + html.escape(one_line(after)),
        "…" if after_end < len(text) else ""
    )


class WordReport:
    """
    One run of a report: the word list, then an occurrence list per word.
    """

    def __init__(self, view, title, found):
        self.view = view
        self.window = view.window()
        self.title = title
        self.text = view.substr(sublime.Region(0, view.size()))

        self.words = sorted(found.items(), key=lambda item: (-len(item[1]), item[0]))

        # Where to return to when the report is closed with Escape.
        self.original_selection = list(view.sel())
        self.original_viewport = view.viewport_position()

    def show_words(self, selected_index=0):
        total = sum(len(regions) for _, regions in self.words)

        items = [
            sublime.QuickPanelItem(word, annotation="{}×".format(len(regions)))
            for word, regions in self.words
        ]

        self.window.show_quick_panel(
            items,
            self.on_word_chosen,
            selected_index=selected_index,
            placeholder="{}: {} in total, {} different words".format(
                self.title, total, len(self.words)
            )
        )

    def on_word_chosen(self, index):
        if index < 0:
            self.restore()
            return

        # A new quick panel can't open from inside another one's callback.
        sublime.set_timeout(lambda: self.show_occurrences(index), 0)

    def show_occurrences(self, word_index):
        word, regions = self.words[word_index]

        items = [
            sublime.QuickPanelItem(
                "Select all {}".format(len(regions)),
                details="Select every “{}” at once".format(html.escape(word))
            )
        ]

        for start, end in regions:
            row, _ = self.view.rowcol(start)
            items.append(sublime.QuickPanelItem(
                "Line {}".format(row + 1),
                details=context_snippet(self.text, start, end)
            ))

        def on_highlight(index):
            if index > 0:
                self.select([regions[index - 1]])

        def on_chosen(index):
            if index < 0:
                sublime.set_timeout(lambda: self.show_words(word_index), 0)
            elif index == 0:
                self.select(regions)
            else:
                self.select([regions[index - 1]])

        self.window.show_quick_panel(
            items,
            on_chosen,
            selected_index=1 if regions else 0,
            on_highlight=on_highlight,
            placeholder="{} ×{}: move through the list to see each one".format(
                word, len(regions)
            )
        )

    def select(self, regions):
        selection = self.view.sel()
        selection.clear()

        for start, end in regions:
            selection.add(sublime.Region(start, end))

        self.view.show_at_center(sublime.Region(regions[0][0], regions[0][1]))

    def restore(self):
        selection = self.view.sel()
        selection.clear()
        selection.add_all(self.original_selection)

        self.view.set_viewport_position(self.original_viewport, False)


class AuthorToolsReportCommand(sublime_plugin.TextCommand):

    TITLE = ""

    def words(self):
        raise NotImplementedError

    def dialogue_only(self):
        return False

    def run(self, edit):
        view = self.view
        text = view.substr(sublime.Region(0, view.size()))

        found = find_words(text, self.words(), self.dialogue_only())

        if not found:
            sublime.status_message("{}: none found".format(self.TITLE))
            return

        WordReport(view, self.TITLE, found).show_words()


class AuthorToolsAdverbsReportCommand(AuthorToolsReportCommand):

    TITLE = "Adverbs"

    def words(self):
        return word_list(ADVERBS, "extra_adverbs")


class AuthorToolsDialogueVerbsReportCommand(AuthorToolsReportCommand):

    TITLE = "Dialogue verbs"

    def words(self):
        return word_list(DIALOGUE_VERBS, "extra_dialogue_verbs")

    def dialogue_only(self):
        # "called", "added" and "offered" are everyday verbs too, so by
        # default only count them in the same sentence as a quote mark.
        return bool(get_settings().get("dialogue_verbs_near_quotes_only", True))
