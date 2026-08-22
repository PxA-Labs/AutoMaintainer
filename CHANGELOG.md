# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- feat(deploy): add Vercel and Render deployment configs ([#160](https://github.com/PxA-Labs/AutoMaintainer/pull/160)) - @purvanshjoshi
- feat(ci): configure OpenSSF Scorecard on-demand dispatch and security status badges ([#144](https://github.com/PxA-Labs/AutoMaintainer/pull/144)) - @purvanshjoshi
- feat(ci): add OpenSSF Scorecard, Gitleaks, Security Audit, and Multi-OS Pytest Matrix ([#136](https://github.com/PxA-Labs/AutoMaintainer/pull/136)) - @purvanshjoshi
- Auto-updating changelog workflow for merged PRs (#68)
- Security issue documentation for exposed API keys (#60)
- Code quality issue documentation (#61)
- Test file issue documentation (#62)
- Frontend code quality issue documentation (#63)
- Dockerfile configuration issue documentation (#64)
- Architecture issue documentation (#65)
- CI/CD improvement issue documentation (#66)
- Documentation improvement issue documentation (#67)

### Changed
- Backend CI formatting fixes (#59)
- Supabase Realtime ecosystem migration
- GitNexus code intelligence integration
- Web IDE implementation with Monaco editor
- Interactive terminal with xterm.js

### Fixed
- fix(deploy): configure static export out directory for Docker and Vercel ([#164](https://github.com/PxA-Labs/AutoMaintainer/pull/164)) - @purvanshjoshi
- fix(vercel): add root package.json and vercel.json for monorepo auto-detection ([#163](https://github.com/PxA-Labs/AutoMaintainer/pull/163)) - @purvanshjoshi
- fix(vercel): standardize next.config.ts and vercel.json for Vercel deployment ([#162](https://github.com/PxA-Labs/AutoMaintainer/pull/162)) - @purvanshjoshi
- fix(dashboard): reset states on new run and align action buttons ([#149](https://github.com/PxA-Labs/AutoMaintainer/pull/149)) - @contributor
- test: randomize workspace test paths and cleanup post-execution ([#147](https://github.com/PxA-Labs/AutoMaintainer/pull/147)) - @contributor
- fix(docs): update OpenSSF Scorecard badge and viewer URL ([#145](https://github.com/PxA-Labs/AutoMaintainer/pull/145)) - @purvanshjoshi
- fix: resolve concurrency, token leakage, file descriptor race, and Windows compatibility ([#137](https://github.com/PxA-Labs/AutoMaintainer/pull/137)) - @purvanshjoshi
- Auto-changelog workflow to preserve existing content (#69)
- CI lint-and-check failures on Black formatting (#58)
- Supabase database inactivity issues (#57)
- CORS misconfiguration in production (#51)
- Implementer commits dummy Python file (#52)
- WebSocket log streaming issues (#40)

### Security
- security(openssf): pin GitHub Action commit SHAs and enforce least-privilege permissions ([#165](https://github.com/PxA-Labs/AutoMaintainer/pull/165)) - @purvanshjoshi

### Documentation
- docs: remove Hugging Face frontmatter and add Vercel/Render deployment guide ([#161](https://github.com/PxA-Labs/AutoMaintainer/pull/161)) - @purvanshjoshi
- Contributing guidelines and code standards (#67)
- Architecture documentation
- API documentation
- Deployment guide

### Maintenance
- chore(deps): bump actions/checkout from 4 to 7 ([#156](https://github.com/PxA-Labs/AutoMaintainer/pull/156)) - @dependabot[bot]
- chore(deps-dev): bump eslint-config-next from 16.3.0 to 16.3.1 in /dashboard ([#155](https://github.com/PxA-Labs/AutoMaintainer/pull/155)) - @dependabot[bot]
- chore(deps): bump ossf/scorecard-action from 2.4.0 to 2.4.4 ([#154](https://github.com/PxA-Labs/AutoMaintainer/pull/154)) - @dependabot[bot]
- chore(deps): bump actions/setup-node from 4 to 7 ([#153](https://github.com/PxA-Labs/AutoMaintainer/pull/153)) - @dependabot[bot]
- chore(deps): bump actions/setup-python from 5 to 7 ([#151](https://github.com/PxA-Labs/AutoMaintainer/pull/151)) - @dependabot[bot]
- chore(deps): bump next from 16.3.0 to 16.3.1 in /dashboard ([#150](https://github.com/PxA-Labs/AutoMaintainer/pull/150)) - @dependabot[bot]
- chore(deps): bump framer-motion from 12.42.2 to 13.1.0 in /dashboard ([#126](https://github.com/PxA-Labs/AutoMaintainer/pull/126)) - @dependabot[bot]
- chore(deps-dev): bump @types/node from 26.1.2 to 26.2.0 in /dashboard ([#127](https://github.com/PxA-Labs/AutoMaintainer/pull/127)) - @dependabot[bot]
- chore(deps): bump lucide-react from 1.28.0 to 1.31.0 in /dashboard ([#125](https://github.com/PxA-Labs/AutoMaintainer/pull/125)) - @dependabot[bot]
- chore(deps): bump react and @types/react in /dashboard ([#79](https://github.com/PxA-Labs/AutoMaintainer/pull/79)) - @dependabot[bot]
- chore(deps): bump lucide-react from 1.27.0 to 1.28.0 in /dashboard ([#103](https://github.com/PxA-Labs/AutoMaintainer/pull/103)) - @dependabot[bot]
- chore(deps-dev): bump eslint-config-next from 16.2.12 to 16.3.0 in /dashboard ([#98](https://github.com/PxA-Labs/AutoMaintainer/pull/98)) - @dependabot[bot]
- chore(deps): bump actions/setup-python from 5 to 7 ([#97](https://github.com/PxA-Labs/AutoMaintainer/pull/97)) - @dependabot[bot]
- chore(deps): bump docker/build-push-action from 5 to 7 ([#99](https://github.com/PxA-Labs/AutoMaintainer/pull/99)) - @dependabot[bot]
- chore(deps): bump lucide-react from 1.17.0 to 1.27.0 in /dashboard ([#91](https://github.com/PxA-Labs/AutoMaintainer/pull/91)) - @dependabot[bot]
- chore(deps): bump release-drafter/release-drafter from 7.6.0 to 7.7.0 ([#94](https://github.com/PxA-Labs/AutoMaintainer/pull/94)) - @dependabot[bot]
- chore(deps): bump docker/setup-buildx-action from 3 to 4 ([#93](https://github.com/PxA-Labs/AutoMaintainer/pull/93)) - @dependabot[bot]
- chore(deps): bump @supabase/supabase-js from 2.110.9 to 2.111.0 in /dashboard ([#89](https://github.com/PxA-Labs/AutoMaintainer/pull/89)) - @dependabot[bot]
- chore(deps): bump pascalgn/size-label-action from 0.5.1 to 0.5.7 ([#74](https://github.com/PxA-Labs/AutoMaintainer/pull/74)) - @dependabot[bot]
- GitHub Actions workflow improvements
- Docker containerization updates
- Dependency updates

## [1.0.0] - 2026-06-20

### Added
- Initial release of AutoMaintainer
- 5-agent hierarchy (Architect, Visionary, Reviewer, Implementer, Maintainer)
- Native GitHub integration with Issues and PRs
- LangGraph agent orchestration
- FastAPI backend with WebSocket support
- Next.js dashboard with real-time monitoring
- GitNexus MCP integration for code intelligence
- Supabase Realtime for telemetry streaming
- Web IDE with Monaco editor
- Interactive terminal with xterm.js
- Docker deployment support
- Hugging Face Spaces deployment

### Changed
- Migrated from WebSocket to Supabase Realtime architecture
- Upgraded to Llama 3.3-70b-versatile model
- Implemented tree-sitter AST parsing

### Fixed
- Multiple security vulnerabilities
- Cross-platform compatibility issues
- CI/CD pipeline reliability

### Documentation
- Comprehensive README with setup instructions
- Architecture documentation
- Contributing guidelines
- Security policy

### Maintenance
- GitHub Actions CI/CD workflows
- Docker containerization
- Dependency management
