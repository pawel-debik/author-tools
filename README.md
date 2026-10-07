# Author Tools - v0.1

Experimental Sublime Text plugin with tools for fiction writers working in
Markdown.

## Shortcuts

| macOS         | Windows        | Command                                 | What it does                              |
|---------------|----------------|-----------------------------------------|-------------------------------------------|
| `cmd+shift+0` | `ctrl+shift+0` | Author Tools: Enable / Disable Analysis | Author Tools on or off                    |
| `cmd+shift+1` | `ctrl+shift+1` | Author Tools: Toggle Colours            | Colours on or off                         |
| `cmd+shift+2` | `ctrl+shift+2` | Author Tools: Colour Marks By…          | Next colour mode (the command lists them) |
| `cmd+shift+A` | `ctrl+shift+A` | Author Tools: Scan Paragraph            | Scan the paragraph, or each selected one  |
| `cmd+shift+T` | `ctrl+shift+T` | Author Tools: Toggle Paragraph Tint     | Paragraph tint on or off                  |

The shortcuts only work in Markdown files; elsewhere the keys keep their
usual Sublime meaning.


## Paragraph analysis

- Jev (or [GPT-6 Luna Decisions](#choosing-the-model)) rates each sense
  separately from 0 (absent) to 4 (dominant); the
  bars are each sense's share of the total. Categories keep a fixed
  order. Near-zero totals show "little sensory content".
- Analyses a paragraph when the caret *enters* it and rests there for
  `delay_ms`. Moving within a paragraph, or typing, never sends a request.
- Each analysed paragraph gets a mark in the margin, coloured by its
  dominant sense (or another measurement, see below). Jev also rates
  telescoping (how far the paragraph is zoomed out): ▲ for zoomed out (a
  landscape, a city, an event as a whole), ▼ for zoomed in (inside a
  character's mind, or up close through their eyes), and a circle in
  between. The status bar shows the same at the end ("▲ zoomed out").
  Turn off with `show_telescoping`.
- Jev also rates showing vs telling, thisness and tension (0–4, shown as
  dots in the status bar). Thisness is how particular the details are:
  "a chipped Delft saucer" rather than "a dish", details that could only
  belong to this character, place or moment. Turn off with `show_showing` /
  `show_thisness` / `show_tension`.
- Jev also picks a mood ("Mood ominous") from a list you can replace with
  the `moods` setting. Turn off with `show_mood`.
- "Author Tools: Colour Marks By…" switches what the mark colours (and the
  minimap tint) show: dominant sense, mood, tension, showing, thisness,
  telescoping, or the [scene structure](#scene-structure) instead of these
  marks. Nothing is re-analysed; the status bar shows the colour key,
  and afterwards the current mode ("Colours: KAV"). "Author Tools: Toggle
  Colours" hides or shows all the colours. Colours are in `mark_scopes`,
  `mood_scopes` and `scale_scopes`.
- "Author Tools: Toggle Paragraph Tint" also colours the paragraphs
  themselves: a background (default) or an outline, set with
  `paragraph_tint_style`.
- Hover over the margin, or run "Author Tools: Show Details", for a card
  with the word count, all scores, and echoes (words repeated close
  together, counted locally).
- Selecting text sends nothing to Jev; the status bar keeps showing the
  paragraph the selection starts in. "Author Tools: Scan Paragraph" with
  text selected sends each paragraph in the selection to Jev, one
  after the other ("scanning 3 of 12…"), and marks each one. Every
  paragraph is sent, even one analysed before, so scanning is also how you
  refresh a result that looks wrong. Up to `max_scan_paragraphs` (30) at
  once; scanning again stops a scan.
- `analyse_selections: true` brings back automatic analysis of a selection
  as one piece of text ("KAV (selection)", also in the details card when
  hovering beside it), up to `max_selection_characters`. It doesn't change
  the margin marks.
- Paragraphs are separated by blank lines. Markdown heading lines are ignored.
- Only `.md` / `.markdown` files (and unsaved Markdown buffers) are analysed.
- Results are remembered by SHA-1 of the paragraph text, and saved beside
  the manuscript (see [Saved scores](#saved-scores)). Editing a paragraph
  changes its hash; it is re-analysed the next time you enter it, or
  immediately via "Author Tools: Scan Paragraph".
- `edited` in the status bar means the results describe the paragraph
  before your latest edit.
- "Author Tools: Enable / Disable Analysis" switches the analysis off; the
  word reports keep working.

### Choosing the model

Paragraphs are rated by TypeSafe's Jev unless you choose OpenAI's GPT-6
Luna Decisions. Set `model` in "Preferences: Author Tools Settings", next
to your API key:

```jsonc
{
    "api_key": "sk-or-...",
    "model": "luna"    // or "jev" (the default)
}
```

| `model`  | Model                                    | Price per paragraph¹ |
|----------|------------------------------------------|----------------------|
| `"jev"`  | `typesafe/jev-1.13`                      | about $0.00006       |
| `"luna"` | `openai/gpt-6-luna-decisions`            | about $0.00017       |

¹ Measured on a 100-word paragraph with every measurement on, October
2026. A 300-page novel is roughly 3,000 paragraphs: about $0.20 with Jev,
$0.50 with Luna.

Both are equally quick (well under a second) and answer the same
questions, so everything above works the same. Their scores differ a little,
and so do their moods now and then: compare a few paragraphs before
switching a whole manuscript. The full id of any other OpenRouter Decisions
model works too.

Saved scores belong to the model that made them. After switching, each
paragraph is rated again when you enter it or scan it.

## Saved scores

Scores are saved in a file next to the manuscript, so they are still there
after restarting Sublime and you don't pay for the same paragraph twice:

```
My Novel/
├── chapter-12.md
└── chapter-12.md.author-tools.json
```

**Your manuscript is never changed.** The marks, colours and status bar
are drawn over the text by Sublime; nothing is written into the `.md` file.

### Setup

None: it is on by default. Optionally, choose where the files go in
"Preferences: Author Tools Settings":

```jsonc
{
    "api_key": "sk-or-...",
    // "beside_manuscript" (default), "sublime_cache" or "off"
    "save_scores": "beside_manuscript"
}
```

- `"beside_manuscript"`: the scores travel with the manuscript, through
  Dropbox, iCloud, Git or to another computer.
- `"sublime_cache"`: in Sublime's own cache folder, keeping your story
  folder clean. The scores are lost if you move or rename the manuscript.
- `"off"`: in memory only, until Sublime quits (the old behaviour).

If your novel is in Git, either commit the `.author-tools.json` files (the
scores come along to other computers) or add this line to `.gitignore`:

```
*.author-tools.json
```

### What happens automatically

- **Opening a manuscript** reads its saved scores. Every paragraph whose
  text is exactly as it was when analysed gets its mark back straight away,
  without a request. Changed paragraphs get none until they are analysed
  again.
- **A new result** (entering a paragraph, or a scan) is added to the file a
  couple of seconds later. The first result creates the file; files you
  never analyse get none.
- **Saving the manuscript** prunes the file to the paragraphs in the saved
  text. Scores of paragraphs you edited or deleted are dropped for good; no
  older versions are kept. When nothing is left, the file is deleted.
- **Scanning** ("Author Tools: Scan Paragraph", with or without a
  selection) always asks the model again and replaces the saved score. Moving
  the caret into a paragraph uses the saved score when there is one.
- Scores are matched by the paragraph's exact text and by the question
  sent. Changing the `model`, the `moods`, or switching a
  measurement on or off means the saved scores no longer match; they are
  requested again as you go, and the old ones are dropped at the next save.
- Enabling Author Tools again redraws the saved scores.

Good to know:

- Unsaved (untitled) buffers have no file, so their scores are kept in
  memory only. Once saved, their scores go into a new file.
- Renaming or moving the manuscript outside Sublime leaves the
  `.author-tools.json` file behind: rename or move it too. "Save As" in
  Sublime starts a file under the new name.
- The file is safe to delete; the scores are then requested again when
  needed. "Author Tools: Clear Cache" forgets all scores and deletes the
  files of the manuscripts opened this session (it asks first).

## Word reports

Two whole-document reports, counted locally (no Jev, no network), in the
Command Palette:

- "Author Tools: Adverbs Report"
- "Author Tools: Dialogue Verbs Report" (only in the same sentence as a
  quote mark, by default)

Each lists the words found, most used first. Choose a word to list its
occurrences; moving through that list scrolls to each one. "Select all"
selects every occurrence. Escape goes back, and from the word list returns
you to where you were.

## Character interviews

Ask your characters what they think of a passage, in their own voice.

1. Select the passage.
2. Run "Author Tools: Ask Character…" and pick a character (those named in
   the selection come first).
3. Pick a question ("Would you really do this?", "How does this make you
   feel?", …) or type your own.

The answer appears in a popup beside the selection and in an "Interview"
tab beside the manuscript, which keeps the whole conversation. "Author
Tools: Ask Follow-up…" (or "Ask Character…" from inside the Interview tab)
asks the same character another question, with the conversation so far.
Turn either display off with `character_reply_display`.

### Setting up your story folder

Open "Preferences: Author Tools Settings" and add `story_folder` next to
your API key:

```jsonc
// Your personal Author Tools settings.
{
    "api_key": "sk-or-...",
    "story_folder": "~/Documents/My Novel"
}
```

On Windows, use forward slashes (or double backslashes):

```jsonc
{
    "api_key": "sk-or-...",
    "story_folder": "C:/Users/you/Documents/My Novel"
}
```

The story folder looks like this:

```
My Novel/
├── story_so_far.md        (optional)
└── characters/
    ├── character1.md
    ├── character1.png         (optional portrait)
    ├── character1.happy.png   (optional, one per mood)
    ├── character1.angry.png
    └── character2.md
```

Without `story_folder`, the example story in
[AuthorTools/example](AuthorTools/example/) is used, for testing: open
`AuthorTools/example/chapter-12.md`, select a passage and ask. Without
`story_folder` and without the example folder, the character commands are
hidden.

### Character file (`characters/character1.md`)

Everything below the header is sent to the model as written, so the
sections are only a suggestion. Without the header, the file name is used
as the name.

```markdown
---
name: Character One
aliases: [One, the Captain]
---
## Who they are
Age, background, role in the story, what shaped them.

## Personality
Traits, values, fears, desires, and contradictions.

## Voice
How they talk: sentence length, pet phrases, what they never say.

## Relationships
- **Character Two**: what they are to each other.

## What they know
Only what has happened up to chapter 1. They don't know that ...

## Example lines
"A line of dialogue in their voice."
"Another one."
```

### Portraits

An image beside a character file with the same name (`character1.png`,
`.jpg` or `.gif`) is shown in the reply popup. Add one image per mood as
`character1.<mood>.png`, using any word you like: `happy`, `sad`, `angry`,
`tired`, `suspicious`… The character then picks one of those moods for each
answer, and the popup shows that portrait. When the mood has no image, the
default `character1.png` is shown; without that, no portrait. Square images
work best; set the size with `character_portrait_size` (96 pixels by
default).

### Story so far (`story_so_far.md`)

```markdown
# The story so far (up to the end of chapter 1)

What has happened, who is where, and what's at stake right now.
```

Answers come from `character_model` (DeepSeek V4.1 Flash by default) through
OpenRouter's chat endpoint, with the same API key. This works even when the
analysis is disabled.

## Scene structure

Story Grid's Five Commandments for each scene in a chapter, drawn over the
text.

1. Open the chapter (or select its paragraphs, in a file with several).
2. Run "Author Tools: Analyse Scene Structure". The status bar shows
   "Structure: analysing 64 paragraphs…"; a free reasoning model can take a
   minute or two.

The model splits the chapter into scenes, and for each scene finds:

| Commandment               | Drawn as                               |
|---------------------------|----------------------------------------|
| Inciting incident         | paragraph outlined yellow, gutter icon |
| Progressive complications | span tinted orange                     |
| Turning point             | paragraph outlined red, gutter icon    |
| Crisis                    | paragraph outlined purple, gutter icon |
| Climax                    | paragraph outlined pink, gutter icon   |
| Resolution                | span tinted green                      |

and the value that shifts across the scene. Labels on the right name each
pivot ("Turning point", "Climax (weak)"), and the first paragraph of every
scene says how it turns: "Scene 2 · safety + → − · no crisis". A scene
with no shift, or with a missing commandment, is worth a second look.

The structure is the last colour mode: cmd+shift+2 cycles KAV, mood,
tension, showing, thisness, telescoping, then **Structure**, which hides
Jev's marks while it is shown. Analysing switches to it. cmd+shift+1
(Toggle Colours) hides it along with the rest.

- Hover over the gutter for what that paragraph does in its scene and why
  ("Turning point P6 · revelation: Mara finds the letter is forged; the
  plan can't work"), with the scene's value shift below it.
- Click a label or the scene in the hover card, or run "Author Tools: Show
  Scene Structure", for the whole scene's card: what the protagonist
  wants, the value shift, and one sentence of reasoning per commandment,
  with links to the paragraphs. Outside any scene it lists all scenes.
- The model's answer is checked: paragraph numbers that don't exist,
  pivots outside their scene or out of order, and quoted opening words
  that belong to another paragraph (the number is then corrected). The card
  shows what was changed, after ⚠.
- Only sent when you ask; nothing is sent while you write. Editing the text
  never re-analyses it: the scene's label says "edited", and the card how
  many paragraphs changed. Analyse again to refresh it.
- Saved beside the manuscript as `chapter-12.md.structure.author-tools.json`
  (or wherever `save_scores` says), and restored when the file is opened,
  even after edits. "Author Tools: Clear Scene Structure" forgets it.
- The model is `openai/gpt-6-luna`, only on Azure's EU servers
  (`structure_providers`: `["azure/eu"]`), so the chapter stays in the EU;
  if that provider is down, the analysis fails rather than going
  elsewhere. Change `structure_model` for any other OpenRouter chat model,
  and `structure_providers` to its provider tags, or `[]` for any. Free
  models have daily and per-minute limits, and some providers log what you
  send: check your OpenRouter privacy settings before sending unpublished
  work.

## API key

Open "Preferences: Author Tools Settings" and set:

    "api_key": "sk-or-..."

The same key works for every model, including `"model": "luna"` (see
[Choosing the model](#choosing-the-model)).

Your key then lives in `Packages/User/`, outside this folder. All settings,
including the word reports', are in that one file.
