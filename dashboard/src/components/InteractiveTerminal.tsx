"use client";

import React, { useEffect, useRef, useState } from "react";
import { Terminal } from "xterm";
import { FitAddon } from "@xterm/addon-fit";
import { getAccessToken } from "@/lib/api";
import "xterm/css/xterm.css";

interface InteractiveTerminalProps {
  repoUrl?: string;
}

export default function InteractiveTerminal({ repoUrl }: InteractiveTerminalProps) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const terminalInstance = useRef<Terminal | null>(null);
  const fitAddon = useRef<FitAddon | null>(null);
  const ws = useRef<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconnectKey, setReconnectKey] = useState(0);

  useEffect(() => {
    if (!terminalRef.current) return;

    // Initialize xterm
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

    terminalInstance.current = term;
    fitAddon.current = fit;

    let backendUrl = "http://localhost:8000";
    if (process.env.NEXT_PUBLIC_BACKEND_URL) {
      backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
    } else if (typeof window !== "undefined") {
      const port = window.location.port;
      const hostname = window.location.hostname;
      if ((hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") && port !== "8000") {
        const formattedHost = (hostname === "::1" || hostname === "[::1]") ? "[::1]" : hostname;
        backendUrl = `${window.location.protocol}//${formattedHost}:8000`;
      } else {
        backendUrl = `${window.location.protocol}//${window.location.host}`;
      }
    }
    
    const protocol = backendUrl.startsWith("https") ? "wss:" : "ws:";
    const host = backendUrl.replace(/^https?:\/\//, "");
    
    let wsUrl = `${protocol}//${host}/api/terminal/ws`;
    if (repoUrl) {
      wsUrl += `?repo_url=${encodeURIComponent(repoUrl)}`;
    }

    // The handshake carries a Supabase token as a subprotocol: browsers cannot
    // set an Authorization header on a WebSocket, and a query parameter would
    // be recorded in proxy and server access logs.
    let socket: WebSocket | null = null;
    let cancelled = false;

    const isOpen = () => socket !== null && socket.readyState === WebSocket.OPEN;

    // Handle user input
    term.onData((data) => {
      if (isOpen()) {
        socket!.send(data);
      }
    });

    // Handle window resize
    const handleResize = () => {
      if (fitAddon.current && terminalInstance.current && isOpen()) {
        fitAddon.current.fit();
        const { cols, rows } = terminalInstance.current;
        socket!.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    };

    window.addEventListener("resize", handleResize);

    (async () => {
      const token = await getAccessToken();
      if (cancelled) return;

      if (!token) {
        setIsConnected(false);
        setError("Sign in required");
        term.write("\r\n\x1b[31m[You must be signed in to use the terminal]\x1b[0m\r\n");
        return;
      }

      const opened = new WebSocket(wsUrl, ["bearer", token]);
      socket = opened;
      ws.current = opened;

      opened.onopen = () => {
        setIsConnected(true);
        setError(null);

        // Initial resize
        if (terminalInstance.current) {
          const { cols, rows } = terminalInstance.current;
          opened.send(JSON.stringify({ type: "resize", cols, rows }));
        }
      };

      opened.onmessage = (event) => {
        if (typeof event.data === "string") {
          term.write(event.data);
        }
      };

      opened.onerror = () => {
        setError("WebSocket connection error");
        setIsConnected(false);
      };

      opened.onclose = (event) => {
        setIsConnected(false);
        // 1008 is the policy-violation close the backend sends when the
        // handshake is rejected: bad origin, missing or expired token, or the
        // terminal is not enabled in this environment.
        if (event.code === 1008) {
          setError("Not authorized");
          term.write("\r\n\x1b[31m[Connection refused: not authorized for this terminal]\x1b[0m\r\n");
          return;
        }
        setError("Disconnected");
        term.write("\r\n\x1b[31m[Process Exited / Connection Closed]\x1b[0m\r\n");
      };
    })();

    return () => {
      cancelled = true;
      window.removeEventListener("resize", handleResize);
      socket?.close();
      term.dispose();
    };
  }, [repoUrl, reconnectKey]);

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
          ) : error ? (
            <>
              <span className="flex items-center gap-1.5 text-red-400"><span className="w-2 h-2 rounded-full bg-red-500"></span> Disconnected</span>
              <button onClick={() => setReconnectKey(k => k + 1)} className="ml-2 px-2 py-0.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded transition-colors border border-zinc-700">Reconnect</button>
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
