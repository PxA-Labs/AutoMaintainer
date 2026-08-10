# AutoMaintainer — Issue Tracker

A living document tracking all identified bugs and enhancements.
Open 2–3 issues daily, work on them, and mark progress here.

Legend: `[ ]` = Not opened | `[o]` = Opened on GitHub | `[/]` = In Progress | `[x]` = Resolved

---

## 🔴 Critical Bugs

| # | Title | Priority | Status | Fork Issue | Upstream Issue |
|---|-------|----------|--------|------------|----------------|
| 1 | CORS misconfiguration blocks Hugging Face deployments | P0 | `[o]` | [#2](https://github.com/archittmittal/AutoMaintainer/issues/2) | [#51](https://github.com/PxA-Labs/AutoMaintainer/issues/51) |
| 2 | Implementer commits dummy code instead of real file changes | P0 | `[o]` | [#3](https://github.com/archittmittal/AutoMaintainer/issues/3) | [#52](https://github.com/PxA-Labs/AutoMaintainer/issues/52) |
| 3 | `/tmp` repo clones never cleaned up — disk exhaustion | P1 | `[ ]` | — | — |
| 4 | Log stream has no auto-scroll | P1 | `[ ]` | — | — |

---

## 🟡 UX Enhancements

| # | Title | Priority | Status | Fork Issue | Upstream Issue |
|---|-------|----------|--------|------------|----------------|
| 5 | System Health widget shows hardcoded fake metrics | P2 | `[ ]` | — | — |
| 6 | Refreshing page wipes all session logs & pipeline state | P2 | `[ ]` | — | — |
| 7 | Rate-limit error strings get committed to GitHub as code | P1 | `[ ]` | — | — |
| 8 | Interactive Terminal only accessible from Web IDE tab | P2 | `[ ]` | — | — |
| 9 | WebIDE shows confusing error before agents clone the repo | P2 | `[ ]` | — | — |
| 10 | Brainstormer always creates Issues with generic title | P2 | `[ ]` | — | — |

---

## 🔵 Infra / Security

| # | Title | Priority | Status | Fork Issue | Upstream Issue |
|---|-------|----------|--------|------------|----------------|
| 11 | Dockerfile pins `gitnexus@latest` — non-reproducible builds | P2 | `[ ]` | — | — |
| 12 | No input validation on `repo_name` in `/start` — SSRF risk | P1 | `[ ]` | — | — |

---

## Daily Log

| Date | Issues Opened | Issues Resolved |
|------|---------------|-----------------|
| 2026-06-12 | Fork [#2](https://github.com/archittmittal/AutoMaintainer/issues/2), [#3](https://github.com/archittmittal/AutoMaintainer/issues/3) · Upstream [#51](https://github.com/PxA-Labs/AutoMaintainer/issues/51), [#52](https://github.com/PxA-Labs/AutoMaintainer/issues/52) | — |
