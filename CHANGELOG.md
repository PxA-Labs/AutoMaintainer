# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- CI lint-and-check failures on Black formatting (#58)
- Supabase database inactivity issues (#57)
- CORS misconfiguration in production (#51)
- Implementer commits dummy Python file (#52)
- WebSocket log streaming issues (#40)

### Documentation
- Contributing guidelines and code standards (#67)
- Architecture documentation
- API documentation
- Deployment guide

### Maintenance
- GitHub Actions workflow improvements
- Docker configuration updates
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
