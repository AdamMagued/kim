import React, { useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { AttachedFile } from './types';
import type { Settings } from '../../types';
import { toast } from '../Toast';
import { ProviderPicker } from '../ProviderPicker';

export interface ChatComposerProps {
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  taskInput: string;
  setTaskInput: (val: string) => void;
  isRunning: boolean;
  cancelling: boolean;
  handleCancel: () => void;
  onSubmit: (fullText: string) => void;
  activeTab: 'chat' | 'code';
  activeProjectPath?: string | null;
  settings: Settings;
  resolveProvider: () => string;
  handleProviderChange: (val: string) => void;
  handleOllamaModeChange: (mode: 'local' | 'cloud') => void;
  handleOllamaModelChange: (mode: 'local' | 'cloud', model: string) => void;
  onOpenSettings?: (pane?: 'ai') => void;
  heroMode?: boolean;
}

export function ChatComposer({
  textareaRef,
  taskInput,
  setTaskInput,
  isRunning,
  cancelling,
  handleCancel,
  onSubmit,
  activeTab,
  activeProjectPath,
  settings,
  resolveProvider,
  handleProviderChange,
  handleOllamaModeChange,
  handleOllamaModelChange,
  onOpenSettings,
  heroMode = false,
}: ChatComposerProps) {
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const removeAttachment = (idx: number) => {
    setAttachedFiles(prev => {
      const next = [...prev];
      const removed = next.splice(idx, 1)[0];
      if (removed.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return next;
    });
  };

  const buildAttachmentPrefix = (files: AttachedFile[]): string => {
    if (files.length === 0) return '';
    return files
      .map(f => {
        if (f.kind === 'image') {
          return `[Attached image: ${f.name}]\nPath on disk: ${f.savedPath ?? 'unknown'}\n`;
        } else if (f.kind === 'text') {
          return `[Attached file: ${f.name}]\n\`\`\`\n${f.content ?? ''}\n\`\`\`\n`;
        } else {
          return `[Attached binary file: ${f.name}]\nPath on disk: ${f.savedPath ?? 'unknown'}\n`;
        }
      })
      .join('\n');
  };

  const fmtBytes = (n: number): string => {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleTextareaInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setTaskInput(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  };

  const MAX_IMAGE_BYTES = 10 * 1024 * 1024; // 10 MB — kept in memory as base64
  const MAX_TEXT_BYTES = 512 * 1024;        // 512 KB — inlined into prompt tokens
  const MAX_BINARY_BYTES = 10 * 1024 * 1024; // 10 MB — saved to disk as base64

  const processFiles = async (files: FileList | File[]) => {
    const arr = Array.from(files);
    const results: AttachedFile[] = [];
    for (const file of arr) {
      const sizeLabel = fmtBytes(file.size);
      if (file.type.startsWith('image/')) {
        if (file.size > MAX_IMAGE_BYTES) {
          toast(`${file.name} is too large (max ${fmtBytes(MAX_IMAGE_BYTES)}).`, 'warning', 4000);
          continue;
        }
        const base64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => {
            const url = reader.result as string;
            resolve(url.split(',')[1]);
          };
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        const previewUrl = URL.createObjectURL(file);
        try {
          const savedPath = await invoke<string>('save_attachment', {
            filename: file.name,
            dataBase64: base64,
          });
          results.push({ name: file.name, kind: 'image', savedPath, previewUrl, sizeLabel });
        } catch {
          toast(`Could not save image: ${file.name}`, 'error', 3000);
        }
      } else if (
        file.type.startsWith('text/') ||
        /\.(md|txt|py|js|ts|tsx|jsx|json|yaml|yml|toml|sh|bash|zsh|fish|csv|xml|sql|rs|go|java|c|cpp|h|swift|kt|rb|php|html|css|scss|less|env|gitignore|dockerfile)$/i.test(
          file.name
        )
      ) {
        if (file.size > MAX_TEXT_BYTES) {
          toast(`${file.name} is too large to inline (max ${fmtBytes(MAX_TEXT_BYTES)}). Attach a smaller excerpt.`, 'warning', 4000);
          continue;
        }
        const content = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.onerror = reject;
          reader.readAsText(file);
        });
        results.push({ name: file.name, kind: 'text', content, sizeLabel });
      } else if (file.type.startsWith('audio/') || file.type.startsWith('video/')) {
        toast(`${file.name}: audio and video files are not supported.`, 'warning', 4000);
      } else {
        if (file.size > MAX_BINARY_BYTES) {
          toast(`${file.name} is too large (max ${fmtBytes(MAX_BINARY_BYTES)}).`, 'warning', 4000);
          continue;
        }
        const base64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => {
            const url = reader.result as string;
            resolve(url.includes(',') ? url.split(',')[1] : url);
          };
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        try {
          const savedPath = await invoke<string>('save_attachment', {
            filename: file.name,
            dataBase64: base64,
          });
          results.push({ name: file.name, kind: 'binary', savedPath, sizeLabel });
        } catch {
          toast(`Could not save attachment: ${file.name}`, 'error', 3000);
        }
      }
    }
    if (results.length > 0) {
      setAttachedFiles(prev => [...prev, ...results]);
    }
  };

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = taskInput.trim();
    if (!text && attachedFiles.length === 0) return;

    const projectRoot = activeTab === 'code' && activeProjectPath ? activeProjectPath : settings.project_root;
    if (activeTab === 'code' && !projectRoot) {
      toast('Create a new project or open a project folder first.', 'warning', 4000);
      return;
    }

    const attachmentPrefix = buildAttachmentPrefix(attachedFiles);
    const fullText = attachmentPrefix ? `${attachmentPrefix}\n${text}` : text;

    // Reset composer state — revoke object URLs before clearing
    attachedFiles.forEach(f => {
      if (f.previewUrl) URL.revokeObjectURL(f.previewUrl);
    });
    setTaskInput('');
    setAttachedFiles([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }

    onSubmit(fullText);
  };

  const codeNeedsProject = activeTab === 'code' && !activeProjectPath;

  const providerPicker = (
    <ProviderPicker
      resolvedProvider={resolveProvider()}
      ollama={settings.ollama}
      onChangeProvider={handleProviderChange}
      onChangeOllamaMode={handleOllamaModeChange}
      onChangeOllamaModel={handleOllamaModelChange}
      onOpenOllamaSettings={() => onOpenSettings?.('ai')}
      disabled={isRunning}
    />
  );

  return (
    <form
      onSubmit={handleFormSubmit}
      className={
        'kim-composer' +
        (isDragOver ? ' kim-composer--drag' : '') +
        (heroMode ? ' kim-composer--hero' : '')
      }
      onDragOver={e => {
        e.preventDefault();
        setIsDragOver(true);
      }}
      onDragLeave={e => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setIsDragOver(false);
      }}
      onDrop={e => {
        e.preventDefault();
        setIsDragOver(false);
        if (e.dataTransfer.files.length > 0) void processFiles(e.dataTransfer.files);
      }}
    >
      <input
        ref={fileInputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        onChange={e => {
          if (e.target.files) {
            void processFiles(e.target.files);
            e.target.value = '';
          }
        }}
      />

      {attachedFiles.length > 0 && (
        <div className="kim-composer__attachments">
          {attachedFiles.map((f, i) => (
            <div key={i} className="kim-composer__attach-chip">
              {f.kind === 'image' && f.previewUrl ? (
                <img src={f.previewUrl} alt={f.name} className="kim-composer__attach-thumb" />
              ) : (
                <svg
                  viewBox="0 0 16 16"
                  width="13"
                  height="13"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  style={{ flexShrink: 0, opacity: 0.7 }}
                >
                  <path d="M9 1H4a1 1 0 00-1 1v12a1 1 0 001 1h8a1 1 0 001-1V5L9 1z" />
                  <polyline points="9 1 9 5 13 5" />
                </svg>
              )}
              <span className="kim-composer__attach-name">{f.name}</span>
              <span className="kim-composer__attach-size">{f.sizeLabel}</span>
              <button
                type="button"
                className="kim-composer__attach-remove"
                onClick={() => removeAttachment(i)}
                aria-label={`Remove ${f.name}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="kim-composer__row">
        <div
          className={
            'kim-composer__box' +
            (isRunning ? ' kim-composer__box--running' : '') +
            (heroMode ? ' kim-composer__box--hero' : '')
          }
        >
          <textarea
            ref={textareaRef}
            value={taskInput}
            onChange={handleTextareaInput}
            disabled={codeNeedsProject}
            placeholder={
              codeNeedsProject
                ? 'Create a new project or open a project folder first.'
                : isRunning
                ? 'Kim is working — type now, Send adds to queue'
                : attachedFiles.length > 0
                ? 'Add a message or just send…'
                : activeTab === 'code' && activeProjectPath
                ? `What should we work on in ${activeProjectPath.split('/').filter(Boolean).pop() ?? 'this project'}?`
                : 'Message Kim…'
            }
            rows={1}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void handleFormSubmit(e as unknown as React.FormEvent);
              }
            }}
            className="kim-composer__textarea"
          />

          {heroMode && (
            <div className="kim-composer__left-tools">
              <button
                type="button"
                className="kim-composer__tool-btn"
                disabled
                tabIndex={-1}
                aria-hidden="true"
                aria-label="Notifications"
              >
                <svg
                  viewBox="0 0 16 16"
                  width="14"
                  height="14"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M4 12V7a4 4 0 018 0v5l1 2H3l1-2z" />
                  <path d="M6.5 14a1.5 1.5 0 003 0" />
                </svg>
              </button>
              <button
                type="button"
                className="kim-composer__tool-btn"
                disabled
                tabIndex={-1}
                aria-hidden="true"
                aria-label="System prompt"
              >
                <svg
                  viewBox="0 0 16 16"
                  width="14"
                  height="14"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                >
                  <path d="M2 4.5h12M2 8h12M2 11.5h8" />
                </svg>
              </button>
              <button
                type="button"
                className="kim-composer__tool-btn"
                tabIndex={0}
                aria-label="Attach file"
                onClick={() => fileInputRef.current?.click()}
              >
                <svg
                  viewBox="0 0 16 16"
                  width="14"
                  height="14"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M11.5 5.5L6 11a2.5 2.5 0 01-3.5-3.5L8 2a1.5 1.5 0 012 2L4.5 9.5a.5.5 0 00.7.7L10 5.5" />
                </svg>
              </button>
            </div>
          )}

          <div className="kim-composer__actions">
            {!heroMode && !isRunning && (
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="kim-btn kim-btn--attach"
                aria-label="Attach file"
                title="Attach file"
              >
                <svg
                  viewBox="0 0 16 16"
                  width="14"
                  height="14"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M11.5 5.5L6 11a2.5 2.5 0 01-3.5-3.5L8 2a1.5 1.5 0 012 2L4.5 9.5a.5.5 0 00.7.7L10 5.5" />
                </svg>
              </button>
            )}
            {isRunning && !cancelling && (
              <button
                type="button"
                onClick={handleCancel}
                disabled={cancelling}
                title="Stop task"
                className="kim-btn kim-btn--stop"
                aria-label="Stop task"
              >
                <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor">
                  <rect x="6" y="6" width="12" height="12" rx="2" />
                </svg>
              </button>
            )}
            {heroMode && !isRunning && (
              <kbd className="kim-composer__shortcut-hint" aria-hidden="true">
                ⌘↵
              </kbd>
            )}
            <button
              type="submit"
              disabled={codeNeedsProject || (!taskInput.trim() && attachedFiles.length === 0)}
              className={'kim-btn kim-btn--send' + (heroMode ? ' kim-btn--send-hero' : '')}
              aria-label="Send"
            >
              <svg
                viewBox="0 0 24 24"
                width={heroMode ? 15 : 17}
                height={heroMode ? 15 : 17}
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d={heroMode ? 'M12 19V5M5 12l7-7 7 7' : 'M5 12h14M13 5l7 7-7 7'} />
              </svg>
            </button>
            {providerPicker}
          </div>
        </div>
      </div>
    </form>
  );
}
