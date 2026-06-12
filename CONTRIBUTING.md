# Contributing to AutoMaintainer

First off, thank you for considering contributing to AutoMaintainer! It's people like you that make AutoMaintainer such a great tool.

## Where do I go from here?

If you've noticed a bug or have a feature request, make sure to check if there's already an [issue](https://github.com/PxA-Labs/AutoMaintainer/issues) for it. If not, feel free to open a new one!

## Setting up your development environment

1. **Fork** the repo on GitHub.
2. **Clone** the project to your own machine.
3. Install dependencies for the backend and frontend:
   - Backend: `pip install -r backend/requirements.txt`
   - Frontend: `npm install` in the `dashboard/` directory.

## Architecture

Before making changes to the LangGraph agents, please read our [ARCHITECTURE.md](./ARCHITECTURE.md) to understand the state flow and interactions with the GitHub API.

## Submitting Changes

1. Create a new branch: `git checkout -b my-feature-branch`.
2. Make your changes.
3. Test your changes to ensure you haven't broken existing functionality.
4. Push your branch to your fork: `git push origin my-feature-branch`.
5. Submit a pull request!

## Code Style

- We use standard Python conventions (PEP 8) for the backend.
- We use Prettier/ESLint rules for the Next.js frontend.

We look forward to reviewing your contributions!
