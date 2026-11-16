/**
 * Labels and help text for run error codes.
 *
 * The backend reports run errors as ErrorEvents (backend/agents.py), stored as
 * `logs` rows with log_type "error" and the event in `metadata`:
 * `{ error_code, message, fatal }`. `fatal: false` events are warnings: the
 * run carries on (REPO_SYNC_FAILED, for example, only means the cached clone
 * was used).
 *
 * The table lives in run-error-codes.json so the backend contract tests can
 * check it covers every code the backend emits (see run-errors.test.ts and
 * backend/tests/contract/test_dashboard_error_codes.py).
 */

import RUN_ERROR_CODES from "./run-error-codes.json";

export interface RunErrorInfo {
  label: string;
  help: string;
}

const KNOWN: Readonly<Record<string, RunErrorInfo>> = RUN_ERROR_CODES;

/** Every code the dashboard has a label for. */
export const KNOWN_RUN_ERROR_CODES: readonly string[] = Object.keys(KNOWN);

// Codes are SCREAMING_SNAKE_CASE identifiers. Anything else (a non-string, a
// huge string, markup) is not echoed back to the user.
const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;

/**
 * The label and help text for `code`. An unknown or malformed code gets a
 * generic description instead of throwing or rendering raw input; a
 * well-formed unknown code is still shown so it can be reported.
 */
export function describeRunError(code: unknown): RunErrorInfo & { code: string } {
  const safe = typeof code === "string" && CODE_PATTERN.test(code) ? code : "UNKNOWN";
  if (Object.prototype.hasOwnProperty.call(KNOWN, safe)) {
    return { code: safe, ...KNOWN[safe] };
  }
  return {
    code: safe,
    label: "Unrecognised error",
    help: "This dashboard does not know this error code yet; the message has the details.",
  };
}

/** What the log views show for one ErrorEvent row. */
export interface RunErrorLog {
  msg: string;
  help: string;
  color: string;
}

/**
 * Format an ErrorEvent log row's metadata for display. Fatal errors (the run
 * failed) are red; non-fatal ones (logged, the run continues) are amber.
 * `fallbackMessage` is the row's own message, used when the metadata has none.
 */
export function formatRunErrorLog(metadata: unknown, fallbackMessage: string): RunErrorLog {
  const meta =
    metadata && typeof metadata === "object" ? (metadata as Record<string, unknown>) : {};
  const info = describeRunError(meta.error_code);
  const message = typeof meta.message === "string" && meta.message ? meta.message : fallbackMessage;
  const fatal = meta.fatal !== false;
  return {
    msg: `${fatal ? "" : "Warning: "}${info.label} [${info.code}]: ${message}`,
    help: info.help,
    color: fatal ? "text-red-400" : "text-amber-400",
  };
}
