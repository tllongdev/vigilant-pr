# Spec: Comment Voice Personalization

Status: Draft (spec only - do not implement until the test group validates the core `github-watch` flow)
Scope owner: Vigilant PR

## Summary

Add an **optional, opt-in** capability that rephrases the *wording* of posted PR
review comments so they read in the reviewing user's own voice, learned from
their past authored reviews. This is a cosmetic layer only. It never changes
what is flagged, the severity of any finding, or the approve/comment decision.

## Section 1: Context & Constraints

### What already exists (do not change)
- The review engine produces, in a single flow: a set of `Finding`s (each with a
  severity of critical / medium / nit), inline comment bodies, a review summary
  body, and a programmatic decision (`APPROVE` when nothing blocks, `COMMENT`
  when something does; never `REQUEST_CHANGES`).
- Severity and the approve/comment decision are **structural** - computed from
  the findings, not parsed out of comment prose.
- Reviews post under the user's GitHub identity with a hidden signature marker
  (no visible bot disclaimer).
- The engine core is stdlib-only; model calls go over plain HTTP via the
  provider layer. A credential/config store already exists at
  `~/.config/vigilant-pr/` (0600 file, 0700 dir), with `.env` and real env vars
  taking precedence over stored values.

### Decisions already made (do not re-litigate)
- **The adversarial review pipeline is untouchable.** No changes to finding
  detection, severity assignment, the system prompt, or the decision logic.
- **Personalization is voice-only** (how a comment reads), never judgment (what
  is flagged or how severe it is). Judgment-learning was explicitly rejected as
  antithetical to the product's purpose.
- **The severity taxonomy (critical / medium / nit) is a fixed product
  convention** (aligned with established AI review tools such as CodeRabbit).
  Personalization does not alter or reinterpret it.
- Delivery mechanism is a **blind, end-of-pipeline rewrite pass**, not prompt
  changes to the review call. This is chosen specifically so style examples
  cannot bleed into what gets flagged.
- Feature is **opt-in and off by default**. With it off, output is byte-identical
  to today.
- Profile sourcing is **hybrid**: auto-drafted from the user's GitHub history,
  then shown to the user to edit and approve before it is ever used.

### Approaches ruled out (do not re-evaluate)
- Injecting style few-shot examples into the review prompt (single-pass): rejected
  due to judgment bleed.
- Auto-learning applied silently without user approval: rejected (transparency,
  "it quietly changed my reviews" failure mode).
- Learning what to flag / severity calibration from history: rejected outright.

### Constraints
- Read-only use of the user's own `gh` token; only the user's own authored data
  is fetched.
- Stdlib-only core preserved; one additional provider/model call per review at
  most (batched), and only when the feature is enabled.
- Cost and latency of the rewrite pass must be bounded and must not affect users
  who have not opted in.

### Open questions resolved during research
- "Sound like me" = voice, confirmed. Substance stays with the tuned engine.
- Blocking-finding prose (critical / medium) is left **verbatim** by default;
  only nit comments and the review summary are eligible for rewriting. Rewriting
  blocking-finding prose is a separate, explicit opt-in (not part of v1 default).

## Section 2: Requirements

### 2.1 Profile learning (the "read my past reviews" flow)

Behavior: an automated command builds a style profile from the user's recent
authored reviews and asks the user to approve it.

Data flow:
1. Resolve the user's GitHub login from their authenticated token.
2. Find pull requests the user has reviewed, most recent first (via the
   `reviewed-by:<login>` search qualifier, `type:pr`, sorted by most recently
   updated).
3. For each such PR, collect the text the user authored: review summary bodies
   and inline (line-level) review comments, filtered to items whose author is the
   user.
4. Filter to signal: drop empty/textless items; strip quoted reply text; retain
   code and suggestion blocks (they indicate whether the user uses them). Stop
   after collecting a bounded number of the most recent comments (target range
   40-60) or after exhausting available history.
5. Distill the corpus into a compact, human-readable style profile capturing at
   least: tone (blunt vs. hedged), typical comment length, whether the user
   prefixes with conventions like `nit:` / `suggestion:`, whether the user uses
   code-suggestion blocks, and 1-2 representative example lines.
6. Present the profile to the user for edit/approval.
7. On approval, persist the profile to the existing config store, gated behind an
   explicit "enabled" flag.

Edge cases:
- **No `gh` auth / no token:** command reports the same actionable guidance used
  elsewhere and exits without writing a profile.
- **Zero reviewed PRs found:** report "not enough history to learn a voice,"
  write nothing, leave the feature off. Offer the manual alternative (2.4).
- **Thin corpus** (very few or only terse comments): still produce a profile but
  explicitly tell the user it is thin; the approval step is where they decide
  whether it is usable.
