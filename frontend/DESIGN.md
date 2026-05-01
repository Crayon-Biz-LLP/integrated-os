# Rhodey OS — DESIGN.md

## For AI agents reading this file

This file is the single source of truth for all UI decisions in the Rhodey OS frontend.
Before making any UI change, read the relevant section.
When in doubt, follow the anti-patterns list at the bottom.
Never use hardcoded colour values. Always use Tailwind token classes.
Scope every change to the minimum files necessary.

# Rhodey OS — DESIGN.md v2

> Grounded in the actual frontend stack: Next.js 16, React 19, Tailwind v4, shadcn/ui, Radix UI, Lucide icons, Sonner toasts, next-themes, Supabase auth, D3.

---

## Product intent

Rhodey OS is an operating system for capture, memory, task execution, email triage, briefings, reflections, and operator clarity. The interface must optimize for truth, trust, speed, and calm focus. It must feel premium enough to use every day and credible enough to demo externally — without becoming a decorative startup dashboard.

---

## Stack constraints

All design decisions must respect the existing tech stack.

| Layer | Current stack | Design implication |
|---|---|---|
| Framework | Next.js 16 App Router | Page-level layouts, nested shells, server/client split |
| UI primitives | shadcn/ui + Radix UI | Use existing components before creating new ones |
| Styling | Tailwind v4 + CSS variables | Tokens defined in `globals.css`, override via `@theme inline` |
| Icons | Lucide React | Use only Lucide icons; no mixing of icon libraries |
| Toasts | Sonner | Toasts for background feedback only; never for primary outcomes |
| Theme | next-themes with `.dark` class | Light and dark both must be designed, not just toggled |
| Charts | D3 | All data visualization uses D3; consistent low-saturation palette |
| Auth state | Supabase | User email and session visible in sidebar footer |
| Animation | tw-animate-css | Use sparingly; layout shifts and entrance animations are banned |

---

## Current token audit

The `globals.css` uses OKLCH-based shadcn defaults. The current state is a zero-chroma (grey-only) token set with no brand accent. This is a safe starting point but intentionally underspecified.

Current state:
- Light mode: pure white background, near-black text, no accent color
- Dark mode: very dark grey background, near-white text, sidebar primary set to blue (`oklch(0.488 0.243 264.376)`)
- Charts: five grey shades only — not usable for meaningful data differentiation
- Radius: base `0.625rem`, scaling via multipliers from `--radius-sm` to `--radius-4xl`

Critical gap: There is no brand accent in light mode. The only non-grey color in the entire dark theme is the sidebar primary (blue), which creates an inconsistency — it looks designed in the sidebar but generic everywhere else.

---

## Required token changes

These are changes to `globals.css` that bring the interface to the right quality level.

### Brand accent

Replace the zero-chroma primary in light mode with a muted teal that works across both modes. This single change gives Rhodey a coherent product identity without requiring a full redesign.

```css
/* Light mode — replace in :root */
--primary: oklch(0.48 0.09 192);           /* muted teal — active state, CTA */
--primary-foreground: oklch(0.985 0 0);    /* white on teal */
--ring: oklch(0.48 0.09 192);             /* focus rings match accent */

/* Dark mode — replace in .dark */
--primary: oklch(0.65 0.09 192);           /* lighter teal, readable on dark surfaces */
--primary-foreground: oklch(0.145 0 0);
--ring: oklch(0.65 0.09 192);
--sidebar-primary: oklch(0.65 0.09 192);   /* unify sidebar accent with global accent */
--sidebar-primary-foreground: oklch(0.145 0 0);
```

### Warmer surfaces (optional upgrade)

The current pure-white light mode feels clinical. A subtle warm tint improves comfort on long sessions.

```css
/* Optional — replace in :root */
--background: oklch(0.985 0.003 80);       /* barely warm off-white */
--card: oklch(0.99 0.002 80);
--sidebar: oklch(0.975 0.003 80);
```

### Chart colors

Current charts use five grey shades which are visually undifferentiated. Replace with a low-saturation multi-hue palette that reads clearly in both modes.

```css
/* Replace in both :root and .dark */
--chart-1: oklch(0.55 0.10 192);   /* teal — matches primary accent */
--chart-2: oklch(0.55 0.10 250);   /* slate blue */
--chart-3: oklch(0.60 0.09 130);   /* muted green */
--chart-4: oklch(0.60 0.10 50);    /* warm amber */
--chart-5: oklch(0.55 0.10 310);   /* muted purple */
```

