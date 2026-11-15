This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

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

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