- **Rate limiting / API errors mid-collection:** use whatever was collected so
  far if it meets a minimum threshold; otherwise abort cleanly with a message.
- **Non-interactive session:** learning requires approval, so in a non-TTY
  context the command must not silently enable; it either writes a
  pending/unapproved profile or exits with guidance (choose one and document it
  in implementation).

### 2.2 Enablement and configuration

- The feature is **off by default**. A single explicit flag in the config store
  turns it on; it can be turned off at any time.
- When off, the review output path is unchanged and no extra model call is made.
- Configuration lives in the existing store; `.env`/env var overrides follow the
  established precedence (real env > `.env` > store).
- Default rewrite scope: **nit comments + review summary only**. Blocking
  findings (critical / medium) are posted verbatim. A separate, clearly labeled
  option may extend rewriting to all comments; it is off by default and out of
  scope for the initial release.

### 2.3 The rewrite pass (the safety-critical part)

Behavior: after the review has fully completed (findings, severities, decision,
and comment bodies all final), if the feature is enabled, the eligible comment
bodies are rephrased in the user's voice and then posted.

Hard contract (all must hold or the pass falls back to originals):
- **Blind to code:** the rewrite step receives only the final comment strings and
  the style profile. It does not receive the diff or PR contents. It therefore
  cannot introduce a new finding.
- **1:1 mapping:** N eligible comments in, exactly N out. Any count mismatch →
  discard the rewrite, post originals.
- **Content preservation validation**, per comment, before posting:
  - Every backticked/code token, file path, and line reference present in the
    original must be present in the rewrite.
  - Every code-suggestion block in the original must survive verbatim.
  - Rewritten length must stay within a defined band of the original (reject
    shrinkage below or growth above configured thresholds).
  - Any severity label/anchor carried in the comment must survive verbatim.
  - On any failure for a given comment → post that comment's **original** text.
- **Severity and decision are never inputs or outputs** of the rewrite pass.
- **Scope enforcement:** by default only nit comments and the summary are passed
  to the rewrite step; blocking-finding comments bypass it entirely.
- **Batching:** all eligible comments are rewritten in a single model call to
  bound cost/latency to at most +1 call per review.

Guarantee to state in docs and tests: with the feature disabled, output is
byte-identical to today; with it enabled at the default scope, no blocking
finding's wording is ever altered, and any validation failure degrades to the
exact original text.

Edge cases:
- **Rewrite model call fails or times out:** post all originals; do not fail the
  review.
- **Rewrite returns malformed output:** treat as validation failure → originals.
- **Profile enabled but empty/missing:** behave as disabled.
- **Mixed review (some blocking, some nits):** blocking posts verbatim, nits may
  be voiced; a per-comment validation failure only affects that comment.

### 2.4 Manual profile alternative (fallback, low priority)

If history is unusable, allow the user to supply a short free-text style
description (and optionally paste 1-2 example comments) that becomes the profile.
Same enablement, scope, and rewrite guarantees apply. This exists so the feature
degrades gracefully; it is not the primary path.

### 2.5 CLI surface (what, not how)

- A command to learn/refresh the profile from history (automated generation +
  interactive approval).
- A way to view the current profile and whether the feature is enabled.
- A way to enable/disable the feature.
- A way to edit or clear the profile.
- Optionally, learning can be offered during `vigilant init` as an opt-in step.

### 2.6 Acceptance criteria (encode as tests)

1. Feature disabled → posted output is identical to the pre-feature output for
   the same review (byte-for-byte).
2. Feature enabled, default scope → for a review containing critical/medium
   findings, those comment bodies are posted verbatim (unchanged).
3. Rewrite that drops a code token / file path / line ref / suggestion block →
   original is posted for that comment.
4. Rewrite returning wrong comment count → all originals posted.
5. Rewrite model call error/timeout → all originals posted; review still succeeds.
6. Severity assignment and the APPROVE/COMMENT decision are provably independent
   of the rewrite pass (same decision with feature on vs. off for identical
   findings).
7. Learning with zero reviewed PRs → no profile written, feature stays off, clear
   message.
8. Only the user's own authored comments enter the corpus (others' comments on the
   same PRs are excluded).

### 2.7 Parallelizable work

These can proceed independently once the contract in 2.3 is fixed:
- The GitHub history collector (2.1 steps 1-4).
- The profile distiller + approval/store flow (2.1 steps 5-7, 2.2).
- The rewrite pass + validation harness (2.3) - can be built and tested against
  synthetic comment inputs with no dependency on the collector.
- The CLI surface (2.5).

## Out of scope
- Any change to finding detection, severity, the system prompt, or the decision.
- Rewriting blocking-finding prose by default.
- Learning judgment / what-to-flag from history (permanently rejected).
- Cross-user or team-shared profiles.