### Radius

The current `--radius: 0.625rem` base is acceptable. Keep it. Do not increase radius on low-density functional components (tables, badges, input fields). Apply only `--radius-sm` and `--radius-md` on those. Reserve `--radius-lg` and above for cards and modals.

---

## Typography

### Current state

The font system currently maps `--font-sans` → Geist Mono fallback. This needs correction.

```css
/* globals.css maps --font-sans and --font-mono */
/* Ensure layout.tsx loads a proper display/body sans font */
```

### Required direction

- Body: `Geist Sans` or `Inter` — clean, legible, tabular-nums-capable
- Display: same as body in product shell — Rhodey is a tool, not a magazine
- Mono: `Geist Mono` — for IDs, timestamps, raw data, and code only
- Heading font: same as body (current `--font-heading: var(--font-sans)` is correct — keep it)

### Type scale rules

Only these sizes should appear in product UI:

| Role | Size | Tailwind class | Notes |
|---|---|---|---|
| Tiny label / badge | 11–12px | `text-xs` | Uppercase tracked for metadata |
| Control / nav / button | 13–14px | `text-sm` | Default for all interactive UI |
| Body / list rows | 15–16px | `text-sm` to `text-base` | Primary reading text |
| Section heading | 18–20px | `text-lg` to `text-xl` | One per section |
| Page title | 22–24px | `text-xl` to `text-2xl` | One per page |

No display-scale type anywhere in the application shell. `text-3xl` and above are banned from product views.

Numbers in tables and stats must use `tabular-nums`:
```tsx
<span className="font-mono tabular-nums text-sm">...</span>
```

---

## Navigation — current state and rules

### What exists

The dashboard `layout.tsx` defines:
- Desktop: fixed left sidebar, `w-64`, `bg-zinc-900 text-zinc-100` (hardcoded, not using CSS variables)
- Mobile header: fixed top bar, hamburger, Sheet slide-in from right
- Mobile bottom: tab bar with 5 items
- Modules: Tasks, Projects, Emails, Memories, Calendar, People, Resources

### What needs to change

The sidebar uses hardcoded `bg-zinc-900` and `text-zinc-100` instead of CSS variable tokens. This breaks theme consistency and dark mode override behavior.

**Fix:**

Replace hardcoded zinc values with semantic tokens:
```tsx
// Current — wrong
className="lg:bg-zinc-900 lg:text-zinc-100"

// Correct — use sidebar tokens from globals.css
className="lg:bg-sidebar lg:text-sidebar-foreground"
```

Border and muted text in sidebar should also use token classes:
```tsx
// Current
"border-zinc-800"   → "border-sidebar-border"
"text-zinc-400"     → "text-sidebar-foreground/60"
"hover:bg-zinc-800" → "hover:bg-sidebar-accent"
```

### Navigation rules

- Active state: `bg-accent text-accent-foreground` (keep current pattern, it is correct)
- Hover state: `hover:bg-accent/50 hover:text-foreground` (keep)
- Icon size: `h-5 w-5` on nav items (keep)
- Mobile sheet: opens from right — acceptable for now; consider left-side in future
- Bottom tab bar: 5 items max — correct as implemented
- Resources and People hidden from mobile bottom bar — correct

---

## Shell and layout rules

### Desktop layout

```
┌─ Sidebar (w-64, fixed) ────┐ ┌─ Main content (flex-1, pl-64) ────────────────┐
│  Logo + product name        │ │  Page content                                  │
│  Nav links                  │ │                                                │
│  └─ User email              │ │                                                │
│  └─ Logout                  │ │                                                │
└─────────────────────────────┘ └────────────────────────────────────────────────┘
```

- Sidebar is fixed at `inset-y-0 z-50` — correct
- Main content offset: `pl-64` — correct
- Page content top padding: `pt-14` — correct for the mobile fixed header; on desktop remove if not needed

### Mobile layout

- Fixed top header: page title + hamburger
- Fixed bottom tab bar: 5 primary modules
- Main content: `pt-14 pb-16` — correct

### One scroll region

Every page must have exactly one scroll region: the `<main>` element. No nested scrolling containers unless the component explicitly requires it (e.g., a virtualized long list). Sidebar must never scroll independently from the content it contains.

### Content width

