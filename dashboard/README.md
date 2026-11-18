# AutoMaintainer dashboard

The AutoMaintainer web dashboard, a [Next.js](https://nextjs.org) application
built as a static export (`output: 'export'` in `next.config.ts`). It starts
and stops runs, shows live logs through Supabase Realtime, run history, the
org admin page and the web IDE.

## Getting started

CI builds and tests the dashboard with Node.js 22.x.

Create `dashboard/.env.local` with the public build-time settings:

```bash
NEXT_PUBLIC_SUPABASE_URL="https://your-project.supabase.co"
NEXT_PUBLIC_SUPABASE_ANON_KEY="your_supabase_anon_key"
NEXT_PUBLIC_BACKEND_URL="http://localhost:8000"
```

Use the Supabase **anon** key, never the service key: these values are
compiled into the public bundle.

Then install the dependencies and start the development server:

```bash
npm ci
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The pages are under
`src/app/` (for example `src/app/page.tsx`) and the components under
`src/components/`. The backend must be running for runs, logs and the IDE;
see the [repository README](../README.md#3-run-backend-fastapi).

## Tests

```bash
npm run lint      # ESLint, including the jsx-a11y recommended rules
npm test          # Vitest unit/component tests (src/**/*.test.ts[x])
npm run test:e2e  # Playwright smoke tests (e2e/)
```

The e2e suite builds the static export with test-only public env, serves
`out/` from `e2e/serve.mjs` on http://127.0.0.1:4173 and drives it in
Chromium. The backend and Supabase are mocked in the browser with
`page.route`; any other outbound request is aborted, so no real backend,
Supabase or GitHub is contacted. First run `npx playwright install chromium`.
Set `E2E_SKIP_BUILD=1` to reuse an existing e2e build, or
`E2E_BROWSER_CHANNEL=chrome` to use an installed Chrome instead of the
Playwright browser.

## Build and deployment

`npm run build` writes the static site to `out/`. Deploy it to Vercel with
the root directory set to `dashboard/`; [`vercel.json`](./vercel.json) sets
the security headers. The API can also serve the same build at `/`. See
[docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) for both options and the
build-time variables.
