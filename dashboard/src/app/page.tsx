"use client";

import { DashboardShell } from "@/components/dashboard/DashboardShell";
import ErrorBoundary from "@/components/ErrorBoundary";

export default function Home() {
  return (
    <ErrorBoundary title="Dashboard Error">
      {/* AuthProvider is mounted once, in the root layout. */}
      <DashboardShell />
    </ErrorBoundary>
  );
}