- Full-width: email inbox table, task list, project list
- Capped width (`max-w-3xl`): detail sheets, forms, memory detail
- Stats/KPI rows: full-width, grid with 3–4 columns

---

## Existing UI components — audit and rules

These are the shadcn/ui primitives present in `/components/ui/`. Rules for each.

### `button.tsx`

Keep all variants: `default`, `destructive`, `outline`, `secondary`, `ghost`, `link`.

Rules:
- Primary action per screen: one `default` variant button maximum
- Destructive actions: always `destructive` variant — never use red text with a ghost button
- Icon-only buttons: always `size="icon"` with `aria-label`
- No gradient backgrounds on any button — banned
- Loading state: show spinner inline, disable the button, do not remove label text

### `badge.tsx`

Use badges for status labels, category chips, and email classification tags.

Rules:
- Triage categories (`actionable`, `fyi`, `ignored`): consistent badge variants
- Never use colored badge backgrounds for decoration — only for semantic status
- Max one badge per list row visible without hover/expand
- Badge text: always `text-xs` uppercase tracked

### `card.tsx`

Cards exist in the current codebase. Use them only for:
- Summary blocks / KPI tiles
- Memory and resource item previews
- People contact cards

Do not use cards as the primary layout unit for list views. Lists with row-by-row items must use the `table.tsx` or a custom list component, not a vertical stack of cards.

Rules:
- Cards must never have colored left-border accents — banned
- Card hover: subtle shadow lift `hover:shadow-md` is acceptable
- Card radius: `rounded-lg` matches current `--radius-lg` — keep

### `dialog.tsx`

Use for confirmations, single-action forms, and destructive action prompts.

Rules:
- Every dialog must have a clear title and a dismiss action
- Dialogs must not be used as primary navigation surfaces
- Max one primary action per dialog

### `sheet.tsx`

Sheets are the primary detail/context mechanism for Rhodey. Currently used in email detail (`email-detail-sheet.tsx`) and mobile navigation. This is correct.

Rules:
- Detail sheets open from the right side by default
- Sheet width: `w-[480px]` on desktop, `w-full` on mobile
- Every sheet must have a title, a close button, and a defined dismiss path
- Sheets should never nest — a sheet opening another sheet is banned

### `table.tsx`

Tables are first-class components in Rhodey. Email inbox, tasks, projects, people, and resources are all table-first.

Rules:
- Every table column must have a clear label
- Use `tabular-nums` on numeric and date columns
- Row hover: `hover:bg-muted/50` — keep consistent across all tables
- Clickable rows must have a visible cursor pointer
- Empty table state: never show a blank table — show a designed empty state with an action

### `tabs.tsx`

Tabs are used in the email module (`email-tabs.tsx`). This pattern is correct for module-level view switching.

Rules:
- Use tabs for switching between views of the same object set (e.g., Inbox / Drafts / Pending)
- Do not use tabs for primary navigation between modules
- Tab labels: sentence case, concise (2–3 words max)

### `input.tsx` and `textarea.tsx`

Rules:
- All inputs must have visible labels — no placeholder-only inputs
- Focus ring must be visible (current `outline-ring/50` in base is acceptable; add `outline-2` on focus)
- Disabled state must be visually distinct
- Error state: red ring + error message immediately below the field

### `skeleton.tsx`

Currently defined as a generic shimmer element. Use it consistently across all async loading states.

Rules:
- Every data-loading view must show a skeleton that mirrors the final layout shape
- Skeleton text should approximate actual line lengths
- Do not show a spinner where a skeleton is possible
- Skeleton shimmer speed: `1.5s ease-in-out infinite` — do not accelerate it

### `sonner.tsx`

Sonner is correctly in place for toasts.

Rules:
- Use toasts only for background/non-critical feedback: synced, refreshed, saved in background
- Never use a toast as the only feedback for a destructive action
- Never use a toast for validation errors
- Toast duration: `3000ms` for info, `5000ms` for warnings

### `avatar.tsx`

Use consistently for user identity across emails, people, and task assignees.

Rules:
- Always provide a fallback with initials
- Avatar size: `h-8 w-8` in list rows, `h-10 w-10` in detail sheets
- No decorative avatar rings or glow effects

### `select.tsx`

Use for filter dropdowns, status changes, and category assignments.

Rules:
- Show current value clearly at rest state
- All selects must have a visible label
- Use `Select` over native `<select>` for consistency

### `separator.tsx`

