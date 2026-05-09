---
name: create-zine-comic
description: >
  Create educational comic-book-style zines that explain any concept visually using AI image generation.
  Use this skill whenever someone wants to make a zine, comic, graphic explainer, visual guide, illustrated
  tutorial, or comic-book-style educational content. Triggers on requests like "make a zine about X",
  "create a comic explaining Y", "I want to make a visual explainer", "illustrated guide to Z",
  "graphic novel style walkthrough", or any request to turn a concept into a visual, panel-based,
  comic-book-style format. Also use when someone references "wizard zines", "Julia Evans style",
  or wants to create something that teaches through illustrated pages.
---

# Create Zine

Create polished, comic-book-style educational zines that explain any concept through illustrated pages. Two AI brains, one workflow: Claude thinks and writes detailed panel descriptions in markdown, then OpenAI's gpt-image-2 renders them into finished comic pages.

This skill follows the same production pipeline used by professional comic studios — adapted for AI. Each phase requires a different type of thinking, and they don't blend. Trying to do two phases at once produces worse results than doing each one properly in sequence.

## The Architecture: Markdown as Prompt

The markdown file IS the generation prompt. There's no separate "image prompt." You write a rich, detailed description of every panel, every piece of text, every visual element — and pipe the whole file to the image model. When something doesn't look right, you edit the markdown and regenerate. You never pass images back as input for edits.

This architecture matters:
- **Iteration is cheap.** Editing text costs nothing. The creative loop stays in text-land where Claude is strongest.
- **Full control.** The markdown is a human-readable spec. The user can review, restructure, hand it off.
- **Detail drives quality.** gpt-image-2 thinks about the prompt before drawing. A rich, specific description produces a far better image than a vague one.

---

## The Production Pipeline

Five phases, each with a quality gate. You don't advance until the gate passes. This discipline exists for the same reason it exists in comic studios: skipping a phase wastes enormous time downstream. A structural problem caught in Phase 2 costs a sentence to fix. The same problem caught in Phase 5 costs regenerating every page.

### Phase 1: The Writer's Room

**Your role:** Editor / interviewer
**The user's role:** Subject matter expert
**Output:** A narrative brief
**Gate:** User agrees on topic, audience, angle, and emotional arc

This is pure content. No visuals, no panels, no layout. The user knows what they want to teach — your job is to draw out the story underneath it.

Interview the user like a manga editor meeting with a writer. Don't accept the first answer — push deeper:

- **What's the topic?** Get specific. "How git works" is too broad. "The three git commands that prevent you from losing work" has an angle.
- **Who's the reader?** Someone who's never opened a terminal? A developer who uses git but doesn't understand it? The answer changes everything.
- **What's the "thing nobody tells you"?** Every great zine page has this. The insight that makes someone say "oh, THAT'S why it works that way."
- **What should the reader feel at the end?** Not just "informed" — empowered? relieved? amused? The emotional destination shapes every decision downstream.
- **Is there a recurring character?** A guide who appears across pages creates continuity and personality. If the user has one, get a reference image. If not, discuss whether to create one. Character consistency across pages is a solved problem — you pass a reference image with every generation.

Don't rush this. The brainstorm should feel like a conversation between a writer and their editor — testing ideas, finding the angle, killing the darlings. Three rounds of back-and-forth here saves three rounds of page rewrites later.

**🚫 Gate check:** Do NOT proceed until the user has signed off on what the zine is about, who it's for, and what it should feel like. If you're unsure, ask: "Before I start planning pages — are we aligned on the direction?"

---

### Phase 2: The Storyboard

**Your role:** Storyboard artist / pacing director
**The user's role:** Creative director (approves or redirects)
**Output:** A page plan with narrative arc
**Gate:** User signs off on the page plan before any page spec is written

This is where pacing decisions happen. Studios protect this phase aggressively because it's where the biggest structural mistakes are made — and where they're cheapest to fix. Moving a concept from page 2 to page 3 in the storyboard costs nothing. Moving it after the pages are written and generated costs real time and money.

**Plan the pages:**

```
Page 1: [Title] — [Core concept] — [Emotional beat]
Page 2: [Title] — [Core concept] — [Emotional beat]
Page 3: [Title] — [Core concept] — [Emotional beat]
Page 4: [Title] — [Core concept] — [Emotional beat]
```

Default is 4 pages per zine. Can go longer for ambitious topics (production examples range from 4 to 13 pages), but more pages means more surface area for inconsistency. Start tight.

**What makes a good storyboard:**

- **One concept per page.** If you're cramming two ideas onto one page, split them. A concept that gets room to breathe teaches better than two concepts fighting for space.
- **Pages thread together.** The end of one page hooks into the beginning of the next. "But how does X actually work?" → next page opens by answering that question. This threading is what makes a zine feel like a journey rather than a slideshow.
- **The emotional arc is deliberate.** Common arcs that work:
  - Curiosity → understanding → empowerment ("you didn't know this → now you do → now you can use it")
  - Empathy → tension → resolution ("you've felt this problem → here's why it exists → here's the fix")
  - History → present → future ("this is where it came from → this is where it is → this is where it's going")
