# Changelog

All notable changes to the AutoMaintainer autonomous AI software engineering platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### CI/CD & Infrastructure

- **backend**: Run the adk and rag extra tests and pin black and flake8 ([#538](https://github.com/PxA-Labs/AutoMaintainer/pull/538)) - @purvanshjoshi
- Fix action pins, least-privilege tokens, frontend unit tests and keep-alive diagnostics ([#496](https://github.com/PxA-Labs/AutoMaintainer/pull/496)) - @purvanshjoshi
- End-to-end docker compose smoke test (api, worker, beat, redis) ([#468](https://github.com/PxA-Labs/AutoMaintainer/pull/468)) - @purvanshjoshi
### Refactoring

- **agents**: Make importing agents free of I/O and build the graph on first use ([#515](https://github.com/PxA-Labs/AutoMaintainer/pull/515)) - @purvanshjoshi
- **agents**: JSON-only PM/Maintainer decisions; fix(observability): bound metric labels ([#465](https://github.com/PxA-Labs/AutoMaintainer/pull/465)) (closes [#403](https://github.com/PxA-Labs/AutoMaintainer/issues/403), [#404](https://github.com/PxA-Labs/AutoMaintainer/issues/404)) - @purvanshjoshi
### Testing & Quality Assurance

- **engine**: Add a concurrency and isolation suite for AutoMaintainerEngine ([#522](https://github.com/PxA-Labs/AutoMaintainer/pull/522)) - @purvanshjoshi
- **redteam**: Add prompt-injection red-team fixtures and CI job ([#485](https://github.com/PxA-Labs/AutoMaintainer/pull/485)) - @purvanshjoshi
- Add an OpenAPI contract snapshot, a dashboard-to-API contract check and RLS tests on real PostgreSQL ([#467](https://github.com/PxA-Labs/AutoMaintainer/pull/467)) - @purvanshjoshi
- **dashboard**: Enforce jsx-a11y lint rules and add a Playwright smoke suite in CI ([#461](https://github.com/PxA-Labs/AutoMaintainer/pull/461)) - @purvanshjoshi
### Features

- **rag**: Add engine retriever injection and a Terraform switch to deploy code retrieval ([#541](https://github.com/PxA-Labs/AutoMaintainer/pull/541)) - @purvanshjoshi
- **observability**: Record run, token and cost metrics from the worker and require Redis AUTH ([#540](https://github.com/PxA-Labs/AutoMaintainer/pull/540)) - @purvanshjoshi
- **rag**: Wire code retrieval into the PM and the ADK Reviewer behind a flag ([#539](https://github.com/PxA-Labs/AutoMaintainer/pull/539)) - @purvanshjoshi
- **runners**: Add a Cloud Run Jobs sandbox backend for verification ([#537](https://github.com/PxA-Labs/AutoMaintainer/pull/537)) - @purvanshjoshi
- **observability**: Export metrics to Cloud Monitoring, with alerts and a dashboard ([#536](https://github.com/PxA-Labs/AutoMaintainer/pull/536)) - @purvanshjoshi
- **rag**: Pgvector code index with org-scoped RLS, chunker, embedders and indexing task ([#535](https://github.com/PxA-Labs/AutoMaintainer/pull/535)) - @purvanshjoshi
- **specialists**: Read-only ADK Reviewer behind reviewer_backend (default langgraph) ([#534](https://github.com/PxA-Labs/AutoMaintainer/pull/534)) - @purvanshjoshi
- **evals**: End-to-end evaluation of the whole engine with oracle, noop, replay and live modes ([#531](https://github.com/PxA-Labs/AutoMaintainer/pull/531)) - @purvanshjoshi
- **infra**: Deploy to Google Cloud with Terraform, keyless CI and Cloud Trace ([#530](https://github.com/PxA-Labs/AutoMaintainer/pull/530)) - @purvanshjoshi
- **runs**: Record the run trigger and allow one active run per issue ([#523](https://github.com/PxA-Labs/AutoMaintainer/pull/523)) (closes [#518](https://github.com/PxA-Labs/AutoMaintainer/issues/518)) - @purvanshjoshi
- **sdk**: Package the engine runtime as an installable automaintainer wheel ([#521](https://github.com/PxA-Labs/AutoMaintainer/pull/521)) - @purvanshjoshi
- **engine**: Add AutoMaintainerEngine with per-engine context, isolated runs and cancellation ([#519](https://github.com/PxA-Labs/AutoMaintainer/pull/519)) - @purvanshjoshi
- **webhooks**: Start a run from the trigger label and answer 401 to bad signatures ([#517](https://github.com/PxA-Labs/AutoMaintainer/pull/517)) (closes [#190](https://github.com/PxA-Labs/AutoMaintainer/issues/190)) - @purvanshjoshi
- **engine**: Add AutoMaintainerConfig engine configuration schema ([#516](https://github.com/PxA-Labs/AutoMaintainer/pull/516)) - @purvanshjoshi
- **db**: Import run history from pre-multi-tenant databases ([#514](https://github.com/PxA-Labs/AutoMaintainer/pull/514)) (closes [#482](https://github.com/PxA-Labs/AutoMaintainer/issues/482)) - @purvanshjoshi
- Audit repository merge-policy changes ([#513](https://github.com/PxA-Labs/AutoMaintainer/pull/513)) - @purvanshjoshi
- Per-repository merge policy under the MERGE_POLICY ceiling ([#495](https://github.com/PxA-Labs/AutoMaintainer/pull/495)) - @purvanshjoshi
- **agents**: Record a merge_decisions audit row per merge decision ([#486](https://github.com/PxA-Labs/AutoMaintainer/pull/486)) - @purvanshjoshi
- **verification**: Record an audit row per verification ([#483](https://github.com/PxA-Labs/AutoMaintainer/pull/483)) - @purvanshjoshi
- **agents**: Show verification results to the Maintainer and hold over-limit changes for a human ([#478](https://github.com/PxA-Labs/AutoMaintainer/pull/478)) - @purvanshjoshi
- **dashboard**: Wire the GitHub App install flow into the UI and fix IDE accessibility ([#477](https://github.com/PxA-Labs/AutoMaintainer/pull/477)) (closes [#471](https://github.com/PxA-Labs/AutoMaintainer/issues/471)) - @purvanshjoshi
- **verification**: Add a Docker sandbox backend for change verification ([#476](https://github.com/PxA-Labs/AutoMaintainer/pull/476)) - @purvanshjoshi
- **evals**: Grow the Implementer eval set from 10 to 30 tasks with categories and leak guards ([#469](https://github.com/PxA-Labs/AutoMaintainer/pull/469)) (closes [#414](https://github.com/PxA-Labs/AutoMaintainer/issues/414)) - @purvanshjoshi
- **agents**: Verify each agent change with the repository's tests and gate MERGE_POLICY=ci on it ([#460](https://github.com/PxA-Labs/AutoMaintainer/pull/460)) - @purvanshjoshi
- **dashboard**: Comprehensive professional UI/UX redesign & architecture decomposition ([#245](https://github.com/PxA-Labs/AutoMaintainer/pull/245)) - @purvanshjoshi
- Web-first SaaS architecture with multi-tenancy, Celery queue, GitHub App auth, and observability ([#167](https://github.com/PxA-Labs/AutoMaintainer/pull/167)) - @purvanshjoshi
- **ci**: Implement industry-standard changelog sync, PR title linter, and release automation ([#176](https://github.com/PxA-Labs/AutoMaintainer/pull/176)) (closes [#158](https://github.com/PxA-Labs/AutoMaintainer/issues/158)) - @purvanshjoshi
### Architecture & Core Agent Platform

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

### Bug Fixes

- **engine**: Finish the SDK follow-ups: completed-write cancel race, FakeDB unique index, startup .env, distribution name ([#529](https://github.com/PxA-Labs/AutoMaintainer/pull/529)) (closes [#524](https://github.com/PxA-Labs/AutoMaintainer/issues/524)) - @purvanshjoshi
- **deploy**: Bound the /start worker probe and read REDIS_URL from Render's connectionString ([#512](https://github.com/PxA-Labs/AutoMaintainer/pull/512)) - @purvanshjoshi
- **ide**: Give each user their own view of their IDE working branch ([#507](https://github.com/PxA-Labs/AutoMaintainer/pull/507)) (closes [#505](https://github.com/PxA-Labs/AutoMaintainer/issues/505)) - @purvanshjoshi
- Start the GitHub App install with the Supabase session only ([#509](https://github.com/PxA-Labs/AutoMaintainer/pull/509)) - @purvanshjoshi
- **dashboard**: Label every run error code and stop retrying refused terminal sessions ([#508](https://github.com/PxA-Labs/AutoMaintainer/pull/508)) - @purvanshjoshi
- Keep IDE writes off the default branch and enforce route auth and tenant checks ([#504](https://github.com/PxA-Labs/AutoMaintainer/pull/504)) (closes [#139](https://github.com/PxA-Labs/AutoMaintainer/issues/139), [#210](https://github.com/PxA-Labs/AutoMaintainer/issues/210)) - @purvanshjoshi
- **agents**: Fail runs on clone failure and report missing target issues as INVALID_TARGET_ISSUE ([#497](https://github.com/PxA-Labs/AutoMaintainer/pull/497)) (closes [#138](https://github.com/PxA-Labs/AutoMaintainer/issues/138), [#142](https://github.com/PxA-Labs/AutoMaintainer/issues/142)) - @purvanshjoshi
- **agents**: Require a trusted trigger to auto-merge and check limits before commit ([#489](https://github.com/PxA-Labs/AutoMaintainer/pull/489)) (closes [#488](https://github.com/PxA-Labs/AutoMaintainer/issues/488)) - @purvanshjoshi
- **verification**: Hold test-runner configuration for a human and stop root modules shadowing pytest ([#484](https://github.com/PxA-Labs/AutoMaintainer/pull/484)) - @purvanshjoshi
- **db**: Replace the non-re-runnable schema file with versioned migrations ([#475](https://github.com/PxA-Labs/AutoMaintainer/pull/475)) (closes [#473](https://github.com/PxA-Labs/AutoMaintainer/issues/473)) - @purvanshjoshi
- **workspace**: Key clones by GitHub repository id for agent runs and IDE routes ([#474](https://github.com/PxA-Labs/AutoMaintainer/pull/474)) (closes [#392](https://github.com/PxA-Labs/AutoMaintainer/issues/392)) - @purvanshjoshi
- **db**: Add runs.repo_name and check tenant references in run and IDE policies ([#472](https://github.com/PxA-Labs/AutoMaintainer/pull/472)) - @purvanshjoshi
- **github-app**: Verified installation linking, minimum-permission tokens and tenant isolation hardening ([#466](https://github.com/PxA-Labs/AutoMaintainer/pull/466)) - @purvanshjoshi
- **backend**: Run PyGithub calls off the event loop, fix trace-field logging, harden LocalRunner ([#458](https://github.com/PxA-Labs/AutoMaintainer/pull/458)) - @purvanshjoshi
- **changelog**: Keep earlier entries and stop stacking separators ([#457](https://github.com/PxA-Labs/AutoMaintainer/pull/457)) - @purvanshjoshi
- Add signed GitHub webhook ingestion ([#216](https://github.com/PxA-Labs/AutoMaintainer/pull/216)) (closes [#215](https://github.com/PxA-Labs/AutoMaintainer/issues/215)) - @SriRamkunamsetty
- **dashboard**: Handle selectedRunId reset on new run and guest auth prompt on start ([#247](https://github.com/PxA-Labs/AutoMaintainer/pull/247)) - @purvanshjoshi
- **backend**: Include target host diagnostic in healthz_supabase error response ([#244](https://github.com/PxA-Labs/AutoMaintainer/pull/244)) - @purvanshjoshi
### Deployment & System Stability

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

- **env**: List the ADK Reviewer and code retrieval settings in backend/.env.example ([#571](https://github.com/PxA-Labs/AutoMaintainer/pull/571)) - @purvanshjoshi
- **readme**: Add an SDK example for the ADK Reviewer and retrieval, and state the full enable rule ([#570](https://github.com/PxA-Labs/AutoMaintainer/pull/570)) - @purvanshjoshi
- **architecture**: Add the non-fatal reviewer error, the transport consequence and new limitations ([#569](https://github.com/PxA-Labs/AutoMaintainer/pull/569)) - @purvanshjoshi
- **adk**: Document operating the ADK Reviewer: prerequisites, checks, rollback, troubleshooting ([#568](https://github.com/PxA-Labs/AutoMaintainer/pull/568)) - @purvanshjoshi
- **adk**: Correct the Reviewer's tool signature, budgets, error event and spike options ([#567](https://github.com/PxA-Labs/AutoMaintainer/pull/567)) - @purvanshjoshi
- **rag**: Correct the fallback cases, egress, batch size and recall options ([#566](https://github.com/PxA-Labs/AutoMaintainer/pull/566)) - @purvanshjoshi
- **contributing**: Describe workflow triggers, stacked PRs and the changelog flow ([#564](https://github.com/PxA-Labs/AutoMaintainer/pull/564)) - @purvanshjoshi
- Install the server extra when running db-migrate ([#563](https://github.com/PxA-Labs/AutoMaintainer/pull/563)) - @purvanshjoshi
- **deploy**: Add a troubleshooting table for the Google Cloud deployment ([#562](https://github.com/PxA-Labs/AutoMaintainer/pull/562)) - @purvanshjoshi
- **deploy**: Document the worker VM as a single point of failure ([#561](https://github.com/PxA-Labs/AutoMaintainer/pull/561)) - @purvanshjoshi
- **deploy**: Document Google Cloud teardown and Terraform state recovery ([#560](https://github.com/PxA-Labs/AutoMaintainer/pull/560)) - @purvanshjoshi
- **deploy**: Add a rollback runbook for the Google Cloud deployment ([#559](https://github.com/PxA-Labs/AutoMaintainer/pull/559)) - @purvanshjoshi
- **deploy**: Add a Google Cloud architecture diagram ([#558](https://github.com/PxA-Labs/AutoMaintainer/pull/558)) - @purvanshjoshi
- **deploy**: Correct the Google Cloud runbook's variables, worker restart and zone ([#557](https://github.com/PxA-Labs/AutoMaintainer/pull/557)) - @purvanshjoshi
- **terraform**: Fix stale runbook references and variable descriptions ([#556](https://github.com/PxA-Labs/AutoMaintainer/pull/556)) - @purvanshjoshi
- Refresh the documentation for the engine, google cloud deployment, rag and sandboxed verification ([#542](https://github.com/PxA-Labs/AutoMaintainer/pull/542)) - @purvanshjoshi
- **readme**: Add a roadmap section ([#528](https://github.com/PxA-Labs/AutoMaintainer/pull/528)) - @purvanshjoshi
- **readme**: Remove the project status banner ([#520](https://github.com/PxA-Labs/AutoMaintainer/pull/520)) - @purvanshjoshi
- Correct stale statements about verification and fix a changelog heading ([#463](https://github.com/PxA-Labs/AutoMaintainer/pull/463)) - @purvanshjoshi
- Describe the system as implemented, retire ISSUES.md, refresh README and SECURITY ([#462](https://github.com/PxA-Labs/AutoMaintainer/pull/462)) (closes [#408](https://github.com/PxA-Labs/AutoMaintainer/issues/408)) - @purvanshjoshi
- **readme**: Add release and changelog section ([#177](https://github.com/PxA-Labs/AutoMaintainer/pull/177)) - @purvanshjoshi
- docs(changelog): update CHANGELOG.md for PR #172 ([#173](https://github.com/PxA-Labs/AutoMaintainer/pull/173)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #171 ([#172](https://github.com/PxA-Labs/AutoMaintainer/pull/172)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #170 ([#171](https://github.com/PxA-Labs/AutoMaintainer/pull/171)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #169 ([#170](https://github.com/PxA-Labs/AutoMaintainer/pull/170)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #168 ([#169](https://github.com/PxA-Labs/AutoMaintainer/pull/169)) - @github-actions[bot]
- docs(changelog): update CHANGELOG.md for PR #166 ([#168](https://github.com/PxA-Labs/AutoMaintainer/pull/168)) - @github-actions[bot]
- docs: remove Hugging Face frontmatter and add Vercel/Render deployment guide ([#161](https://github.com/PxA-Labs/AutoMaintainer/pull/161)) - @purvanshjoshi
- docs: Update README with recent architectural features ([#48](https://github.com/PxA-Labs/AutoMaintainer/pull/48)) - @purvanshjoshi

### Dependency Updates & Maintenance

- **deps**: Add adk and rag extras and the Cloud Monitoring metrics exporter ([#533](https://github.com/PxA-Labs/AutoMaintainer/pull/533)) - @purvanshjoshi
- **deps**: Bump langgraph from 1.2.20 to 1.2.21 in /backend ([#503](https://github.com/PxA-Labs/AutoMaintainer/pull/503)) - @dependabot[bot]
- **deps-dev**: Bump pytest-asyncio from 1.4.1.post1 to 1.5.0 in /backend ([#501](https://github.com/PxA-Labs/AutoMaintainer/pull/501)) - @dependabot[bot]
- **deps**: Bump fastapi from 0.162.5 to 0.162.7 in /backend ([#499](https://github.com/PxA-Labs/AutoMaintainer/pull/499)) - @dependabot[bot]
- **deps-dev**: Bump @playwright/test from 1.64.1 to 1.65.0 in /dashboard ([#498](https://github.com/PxA-Labs/AutoMaintainer/pull/498)) - @dependabot[bot]
- **deps**: Bump actions/first-interaction from 3.0.0 to 3.1.0 ([#502](https://github.com/PxA-Labs/AutoMaintainer/pull/502)) - @dependabot[bot]
- **deps**: Keep mcp on 1.x and drop unused server-only ([#456](https://github.com/PxA-Labs/AutoMaintainer/pull/456)) - @purvanshjoshi
- **deps**: Bump langchain from 1.4.2 to 1.4.3 in /backend ([#305](https://github.com/PxA-Labs/AutoMaintainer/pull/305)) - @dependabot[bot]
- **deps**: Pin backend dependencies for reproducible installs ([#204](https://github.com/PxA-Labs/AutoMaintainer/pull/204)) (closes [#121](https://github.com/PxA-Labs/AutoMaintainer/issues/121)) - @SriRamkunamsetty
- **deps**: Bump actions/setup-python from 5.4.0 to 7.0.0 ([#285](https://github.com/PxA-Labs/AutoMaintainer/pull/285)) - @dependabot[bot]
- **deps**: Bump docker/metadata-action from 5.7.0 to 6.2.0 ([#276](https://github.com/PxA-Labs/AutoMaintainer/pull/276)) - @dependabot[bot]
- **deps**: Bump amannn/action-semantic-pull-request from 5.5.3 to 6.1.1 ([#264](https://github.com/PxA-Labs/AutoMaintainer/pull/264)) - @dependabot[bot]
- **deps-dev**: Bump eslint-config-next from 16.3.3 to 16.3.4 in /dashboard ([#249](https://github.com/PxA-Labs/AutoMaintainer/pull/249)) - @dependabot[bot]
- **deps**: Bump release-drafter/release-drafter from 6.4.0 to 7.7.0 ([#228](https://github.com/PxA-Labs/AutoMaintainer/pull/228)) - @dependabot[bot]
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

---

[Unreleased]: https://github.com/PxA-Labs/AutoMaintainer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/PxA-Labs/AutoMaintainer/releases/tag/v1.0.0
