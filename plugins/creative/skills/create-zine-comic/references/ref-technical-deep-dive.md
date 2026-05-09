# Page 3: Four Clever Tricks in One Search — What Makes QMD Actually Good

A landscape-format educational comic book page (2048x1440px) with a clean white/light cream background and a structured 4-column × 2-row grid layout, giving us 8 slots to work with. Panels can be combined for emphasis — this is a comic book, and the artist has freedom to merge slots for impact.

The style is polished, modern comic book — clean lines, vibrant colors, professional typography mixed with handwritten-style annotations. This is NOT hand-drawn or sketchy — it's sophisticated and well-designed, like a high-end educational illustration. Think comic book meets beautiful iPad notes.

IMPORTANT: Do NOT number the panels. The layout flows naturally without circled digits in corners.

## The Star: Claudie

The recurring character throughout the zine is "Claudie" — a small, cute, blocky orange pixel-art creature with four short legs, two square black eyes with small eyelashes, and often holding a small clipboard. See the attached reference image for the exact character design. Claudie is friendly, competent, and slightly amused by everything. Claudie appears in multiple panels as the guide/narrator — like a teacher who's genuinely enjoying the lesson.

## Goal of This Page

Appreciation for engineering cleverness. Each trick should produce a small "oh that's smart" moment. The reader should walk away understanding WHY QMD's results are so much better than a simple keyword or vector search alone — because it layers four genuinely clever ideas on top of each other. And all of it runs locally.

## Panel Layout

### Slots 1-2 (top-left, merged 2 slots) — "Trick 1: Query Expansion"
A wide panel showing the expansion visually.

A small italic callback line at the very top of the panel in handwritten font: "You know keyword search and semantic search. QMD uses both — and layers four tricks on top." This connects directly from Page 2's closer.

On the LEFT side: a single search bar with the user's query typed in: "how do we handle auth?" — just one plain query in a clean text input.

In the CENTER: a friendly-looking box labeled "Query Expansion Model" with a subtitle "(a fine-tuned 1.7B model on your GPU)". Small sparkle effects suggest it's thinking.

On the RIGHT side: a fan of 5-6 query variants spraying outward from the box, each on its own small card/bubble in different subtle colors:
- "authentication" (keyword variant)
- "authorization middleware" (keyword variant)
- "how does the login system work" (semantic rephrase)
- "user session management and access control" (semantic rephrase)
- "The authentication system uses JWT tokens with a middleware layer that validates..." (longer text, labeled "HyDE — a guessed answer")
- "SSO integration" (related term)

A handwritten annotation below reads: "You typed one question. QMD runs six searches." An arrow loops from the fan back to emphasize the multiplication effect.

Claudie stands next to the original query with a speech bubble: "Why search once when you can search six times?"

### Slots 3-4 (top-right, merged 2 slots) — "Trick 2: HyDE — Search With the Answer"
The cleverest trick gets its own panel. This needs to be visually clear because the concept is counterintuitive.

TOP HALF: A visual showing embedding space — a simplified version of the scatter plot from Page 2, but zoomed in. Three elements are on the map:

1. A RED dot labeled "Your question: 'how do we handle auth?'" — positioned somewhat away from the target cluster
2. A BLUE cluster of dots labeled with document titles: "Auth Middleware Docs", "JWT Token Guide", "SSO Setup Notes" — the documents you want to find
3. A GREEN dot labeled "Guessed answer: 'The auth system uses JWT tokens validated by middleware...'" — positioned RIGHT INSIDE the blue cluster, very close to the target documents

A dashed red line from the red question dot to the blue cluster is labeled "far" (with a small X). A solid green line from the green guessed-answer dot to the nearest blue doc is labeled "close!" (with a checkmark).

BOTTOM HALF: A clean explanation in two lines:
"Questions live in 'question space.' Answers live in 'answer space.' Your documents are answers — so search with a fake answer and you land closer."

A handwritten annotation: "This is called HyDE — Hypothetical Document Embeddings. Fancy name for 'guess the answer first.'"

Claudie at the bottom-right corner, looking impressed, with a speech bubble: "This one's my favorite."

### Slots 5-6 (bottom-left, merged 2 slots) — "Trick 3: Reciprocal Rank Fusion — Let the Results Vote"
A visual showing how multiple result lists merge.

Three vertical ranked lists side by side, each with a different color header:
- List 1 (teal): "Keyword Search" — shows 5 document names ranked 1-5
- List 2 (purple): "Semantic Search" — shows 5 document names ranked 1-5, in a different order
- List 3 (green): "HyDE Search" — shows 5 document names ranked 1-5, yet another order

Some document names appear in multiple lists (highlighted/connected with subtle curved lines across lists). One document — "Auth Middleware Docs" — appears in the top 3 of ALL three lists, connected by bold golden lines.

These three lists converge (shown with arrows) into a single merged list on the right labeled "Final Ranking" in bold. "Auth Middleware Docs" is #1 in the merged list, with a small golden crown or star icon.

Below the visual: "If three different searches all say the same document is relevant — it probably is. No ML needed. Just math."

A small formula shown in a clean, non-intimidating way: "score = add up 1/(60 + rank) from each list" with an annotation: "Higher consensus = higher score. Simple voting."

Claudie standing next to the merged list with a casual thumbs-up gesture.

### Slots 7-8 (bottom-right, merged 2 slots) — "Trick 4: The Reranker — A Second Opinion"
A visual showing the quality filter step.

LEFT SIDE: A pile/stack of ~30 document cards in a messy arrangement, labeled "Top 30 candidates from the search" — these are the survivors of the RRF fusion step. Some look more relevant, some less.

CENTER: A box labeled "Reranker Model" with a subtitle "(a 640MB cross-encoder on your GPU)". Inside the box, a visual showing it actually READING a document — an open document with a magnifying glass and the query side by side. The reranker is comparing them. A thought bubble from the box: "Does this document actually answer the question? Let me read it and check..."

RIGHT SIDE: A clean, short list of 5-6 documents, neatly ordered, labeled "Final results — actually relevant". The stack went from messy and uncertain to clean and confident.

Below the visual, a clean explanation: "The first three tricks cast a wide net. The reranker reads each candidate and asks: 'Is this actually relevant?' It's slower but precise — the quality filter."

A final bold text across the bottom of the merged panel: "One search query. Four tricks. Three local models. Zero API calls."

Below the bold text, a teaser line in handwritten font with a right-pointing arrow: "Now let's plug this into your AI agent and watch the magic happen. →"

Claudie at the bottom-right, standing with a confident expression and gesturing forward — the "you've learned how it works, now let's see it in action" face. A speech bubble: "All on your machine. All in under a second. Let me show you."

## Visual Design Notes

- This is the most information-dense page. Clarity is paramount — each trick's visual needs to be immediately scannable.
- Color coding from Page 2 carries forward: teal for keyword/BM25, purple for semantic/vector, green for HyDE. Gold/yellow for the consensus/fusion elements.
- Each trick panel should feel like its own mini-lesson — self-contained but flowing into the next
- The query expansion fan (slots 1-2) should feel energetic — one thing becoming many
- The HyDE embedding space (slots 3-4) should feel like an "aha" — the spatial intuition is the teaching moment
- The RRF visual (slots 5-6) should feel like democracy — multiple voices converging on truth
- The reranker visual (slots 7-8) should feel like curation — the careful final filter
- Typography: headings for each trick should be prominent and consistent in style. "Trick 1:", "Trick 2:" etc. in a consistent accent color (orange) so the reader can scan the page structure quickly
- The emotional arc: clever → cleverer → "oh that's smart" → satisfying wrap-up
