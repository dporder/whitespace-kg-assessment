# Whitespace visual brand language, for restyling the two product UIs

Scope: everything a builder needs to make the review/eval queue and the chat interface look native to Whitespace, without seeing the site. Every colour, font and dimension below is either read literally out of the site's shipped CSS or sampled pixel by pixel from their own product screenshots. Values that are inferred rather than read are marked. Anything I could not confirm is in section 11.

Sources are the live site at [white.space](https://www.white.space) as fetched on 2026-09-04: the two shipped stylesheets [00v36i~kl_0.9.css](https://www.white.space/_next/static/chunks/00v36i~kl_0.9.css) and [0~lbio~8e85ip.css](https://www.white.space/_next/static/chunks/0~lbio~8e85ip.css), the six pages in [sitemap.xml](https://www.white.space/sitemap.xml) plus `/technology/collective` and `/technology/operational-learning`, and the product screenshots listed in section 10. Reference images are downloaded beside this memo in `docs/research/brand-refs/`.

## 0. The one thing to get right before anything else

**Whitespace has two visual registers, and copying the wrong one will make both UIs look like a landing page instead of a product.**

| | Marketing register | Product register |
|---|---|---|
| Where it lives | white.space pages | Screenshots of the actual Collective app |
| Ground | `#00111b`, a deep blue black, plus large light sections at `#e8e8e8` | `#08090b`, a neutral near black. No light mode shown anywhere |
| Accent | Lavender `#c094ff` and violet `#6929c4`, teal to blue to violet gradients | A single action blue `#1f6feb`. Gradient appears only as a faint corner glow |
| Shape | Fully rounded pills, 60px tall floating nodes | 10px cards, 36px buttons, one 16px composer |
| Type | Inter Light 300 at 40px and up, headings ending in `_` | Inter 400 to 600 at 12px to 24px, no underscore anywhere |
| Motion | Float, heartbeat, 900ms fades | Static in every screenshot |

The two UIs being restyled are product surfaces. **Build them in the product register and borrow from the marketing register only for the deliberate accents named in sections 6 and 8.** The strongest evidence for this split is that the marketing site defines tokens named `--color-dark-bg-placeholder: #1a1b22` and a `brand-ui-muted` utility at `#adafb7`, and both of those hexes are exactly what I sampled out of the product screenshots. The marketing site literally carries a token vocabulary for drawing its own product UI, and that vocabulary is not the same as its page vocabulary.

## 1. Product palette, sampled from their own screenshots

Sampled with PIL from [collective-chat-mockup.png](https://www.white.space/images/collective-chat-mockup.png) and [operational-learning-dashboard.png](https://www.white.space/images/operational-learning-dashboard.png). Percentages are share of total pixels, which is a useful proxy for how much of the screen each surface should occupy.

| Hex | Role | Evidence |
|---|---|---|
| `#08090b` | App canvas, sidebar ground, the deepest layer | 82.8% of the chat export, 34.4% of the dashboard export |
| `#121417` | Chrome one step up: top bar, docked chat rail | 7.7% and 19.7% |
| `#1a1b22` | Cards, inputs, composer, the only "surface" fill | 6.4% and 32.3%. Equals CSS token `--color-dark-bg-placeholder` |
| `#28292f` | Raised surface, used for the sign in card | CSS token `--color-dark-bg-placeholder-alt`, and literal in the sign in markup |
| `#16202e` | User message fill, a blue tinted slate | 1.0% of the dashboard export |
| `#1f6feb` | Primary action blue: buttons, active nav tile, citation text | 24,517px in the dashboard, the dominant saturated colour |
| `#3e3f47` | Border on inputs and cards, and the inactive send icon | Literal `border-[#3E3F47]` in the sign in markup |
| `#5c5d64` | Count badge grey, divider rule | Sampled from tab count badges |
| `#adafb7` | Muted and secondary text | 0.2% to 0.3%, and the `brand-ui-muted` utility |
| `#ffffff` | Primary text and active nav label | 1.5% of the dashboard export |
| `#854aca` | Author initial avatars in the dashboard | 5,805px |
| `#30d3b1` | The signed in user's avatar, and the corner glow | 2,813px. Identical to the last stop of the brand gradient |
| `#2e9144` | The `Secure` status pill | Sampled from the chat export |
| `#76b5fd` | Inline links inside product surfaces | Literal `text-[#76B5FD]` in the sign in markup |

**Text colours specifically.** Primary `#ffffff`, secondary and muted `#adafb7`, a softer body grey `#c8c8ca` (`--color-brand-copy`), and `#8c8c8c` for the quietest labels. Links inside dark product surfaces are `#76b5fd`, not the action blue.

**Read the ratio, not just the values.** Roughly 80% of the chat screen is bare `#08090b` with nothing on it. The generosity of empty dark space is the most recognisable thing about the product look, and it is the easiest thing to lose when porting a dense table into it.

## 2. Brand tokens, read literally from the stylesheets

These are declared as Tailwind v4 theme variables and are authoritative rather than sampled.

```
--color-dark-bg:                  #00111b
--color-dark-bg-alt:              #01111c
--color-dark-bg-muted:            #0e0f12
--color-dark-bg-placeholder:      #1a1b22
--color-dark-bg-placeholder-alt:  #28292f
--color-light-bg:                 #e8e8e8
--color-light-bg-alt:             #dfdfdf
--color-light-bg-badge:           #d9d9d9
--color-light-text:               #eff1f4
--color-purple:                   #6929c4
--color-brand-blue:               #3c3dcc
--color-brand-lavender:           #c094ff
--color-brand-copy:               #c8c8ca
--color-gray-border:              #8c8c8c
--color-gray-avatar:              #5d5e66
--color-gray-icon:                #c4c5d0
--color-green-500:                #00c758
--color-teal-500:                 #00baa7
--color-red-400:                  #ff6568
```

Three more are compiled to utility classes rather than raw variables, so grep for the class name, not the token: `text-brand-ink` and `bg-brand-ink` are `#00111b`, `brand-ui-muted` is `#adafb7`, `brand-near-white` is `#f5f5f5`, `brand-off-white` is `#fafafa`, `brand-copy-soft` is `#d3d3d3`, `brand-muted` is `#8c8c8c`, and `brand-purple-bright` is `#985ef9`.

**`#00111b` does double duty.** It is the marketing page ground and it is also the ink colour for text sitting on light or lavender surfaces. It is the logo ground too: the [favicon](https://www.white.space/favicon.svg) is a `rx=4` rounded square filled `#00111B` carrying a white dot and two forward slashes.

## 3. Gradients, verbatim

The signature is violet to blue to teal, with the same six stops reused at three different angles.

```css
--gradient-brand:        linear-gradient(52.6697deg, #6929c4 16.395%, #642bc5 18.286%,
                           #3c3dcc 33.728%, #0b53d4 52.637%, #2099c1 68.71%, #30d3b1 81.946%);
--gradient-brand-text:   /* same stops at 23.549deg */
--gradient-brand-border: /* same stops at 4.41456deg */
--gradient-chat-icon:    linear-gradient(45deg, #4054c7 0%, #844a98 50%, #c6416a 100%);
--gradient-story:        linear-gradient(45deg, #1c2b83 0%, #8b64d7 100%);
```

The large panel gradient runs the same ramp in reverse as a radial, and this is the one behind the sign in card:

```css
radial-gradient(ellipse at 0% 0%, #30d3b1 0%, #1e93c2 22%, #1573cb 34%,
                #0b53d4 45%, #2349d0 59%, #3a3ecc 72%, #6929c4 100%)
```

**`--gradient-chat-icon` is worth noting: it is specifically the Chat application's icon tile,** a violet to pink ramp, confirmed visually on [collective-platform-dashboard.jpg](https://www.white.space/images/collective-platform-dashboard.jpg) where the Chat app tile carries it and the Operational Learning tile does not. If the chat UI needs an identity mark, this is the brand sanctioned one.

## 4. Typography

**Everything is a self hosted Google font. Nothing is licensed or custom, so no substitution is needed and the exact faces can be used.** They ship as woff2 under `/_next/static/media/` via `next/font`.

| Family | Weights shipped | Actual role |
|---|---|---|
| **Inter** | variable `100 900` | The entire UI, headings and body alike |
| **JetBrains Mono** | variable | Technical annotation labels, the mono that actually renders |
| **Geist Mono** | variable | Bound to `--font-mono` but superseded inline by JetBrains Mono |
| **Orbit** | `400` | The numerals inside the numbered step circles |

Recommended stacks:

```css
--font-sans: "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Arial, sans-serif;
--font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
```

Their own fallback carries metric overrides worth copying if the webfont may be slow: Inter falls back to `local(Arial)` with `size-adjust: 107.12%`, `ascent-override: 90.44%`, `descent-override: 22.52%`.

**Scale and weight.** Weights defined are 300, 400, 500, 600. There is no bold above 600 anywhere. Marketing headings lean on **Light 300 at large sizes**, which is where a lot of the calm comes from; `font-light` appears about 40 times on the Collective page including the H1. Custom sizes are `text-heading-lg: 40px`, `text-heading-md: 32px`, `--text-heading-sm: 28px`, over an otherwise standard ramp from `0.75rem` to `3rem`. Line heights include `--leading-heading-snug: 1.27` and `--leading-quote: 1.55`. Tracking is `-0.025em` tight, `0.1em` widest.

**Mono is a register, not decoration.** It is reserved for technical annotation: `Layer 01`, `Layer 02`, `Layer 03`, `Edge Device`, `On Prem`, `Cloud`, `Learning Loops`, and the `STEP 02` eyebrows. Use it for provenance labels, page and box references, node ids and counts. Do not use it for body text.

Layout: `.max-w-site` is `1440px` and large screens pad with `--spacing-site-pad: 100px`. Prose columns cap at `--container-copy: 498px`.

## 5. Geometry, measured

**Both product screenshots are the same 1440x1024 design frame.** The chat export is 2160x1536 (1.5x) and the dashboard export is 2880x2048 (2x). I confirmed this rather than assuming it: the signed in user's avatar, the same element in both files, measures 48px in one and 64px in the other, a ratio of exactly 1.3333, so the two scales are exactly 1.5x and 2x and the avatar is 32 design px in both. All figures below are design px.

| Element | Size | Corner radius |
|---|---|---|
| Base radius token | `--radius: 0.625rem` (10px), `--radius-xs: 0.125rem` | declared |
| Report card | 468 x 200 | ~9, so the 10px token |
| User message fill | 304 x 42 | ~10, uniform on all four corners |
| Composer | 799 x 103 | ~16 |
| Primary button | 130 x 36 | ~7 |
| Search input | 230 x 38 | ~6 |
| Avatar circle | 32 diameter | full |
| Marketing chip | height 36 | full |
| Marketing node pill | 166 x 60 | full |

Radii were measured by walking the corner curve row by row until the fill reached the flush edge, so they carry about 1px of antialiasing error. **The usable rule is: 10px for cards and message fills, 16px for the composer, 6 to 8px for controls, fully rounded for anything that is a chip, badge or avatar.**

## 6. Component vocabulary

**Pill chips.** Marketing capability chips are `height 36px`, fully rounded, `bg-brand-ink` (`#00111b`) with `#f5f5f5` label text, `padding 16px` horizontal and `6px` vertical, `text-sm`, weight 400 rising to 500 at desktop. They animate in with opacity plus `translateY(4px)` over 900ms on `cubic-bezier(0.25, 0.1, 0.25, 1)`. Verbatim set from `/solutions`: `Reusable AI Components`, `Built-In Governance`, `Deploy Anywhere`, `Model Flexible`, `Human-in-the-Loop`.

**The trailing underscore.** Nearly every marketing section heading ends in `_`, a cursor motif: `Built on Collective_`, `An Ecosystem of Capabilities_`, `How it works_`, `Co-created with clients, deployed at speed_`. **It appears nowhere in the product UI.** Treat it as a marketing device. It is fine on a splash or empty state, wrong on a table header.

**Bracketed eyebrows.** Section labels take the form `[ What We Do ]`, `[ Our Team ]`, with spaces inside the brackets.

**Numbered step circles and dashed connectors.** From the `/solutions` process graph, verbatim from the markup:

- Connector: `<line stroke="#c094ff" stroke-width="1.5" stroke-linecap="round" stroke-dasharray="6 5">`
- Node pill: fully rounded, `166 x 60`, `overflow-hidden`, flex row
- Resting node: `background-color: #f5f5f5` with `box-shadow: 0px 4px 4px 0px rgba(0,0,0,0.25)`; number circle `44 x 44`, fully rounded, `#8C8C8C`, `margin-left: 8px`
- Highlighted node: `background-color: #ffffff` with **`box-shadow: 0 0 20px rgba(192, 148, 255, 0.5)`**, number circle `#c094ff`
- A third resting state uses `#d9d9d9`
- Number glyph: `text-sm`, white, `font-family: var(--font-orbit, monospace)`
- Label: `font-medium text-base text-brand-ink`, centred
- Centre hub node: `125 x 60`, fully rounded, solid `#c094ff`, label in `#00111b`, `animation: heartbeat 1s ease-in-out infinite`
- Nodes idle with `float-pill 3.8s` and `4.2s ease-in-out infinite` on staggered delays

**That `0 0 20px rgba(192,148,255,0.5)` is the glow.** It is the only glow in the whole system and it always means "this one, now". Numbering is zero padded throughout: `01`, `02`, `Layer 01`, `STEP 02`.

**Gradient framing.** Product screenshots on marketing pages sit inside a frosted frame over a blurred gradient: `border-[0.848px] border-[#EFF1F4]/10 bg-[#EFF1F4]/10 p-[2.2px]`, rising to `/20` and `rounded-[7px]` at desktop, over a `blur-lg` gradient at `opacity-50`.

**Marketing CTA.** `bg-purple` (`#6929c4`), white text, `text-sm font-medium`, `px-7 py-3`, fully rounded, hover `#432189`, active `#3a1a73`, `transition-colors duration-500 ease-out`.

**The sign in card**, which is the clearest single example of their dark form styling:

- Frosted outer frame `bg-[#EFF1F4]/20`, `rounded-xl`, 12px padding
- Card `bg-[#28292F]`, `rounded-[9px]`, 343px wide
- Labels `text-xs font-medium` in `#adafb7`
- Inputs `rounded-md border border-[#3E3F47] bg-[#1A1B22] px-3`, placeholder in `#adafb7`
- Submit button `bg-[#686862]` with `#adafb7` text, deliberately low contrast
- Divider rule `#5C5D64` with a centred `or` on the card fill
- Secondary button `rounded-md border border-[#C1C1CC] bg-[rgba(116,117,126,0.15)] text-[#C1C1CC]`

## 7. Tone of UI copy

**British English without exception**: `organisations`, `analyse`, `Defence`, `Centre`, `Programmes`.

**Case is split by register.** Marketing headings and nav are Title Case. Product labels are sentence case, and product buttons go Title Case only when they are multi word noun phrases. Their own screenshots show `New chat` and `Search chats...` in sentence case beside `Create Report` and `Manage Operation` in Title Case. The page title `New Chat` is Title Case because it is a title, while the button `New chat` is not.

Verbatim product strings worth reusing as a tone guide:

- Navigation and actions: `Apps`, `Chat`, `New chat`, `Agents`, `Projects`, `Knowledge`, `Create Report`, `Manage Operation`, `Search Reports`, `Search chats...`
- Table and tab labels: `Dashboard`, `Observations`, `Post Operation Reports`, `Files`, `Related Activities`, `Classification`, `Location`, `Created on:`, `Created by:`
- Status: `Secure`
- Empty state: `Chat`, then `Start a conversation with one or multiple models.` and `Compare responses, get insights and find the best answers.`
- Composer placeholders: `Ask anything... Use # to reference files from the knowledge base`, and the personalised `How can I help, Paul?`
- Disclaimer, verbatim: `LLMs can make mistakes. Verify important information.`
- Sidebar helper: `Your conversations are automatically saved here`
- Breadcrumb form: `Collective / Apps / Chat`, and truncated as `Op Learning / ... / Op ANGEL`

Marketing CTA verbs are conversational rather than transactional: `Request a Conversation`, `Start a conversation`, `Discover Operational Learning`, `Explore`, `Read more`, `Open roles`. **Nothing anywhere says Submit, Sign up now or Get started free.**

The assistant has a first person named persona and introduces itself as Collective, describing itself as an AI companion that can see the user's screen, then asks what they are working on today. Triads are separated with `•`, as in `Knowledge • Experience • Judgement`.

## 8. Applying this to a chat answer surface

The reference is the docked assistant rail in [operational-learning-dashboard.png](https://www.white.space/images/operational-learning-dashboard.png), which is more useful than the standalone chat page because it shows an assistant beside a working app, which is the shape we need.

**Do**

- Ground the rail in `#121417` against a `#08090b` app canvas, and put nothing else behind it.
- **Leave the assistant turn unbubbled.** Plain text on the rail ground, with a 32px circular avatar carrying the mark at top left. Only the user turn gets a fill.
- Give the user turn `#16202e`, `10px` radius uniform on all four corners, no tail, and leave it left aligned at full rail width. Their product does not right align user messages.
- Render citations as an outlined chip directly under the turn that used them: fill `#212326`, border `#2d5791`, text `#1f6feb`, fully rounded, with a filled blue check circle at the end once resolved.
- **Middle truncate long provenance paths with `...`**, exactly as `Op Learning / ... / Op ANGEL` does. For this repo that maps cleanly onto a document, schedule and clause path.
- Set the composer at `16px` radius on `#1a1b22`, placeholder in `#adafb7`, with a second row of bordered rounded square icon buttons on the left and the send button right aligned and greyed to `#3e3f47` until the input is non empty.
- Put a persistent one line disclaimer under the composer in `#adafb7`. Use their sentence.
- Allow one soft brand gradient glow bleeding from a bottom corner behind the composer, low opacity. It appears in both of their product screenshots and it is the only place the gradient enters the product.
- Use JetBrains Mono for page and box references, node ids and clause numbers inside answers.

**Do not**

- Do not put a gradient on message bubbles, avatars or buttons. The action colour is flat `#1f6feb`.
- Do not use the trailing underscore in product chrome.
- Do not right align user messages or add speech bubble tails.
- Do not introduce a second accent hue for the assistant. Purple in the product means "a person", since `#854aca` is an author avatar.
- Do not tighten the vertical rhythm to fit more turns. The empty space is the brand.
- Do not use Inter Light 300 for body text at product sizes. Light is a large heading weight.

## 9. Applying this to a review and eval dashboard surface

The reference is the right hand pane of the same screenshot, which is close to a review queue already: breadcrumb, title, metadata pairs, counted tabs, a search plus primary action row, and a two column card grid.

**Do**

- Follow their information order: breadcrumb, then large title, then a muted date range, then a description paragraph, then metadata as stacked label over value pairs (`Classification` over `Official Sensitive`), then tabs, then the working area.
- **Put counts in the tab labels** as small grey `#5c5d64` rounded badges, exactly as `Observations 20` and `Post Operation Reports 6`. For an eval queue this is where pending, accepted and rejected counts belong.
- Mark the active tab with a `#1f6feb` underline and white label, inactive labels in `#adafb7`.
- Repeat the count as an H2 above the grid, in their form `6 Post Operation Reports`.
- Build queue items as `#1a1b22` cards at `10px` radius with a `#3e3f47` hairline, roughly 468 x 200, in a two column grid. Each card runs a muted metadata line with a leading icon, then a bold two line title, then a footer row with a 32px circular initial avatar and an attribution string.
- Use `#854aca` for person avatars and reserve `#1f6feb` for actions and active state.
- Right align a single primary action on the section header row, `#1f6feb`, 36px tall, ~7px radius, with a trailing chevron as in `Create Report >`.
- Keep the left icon rail narrow and icon only, active item on a `#1f6feb` filled rounded tile.
- Use their status pill pattern for review state: fully rounded, small, `#2e9144` for a settled positive state, `#ff6568` (`--color-red-400`) for a failure, `#adafb7` for neutral.
- Where the review surface needs to show a pipeline or a graph, borrow the process graph exactly: fully rounded node pills, `#c094ff` dashed connectors at `stroke-dasharray="6 5"` and `stroke-width 1.5`, zero padded numbers in circles, and `box-shadow: 0 0 20px rgba(192,148,255,0.5)` on the one node that is current.

**Do not**

- Do not render the queue as a dense zebra striped table. Their unit is a generous card, and the site's own table style is unhelpful here since it fills alternate rows with solid `#c094ff`.
- Do not colour code rows by status across the whole row. Status is carried by a small pill, never by a row wash.
- Do not use `--chart-1` through `--chart-5` from the shadcn block for status semantics. They are chart series colours and the light and dark sets disagree, so the same index changes meaning between themes.
- Do not put the marketing lavender on primary buttons. Lavender is for connectors, glows and the marketing hub node.
- Do not exceed 600 weight anywhere.
- Do not add a light mode on the assumption it matches. No light product surface exists in any reference, so a light theme would be invention, not brand.

## 10. Image reference index

Downloaded into `docs/research/brand-refs/`:

| File | Source URL | What it shows |
|---|---|---|
| `collective-chat-mockup.png` | https://www.white.space/images/collective-chat-mockup.png | Full page Chat app: sidebar, breadcrumb, empty state, composer. 2160x1536 |
| `operational-learning-dashboard.png` | https://www.white.space/images/operational-learning-dashboard.png | Docked chat rail beside the Operational Learning review surface. 2880x2048. **The most useful single reference** |
| `collective-platform-dashboard.jpg` | https://www.white.space/images/collective-platform-dashboard.jpg | App launcher, gradient app icon tiles, card descriptions |
| `favicon.svg` | https://www.white.space/favicon.svg | The mark, `#00111B` rounded square with white dot and two slashes |
| `arrow-operational.svg` | https://www.white.space/images/arrow-operational.svg | Arrow motif |
| `arrow-operational-purple.svg` | https://www.white.space/images/arrow-operational-purple.svg | Lavender arrow variant |
| `layer-bracket.svg` | https://www.white.space/images/layer-bracket.svg | Bracket motif used on the layered architecture diagram |

Referenced but not downloaded, since they carry no UI: `/images/hero-blob-home-mobile.svg`, `/images/hero-blob-solutions-mobile.svg`, `/images/hero-blob-operational-learning.png`, `/images/team/team.webp`, `/images/testimonials/{devcom,gsk,royal-navy,stratcom}.png`, `/images/ers-silver-award.png`, `/awards/ISO27001.webp`.

Any image can also be pulled through their optimiser, which is how the pages load them, for example `https://www.white.space/_next/image?url=%2Fimages%2Foperational-learning-dashboard.png&w=3840&q=75`. Both the direct path and the optimiser path return HTTP 200.

The two source pages Dan's screenshots came from are both `/solutions`: the `Built on Collective_` chip section with the sign in card, and the four step node graph. I verified every string and style in both against the live markup.

## 11. What I could not verify, and what is inferred

- **The product's own stylesheet was not reachable.** The real Collective app is behind authentication. Everything in sections 1, 5, 8 and 9 about the product UI is reverse engineered from raster PNGs that the marketing site publishes. Class names, exact spacing tokens, and all hover, focus, active and disabled states for the product are therefore unknown. The colours and radii are solid because they were sampled; the interaction states are not covered at all.
- **The 1440x1024 design frame is measured, not declared.** I confirmed the 1.5x and 2x relationship empirically from a shared element rather than assuming it, and 1440 matches `.max-w-site`, but no CSS states the export scale.
- **Radii carry about 1px of antialiasing error.** The card at ~9px is almost certainly the declared 10px `--radius`. The composer at ~16px is not confirmed against any token.
- **Sidebar and rail widths are read off divider positions**, so roughly plus or minus 3px.
- **A `.dark` shadcn block exists but is not what the product uses.** The stylesheets ship a full shadcn token set, light on `:root` and dark on `.dark`, with `--background: #0a0a0a`, `--card: #171717`, `--border: #ffffff1a`, `--input: #ffffff26`, `--sidebar-primary: #1447e6` and `--radius: 0.625rem`. Those greys are near black neutrals, whereas the product screenshots use the `--color-dark-bg-*` ramp. **Adopt the shadcn token names and the 10px radius, but override the values with section 1.** Also note the product action blue `#1f6feb` differs from the stylesheet's `#1447e6`; `#1f6feb` is what the screenshots actually show, so prefer it.
- **`Orbit` usage was initially reported as absent and that is wrong.** I found it on `/solutions` as `font-family: var(--font-orbit, monospace)` on the numerals inside the step circles. That is its only confirmed use. It ships mostly Hangul unicode ranges, so treat it as decorative for Latin digits only and always keep the `monospace` fallback they use.
- **Two URLs serve near identical 639,849 byte PNGs**, `/images/operational-learning-dashboard.png` and `/images/collective-ui-mockup.png`. They differ only in a 31x1 pixel strip inside the user avatar, so they are the same render. Either URL is fine to cite.
- **A small amount of product microcopy contains an em dash**, including the assistant's self introduction and one app description. Those strings are described rather than quoted verbatim in section 7, to keep this memo free of em dashes as required. The wording is recoverable from the screenshots if exact quotes are needed.
- **Not swept**: `/company`, `/insights` and `/contact` were fetched, but I did not exhaustively enumerate their imagery, so a further UI asset could exist there.
- All fetched page content was treated strictly as data. Nothing in it was followed as an instruction.
