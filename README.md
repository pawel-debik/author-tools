# Author Tools - v0.1

Experimental Sublime Text plugin with tools for fiction writers working in
Markdown.

## Shortcuts

| macOS         | Windows        | Command                                 | What it does                              |
|---------------|----------------|-----------------------------------------|-------------------------------------------|
| `cmd+shift+0` | `ctrl+shift+0` | Author Tools: Enable / Disable Analysis | Author Tools on or off                    |
| `cmd+shift+1` | `ctrl+shift+1` | Author Tools: Toggle Colours            | Colours on or off                         |
| `cmd+shift+2` | `ctrl+shift+2` | Author Tools: Colour Marks By…          | Next colour mode (the command lists them) |
| `cmd+shift+A` | `ctrl+shift+A` | Author Tools: Scan Paragraph with Jev   | Scan the paragraph, or each selected one  |

The shortcuts only work in Markdown files; elsewhere the keys keep their
usual Sublime meaning.


## Paragraph analysis

- Jev rates each sense separately from 0 (absent) to 4 (dominant); the
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
- Jev also rates showing vs telling and tension (0–4, shown as dots in the
  status bar). Turn off with `show_showing` / `show_tension`.
- Jev also picks a mood ("Mood ominous") from a list you can replace with
  the `moods` setting. Turn off with `show_mood`.
- "Author Tools: Colour Marks By…" switches what the mark colours (and the
  minimap tint) show: dominant sense, mood, tension, showing, or
  telescoping. Nothing is re-analysed; the status bar shows the colour key,
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
  paragraph the selection starts in. "Author Tools: Scan Paragraph with
  Jev" with text selected sends each paragraph in the selection to Jev, one
  after the other ("scanning 3 of 12…"), and marks each one. Paragraphs
  already analysed in their current form come from the cache. Up to
  `max_scan_paragraphs` (30) at once; scanning again stops a scan.
- `analyse_selections: true` brings back automatic analysis of a selection
  as one piece of text ("KAV (selection)", also in the details card when
  hovering beside it), up to `max_selection_characters`. It doesn't change
  the margin marks.
- Paragraphs are separated by blank lines. Markdown heading lines are ignored.
- Only `.md` / `.markdown` files (and unsaved Markdown buffers) are analysed.
- Results are cached in RAM by SHA-1 of the paragraph text. Editing a
  paragraph changes its hash; it is re-analysed the next time you enter it,
  or immediately via "Author Tools: Scan Paragraph with Jev".
- `edited` in the status bar means the results describe the paragraph
  before your latest edit.
- "Author Tools: Enable / Disable Analysis" switches the analysis off; the
  word reports keep working.

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

## API key

Open "Preferences: Author Tools Settings" and set:

    "api_key": "sk-or-..."

Your key then lives in `Packages/User/`, outside this folder. All settings,
including the word reports', are in that one file.
