/* eslint-disable @typescript-eslint/no-explicit-any */
/* eslint-disable @typescript-eslint/no-unused-vars */
/* eslint-disable react-hooks/exhaustive-deps */
"use client";

import React, { useState, useEffect, useRef, FormEvent } from "react";
import { 
  ChevronRight, ChevronDown, File as FileIcon, FolderOpen, Folder, 
  Save, Search, X, Plus, Trash2, FilePlus, FolderPlus, Sparkles,
  GitPullRequest, GitBranch, Diff, CheckCircle, AlertCircle,
  ArrowUpRight, RefreshCw, Copy, ExternalLink
} from "lucide-react";
import { motion } from "framer-motion";
import Editor from "@monaco-editor/react";
import InlineAssist, { SelectionContext } from "./InlineAssist";
import { useAuth } from "@/lib/auth";

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
  accessToken?: string;
}

interface FileChange {
  path: string;
  oldContent: string;
  newContent: string;
  status: 'modified' | 'created' | 'deleted';
}

interface ProposedChange {
  id: string;
  title: string;
  description: string;
  branchName: string;
  changes: FileChange[];
  status: 'draft' | 'pending' | 'opened' | 'merged' | 'rejected';
  prNumber?: number;
  prUrl?: string;
  createdAt: string;
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
    if ((hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") && port !== "8000") {
      const formattedHost = (hostname === "::1" || hostname === "[::1]") ? "[::1]" : hostname;
      return `${window.location.protocol}//${formattedHost}:8000`;
    }
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://localhost:8000";
}

function generateDiff(oldContent: string, newContent: string): string {
  const oldLines = oldContent.split('\n');
  const newLines = newContent.split('\n');
  const maxLen = Math.max(oldLines.length, newLines.length);
  let diff = '';
  
  for (let i = 0; i < maxLen; i++) {
    const oldLine = oldLines[i];
    const newLine = newLines[i];
    
    if (oldLine === newLine) {
      diff += ` ${oldLine || ''}\n`;
    } else if (oldLine !== undefined && newLine !== undefined) {
      diff += `-${oldLine}\n+${newLine}\n`;
    } else if (oldLine !== undefined) {
      diff += `-${oldLine}\n`;
    } else {
      diff += `+${newLine}\n`;
    }
  }
  return diff;
}

function DiffView({ oldContent, newContent, fileName }: { oldContent: string; newContent: string; fileName: string }) {
  const diff = generateDiff(oldContent, newContent);
  const lines = diff.split('\n');
  
  return (
    <div className="bg-[#1e1e1e] rounded-lg border border-zinc-800 overflow-hidden">
      <div className="px-4 py-2 bg-zinc-900/50 border-b border-zinc-800 flex items-center justify-between">
        <span className="text-sm font-medium text-zinc-300">{fileName}</span>
        <div className="flex items-center gap-2 text-xs text-zinc-500">
          <span className="text-red-400">− {oldContent.split('\n').length} lines</span>
          <span className="text-green-400">+ {newContent.split('\n').length} lines</span>
        </div>
      </div>
      <div className="p-4 font-mono text-sm overflow-x-auto max-h-96 overflow-y-auto">
        {lines.map((line, i) => (
          <div key={i} className={`flex py-0.5 ${line.startsWith('-') ? 'bg-red-500/10' : line.startsWith('+') ? 'bg-green-500/10' : ''}`}>
            <span className={`w-8 text-right pr-3 text-zinc-500 select-none ${line.startsWith('-') ? 'text-red-400' : line.startsWith('+') ? 'text-green-400' : ''}`}>
              {line.startsWith('-') || line.startsWith('+') ? line[0] : ' '}
            </span>
            <span className={`flex-1 ${line.startsWith('-') ? 'text-red-300' : line.startsWith('+') ? 'text-green-300' : 'text-zinc-300'}`}>
              {line.slice(1) || ' '}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ChangeSummary({ changes }: { changes: FileChange[] }) {
  return (
    <div className="space-y-2">
      {changes.map((change, i) => (
        <div key={i} className="flex items-center gap-3 p-3 bg-zinc-900/50 border border-zinc-800 rounded-lg">
          <div className={`w-8 h-8 rounded flex items-center justify-center flex-shrink-0 ${
            change.status === 'created' ? 'bg-green-500/20 text-green-400' :
            change.status === 'deleted' ? 'bg-red-500/20 text-red-400' :
            'bg-blue-500/20 text-blue-400'
          }`}>
            {change.status === 'created' && <CheckCircle className="w-5 h-5" />}
            {change.status === 'deleted' && <X className="w-5 h-5" />}
            {change.status === 'modified' && <Diff className="w-5 h-5" />}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-zinc-100 truncate">{change.path}</p>
            <p className="text-xs text-zinc-500 capitalize">{change.status}</p>
          </div>
          <span className={`px-2 py-0.5 rounded text-xs font-medium ${
            change.status === 'created' ? 'bg-green-500/20 text-green-400' :
            change.status === 'deleted' ? 'bg-red-500/20 text-red-400' :
            'bg-blue-500/20 text-blue-400'
          }`}>
            {change.status}
          </span>
        </div>
      ))}
    </div>
  );
}

function ProposeChangesModal({ 
  isOpen, 
  onClose, 
  changes, 
  onPropose,
  loading 
}: { 
  isOpen: boolean; 
  onClose: () => void; 
  changes: FileChange[];
  onPropose: (title: string, description: string) => void;
  loading: boolean;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [step, setStep] = useState<'review' | 'confirm'>('review');
  
  if (!isOpen) return null;
  
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        className="w-full max-w-3xl h-[85vh] bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden flex flex-col"
      >
        {/* Header */}
        <div className="px-6 py-4 border-b border-zinc-800 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${step === 'review' ? 'bg-blue-500/20 text-blue-400' : 'bg-emerald-500/20 text-emerald-400'}`}>
              {step === 'review' ? <Diff className="w-6 h-6" /> : <GitPullRequest className="w-6 h-6" />}
            </div>
            <div>
              <h3 className="text-lg font-semibold text-zinc-100">
                {step === 'review' ? 'Review Changes' : 'Create Pull Request'}
              </h3>
              <p className="text-sm text-zinc-500">
                {step === 'review' 
                  ? `${changes.length} file${changes.length !== 1 ? 's' : ''} will be included`
                  : 'Fill in details to open a PR'}
              </p>
            </div>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-zinc-800 rounded-lg text-zinc-400 hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>
        
        {/* Step Indicator */}
        <div className="px-6 py-3 border-b border-zinc-800 flex items-center gap-2">
          <div className={`flex items-center gap-2 ${step === 'review' ? 'text-blue-400' : 'text-zinc-500'}`}>
            <div className="w-6 h-6 rounded-full border-2 flex items-center justify-center text-xs font-medium">
              1
            </div>
            <span className="text-sm font-medium">Review</span>
          </div>
          <div className="flex-1 h-px bg-zinc-800" />
          <div className={`flex items-center gap-2 ${step === 'confirm' ? 'text-emerald-400' : 'text-zinc-500'}`}>
            <div className="w-6 h-6 rounded-full border-2 flex items-center justify-center text-xs font-medium">
              2
            </div>
            <span className="text-sm font-medium">Propose</span>
          </div>
        </div>
        
        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6">
          {step === 'review' ? (
            <div className="space-y-6">
              <ChangeSummary changes={changes} />
              
              <div className="border-t border-zinc-800 pt-4">
                <h4 className="text-sm font-medium text-zinc-400 mb-3">File Diffs</h4>
                <div className="space-y-4 max-h-[50vh] overflow-y-auto">
                  {changes.map((change, i) => (
                    <DiffView 
                      key={i} 
                      oldContent={change.oldContent} 
                      newContent={change.newContent} 
                      fileName={change.path}
                    />
                  ))}
                </div>
              </div>
              
              <div className="flex justify-end gap-3 pt-4 border-t border-zinc-800">
                <button onClick={onClose} className="px-4 py-2 text-zinc-300 hover:text-white transition-colors">
                  Cancel
                </button>
                <button 
                  onClick={() => setStep('confirm')}
                  className="px-4 py-2 bg-blue-500/10 border border-blue-500/20 text-blue-400 rounded-lg hover:bg-blue-500/20 transition-colors flex items-center gap-2"
                >
                  <ArrowUpRight className="w-4 h-4" />
                  Propose Changes
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-6">
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">PR Title *</label>
                  <input
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder="e.g., Add user authentication module"
                    className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-white focus:border-blue-500 focus:outline-none"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-zinc-300 mb-2">Description</label>
                  <textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Describe the changes and why they're needed..."
                    rows={6}
                    className="w-full bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2 text-white focus:border-blue-500 focus:outline-none font-mono text-sm"
                  />
                </div>
              </div>
              
              <div className="bg-zinc-800/50 rounded-lg p-4 border border-zinc-700">
                <h5 className="text-sm font-medium text-zinc-300 mb-2 flex items-center gap-2">
                  <GitBranch className="w-4 h-4" />
                  A new branch will be created: <code className="bg-zinc-900 px-2 py-0.5 rounded text-xs text-emerald-400">automaintainer/{title.toLowerCase().replace(/\s+/g, '-').slice(0, 50)}</code>
                </h5>
                <p className="text-xs text-zinc-500">
                  Changes will be pushed to this branch and a Pull Request will be opened against the default branch.
                </p>
              </div>
              
              <div className="flex justify-end gap-3 pt-4 border-t border-zinc-800">
                <button 
                  onClick={() => setStep('review')}
                  className="px-4 py-2 text-zinc-300 hover:text-white transition-colors"
                >
                  Back
                </button>
                <button 
                  onClick={() => onPropose(title, description)}
                  disabled={!title.trim() || loading}
                  className="px-4 py-2 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 rounded-lg hover:bg-emerald-500/20 transition-colors flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {loading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <GitPullRequest className="w-4 h-4" />}
                  {loading ? 'Creating PR...' : 'Create Pull Request'}
                </button>
              </div>
            </div>
          )}
        </div>
      </motion.div>
    </div>
  );
}

function ProposedChangesPanel({ changes, onClose }: { changes: ProposedChange[]; onClose: () => void }) {
  if (changes.length === 0) return null;
  
  return (
    <div className="fixed bottom-4 right-4 z-40 w-96 bg-zinc-900 border border-zinc-800 rounded-xl shadow-2xl overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between">
        <h4 className="font-medium text-zinc-100 flex items-center gap-2">
          <GitPullRequest className="w-5 h-5 text-emerald-400" />
          Proposed Changes ({changes.length})
        </h4>
        <button onClick={onClose} className="p-1 hover:bg-zinc-800 rounded text-zinc-400">
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="max-h-64 overflow-y-auto">
        {changes.map((change) => (
          <div key={change.id} className="px-4 py-3 border-b border-zinc-800/50 hover:bg-zinc-800/30">
            <div className="flex items-start gap-3">
              <div className={`w-8 h-8 rounded flex items-center justify-center flex-shrink-0 ${
                change.status === 'merged' ? 'bg-emerald-500/20 text-emerald-400' :
                change.status === 'opened' ? 'bg-blue-500/20 text-blue-400' :
                change.status === 'pending' ? 'bg-amber-500/20 text-amber-400' :
                'bg-zinc-500/20 text-zinc-400'
              }`}>
                <GitPullRequest className="w-5 h-5" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-zinc-100 truncate">{change.title}</p>
                <p className="text-xs text-zinc-500 truncate">{change.description.slice(0, 60)}...</p>
                <div className="flex items-center gap-2 mt-1">
                  <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                    change.status === 'merged' ? 'bg-emerald-500/20 text-emerald-400' :
                    change.status === 'opened' ? 'bg-blue-500/20 text-blue-400' :
                    change.status === 'pending' ? 'bg-amber-500/20 text-amber-400' :
                    'bg-zinc-500/20 text-zinc-400'
                  }`}>
                    {change.status}
                  </span>
                  {change.prNumber && (
                    <span className="text-xs text-zinc-500">#PR-{change.prNumber}</span>
                  )}
                </div>
              </div>
              {change.prUrl && (
                <a href={change.prUrl} target="_blank" rel="noopener noreferrer" className="p-1 hover:bg-zinc-800 rounded text-zinc-400 hover:text-white">
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function WebIDE({ repoUrl, accessToken }: WebIDEProps) {
  const { session } = useAuth();
  const token = accessToken ?? session?.access_token;
  const authHeaders: Record<string, string> = token
    ? { Authorization: `Bearer ${token}` }
    : {};

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
  
  // Preview/PR Flow State
  const [stagedChanges, setStagedChanges] = useState<FileChange[]>([]);
  const [showProposeModal, setShowProposeModal] = useState(false);
  const [proposing, setProposing] = useState(false);
  const [proposedChanges, setProposedChanges] = useState<ProposedChange[]>([]);
  const [showProposedPanel, setShowProposedPanel] = useState(false);
  
  // Track original content for diff
  const [originalContents, setOriginalContents] = useState<Record<string, string>>({});

  // Monaco & Inline Assist State
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);
  const editorContainerRef = useRef<HTMLDivElement>(null);
  const previewDecorationsRef = useRef<string[]>([]);
  const [inlineAssist, setInlineAssist] = useState<{
    isOpen: boolean;
    selectionContext: SelectionContext;
    position: { top: number; left: number };
  } | null>(null);

  const clearPreviewDecorations = () => {
    if (editorRef.current && previewDecorationsRef.current.length > 0) {
      previewDecorationsRef.current = editorRef.current.deltaDecorations(previewDecorationsRef.current, []);
    }
  };

  const handleUpdatePreviewDecorations = (previewText: string | null) => {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    if (!editor || !monaco || !inlineAssist) return;

    if (!previewText) {
      clearPreviewDecorations();
      return;
    }

    const { selectionContext } = inlineAssist;
    const range = new monaco.Range(
      selectionContext.startLine,
      selectionContext.startColumn,
      selectionContext.endLine,
      selectionContext.endColumn
    );

    const decorations = [
      {
        range: range,
        options: {
          isWholeLine: true,
          className: "bg-indigo-950/40 border-l-4 border-indigo-400 font-mono",
          glyphMarginClassName: "bg-indigo-500",
          hoverMessage: { value: "**AI Inline Assist Preview**\n\n```\n" + previewText + "\n```" },
        },
      },
    ];

    previewDecorationsRef.current = editor.deltaDecorations(previewDecorationsRef.current, decorations);
  };

  const triggerInlineAssist = (editorInstance?: any, monacoInstance?: any) => {
    const editor = editorInstance || editorRef.current;
    const monaco = monacoInstance || monacoRef.current;
    if (!editor || !monaco || !activeTab) return;

    clearPreviewDecorations();

    const selection = editor.getSelection();
    const model = editor.getModel();
    if (!model) return;

    const startLine = selection ? selection.startLineNumber : 1;
    const endLine = selection ? selection.endLineNumber : startLine;
    let startColumn = selection ? selection.startColumn : 1;
    let endColumn = selection ? selection.endColumn : 1;

    let selectedCode = selection ? model.getValueInRange(selection) : "";

    if (!selectedCode && selection) {
      const lineContent = model.getLineContent(startLine);
      selectedCode = lineContent;
      startColumn = 1;
      endColumn = lineContent.length + 1;
    }

    const totalLines = model.getLineCount();
    const prefixStartLine = Math.max(1, startLine - 50);
    const prefixRange = new monaco.Range(prefixStartLine, 1, startLine, startColumn);
    const prefixCode = model.getValueInRange(prefixRange);

    const suffixEndLine = Math.min(totalLines, endLine + 50);
    const suffixEndCol = model.getLineMaxColumn(suffixEndLine);
    const suffixRange = new monaco.Range(endLine, endColumn, suffixEndLine, suffixEndCol);
    const suffixCode = model.getValueInRange(suffixRange);

    const pos = editor.getPosition();
    const scrolledPos = pos ? editor.getScrolledVisiblePosition(pos) : null;

    let top = 60;
    let left = 40;
    if (scrolledPos && editorContainerRef.current) {
      const rect = editorContainerRef.current.getBoundingClientRect();
      top = Math.max(20, Math.min(scrolledPos.top + 30, rect.height - 350));
      left = Math.max(20, Math.min(scrolledPos.left + 40, rect.width - 550));
    }

    setInlineAssist({
      isOpen: true,
      selectionContext: {
        startLine,
        startColumn,
        endLine,
        endColumn,
        selectedCode,
        prefixCode,
        suffixCode,
      },
      position: { top, left },
    });
  };

  const handleEditorMount = (editor: any, monaco: any) => {
    editorRef.current = editor;
    monacoRef.current = monaco;

    // Register Cmd+K / Ctrl+K keybinding in Monaco
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyK, () => {
      triggerInlineAssist(editor, monaco);
    });
  };

  const handleAcceptInlineAssist = (replacementCode: string) => {
    if (!editorRef.current || !monacoRef.current || !inlineAssist || !activeTab) return;

    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const { selectionContext } = inlineAssist;

    clearPreviewDecorations();

    const range = new monaco.Range(
      selectionContext.startLine,
      selectionContext.startColumn,
      selectionContext.endLine,
      selectionContext.endColumn
    );

    editor.executeEdits("inline-assist", [
      {
        range: range,
        text: replacementCode,
        forceMoveMarkers: true,
      },
    ]);

    const updatedValue = editor.getValue();
    handleContentChange(activeTab, updatedValue);
    setInlineAssist(null);
  };

  const fetchTree = async () => {
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/tree`, {
        headers: authHeaders,
      });
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
  }, [repoUrl, token]);

  const clearInlineAssistState = () => {
    clearPreviewDecorations();
    setInlineAssist(null);
  };

  const switchTab = (path: string) => {
    clearInlineAssistState();
    setActiveTab(path);
  };

  const openFile = async (path: string) => {
    clearInlineAssistState();
    if (!openTabs.includes(path)) {
      setOpenTabs([...openTabs, path]);
    }
    setActiveTab(path);
    
    if (fileContents[path] === undefined) {
      setLoadingFiles(prev => ({...prev, [path]: true}));
      try {
        const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file?file_path=${encodeURIComponent(path)}`, {
          headers: authHeaders,
        });
        if (!res.ok) throw new Error("File not found");
        const data = await res.json();
        setFileContents(prev => ({...prev, [path]: data.content}));
        setEditedContents(prev => ({...prev, [path]: data.content}));
        // Store original for diff
        setOriginalContents(prev => ({...prev, [path]: data.content}));
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
    clearInlineAssistState();
    const newTabs = openTabs.filter(t => t !== path);
    setOpenTabs(newTabs);
    
    if (activeTab === path) {
      setActiveTab(newTabs.length > 0 ? newTabs[newTabs.length - 1] : null);
    }
    
    // Clean up memory
    setFileContents(prev => { const n = {...prev}; delete n[path]; return n; });
    setEditedContents(prev => { const n = {...prev}; delete n[path]; return n; });
    setOriginalContents(prev => { const n = {...prev}; delete n[path]; return n; });
  };

  const handleSave = async () => {
    if (!activeTab || editedContents[activeTab] === undefined) return;
    const content = editedContents[activeTab];
    if (content === fileContents[activeTab]) return;

    setIsSaving(true);
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({ file_path: activeTab, content })
      });
      if (!res.ok) throw new Error("Failed to save");
      setFileContents(prev => ({...prev, [activeTab]: content}));
      // Update original content after successful save to main branch
      setOriginalContents(prev => ({...prev, [activeTab]: content}));
      
      // Remove from staged changes since it's now saved to main
      setStagedChanges(prev => prev.filter(c => c.path !== activeTab));
    } catch (err: any) {
      alert("Failed to save file: " + err.message);
    } finally {
      setIsSaving(false);
    }
  };

  const handleContentChange = (path: string, newContent: string) => {
    setEditedContents(prev => ({...prev, [path]: newContent}));
    
    // Track staged changes
    const original = originalContents[path] || "";
    if (newContent !== original) {
      setStagedChanges(prev => {
        const existing = prev.findIndex(c => c.path === path);
        const change: FileChange = {
          path,
          oldContent: original,
          newContent,
          status: original ? 'modified' : 'created'
        };
        if (existing >= 0) {
          const next = [...prev];
          next[existing] = change;
          return next;
        }
        return [...prev, change];
      });
    } else {
      // Content matches original, remove from staged
      setStagedChanges(prev => prev.filter(c => c.path !== path));
    }
  };

  const handleCreate = async (parentPath: string, isDir: boolean) => {
    const name = prompt(`Enter name for new ${isDir ? 'folder' : 'file'} in ${parentPath}:`);
    if (!name) return;
    
    const newPath = parentPath === "." || parentPath === "" ? name : `${parentPath}/${name}`;
    try {
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/file/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
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
        method: "DELETE",
        headers: authHeaders,
      });
      if (!res.ok) throw new Error("Failed to delete");
      if (openTabs.includes(path)) closeTab({ stopPropagation: () => {} } as any, path);
      
      // Track as deleted in staged changes
      const original = originalContents[path] || "";
      if (original) {
        setStagedChanges(prev => {
          const existing = prev.findIndex(c => c.path === path);
          const change: FileChange = {
            path,
            oldContent: original,
            newContent: "",
            status: 'deleted'
          };
          if (existing >= 0) {
            const next = [...prev];
            next[existing] = change;
            return next;
          }
          return [...prev, change];
        });
      }
      
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
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/search?q=${encodeURIComponent(searchQuery)}`, {
        headers: authHeaders,
      });
      if (!res.ok) throw new Error("Search failed");
      const data = await res.json();
      setSearchResults(data.results);
    } catch (err: any) {
      alert(err.message);
    } finally {
      setIsSearching(false);
    }
  };

  const handleProposeChanges = async (title: string, description: string) => {
    if (!title.trim() || stagedChanges.length === 0) return;
    
    setProposing(true);
    try {
      // Call backend to create PR
      const res = await fetch(`${getBackendUrl()}/repo/${encodeURIComponent(repoUrl)}/propose-changes`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({
          title,
          description,
          changes: stagedChanges.map(c => ({
            path: c.path,
            content: c.newContent,
            status: c.status
          }))
        })
      });
      
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Failed to create PR");
      }
      
      const data = await res.json();
      
      // Add to proposed changes list
      const newProposed: ProposedChange = {
        id: data.branch_name,
        title,
        description,
        branchName: data.branch_name,
        changes: stagedChanges,
        status: 'opened',
        prNumber: data.pr_number,
        prUrl: data.pr_url,
        createdAt: new Date().toISOString(),
      };
      
      setProposedChanges(prev => [newProposed, ...prev]);
      setShowProposedPanel(true);
      setShowProposeModal(false);
      
      // Clear staged changes
      setStagedChanges([]);
      setOriginalContents({});
      setEditedContents({});
      setFileContents({});
      setOpenTabs([]);
      setActiveTab(null);
      await fetchTree();
      
    } catch (err: any) {
      alert("Failed to propose changes: " + err.message);
    } finally {
      setProposing(false);
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

  const hasActiveTabUnsavedChanges = activeTab 
    ? (editedContents[activeTab] !== undefined && editedContents[activeTab] !== fileContents[activeTab]) 
    : false;
  
  const hasStagedChanges = stagedChanges.length > 0;

  return (
    <div className="flex h-full w-full bg-[#1e1e1e] border-l border-zinc-800 font-sans shadow-2xl overflow-hidden relative">
      {/* Proposed Changes Panel */}
      <ProposedChangesPanel 
        changes={proposedChanges} 
        onClose={() => setShowProposedPanel(false)} 
      />
      
      {/* Propose Changes Modal */}
      <ProposeChangesModal
        isOpen={showProposeModal}
        onClose={() => setShowProposeModal(false)}
        changes={stagedChanges}
        onPropose={handleProposeChanges}
        loading={proposing}
      />
      
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
        {hasStagedChanges && (
          <button 
            onClick={() => setShowProposeModal(true)}
            className="p-2 rounded-lg mt-4 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
            title="Propose Changes"
          >
            <GitPullRequest className="w-6 h-6" />
          </button>
        )}
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
                const isStaged = stagedChanges.some(c => c.path === tab);
                return (
                  <div 
                    key={tab}
                    onClick={() => switchTab(tab)}
                    className={`flex items-center gap-2 h-full px-3 text-sm cursor-pointer border-r border-[#1e1e1e] group ${
                      isTabActive ? "bg-[#1e1e1e] text-[#cccccc] border-t border-t-[#007acc]" : "bg-[#2d2d2d] text-[#888888] hover:bg-[#2b2b2b]"
                    }`}
                    style={{ minWidth: "120px", maxWidth: "200px" }}
                  >
                    <FileIcon className="w-3.5 h-3.5 shrink-0 text-[#519aba]" />
                    <span className="truncate flex-1">{tab.split("/").pop()}</span>
                    {(isUnsaved || isStaged) && (
                      <span className="w-2 h-2 bg-emerald-400 rounded-full mx-1 animate-pulse" title={isStaged ? "Staged for PR" : "Unsaved"} />
                    )}
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
            {/* Editor Breadcrumbs & Actions */}
            <div className="h-8 flex items-center justify-between px-4 text-xs text-[#cccccc] shrink-0 bg-[#1e1e1e] border-b border-zinc-800">
              <div className="flex items-center">
                <span className="opacity-70">{repoUrl}</span>
                <span className="mx-1 opacity-50">&gt;</span>
                <span className="opacity-70">{activeTab?.split("/").join(" > ")}</span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => triggerInlineAssist()}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded text-indigo-300 hover:text-white bg-indigo-950/40 hover:bg-indigo-900/60 border border-indigo-800/60 transition-colors"
                  title="AI Inline Assist (Cmd+K)"
                >
                  <Sparkles className="w-3 h-3 text-indigo-400" />
                  <span>Inline Assist (⌘K)</span>
                </button>
                <button 
                  onClick={handleSave}
                  disabled={!hasActiveTabUnsavedChanges || isSaving}
                  className={`flex items-center gap-1 px-3 py-1 rounded transition-colors ${hasActiveTabUnsavedChanges ? 'bg-[#0e639c] text-white hover:bg-[#1177bb]' : 'text-zinc-500 cursor-not-allowed'}`}
                >
                  <Save className="w-3 h-3" />
                  {isSaving ? "Saving..." : "Save"}
                </button>
                {hasStagedChanges && (
                  <button 
                    onClick={() => setShowProposeModal(true)}
                    className="flex items-center gap-1 px-3 py-1 rounded transition-colors bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 hover:bg-emerald-500/20 text-xs"
                  >
                    <GitPullRequest className="w-3 h-3" />
                    Propose ({stagedChanges.length})
                  </button>
                )}
              </div>
            </div>

            {/* Code Content */}
            <div ref={editorContainerRef} className="flex-1 overflow-hidden bg-[#1e1e1e] relative">
              {activeTab && loadingFiles[activeTab] ? (
                <div className="p-8 text-[#cccccc] text-sm animate-pulse">Loading file content...</div>
              ) : activeTab && editedContents[activeTab] !== undefined ? (
                <>
                  <Editor
                    height="100%"
                    theme="vs-dark"
                    language={getLanguage(activeTab)}
                    value={editedContents[activeTab]}
                    onMount={handleEditorMount}
                    onChange={(value) => handleContentChange(activeTab!, value ?? "")}
                    options={{
                      minimap: { enabled: true },
                      fontSize: 14,
                      wordWrap: "on",
                      padding: { top: 16 }
                    }}
                  />
                  {inlineAssist?.isOpen && (
                    <InlineAssist
                      filePath={activeTab}
                      selectionContext={inlineAssist.selectionContext}
                      position={inlineAssist.position}
                      onClose={() => {
                        clearPreviewDecorations();
                        setInlineAssist(null);
                      }}
                      onAccept={handleAcceptInlineAssist}
                      onUpdatePreview={handleUpdatePreviewDecorations}
                      getBackendUrl={getBackendUrl}
                      repoUrl={repoUrl}
                    />
                  )}
                </>
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