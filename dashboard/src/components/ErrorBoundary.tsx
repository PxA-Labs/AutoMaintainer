"use client";

import React, { Component, ErrorInfo, ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

export interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((props: { error: Error; reset: () => void }) => ReactNode);
  onReset?: () => void;
  title?: string;
}

export interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public state: ErrorBoundaryState = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("Uncaught error caught by ErrorBoundary:", error, errorInfo);
  }

  public handleReset = (): void => {
    this.props.onReset?.();
    this.setState({ hasError: false, error: null });
  };

  public render(): ReactNode {
    if (this.state.hasError) {
      if (typeof this.props.fallback === "function" && this.state.error) {
        const renderFallback = this.props.fallback as (props: { error: Error; reset: () => void }) => ReactNode;
        return renderFallback({ error: this.state.error, reset: this.handleReset });
      }

      if (this.props.fallback && typeof this.props.fallback !== "function") {
        return this.props.fallback;
      }

      return (
        <div className="p-6 bg-zinc-900/80 border border-zinc-800 rounded-xl text-zinc-100 flex flex-col items-center justify-center text-center my-4">
          <div className="w-12 h-12 rounded-xl bg-red-500/20 text-red-400 flex items-center justify-center mb-4">
            <AlertTriangle className="w-6 h-6" />
          </div>
          <h3 className="text-lg font-semibold mb-2">
            {this.props.title || "Something went wrong"}
          </h3>
          <p className="text-sm text-zinc-400 max-w-md mb-4 font-mono break-all bg-zinc-950/60 p-3 rounded-lg border border-zinc-800/80">
            {this.state.error?.message || "An unexpected rendering error occurred."}
          </p>
          <button
            onClick={this.handleReset}
            className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 rounded-lg text-sm font-medium hover:bg-indigo-500/20 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            Try Again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