- **The first page is an empathy hook.** Don't open with definitions. Open with recognition — make the reader feel seen. "You already do this." "You've seen this and wondered what it was." "Everyone gets confused here."
- **The last page is a payoff that callbacks the opener.** The best closers circle back to page 1 and reframe it. The reader re-evaluates everything they just learned through the lens of the final insight.
- **Page turns are reveals.** The last panel on a page plants a question. The first panel on the next page answers it. This is the one storytelling tool that comics have and no other medium possesses — use it deliberately.

Expect to iterate on the page plan 2-3 times. That's normal — the planning IS the creative work.

**🚫 Gate check:** Do NOT write any page specs until the user has approved the page plan. Ask explicitly: "Does this page flow feel right? Any concepts in the wrong order, or anything missing?"

---

### Phase 3: Art Direction

**Your role:** Art director (composition, mood, text placement, visual storytelling)
**The user's role:** Creative director (approves or redirects the spec)
**Output:** One markdown file per page — the complete visual spec AND generation prompt
**Gate:** User reviews the spec before generation

This is the densest phase. In a comic studio, this work is split across specialists — penciler (composition), inker (line detail), colorist (mood), letterer (text). You're doing all of those in one markdown document, but the thinking is still layered. Approach each page spec through these four lenses:

**Lens 1: Composition (the penciler's job)**
Which of the 8 grid slots does each panel occupy? Which slots merge for emphasis? Where does the reader's eye flow? A 3-slot merged panel says "this is important." A full-width panel says "this is THE moment." Use merging deliberately — if everything is big, nothing is.

**Lens 2: Performance (the character actor's job)**
What expression does the character have? What's their body language? Are they confused, excited, smug, eating popcorn? Characters should react emotionally to the content — that reaction teaches the reader how to feel about what they're seeing. A character looking impressed at a concept tells the reader "this is worth paying attention to."

**Lens 3: Atmosphere (the colorist's job)**
What's the color palette? Does it shift across the page? Color carries narrative — desaturated gray tones for "the old way," warm golden tones for "the new way," dark cinematic tones for dramatic moments. A page that literally gets warmer as you read it tells a story before the reader processes a single word.

**Lens 4: Text (the letterer's job)**
What words appear on the page? Where? Text placement controls reading order — the reader sees text before art in most panels. Mix clean sans-serif for main text with handwritten-style annotations for callouts and "here's the real insight" moments. Keep text minimal — this is a visual medium. If the art shows it, the text shouldn't restate it.

**Page spec format — every file must include:**

1. **Page header** — dimensions (2048x1440px landscape), grid format (4 columns × 2 rows), style description
2. **"IMPORTANT: Do NOT number the panels."** — Include this directive in every single page file. Without it, the model adds circled numbers to panel corners. This is the single most common gotcha.
3. **Character description** — if there's a recurring character, copy-paste the identical character block on every page. Don't rephrase it — the image model interprets variations as different characters.
4. **Goal of the page** — what the reader should feel/learn. This grounds every panel decision.
5. **Panel-by-panel layout** — for each panel: which slots it occupies, what's illustrated, what text appears, what annotations exist, where the character is and what they're doing
6. **Visual design notes** — color palette, typography, mood shifts, emotional arc across the page

Read the reference examples in `references/` to see what great page specs look like at this phase. They demonstrate different page types — empathy hooks, narrative storytelling, technical deep-dives, taxonomy grids, architecture infographics, and narrative closers.

**The user's job in this phase is creative direction, not writing.** They shouldn't have to write panel descriptions — that's your job. Present the spec, and let them approve or redirect: "This feels right" or "The emphasis is wrong here — the aha moment should be bigger." Think of the relationship between a film director and a storyboard artist.

**🚫 Gate check:** Share the page spec with the user before generating. They don't need to approve every word, but they should confirm the structure, emphasis, and emotional direction feel right.

---

### Phase 4: The Press

**Your role:** Production manager
**Output:** Generated PNG images (2048x1440px)

This is mechanical. The creative work is done — now you're feeding the approved spec to the renderer. In a studio, this is the equivalent of going to print. You don't make story decisions at the press.

**Generation:**

Use OpenAI's gpt-image-2 via the `client-work:openai-imagegen` skill if available. Otherwise, use the generation script directly:

```bash
SCRIPTS_DIR="/Users/claudie/.claude/plugins/cache/every-consulting/client-work/1.7.1/skills/openai-imagegen/scripts"
PYTHON="${SCRIPTS_DIR}/.venv/bin/python3"
export OPENAI_API_KEY=$(security find-generic-password -s 'OPENAI_API_KEY' -w)
PAGE_CONTENT=$(cat path/to/page-file.md)

$PYTHON "${SCRIPTS_DIR}/edit_image.py" \
  "$PAGE_CONTENT" \
  output-path.png \
  --image path/to/character-reference.jpeg \
  --size 2048x1440 \
  --quality medium
```

- Always pass the character reference image with `--image` if there's a recurring character
- Use `medium` quality for iteration (~$0.05/image), bump to `high` for final versions
- Pages can be generated in parallel (3 at once works fine)
- If a generation fails, it's usually a content filter trigger — restyle the offending panel to be more abstract and retry

---

### Phase 5: Editorial Review

**Your role:** Editor
**The user's role:** Editor-in-chief
**Output:** Feedback that routes back to Phase 3

Review the generated output against the story intent from Phase 1 and the page plan from Phase 2. Did the emotional beats land? Is the pacing right? Is the text legible? Do the pages flow as a sequence?

**The fix is almost always in the markdown.** This is the core principle — you never try to fix the image directly. You go back to Phase 3, edit the spec, and regenerate. The feedback loop is:

| Problem | Fix (in Phase 3) |
|---------|-------------------|
| Layout wrong | Restructure the panel slot assignments |
| Content unclear | Rewrite what's in the panels |
| Pages feel disconnected | Add threading hooks between pages |
| Character inconsistent | Verify the reference image is being passed and the character description is identical across files |
| Text garbled or misplaced | Be more explicit about what text appears and where it sits in the panel |
| Emotional beat doesn't land | Revisit the visual design notes — adjust color, character expression, panel size |
| Too crowded | Split content across more panels or merge slots to give key content more room |

This phase can loop multiple times. Each loop should be tighter than the last — you're refining, not restructuring. If you're restructuring, that's a sign Phase 2 needed more time.

---

## Craft Principles

These patterns come from 33 pages of production zines. They're not rules — they're the things that consistently separate pages that teach from pages that just display information.

**Panel size controls reading speed.** A large merged panel makes the reader slow down and absorb. A sequence of small panels creates quick cuts — urgency, speed, action. Varying the rhythm across a page keeps it alive. A page of identically-sized panels is monotonous.

**Show, don't restate.** If the illustration shows a frustrated developer, the text shouldn't say "the developer was frustrated." Use text to add information the image can't convey. Redundancy weakens both channels.

**Annotations do the teaching.** Handwritten-style callouts — "this is the important part," "you can ignore this for now," "yes, everyone forgets this" — are where the real teaching voice lives. They feel personal and honest in a way that body text doesn't.

**Characters are emotional guides.** The recurring character shouldn't just narrate — they should react. Confused when concepts are confusing (gives the reader permission to feel the same). Excited at discoveries. Smug at the punchline. Eating popcorn during the dramatic moment. Their emotional journey IS the reader's emotional journey.

**Color carries narrative.** A page that shifts from cool desaturated tones to warm golden tones tells a story of transformation before the reader processes a single word. Use temperature deliberately — it's one of the most powerful and least obvious tools.

**Concrete over abstract.** Show real terminal output, real code, real UI mockups — what users actually see on their screens. Abstract metaphors have their place (usually one panel per page), but the teaching happens through concrete, recognizable visuals.

**Thread your pages.** End each page with a question or hook ("But how does X actually work? →") and open the next page by answering it. This threading creates momentum — the reader feels pulled forward rather than deciding whether to continue.

---

## Reference Examples

The `references/` directory contains page specs from completed production zines. These are real specs that were used to generate real pages — not templates. Read them to absorb the level of detail, the panel description patterns, and how emotional direction is woven into visual specs.

| File | Page type | What it demonstrates |
|------|-----------|---------------------|
| `ref-opener-empathy-hook.md` | Opener | How to make the reader feel seen before teaching anything — the "you already do this" pattern |
| `ref-character-driven-narrative.md` | Narrative | How to tell an origin story through a consistent human character who appears across multiple panels |
| `ref-technical-deep-dive.md` | Technical | How to explain a dense concept (4 techniques in one page) with clarity through visual metaphors |
| `ref-taxonomy-grid.md` | Taxonomy + pivot | How to present a classification grid and then pivot to the emotional "real" lesson underneath |
| `ref-architecture-infographic.md` | Infographic | How to turn a system architecture into a visual stack diagram with layered annotations |
| `ref-narrative-closer.md` | Closer | How to close a zine with a satisfying emotional arc and a callback to the opening page |

Don't copy these literally — use them to understand the quality bar and the patterns that make pages work. Every zine has its own voice; these show the range of what's possible within the format.

---

## Gotchas

These will bite you if you don't know about them:

- **"Do NOT number the panels"** must appear in every page file. The model defaults to adding circled digits in panel corners. This single line prevents it.
- **Character descriptions must be identical across pages.** Copy-paste, don't rephrase. The image model treats even slight rewording as a different character.
- **Don't skip Phase 2.** The most expensive mistake is writing detailed page specs before the page plan is solid. A page in the wrong position costs a full rewrite. A page in the wrong position in the storyboard costs a single line edit.
- **Content filters exist.** If generation fails silently, a visual element probably triggered a safety filter. Make the panel more abstract (remove realistic violence, medical imagery, etc.) and retry.
- **More detail = better results.** Vague specs produce generic art. The reference examples are long for a reason — every sentence gives the model something specific to render.