Use for section breaks within sheets and cards. Use `--border` color only.

---

## Component Patterns

These are utility classes defined in globals.css that apply consistent 
visual treatment across all modules. Every AI agent must use these 
classes instead of writing raw equivalent Tailwind strings.

**card-premium**
Utility class for all lifted surface cards in Rhodey OS.
Applies: white background, 1px border at oklch(0.92), box-shadow 
(card-level depth), border-radius lg, and hover lift (translateY -1px 
with shadow-raised on hover).
Use on: every KPI stat card, project card, person card, resource card, 
and draft card.
Do not compose this manually with raw shadow/border/bg classes.
Note: This supplements the shadcn Card component — use card-premium 
as the outer wrapper className in place of the Card primitive 
when building list-item cards and KPI tiles.

**stat-number**
Utility class for KPI numbers in stat cards.
Applies: 2.25rem size, font-weight 700, tabular-nums, letter-spacing tight.
Color rules (always add a color class alongside stat-number):
- Default / neutral metric → text-foreground
- Primary / actionable metric → text-primary (teal)
- Warning / overdue / idle → text-amber-500
- Error / destructive / failed → text-destructive
- Success / completed → text-primary

**section-label**
Utility class for all secondary category labels, table column headers, 
and module group headings.
Applies: 0.65rem size, uppercase, letter-spacing 0.1em, text-muted-foreground.
Use on: all <th> cells in tables, mission/group section headings, 
"Linked Entities" labels, and any metadata category header.

**Active nav item pattern**
Active sidebar nav links must use border-l-2 border-primary pl-[10px].
This is the teal left-stripe pattern established in layout.tsx.
Never revert to bg-only active states (bg-accent without the left border).
Inactive items use pl-3 to maintain alignment.

**Badge color system**
All classification and status badges across all modules must use 
these exact token combinations. No module may define its own 
alternative badge colors.

| Type | Background | Text | Border |
|---|---|---|---|
| actionable | bg-primary/10 | text-primary | border-primary/20 |
| fyi | bg-blue-500/10 | text-blue-600 | border-blue-500/20 |
| ignored | bg-muted | text-muted-foreground | border-border |
| pending | bg-amber-500/10 | text-amber-600 | border-amber-500/20 |
| destructive | bg-destructive/10 | text-destructive | border-destructive/20 |
| active / in-progress | bg-primary/10 | text-primary | border-primary/20 |
| idle / paused | bg-amber-500/10 | text-amber-600 | border-amber-500/20 |
| done / completed | bg-muted | text-muted-foreground | border-border |

All badges must include: text-xs px-2 py-0.5 rounded-full font-medium 
border alongside the color classes above.

---

## Module components — existing patterns

### Emails module

Existing components:
- `emails-inbox-table.tsx` — primary list view
- `email-detail-sheet.tsx` — right-side sheet for email body and actions
- `email-filters.tsx` — filter bar
- `email-stats.tsx` — KPI summary
- `email-tabs.tsx` — inbox / drafts / pending tab switcher
- `drafts-list.tsx` — draft email list
- `pending-tasks-list.tsx` — extracted pending tasks from emails

This is a well-structured module. The pattern of table + detail sheet + filter bar is the **reference pattern** for all other modules.

Rules:
- Classification badges must be visually distinct: `actionable` → teal, `fyi` → grey, `ignored` → muted
- Email row click must open detail sheet without full page navigation
- Source and timestamp must always be visible in the row
- Draft rows must be visually distinguishable from inbox rows

### Tasks module

Mirror the emails module pattern: list table → detail sheet → filter bar.

Rules:
- Status chips must use the defined status vocabulary (see below)
- Due date column must use tabular-nums
- Overdue tasks must be visually flagged — not with a red text blob, but with a subtle row indicator or badge

### Memories, People, Resources, Projects

All modules must follow the same shell pattern:
1. Filter / search bar at top
2. Table or list view in main area
3. Detail sheet on row click
4. Empty state if no data

---

## Status vocabulary

Consistent across all modules. Map to badge variants:

| Status | Badge variant | Use |
|---|---|---|
| `new` | outline | Just captured, not yet reviewed |
| `triaged` | secondary | Reviewed, categorized, no action yet |
| `pending` | warning/yellow | Waiting for something or someone |
| `in-progress` | accent/teal | Actively being worked |
| `blocked` | destructive | Cannot proceed |
| `done` | secondary muted | Completed |
| `ignored` | muted | Intentionally deprioritized |
| `failed` | destructive | System or execution failure |

