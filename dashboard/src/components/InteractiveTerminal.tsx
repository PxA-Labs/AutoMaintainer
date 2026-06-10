"use client";

import React, { useEffect, useRef, useState } from "react";
import { Terminal } from "xterm";
import { FitAddon } from "@xterm/addon-fit";
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

    // Build the WebSocket URL
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = process.env.NEXT_PUBLIC_BACKEND_URL
      ? process.env.NEXT_PUBLIC_BACKEND_URL.replace("http://", "").replace("https://", "")
      : "localhost:8000";
    
    let wsUrl = `${protocol}//${host}/api/terminal/ws`;
    if (repoUrl) {
      wsUrl += `?repo_url=${encodeURIComponent(repoUrl)}`;
    }

    const socket = new WebSocket(wsUrl);
    ws.current = socket;

    socket.onopen = () => {
      setIsConnected(true);
      setError(null);
      
      // Initial resize
      if (terminalInstance.current) {
        const { cols, rows } = terminalInstance.current;
        socket.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    };

    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        term.write(event.data);
      }
    };

    socket.onerror = () => {
      setError("WebSocket connection error");
      setIsConnected(false);
    };

    socket.onclose = () => {
      setIsConnected(false);
      term.write("\r\n\x1b[31m[Process Exited / Connection Closed]\x1b[0m\r\n");
    };

    // Handle user input
    term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(data);
      }
    });

    // Handle window resize
    const handleResize = () => {
      if (fitAddon.current && terminalInstance.current && socket.readyState === WebSocket.OPEN) {
        fitAddon.current.fit();
        const { cols, rows } = terminalInstance.current;
        socket.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      socket.close();
      term.dispose();
    };
  }, [repoUrl]);

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
            <span className="flex items-center gap-1.5 text-red-400"><span className="w-2 h-2 rounded-full bg-red-500"></span> Error</span>
          ) : (
            <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-zinc-500"></span> Connecting...</span>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-hidden p-2" ref={terminalRef}></div>
    </div>
  );
}
