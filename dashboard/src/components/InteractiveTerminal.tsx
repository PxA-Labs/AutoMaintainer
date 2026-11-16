"use client";

import React, { useEffect, useRef, useState } from "react";
import { Terminal } from "xterm";
import { FitAddon } from "@xterm/addon-fit";
import { getAccessToken } from "@/lib/api";
import { getBackendUrl } from "@/lib/config";
import { normalizeRepoSlug } from "@/lib/repo";
import { useAuth } from "@/lib/auth";
import {
  buildTerminalWsUrl,
  classifyTerminalClose,
  reconnectDelay,
  RECONNECT_MAX_ATTEMPTS,
} from "@/lib/terminal";
import { useDebouncedValue } from "@/lib/hooks/use-debounced-value";
import "xterm/css/xterm.css";

interface InteractiveTerminalProps {
  repoUrl?: string;
}

/** Wait this long after the repository changes before opening a new shell. */
const REPO_DEBOUNCE_MS = 500;

export default function InteractiveTerminal({ repoUrl }: InteractiveTerminalProps) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // What the user can do about `error`: reconnect by hand, sign in again
  // (4401), or nothing (4403: retrying gets the same answer).
  const [recovery, setRecovery] = useState<"reconnect" | "signin" | "none">("reconnect");
  const [reconnectKey, setReconnectKey] = useState(0);
  const { signInWithGitHub } = useAuth();

  // Reconnect only when the *normalised* repository actually changes, and
  // only once it has settled; every new connection spawns a server shell.
  const repoSlug = useDebouncedValue(normalizeRepoSlug(repoUrl), REPO_DEBOUNCE_MS);

  useEffect(() => {
    if (!terminalRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      theme: {
        background: "#1e1e1e",
        foreground: "#d4d4d4",
      },
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      fontSize: 14,
    });

    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(terminalRef.current);
    fit.fit();

    const wsUrl = buildTerminalWsUrl(getBackendUrl(), repoSlug);

    // Set on cleanup. Socket callbacks and timers can fire after the effect is
    // torn down; writing to a disposed xterm throws, so every write and state
    // update is guarded by this flag.
    let disposed = false;
    let socket: WebSocket | null = null;
    let attempts = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const write = (text: string) => {
      if (!disposed) term.write(text);
    };
    const isOpen = () => socket !== null && socket.readyState === WebSocket.OPEN;

    const dataSub = term.onData((data) => {
      if (isOpen()) socket!.send(data);
    });

    const handleResize = () => {
      if (disposed || !isOpen()) return;
      fit.fit();
      socket!.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    };
    window.addEventListener("resize", handleResize);

    const requireSignIn = (message: string) => {
      setRecovery("signin");
      setError("Sign in required");
      write(`\r\n\x1b[31m[${message}]\x1b[0m\r\n`);
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      setRecovery("reconnect");
      if (attempts >= RECONNECT_MAX_ATTEMPTS) {
        setError("Disconnected");
        write("\r\n\x1b[31m[Connection lost. Use Reconnect to try again.]\x1b[0m\r\n");
        return;
      }
      const delay = reconnectDelay(attempts);
      attempts += 1;
      setError("Reconnecting");
      write(`\r\n\x1b[33m[Connection closed. Reconnecting in ${Math.round(delay / 1000)}s...]\x1b[0m\r\n`);
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void connect();
      }, delay);
    };

    // The handshake carries a Supabase token as a subprotocol: browsers cannot
    // set an Authorization header on a WebSocket, and a query parameter would
    // be recorded in proxy and server access logs.
    const connect = async () => {
      const token = await getAccessToken();
      if (disposed) return;

      if (!token) {
        setIsConnected(false);
        requireSignIn("You must be signed in to use the terminal");
        return;
      }

      const opened = new WebSocket(wsUrl, ["bearer", token]);
      socket = opened;

      opened.onopen = () => {
        if (disposed) return;
        attempts = 0;
        setIsConnected(true);
        setError(null);
        opened.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      };

      opened.onmessage = (event) => {
        if (typeof event.data === "string") write(event.data);
      };

      opened.onerror = () => {
        if (disposed) return;
        setIsConnected(false);
      };

      opened.onclose = (event) => {
        if (disposed) return;
        setIsConnected(false);
        // The backend refuses a handshake with 4401 (no valid session) or
        // 4403 (terminal disabled, user not allowed, or not this org's
        // repository); older backends sent 1008 for both. Retrying cannot
        // help, so only other closes (1006 and the like) reconnect.
        switch (classifyTerminalClose(event.code)) {
          case "signin":
            requireSignIn("Your session has expired. Sign in again to use the terminal");
            return;
          case "forbidden":
            setRecovery("none");
            setError("Terminal unavailable");
            write(
              "\r\n\x1b[31m[Terminal disabled or not allowed for this repository]\x1b[0m\r\n"
            );
            return;
          case "policy":
            setRecovery("reconnect");
            setError("Not authorized");
            write("\r\n\x1b[31m[Connection refused: not authorized for this terminal]\x1b[0m\r\n");
            return;
          case "retry":
            scheduleReconnect();
        }
      };
    };

    void connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      window.removeEventListener("resize", handleResize);
      if (socket) {
        // Detach handlers first so the close event cannot schedule a reconnect
        // or write into the terminal disposed below.
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
      dataSub.dispose();
      term.dispose();
    };
  }, [repoSlug, reconnectKey]);

  return (
    <div className="flex flex-col h-full w-full bg-[#1e1e1e] border-l border-zinc-800">
      <div className="flex items-center justify-between px-4 py-2 bg-zinc-900 border-b border-zinc-800 text-xs text-zinc-400">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
          <span className="uppercase font-semibold tracking-wider">Terminal</span>
        </div>
        <div className="flex items-center gap-2">
          {isConnected ? (
            <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span> Connected</span>
          ) : error === "Reconnecting" ? (
            <span className="flex items-center gap-1.5 text-amber-400"><span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse"></span> Reconnecting...</span>
          ) : error ? (
            <>
              <span className="flex items-center gap-1.5 text-red-400"><span className="w-2 h-2 rounded-full bg-red-500"></span> {error}</span>
              {recovery === "signin" ? (
                // Returns to this page after OAuth (lib/auth-return).
                <button onClick={() => void signInWithGitHub()} className="ml-2 px-2 py-0.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded transition-colors border border-zinc-700">Sign in</button>
              ) : recovery === "reconnect" ? (
                <button onClick={() => setReconnectKey(k => k + 1)} className="ml-2 px-2 py-0.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded transition-colors border border-zinc-700">Reconnect</button>
              ) : null}
            </>
          ) : (
            <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-zinc-500"></span> Connecting...</span>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-hidden p-2" ref={terminalRef}></div>
    </div>
  );
}
