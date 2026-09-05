"use client";

import { useState, useEffect } from "react";
import { getBackendUrl } from "@/lib/config";

export function useSupabaseHealth() {
  const [isUnreachable, setIsUnreachable] = useState(false);

  useEffect(() => {
    let active = true;
    let timeoutId: NodeJS.Timeout | null = null;
    let currentController: AbortController | null = null;

    const checkSupabaseHealth = async () => {
      if (!active) return;

      if (currentController) {
        currentController.abort();
      }

      const controller = new AbortController();
      currentController = controller;

      const abortTimeout = setTimeout(() => {
        controller.abort();
      }, 8000);

      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/healthz/supabase`, {
          signal: controller.signal,
        });
        clearTimeout(abortTimeout);

        if (!res.ok) {
          setIsUnreachable(true);
        } else {
          setIsUnreachable(false);
        }
      } catch (err: unknown) {
        clearTimeout(abortTimeout);
        if (err instanceof Error && err.name !== "AbortError") {
          console.error("Failed to check Supabase health:", err);
          setIsUnreachable(true);
        }
      } finally {
        if (active) {
          timeoutId = setTimeout(checkSupabaseHealth, 15000);
        }
      }
    };

    checkSupabaseHealth();

    return () => {
      active = false;
      if (currentController) {
        currentController.abort();
      }
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  }, []);

  return isUnreachable;
}
