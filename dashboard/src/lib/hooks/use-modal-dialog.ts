"use client";

import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusableWithin(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
}

/**
 * Keyboard behaviour for a modal dialog (WAI-ARIA dialog pattern):
 *
 * - on open, focus moves into the dialog (an `autoFocus` child wins; else the
 *   first focusable element; else the container itself, which needs
 *   tabIndex={-1});
 * - Tab / Shift+Tab wrap around inside the dialog instead of escaping to the
 *   page behind it;
 * - Escape calls `onClose`;
 * - on close, focus returns to the element that opened the dialog.
 *
 * The caller still renders role="dialog", aria-modal and aria-labelledby.
 * `onClose` is read through a ref, so an inline arrow function does not
 * re-run the effect (which would steal focus back on every render).
 */
export function useModalDialog(
  containerRef: RefObject<HTMLElement | null>,
  isOpen: boolean,
  onClose: () => void
): void {
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!isOpen) return;
    const container = containerRef.current;
    if (!container) return;

    // The element to return focus to. If focus is already inside the dialog
    // (a child used autoFocus, which React applies before effects run), the
    // opener is unknown and focus is simply left where it lands on close.
    const active = document.activeElement;
    const opener =
      active instanceof HTMLElement && active !== document.body && !container.contains(active)
        ? active
        : null;

    if (!container.contains(active)) {
      (focusableWithin(container)[0] ?? container).focus();
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusableWithin(container);
      if (items.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !container.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !container.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };

    // Listen on the document, not the dialog: a click on the backdrop drops
    // focus to <body>, and Escape / Tab must still work from there.
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // Restore focus only if it is still inside the (closing) dialog or was
      // dropped to <body>; never yank it away from somewhere the user moved it.
      const current = document.activeElement;
      if (
        opener &&
        opener.isConnected &&
        (!current || current === document.body || container.contains(current))
      ) {
        opener.focus();
      }
    };
  }, [containerRef, isOpen]);
}
