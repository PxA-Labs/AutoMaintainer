"use client";

import { DashboardShell } from "@/components/dashboard/DashboardShell";
import ErrorBoundary from "@/components/ErrorBoundary";
import { AuthProvider } from "@/lib/auth";

export default function Home() {
  return (
    <ErrorBoundary title="Dashboard Error">
      <AuthProvider>
        <DashboardShell />
      </AuthProvider>
    </ErrorBoundary>
  );
}