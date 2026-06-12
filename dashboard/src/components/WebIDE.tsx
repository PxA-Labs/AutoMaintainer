"use client";

import React, { useState, useEffect } from "react";
import { ChevronRight, ChevronDown, File as FileIcon, FolderOpen, Folder, Save } from "lucide-react";
import Editor from "@monaco-editor/react";

interface TreeNode {
  name: string;
  path: string;
  type: "directory" | "file";
  children?: TreeNode[];
}

interface WebIDEProps {
  repoUrl: string;
}

const FileTreeNode = ({
  node,
  activePath,
  onSelect,
  level = 0,
}: {
  node: TreeNode;
  activePath: string | null;
  onSelect: (path: string) => void;
  level?: number;
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const isDir = node.type === "directory";
  const isActive = activePath === node.path;

  const handleToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isDir) {
      setIsOpen(!isOpen);
    } else {
      onSelect(node.path);
    }
  };

  return (
    <div>
      <button
        onClick={handleToggle}
        aria-expanded={isDir ? isOpen : undefined}
        className={`flex items-center w-full text-left py-1 px-2 cursor-pointer select-none text-sm font-sans transition-colors ${
          isActive ? "bg-[#37373d] text-white" : "text-[#cccccc] hover:bg-[#2a2d2e] hover:text-white"
        }`}
        style={{ paddingLeft: `${level * 12 + 8}px` }}
      >
        <span className="w-4 h-4 mr-1 flex items-center justify-center shrink-0">
          {isDir ? (
            isOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />
          ) : null}
        </span>
        <span className="w-4 h-4 mr-2 flex items-center justify-center shrink-0">
          {isDir ? (
            isOpen ? <FolderOpen className="w-4 h-4 text-[#dcb67a]" /> : <Folder className="w-4 h-4 text-[#dcb67a]" />
          ) : (
            <FileIcon className="w-4 h-4 text-[#519aba]" />
          )}
        </span>
        <span className="truncate">{node.name}</span>
      </button>
      {isDir && isOpen && node.children && (
        <div>
          {node.children.map((child, idx) => (
            <FileTreeNode
              key={idx}
              node={child}
              activePath={activePath}
              onSelect={onSelect}
              level={level + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
};

function getBackendUrl(): string {
  if (process.env.NEXT_PUBLIC_BACKEND_URL) {
    return process.env.NEXT_PUBLIC_BACKEND_URL.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    const port = window.location.port;
    const hostname = window.location.hostname;
    if (port === "3000") {
      return `${window.location.protocol}//${hostname}:8000`;
    }
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://localhost:8000";
}


export default function WebIDE({ repoUrl }: WebIDEProps) {
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [loadingTree, setLoadingTree] = useState(true);
  const [loadingFile, setLoadingFile] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editedContent, setEditedContent] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const hasUnsavedChanges = editedContent !== null && editedContent !== fileContent;

  useEffect(() => {
    const controller = new AbortController();
    const fetchTree = async () => {
      setLoadingTree(true);
      setError(null);
      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(`${backendUrl}/repo/${encodeURIComponent(repoUrl)}/tree`, {
          signal: controller.signal
        });
        if (!res.ok) {
          throw new Error("Repository not found or API error");
        }
        const data = await res.json();
        setTree(data);
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : String(err) || "Failed to fetch repository tree");
      } finally {
        setLoadingTree(false);
      }
    };
    fetchTree();
    return () => controller.abort();
  }, [repoUrl]);

  useEffect(() => {
    if (!activeFile) return;
    const controller = new AbortController();
    const fetchFile = async () => {
      setLoadingFile(true);
      setFileContent(null);
      try {
        const backendUrl = getBackendUrl();
        const res = await fetch(
          `${backendUrl}/repo/${encodeURIComponent(repoUrl)}/file?file_path=${encodeURIComponent(activeFile)}`,
          { signal: controller.signal }
        );
        if (!res.ok) {
          throw new Error("Cannot read binary file or file not found");
        }
        const data = await res.json();
        setFileContent(data.content);
        setEditedContent(data.content);
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") return;
        setFileContent(`// Error loading file: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        setLoadingFile(false);
      }
    };
    fetchFile();
    return () => controller.abort();
  }, [activeFile, repoUrl]);

  const handleSave = async () => {
    if (!activeFile || !editedContent) return;
    setIsSaving(true);
    try {
      const backendUrl = getBackendUrl();
      const res = await fetch(`${backendUrl}/repo/${encodeURIComponent(repoUrl)}/file`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_path: activeFile,
          content: editedContent
        })
      });
      if (!res.ok) throw new Error("Failed to save");
      setFileContent(editedContent);
    } catch (err) {
      alert("Failed to save file: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setIsSaving(false);
    }
  };

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [activeFile, editedContent, repoUrl]);

  const getLanguage = (filename: string) => {
    const ext = filename.split(".").pop()?.toLowerCase();
    const map: Record<string, string> = {
      js: "javascript",
      jsx: "jsx",
      ts: "typescript",
      tsx: "tsx",
      py: "python",
      json: "json",
      md: "markdown",
      html: "html",
      css: "css",
      sh: "bash",
    };
    return map[ext || ""] || "text";
  };

  return (
    <div className="flex h-full w-full bg-[#1e1e1e] border-l border-zinc-800 font-sans shadow-2xl">
      {/* VS Code Sidebar */}
      <div className="w-64 bg-[#252526] flex flex-col border-r border-[#333333] shrink-0 overflow-hidden">
        <div className="h-9 flex items-center px-4 text-xs font-semibold text-[#cccccc] uppercase tracking-wider shrink-0">
          Explorer
        </div>
        <div className="flex-1 overflow-y-auto overflow-x-hidden custom-scrollbar pb-4">
          {loadingTree ? (
            <div className="p-4 text-sm text-[#cccccc]">Loading tree...</div>
          ) : error ? (
            <div className="p-4 text-sm text-red-400">{error}</div>
          ) : tree && tree.children ? (
            tree.children.map((child, idx) => (
              <FileTreeNode
                key={idx}
                node={child}
                activePath={activeFile}
                onSelect={setActiveFile}
              />
            ))
          ) : (
            <div className="p-4 text-sm text-[#cccccc]">No files found.</div>
          )}
        </div>
      </div>

      {/* VS Code Editor Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-[#1e1e1e]">
        {activeFile ? (
          <>
            {/* Editor Tab Bar */}
            <div className="flex bg-[#252526] h-9 items-end shrink-0 overflow-x-auto custom-scrollbar justify-between pr-2">
              <div className="bg-[#1e1e1e] text-[#cccccc] px-3 py-2 flex items-center gap-2 text-sm border-t border-[#007acc] min-w-fit">
                <FileIcon className="w-3.5 h-3.5 text-[#519aba]" />
                {activeFile.split("/").pop()}
                {hasUnsavedChanges && <span className="w-2 h-2 bg-white rounded-full ml-1"></span>}
              </div>
              <button 
                onClick={handleSave}
                disabled={!hasUnsavedChanges || isSaving}
                className={`flex items-center gap-1 px-3 py-1 text-xs rounded transition-colors mb-1 ${hasUnsavedChanges ? 'bg-[#0e639c] text-white hover:bg-[#1177bb]' : 'text-zinc-500 cursor-not-allowed'}`}
              >
                <Save className="w-3 h-3" />
                {isSaving ? "Saving..." : "Save"}
              </button>
            </div>
            
            {/* Editor Breadcrumbs */}
            <div className="h-6 flex items-center px-4 text-xs text-[#cccccc] shrink-0 bg-[#1e1e1e]">
              <span className="opacity-70">{repoUrl}</span>
              <span className="mx-1 opacity-50">&gt;</span>
              <span className="opacity-70">{activeFile.split("/").join(" > ")}</span>
            </div>

            {/* Code Content */}
            <div className="flex-1 overflow-hidden bg-[#1e1e1e] relative">
              {loadingFile ? (
                <div className="p-8 text-[#cccccc] text-sm animate-pulse">Loading file content...</div>
              ) : fileContent !== null ? (
                <Editor
                  height="100%"
                  theme="vs-dark"
                  language={getLanguage(activeFile)}
                  value={editedContent ?? fileContent}
                  onChange={(value) => setEditedContent(value ?? "")}
                  options={{
                    minimap: { enabled: true },
                    fontSize: 14,
                    wordWrap: "on",
                    padding: { top: 16 }
                  }}
                />
              ) : null}
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="w-32 h-32 mx-auto mb-6 opacity-5">
                <svg viewBox="0 0 100 100" fill="currentColor" className="w-full h-full text-white">
                  <path d="M10,10 L90,10 L90,90 L10,90 Z" fill="none" stroke="currentColor" strokeWidth="2"/>
                  <path d="M20,30 L80,30 M20,50 L80,50 M20,70 L50,70" stroke="currentColor" strokeWidth="2"/>
                </svg>
              </div>
              <h2 className="text-[#cccccc] text-2xl font-light mb-2 tracking-wide">AutoMaintainer Editor</h2>
              <p className="text-[#888888] text-sm">Select a file from the explorer to view its contents.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
