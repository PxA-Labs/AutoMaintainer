/* eslint-disable @typescript-eslint/no-explicit-any */
/* eslint-disable @typescript-eslint/no-unused-vars */
/* eslint-disable react-hooks/exhaustive-deps */
"use client";

import React, { useState, useEffect, FormEvent } from "react";
import { ChevronRight, ChevronDown, File as FileIcon, FolderOpen, Folder, Save, Search, X, Plus, Trash2, FilePlus, FolderPlus } from "lucide-react";
import Editor from "@monaco-editor/react";

interface TreeNode {
  name: string;
  path: string;
  type: "directory" | "file";
  children?: TreeNode[];
}

interface SearchResult {
  file: string;
  line_number: number;
  snippet: string;
}

interface WebIDEProps {
  repoUrl: string;
}

const FileTreeNode = ({
  node,
  activePath,
  onSelect,
  onCreate,
  onDelete,
  level = 0,
}: {
  node: TreeNode;
  activePath: string | null;
  onSelect: (path: string) => void;
  onCreate: (parentPath: string, isDir: boolean) => void;
  onDelete: (path: string) => void;
  level?: number;
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
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
      <div
        onClick={handleToggle}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className={`flex items-center justify-between w-full text-left py-1 px-2 cursor-pointer select-none text-sm font-sans transition-colors group ${
          isActive ? "bg-[#37373d] text-white" : "text-[#cccccc] hover:bg-[#2a2d2e] hover:text-white"
        }`}
        style={{ paddingLeft: `${level * 12 + 8}px` }}
      >
        <div className="flex items-center overflow-hidden">
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
        </div>
        
        {/* Action Icons (Visible on Hover) */}
        {isHovered && (
          <div className="flex items-center gap-1 shrink-0 bg-transparent px-1">
            {isDir && (
              <>
                <button 
                  onClick={(e) => { e.stopPropagation(); onCreate(node.path, false); }}
                  className="p-0.5 hover:bg-[#4d4d4d] rounded text-zinc-400 hover:text-white"
                  title="New File"
                >
                  <FilePlus className="w-3.5 h-3.5" />
                </button>
                <button 
                  onClick={(e) => { e.stopPropagation(); onCreate(node.path, true); }}
                  className="p-0.5 hover:bg-[#4d4d4d] rounded text-zinc-400 hover:text-white"
                  title="New Folder"
                >
                  <FolderPlus className="w-3.5 h-3.5" />
                </button>
              </>
            )}
            <button 
              onClick={(e) => { e.stopPropagation(); onDelete(node.path); }}
              className="p-0.5 hover:bg-[#4d4d4d] rounded text-zinc-400 hover:text-red-400"
              title="Delete"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
      {isDir && isOpen && node.children && (
        <div>
          {node.children.map((child, idx) => (
            <FileTreeNode
              key={idx}
              node={child}
              activePath={activePath}
              onSelect={onSelect}
              onCreate={onCreate}
              onDelete={onDelete}
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
  // Tree State
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [loadingTree, setLoadingTree] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Tab State
  const [openTabs, setOpenTabs] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [fileContents, setFileContents] = useState<Record<string, string>>({});
  const [editedContents, setEditedContents] = useState<Record<string, string>>({});
  const [loadingFiles, setLoadingFiles] = useState<Record<string, boolean>>({});
  
  // Sidebar State
  const [activeSidebarMode, setActiveSidebarMode] = useState<"explorer" | "search">("explorer");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [isSaving, setIsSaving] = useState(false);

  const fetchTree = async () => {
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/tree`);
      if (!res.ok) throw new Error("Repository not found or API error");
      const data = await res.json();
      setTree(data);
    } catch (err: any) {
      setError(err.message || "Failed to fetch repository tree");
    } finally {
      setLoadingTree(false);
    }
  };

  useEffect(() => {
    setLoadingTree(true);
    fetchTree();
  }, [repoUrl]);

  const openFile = async (path: string) => {
    if (!openTabs.includes(path)) {
      setOpenTabs([...openTabs, path]);
    }
    setActiveTab(path);
    
    if (fileContents[path] === undefined) {
      setLoadingFiles(prev => ({...prev, [path]: true}));
      try {
        const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file?file_path=${encodeURIComponent(path)}`);
        if (!res.ok) throw new Error("File not found");
        const data = await res.json();
        setFileContents(prev => ({...prev, [path]: data.content}));
        setEditedContents(prev => ({...prev, [path]: data.content}));
      } catch (err: any) {
        setFileContents(prev => ({...prev, [path]: `// Error: ${err.message}`}));
        setEditedContents(prev => ({...prev, [path]: `// Error: ${err.message}`}));
      } finally {
        setLoadingFiles(prev => ({...prev, [path]: false}));
      }
    }
  };

  const closeTab = (e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    const newTabs = openTabs.filter(t => t !== path);
    setOpenTabs(newTabs);
    
    if (activeTab === path) {
      setActiveTab(newTabs.length > 0 ? newTabs[newTabs.length - 1] : null);
    }
    
    // Clean up memory
    setFileContents(prev => { const n = {...prev}; delete n[path]; return n; });
    setEditedContents(prev => { const n = {...prev}; delete n[path]; return n; });
  };

  const handleSave = async () => {
    if (!activeTab || editedContents[activeTab] === undefined) return;
    const content = editedContents[activeTab];
    if (content === fileContents[activeTab]) return;

    setIsSaving(true);
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_path: activeTab, content })
      });
      if (!res.ok) throw new Error("Failed to save");
      setFileContents(prev => ({...prev, [activeTab]: content}));
    } catch (err: any) {
      alert("Failed to save file: " + err.message);
    } finally {
      setIsSaving(false);
    }
  };

  const handleCreate = async (parentPath: string, isDir: boolean) => {
    const name = prompt(`Enter name for new ${isDir ? 'folder' : 'file'} in ${parentPath}:`);
    if (!name) return;
    
    const newPath = parentPath === "." || parentPath === "" ? name : `${parentPath}/${name}`;
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_path: newPath, is_dir: isDir, content: "" })
      });
      if (!res.ok) throw new Error("Failed to create");
      await fetchTree();
      if (!isDir) openFile(newPath);
    } catch (err: any) {
      alert("Failed to create: " + err.message);
    }
  };

  const handleDelete = async (path: string) => {
    if (!confirm(`Are you sure you want to delete ${path}? This will also push a commit to GitHub.`)) return;
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file?file_path=${encodeURIComponent(path)}`, {
        method: "DELETE"
      });
      if (!res.ok) throw new Error("Failed to delete");
      if (openTabs.includes(path)) closeTab({ stopPropagation: () => {} } as any, path);
      await fetchTree();
    } catch (err: any) {
      alert("Failed to delete: " + err.message);
    }
  };

  const handleSearch = async (e: FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) return;
    setIsSearching(true);
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/search?q=${encodeURIComponent(searchQuery)}`);
      if (!res.ok) throw new Error("Search failed");
      const data = await res.json();
      setSearchResults(data.results);
    } catch (err: any) {
      alert(err.message);
    } finally {
      setIsSearching(false);
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
  }, [activeTab, editedContents]);

  const getLanguage = (filename: string) => {
    const ext = filename.split(".").pop()?.toLowerCase();
    const map: Record<string, string> = {
      js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
      py: "python", json: "json", md: "markdown", html: "html", css: "css", sh: "bash",
    };
    return map[ext || ""] || "text";
  };

  const hasActiveTabUnsavedChanges = activeTab ? (editedContents[activeTab] !== undefined && editedContents[activeTab] !== fileContents[activeTab]) : false;

  return (
    <div className="flex h-full w-full bg-[#1e1e1e] border-l border-zinc-800 font-sans shadow-2xl overflow-hidden">
      
      {/* Activity Bar */}
      <div className="w-12 bg-[#333333] flex flex-col items-center py-2 shrink-0 border-r border-[#252526]">
        <button 
          onClick={() => setActiveSidebarMode("explorer")}
          className={`p-2 rounded-lg mb-2 transition-colors ${activeSidebarMode === 'explorer' ? 'text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
          title="Explorer"
        >
          <FileIcon className="w-6 h-6" />
        </button>
        <button 
          onClick={() => setActiveSidebarMode("search")}
          className={`p-2 rounded-lg transition-colors ${activeSidebarMode === 'search' ? 'text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
          title="Search"
        >
          <Search className="w-6 h-6" />
        </button>
      </div>

      {/* Sidebar */}
      <div className="w-64 bg-[#252526] flex flex-col border-r border-[#333333] shrink-0 overflow-hidden">
        <div className="h-9 flex items-center px-4 text-xs font-semibold text-[#cccccc] uppercase tracking-wider shrink-0 flex-row justify-between">
          <span>{activeSidebarMode === "explorer" ? "Explorer" : "Search"}</span>
          {activeSidebarMode === "explorer" && (
            <div className="flex gap-2">
              <button onClick={() => handleCreate(".", false)} title="New File in Root"><FilePlus className="w-4 h-4 text-zinc-400 hover:text-white" /></button>
              <button onClick={() => handleCreate(".", true)} title="New Folder in Root"><FolderPlus className="w-4 h-4 text-zinc-400 hover:text-white" /></button>
            </div>
          )}
        </div>
        
        <div className="flex-1 overflow-y-auto custom-scrollbar pb-4">
          {activeSidebarMode === "explorer" ? (
            loadingTree ? (
              <div className="p-4 text-sm text-[#cccccc]">Loading tree...</div>
            ) : error ? (
              <div className="p-4 text-sm text-red-400">{error}</div>
            ) : tree && tree.children ? (
              tree.children.map((child, idx) => (
                <FileTreeNode
                  key={idx}
                  node={child}
                  activePath={activeTab}
                  onSelect={openFile}
                  onCreate={handleCreate}
                  onDelete={handleDelete}
                />
              ))
            ) : (
              <div className="p-4 text-sm text-[#cccccc]">No files found.</div>
            )
          ) : (
            <div className="p-4 flex flex-col h-full">
              <form onSubmit={handleSearch} className="mb-4">
                <input 
                  type="text" 
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  placeholder="Search..." 
                  className="w-full bg-[#3c3c3c] text-white border border-[#3c3c3c] focus:border-[#007acc] rounded px-2 py-1 text-sm outline-none"
                />
              </form>
              <div className="flex-1 overflow-y-auto custom-scrollbar">
                {isSearching ? (
                  <div className="text-sm text-zinc-400">Searching...</div>
                ) : searchResults.length > 0 ? (
                  searchResults.map((res, i) => (
                    <div 
                      key={i} 
                      className="text-sm mb-2 cursor-pointer hover:bg-[#2a2d2e] p-1 rounded group"
                      onClick={() => openFile(res.file)}
                    >
                      <div className="text-[#519aba] truncate font-medium flex items-center gap-1">
                        <FileIcon className="w-3 h-3" /> {res.file}
                      </div>
                      <div className="text-zinc-400 truncate text-xs pl-4 group-hover:text-zinc-300">
                        <span className="text-zinc-500 mr-1">{res.line_number}:</span>
                        {res.snippet}
                      </div>
                    </div>
                  ))
                ) : searchQuery ? (
                  <div className="text-sm text-zinc-400">No results found.</div>
                ) : (
                  <div className="text-sm text-zinc-500">Enter a query to search.</div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Editor Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-[#1e1e1e]">
        {openTabs.length > 0 ? (
          <>
            {/* Tab Bar */}
            <div className="flex bg-[#252526] h-9 items-end shrink-0 overflow-x-auto custom-scrollbar">
              {openTabs.map(tab => {
                const isTabActive = tab === activeTab;
                const isUnsaved = editedContents[tab] !== undefined && editedContents[tab] !== fileContents[tab];
                return (
                  <div 
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className={`flex items-center gap-2 h-full px-3 text-sm cursor-pointer border-r border-[#1e1e1e] group ${
                      isTabActive ? "bg-[#1e1e1e] text-[#cccccc] border-t border-t-[#007acc]" : "bg-[#2d2d2d] text-[#888888] hover:bg-[#2b2b2b]"
                    }`}
                    style={{ minWidth: "120px", maxWidth: "200px" }}
                  >
                    <FileIcon className="w-3.5 h-3.5 shrink-0 text-[#519aba]" />
                    <span className="truncate flex-1">{tab.split("/").pop()}</span>
                    <button 
                      onClick={(e) => closeTab(e, tab)}
                      className={`p-0.5 rounded hover:bg-[#4d4d4d] shrink-0 ${isUnsaved && !isTabActive ? "invisible" : ""}`}
                    >
                      {isUnsaved ? (
                        <div className="w-2 h-2 bg-white rounded-full mx-1"></div>
                      ) : (
                        <X className="w-3 h-3 opacity-0 group-hover:opacity-100 transition-opacity" />
                      )}
                    </button>
                  </div>
                );
              })}
            </div>
            
            {/* Editor Breadcrumbs & Save */}
            <div className="h-8 flex items-center justify-between px-4 text-xs text-[#cccccc] shrink-0 bg-[#1e1e1e] border-b border-zinc-800">
              <div className="flex items-center">
                <span className="opacity-70">{repoUrl}</span>
                <span className="mx-1 opacity-50">&gt;</span>
                <span className="opacity-70">{activeTab?.split("/").join(" > ")}</span>
              </div>
              <button 
                onClick={handleSave}
                disabled={!hasActiveTabUnsavedChanges || isSaving}
                className={`flex items-center gap-1 px-3 py-1 rounded transition-colors ${hasActiveTabUnsavedChanges ? 'bg-[#0e639c] text-white hover:bg-[#1177bb]' : 'text-zinc-500 cursor-not-allowed'}`}
              >
                <Save className="w-3 h-3" />
                {isSaving ? "Saving..." : "Save"}
              </button>
            </div>

            {/* Code Content */}
            <div className="flex-1 overflow-hidden bg-[#1e1e1e] relative">
              {activeTab && loadingFiles[activeTab] ? (
                <div className="p-8 text-[#cccccc] text-sm animate-pulse">Loading file content...</div>
              ) : activeTab && editedContents[activeTab] !== undefined ? (
                <Editor
                  height="100%"
                  theme="vs-dark"
                  language={getLanguage(activeTab)}
                  value={editedContents[activeTab]}
                  onChange={(value) => setEditedContents(prev => ({...prev, [activeTab]: value ?? ""}))}
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
              <p className="text-[#888888] text-sm mb-4">Select a file from the explorer or search to begin.</p>
              <div className="flex justify-center gap-4 text-xs text-zinc-500">
                <span className="flex items-center gap-1"><Search className="w-3 h-3"/> Search</span>
                <span className="flex items-center gap-1"><FilePlus className="w-3 h-3"/> Create File</span>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
