# Changelog

All notable changes to the AutoMaintainer autonomous AI software engineering platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Architecture & Core Agent Platform

- revert: Web-first SaaS architecture changes ([#166](https://github.com/PxA-Labs/AutoMaintainer/pull/166)) - @purvanshjoshi
- revert: Web-first SaaS architecture changes ([#166](https://github.com/PxA-Labs/AutoMaintainer/pull/166)) - @purvanshjoshi
- feat: Phase 4 - Integrate Native AST Mapping ([#25](https://github.com/PxA-Labs/AutoMaintainer/pull/25)) - @purvanshjoshi
- feat: Phase 3 - Build Split-Pane Web IDE Interface ([#24](https://github.com/PxA-Labs/AutoMaintainer/pull/24)) - @purvanshjoshi
- feat: Phase 2 File System Tree API ([#23](https://github.com/PxA-Labs/AutoMaintainer/pull/23)) - @purvanshjoshi
- feat: Phase 1 Native AST Codebase Analyzer ([#22](https://github.com/PxA-Labs/AutoMaintainer/pull/22)) - @purvanshjoshi
- feat: Implement Targeted Issue Mode ([#7](https://github.com/PxA-Labs/AutoMaintainer/pull/7)) - @purvanshjoshi

### Web IDE & Developer Experience

- fix: Resolve hardcoded websocket url in terminal ([#46](https://github.com/PxA-Labs/AutoMaintainer/pull/46)) - @purvanshjoshi
- feat: Terminal Context Awareness & Polish (Part 4) ([#45](https://github.com/PxA-Labs/AutoMaintainer/pull/45)) - @purvanshjoshi
- feat: Frontend xterm.js Integration & UI Toggle ([#44](https://github.com/PxA-Labs/AutoMaintainer/pull/44)) - @purvanshjoshi
- feat: Backend PTY Foundation for Interactive Terminal ([#43](https://github.com/PxA-Labs/AutoMaintainer/pull/43)) - @purvanshjoshi
- feat: Upgrade WebIDE to Monaco VS Code Engine ([#41](https://github.com/PxA-Labs/AutoMaintainer/pull/41)) - @purvanshjoshi
- feat: Integrate GitNexus Code Intelligence ([#9](https://github.com/PxA-Labs/AutoMaintainer/pull/9)) - @purvanshjoshi

### Security & Governance

- security(openssf): pin GitHub Action commit SHAs and enforce least-privilege permissions ([#165](https://github.com/PxA-Labs/AutoMaintainer/pull/165)) - @purvanshjoshi
- chore(deps): bump ossf/scorecard-action from 2.4.0 to 2.4.4 ([#154](https://github.com/PxA-Labs/AutoMaintainer/pull/154)) - @dependabot[bot]
- fix(docs): update OpenSSF Scorecard badge and viewer URL ([#145](https://github.com/PxA-Labs/AutoMaintainer/pull/145)) - @purvanshjoshi
- feat(ci): configure OpenSSF Scorecard on-demand dispatch and security status badges ([#144](https://github.com/PxA-Labs/AutoMaintainer/pull/144)) - @purvanshjoshi
- feat(ci): add OpenSSF Scorecard, Gitleaks, Security Audit, and Multi-OS Pytest Matrix ([#136](https://github.com/PxA-Labs/AutoMaintainer/pull/136)) - @purvanshjoshi
- test: fix websocket origin security assertion ([#122](https://github.com/PxA-Labs/AutoMaintainer/pull/122)) - @manyagkarle13
- Fix: Resolved Identified Bugs, Security Vulnerabilities, and Cross-Platform Issues ([#56](https://github.com/PxA-Labs/AutoMaintainer/pull/56)) - @purvanshjoshi
- fix(security): Resolve CodeQL path traversal vulnerabilities ([#47](https://github.com/PxA-Labs/AutoMaintainer/pull/47)) - @purvanshjoshi

### Bug Fixes & System Stability

- fix(deploy): configure static export out directory for Docker and Vercel ([#164](https://github.com/PxA-Labs/AutoMaintainer/pull/164)) - @purvanshjoshi
- fix(vercel): add root package.json and vercel.json for monorepo auto-detection ([#163](https://github.com/PxA-Labs/AutoMaintainer/pull/163)) - @purvanshjoshi
- fix(vercel): standardize next.config.ts and vercel.json for Vercel deployment ([#162](https://github.com/PxA-Labs/AutoMaintainer/pull/162)) - @purvanshjoshi
- Fix #141: Reset all dashboard states (logs, pipeline, activity, agentStatus) on new agent run ([#149](https://github.com/PxA-Labs/AutoMaintainer/pull/149)) - @rahul05au
- Fix #143: Randomize sandbox repo name in test_fs_api.py to prevent workspace collision ([#147](https://github.com/PxA-Labs/AutoMaintainer/pull/147)) - @rahul05au
- fix: resolve concurrency, token leakage, file descriptor race, and Windows compatibility ([#137](https://github.com/PxA-Labs/AutoMaintainer/pull/137)) - @purvanshjoshi
- Fixes #117: Token count is double-counted in frontend when replaying historical Supabase logs. ([#130](https://github.com/PxA-Labs/AutoMaintainer/pull/130)) - @manyagkarle13
- fix: remove duplicate BaseModel import in main.py ([#129](https://github.com/PxA-Labs/AutoMaintainer/pull/129)) - @gopal-labs
- fix: address codebase findings on ipv6 local loopback, emoji labels, issue categorization, and request timeouts ([#109](https://github.com/PxA-Labs/AutoMaintainer/pull/109)) - @archittmittal
- fix: implement supabase keepalive ping, connection health checks, and dashboard warnings ([#108](https://github.com/PxA-Labs/AutoMaintainer/pull/108)) - @archittmittal
- fix: resolve outdated function signature in test_e2e.py and configure pytest suite ([#107](https://github.com/PxA-Labs/AutoMaintainer/pull/107)) - @archittmittal
- fix: resolve lost websocket logs, missing agent loop cancellation, and webide port mismatch ([#106](https://github.com/PxA-Labs/AutoMaintainer/pull/106)) - @archittmittal
- fix(ci): simplify auto-changelog workflow to prevent duplicate sections ([#70](https://github.com/PxA-Labs/AutoMaintainer/pull/70)) - @purvanshjoshi
- fix(ci): fix auto-changelog workflow to preserve existing content ([#69](https://github.com/PxA-Labs/AutoMaintainer/pull/69)) - @purvanshjoshi
- fix(ci): format backend files with black to resolve lint errors ([#59](https://github.com/PxA-Labs/AutoMaintainer/pull/59)) - @purvanshjoshi
- fix: Resolve infinite LangGraph state duplication bug ([#49](https://github.com/PxA-Labs/AutoMaintainer/pull/49)) - @purvanshjoshi
- fix: resolve critical agent race conditions, task cancellation, and local WebIDE port mismatch ([#39](https://github.com/PxA-Labs/AutoMaintainer/pull/39)) - @archittmittal

### CI/CD, Infrastructure & Deployment

- feat(deploy): add Vercel and Render deployment configs ([#160](https://github.com/PxA-Labs/AutoMaintainer/pull/160)) - @purvanshjoshi
- chore(deps): bump actions/checkout from 4 to 7 ([#156](https://github.com/PxA-Labs/AutoMaintainer/pull/156)) - @dependabot[bot]
- chore(deps): bump actions/setup-node from 4 to 7 ([#153](https://github.com/PxA-Labs/AutoMaintainer/pull/153)) - @dependabot[bot]
- chore(deps): bump actions/setup-python from 5 to 7 ([#151](https://github.com/PxA-Labs/AutoMaintainer/pull/151)) - @dependabot[bot]
- chore(deps): bump github/codeql-action from 4.37.3 to 4.37.6 ([#104](https://github.com/PxA-Labs/AutoMaintainer/pull/104)) - @dependabot[bot]
- chore(deps): bump docker/build-push-action from 5 to 7 ([#99](https://github.com/PxA-Labs/AutoMaintainer/pull/99)) - @dependabot[bot]
- chore(deps): bump actions/setup-python from 5 to 7 ([#97](https://github.com/PxA-Labs/AutoMaintainer/pull/97)) - @dependabot[bot]
- chore(deps): bump actions/first-interaction from 1 to 3 ([#96](https://github.com/PxA-Labs/AutoMaintainer/pull/96)) - @dependabot[bot]
- chore(deps): bump docker/login-action from 3 to 4 ([#95](https://github.com/PxA-Labs/AutoMaintainer/pull/95)) - @dependabot[bot]
- chore(deps): bump release-drafter/release-drafter from 7.6.0 to 7.7.0 ([#94](https://github.com/PxA-Labs/AutoMaintainer/pull/94)) - @dependabot[bot]
- chore(deps): bump docker/setup-buildx-action from 3 to 4 ([#93](https://github.com/PxA-Labs/AutoMaintainer/pull/93)) - @dependabot[bot]
- chore(deps): bump github/codeql-action from 3 to 4.37.3 ([#92](https://github.com/PxA-Labs/AutoMaintainer/pull/92)) - @dependabot[bot]
- chore(deps): bump actions/setup-node from 4 to 7 ([#80](https://github.com/PxA-Labs/AutoMaintainer/pull/80)) - @dependabot[bot]
- chore(deps): bump release-drafter/release-drafter from 6 to 7.6.0 ([#78](https://github.com/PxA-Labs/AutoMaintainer/pull/78)) - @dependabot[bot]
- chore(deps): bump actions/checkout from 4 to 7 ([#77](https://github.com/PxA-Labs/AutoMaintainer/pull/77)) - @dependabot[bot]
- chore(deps): bump docker/metadata-action from 5 to 6 ([#76](https://github.com/PxA-Labs/AutoMaintainer/pull/76)) - @dependabot[bot]
- chore(deps): bump pascalgn/size-label-action from 0.5.1 to 0.5.7 ([#74](https://github.com/PxA-Labs/AutoMaintainer/pull/74)) - @dependabot[bot]
- feat(ci): add auto-updating changelog workflow ([#68](https://github.com/PxA-Labs/AutoMaintainer/pull/68)) - @purvanshjoshi
- ci: Automate GHCR Docker Image Publishing ([#50](https://github.com/PxA-Labs/AutoMaintainer/pull/50)) - @purvanshjoshi

### Documentation & Blueprints

- docs(changelog): update CHANGELOG.md for PR #169 ([#170](https://github.com/PxA-Labs/AutoMaintainer/pull/170)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #168 ([#169](https://github.com/PxA-Labs/AutoMaintainer/pull/169)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #166 ([#168](https://github.com/PxA-Labs/AutoMaintainer/pull/168)) - @github-actions[bot]
- docs: remove Hugging Face frontmatter and add Vercel/Render deployment guide ([#161](https://github.com/PxA-Labs/AutoMaintainer/pull/161)) - @purvanshjoshi
- docs: Update README with recent architectural features ([#48](https://github.com/PxA-Labs/AutoMaintainer/pull/48)) - @purvanshjoshi

### Dependency Updates & Maintenance

- chore(deps-dev): bump eslint-config-next from 16.3.0 to 16.3.1 in /dashboard ([#155](https://github.com/PxA-Labs/AutoMaintainer/pull/155)) - @dependabot[bot]
- chore(deps): bump next from 16.3.0 to 16.3.1 in /dashboard ([#150](https://github.com/PxA-Labs/AutoMaintainer/pull/150)) - @dependabot[bot]
- chore(deps): bump @supabase/supabase-js from 2.112.1 to 2.112.3 in /dashboard ([#128](https://github.com/PxA-Labs/AutoMaintainer/pull/128)) - @dependabot[bot]
- chore(deps-dev): bump @types/node from 26.1.2 to 26.2.0 in /dashboard ([#127](https://github.com/PxA-Labs/AutoMaintainer/pull/127)) - @dependabot[bot]
- chore(deps): bump framer-motion from 12.42.2 to 13.1.0 in /dashboard ([#126](https://github.com/PxA-Labs/AutoMaintainer/pull/126)) - @dependabot[bot]
- chore(deps): bump lucide-react from 1.28.0 to 1.31.0 in /dashboard ([#125](https://github.com/PxA-Labs/AutoMaintainer/pull/125)) - @dependabot[bot]
- chore(deps): bump lucide-react from 1.27.0 to 1.28.0 in /dashboard ([#103](https://github.com/PxA-Labs/AutoMaintainer/pull/103)) - @dependabot[bot]
- chore(deps): bump next from 16.2.12 to 16.3.0 in /dashboard ([#102](https://github.com/PxA-Labs/AutoMaintainer/pull/102)) - @dependabot[bot]
- chore(deps): bump @supabase/supabase-js from 2.111.0 to 2.112.1 in /dashboard ([#101](https://github.com/PxA-Labs/AutoMaintainer/pull/101)) - @dependabot[bot]
- chore(deps-dev): bump eslint-config-next from 16.2.12 to 16.3.0 in /dashboard ([#98](https://github.com/PxA-Labs/AutoMaintainer/pull/98)) - @dependabot[bot]
- chore(deps): bump lucide-react from 1.17.0 to 1.27.0 in /dashboard ([#91](https://github.com/PxA-Labs/AutoMaintainer/pull/91)) - @dependabot[bot]
- chore(deps): bump @supabase/supabase-js from 2.110.9 to 2.111.0 in /dashboard ([#89](https://github.com/PxA-Labs/AutoMaintainer/pull/89)) - @dependabot[bot]
- chore(deps-dev): bump @tailwindcss/postcss from 4.3.0 to 4.3.3 in /dashboard ([#87](https://github.com/PxA-Labs/AutoMaintainer/pull/87)) - @dependabot[bot]
- chore(deps-dev): bump tailwindcss from 4.3.0 to 4.3.3 in /dashboard ([#85](https://github.com/PxA-Labs/AutoMaintainer/pull/85)) - @dependabot[bot]
- chore(deps): bump framer-motion from 12.40.0 to 12.42.2 in /dashboard ([#83](https://github.com/PxA-Labs/AutoMaintainer/pull/83)) - @dependabot[bot]
- chore(deps): bump @supabase/supabase-js from 2.108.1 to 2.110.9 in /dashboard ([#82](https://github.com/PxA-Labs/AutoMaintainer/pull/82)) - @dependabot[bot]
- chore(deps-dev): bump @types/node from 20.19.41 to 26.1.2 in /dashboard ([#81](https://github.com/PxA-Labs/AutoMaintainer/pull/81)) - @dependabot[bot]
- chore(deps): bump react and @types/react in /dashboard ([#79](https://github.com/PxA-Labs/AutoMaintainer/pull/79)) - @dependabot[bot]
- chore(deps): bump next from 16.2.6 to 16.2.12 in /dashboard ([#75](https://github.com/PxA-Labs/AutoMaintainer/pull/75)) - @dependabot[bot]
- chore(deps-dev): bump eslint-config-next from 16.2.6 to 16.2.12 in /dashboard ([#73](https://github.com/PxA-Labs/AutoMaintainer/pull/73)) - @dependabot[bot]

---

## [1.0.0] - 2026-06-20 - Initial Open Source Release

### Autonomous Multi-Agent Architecture
- **5-Agent Hierarchical Team**: Fully integrated Architect, Visionary, Reviewer, Implementer, and Maintainer agents.
- **LangGraph State Orchestration**: Cyclic state machine managing issue triage, branch generation, code synthesis, iterative verification, and automated PR submission.
- **Llama 3.3-70B Engine via Groq**: Low-latency, high-accuracy inference pipeline for code generation and review.
- **Supabase Realtime Telemetry**: High-throughput pub/sub event stream powering real-time status, activity feeds, and agent logs.
- **GitNexus Code Intelligence**: AST-aware knowledge graph and symbol indexer enabling whole-repo semantic context.
- **Pro Monaco WebIDE**: In-browser VSCode-grade editor with multi-file tabs, syntax highlighting, and interactive terminal (xterm.js).
- **Automated GitHub Integration**: Native issue reading, PR generation, CI verification loops, and automated branch mergers.
