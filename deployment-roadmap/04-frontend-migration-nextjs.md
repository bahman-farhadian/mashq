# 04 — Frontend Migration: `web/app.js` → Next.js

## 4.1 View-by-view mapping

The legacy UI has four views, all driven by one selector cascade
(`User → Language → Level → Part of speech`). Next.js's App Router maps
onto this almost directly:

| Legacy view | Route | Component type | Notes |
|---|---|---|---|
| Practice (setup + live session + summary) | `app/practice/page.tsx` | Client Component | Stateful, low-latency, talks to `practice` API continuously — see §4.3 |
| Report (embedded in Practice setup, not a separate page) | rendered inline in `app/practice/page.tsx`, behind the same selector | Client Component (data changes as the cascade changes) | Preserve the "no separate Load Report click" behavior — it's a deliberate UX guarantee in the current app, not a gap to fill in |
| Word Lists (editor) | `app/lists/page.tsx` | Client Component (form-heavy) with Server Component data fetch for the initial list | Django admin (see `03`) becomes the *bulk/authoring* editor; this page stays as the *lightweight, in-flow* personal-override editor the legacy app documents |
| About | `app/about/page.tsx` | Server Component (static content) | No reason for this to be client-rendered |

The selector cascade itself (`User → Language → Level → Part of speech`)
becomes a shared component (`components/MaterialSelector.tsx`) used by both
`practice` and `lists` pages, mirroring how the legacy app already reuses
the exact same cascade in both views.

## 4.2 Theming: dark and light, done at the token layer

- Design tokens (color, spacing, radii) as CSS custom properties on
  `:root`, ported from the current `web/style.css` — get the existing
  visual language into tokens first, then add a dark palette, rather than
  designing a new look from scratch. This preserves the actual product the
  students are meant to be presenting.
- `next-themes` for the toggle: handles `prefers-color-scheme` as the
  default, persists an explicit user choice, and avoids the
  flash-of-incorrect-theme problem on first paint (it injects the theme
  class before hydration).
- Every token gets both a light and dark value defined up front — no
  component ships hardcoded colors that only work in one theme. This is a
  lint-enforceable rule (a small custom ESLint rule or a stylelint check
  against raw hex/`rgb()` values outside the tokens file) worth adding as
  a CI gate specifically because "we forgot to theme one component" is the
  single most common dark-mode bug in real projects — worth teaching as
  its own lesson.

## 4.3 The interaction-model rules that must survive verbatim

The current README's "Speech and interaction model" section is a list of
precise behavioral guarantees, not incidental implementation detail. Each
one needs an explicit test in the Next.js port (component/interaction
tests, e.g. Playwright or Testing Library), not just "it happened to still
work":

- Typing is allowed while the prompt is being spoken; submit, action
  buttons, replay, end, and view navigation are all locked until speech
  finishes; the typed text is preserved across that lock.
- Speech requests are serialized — never two overlapping audio streams.
- Every stage plays its prompt automatically; Replay is always available;
  audio is never conditionally disabled by stage.
- After answer submission, the UI stays interaction-locked through the
  answer request, any feedback speech, and the card transition — no
  double-submit window.

These map cleanly onto a small client-side state machine
(`useSessionMachine` hook or similar) that owns exactly these lock states,
kept separate from data-fetching concerns (which live in the API client /
query layer below) so the interaction-lock logic can be unit-tested without
a live backend.

## 4.4 Data layer

- A typed API client (`lib/api.ts`) wrapping `fetch`, generated from (or
  kept in sync with) the DRF OpenAPI schema — this is also the natural
  place session-auth cookies/CSRF tokens are attached.
- TanStack Query (React Query) for client-side request state on the
  Practice view — not because this app is high-frequency real-time (it
  isn't; it's one request per learner action), but because it gives
  request de-duplication, retry/backoff, and cache invalidation for free,
  and it's a standard, teachable pattern rather than hand-rolled fetch
  state.
- No WebSocket layer is needed for the practice flow itself — every
  interaction in the legacy app is already strictly request/response
  (submit answer → get next question). Real-time *dashboards* are Grafana's
  job (`05`), not the learner-facing app's.

## 4.5 What changes vs. what's preserved

| Preserved exactly | Changed |
|---|---|
| Selector cascade, no "which file" decision | Rendering: SSR/static for content, CSR for the live session |
| No separate Report page/click | Styling system: hand-rolled CSS → design tokens + component library |
| Speech/interaction locking rules (§4.3) | Auth: browser now authenticates as a real logged-in user, not a trusted-client `user` string typed into a form |
| "No reveal, flag, mastery, or manual-drill shortcuts" during a session | Editor: heavy bulk editing moves to Django admin; in-app editor stays lightweight |
| Practice/Report/Word Lists/About as the four views | Dark/light theming added throughout |

Next: [05 — Data Platform: Mongo → ClickHouse → Grafana](05-data-platform-mongo-clickhouse-grafana.md).