Do not invent new statuses in modules. Extend this list in this file first.

---

## Empty states

Every module must have a designed empty state. Required elements:

1. Icon (Lucide, `h-10 w-10 text-muted-foreground`)
2. Heading: what belongs here (`text-lg font-medium`)
3. Description: why it is empty or what to do (`text-sm text-muted-foreground`)
4. Primary action button (if applicable)

Example for empty Tasks:
```
[CheckSquare icon]
No tasks yet
Tasks extracted from emails and captures will appear here.
[+ Add task]
```

Never show a blank white space, a spinner with no timeout, or a raw error code.

---

## Loading states

Skeleton rules per module:

| Module | Skeleton pattern |
|---|---|
| Emails inbox | 6–8 rows: avatar circle + three text bars per row |
| Task list | 5–7 rows: checkbox + two text bars + badge placeholder |
| Memory list | 4–5 rows: heading bar + two body bars |
| People list | 5 rows: avatar + name bar + role bar |
| Detail sheet | Header bar + 3 body sections + button row |

Skeletons must match the exact column structure of the real table.

---

## Demo readiness rules

Because Rhodey may be shown to prospects, these rules apply to all primary screens:

- All primary screens must look intentional with seed data present
- Page titles must be clearly readable in the top header
- Module icons must be recognizable and consistent (Lucide only)
- No raw Supabase IDs visible in any primary UI element
- No TODO comments or placeholder text visible in any rendered view
- All badge and chip variants must render without visual bugs

---

## Color usage rules

- Accent (teal): active nav, primary buttons, active row/tab indicator, chart-1
- Destructive (red): only for errors and irreversible actions — not for warnings
- Muted foreground: secondary text, timestamps, metadata
- Border: table dividers, card edges, separator lines — always `border-border`
- No custom hex values anywhere in component files — only Tailwind token classes

---

## Dark mode rules

The `.dark` class is set by `next-themes`. Both modes must be explicitly tested.

Currently the dark sidebar has `bg-zinc-900` hardcoded. After fixing that (see Navigation section), all surfaces in dark mode must use sidebar and background tokens only — no hardcoded grey values.

Test checklist for dark mode:
- Sidebar text: readable on sidebar background
- Badge text: readable on badge background
- Table row hover: subtle, not blinding
- Sheet content: card background, not pure black
- Input fields: visible border and placeholder
- Buttons: all variants readable

---

## Motion rules

`tw-animate-css` is available. Use it only for these cases:

- Sheet/drawer entrance: `animate-in slide-in-from-right`
- Dialog entrance: `animate-in fade-in zoom-in-95`
- Skeleton shimmer: built into the component
- Tab indicator transition: `transition-transform duration-150`

Banned animations:
- Page-level entrance animations on every render
- Floating or pulsing decorative elements
- Scroll-triggered reveal animations in product views
- Any animation that exceeds `300ms` duration in product UI

---

## Accessibility checklist

- Every icon-only button must have `aria-label`
- Every input must have an associated `<label>` or `aria-label`
- Sheet close button must be keyboard accessible
- Table rows that open detail views must be keyboard navigable
- Focus ring must be visible on all interactive elements
- Color must not be the only indicator of status — always pair with label or icon

---

## Anti-patterns — enforce in code review

Do not ship:
- Hardcoded colour values (`bg-zinc-900`, `text-zinc-400`) in component files — use token classes
- `border-l-4 border-accent` on cards — banned
- Gradient backgrounds on buttons or cards — banned
- Toasts as primary success/error feedback for forms — banned
- Empty tables with no empty state — banned
- Icon-only buttons without `aria-label` — banned
- Multiple primary action buttons on the same screen — banned
- `text-3xl` or above anywhere in the product shell — banned
- Nested sheets or nested modals — banned
- Raw Supabase IDs rendered in visible UI — banned
- Placeholder text as substitute for form labels — banned

---

## Build priority

1. Fix sidebar token references (zinc → sidebar tokens)
2. Add brand accent to `globals.css` light mode primary
3. Normalize chart colors
4. Audit and align badge variants to status vocabulary
5. Add skeleton states to all data-loading views
6. Design empty states for all 7 modules
7. Confirm sheet pattern works across all modules
8. Dark mode audit pass
9. Demo readiness pass — seed data, no raw IDs, all states look complete
