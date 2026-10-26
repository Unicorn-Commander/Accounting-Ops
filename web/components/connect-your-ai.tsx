"use client";

import { useMemo, useState } from "react";
import { Check, Code2, Copy, MessageSquare, Monitor, Terminal } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";

type TabId = "claude-code" | "claude-desktop" | "chatgpt" | "other";

const tabs: {
  id: TabId;
  label: string;
  icon: typeof Terminal;
}[] = [
  { id: "claude-code", label: "Claude Code", icon: Terminal },
  { id: "claude-desktop", label: "Claude Desktop / claude.ai", icon: Monitor },
  { id: "chatgpt", label: "ChatGPT", icon: MessageSquare },
  { id: "other", label: "Cursor / Codex / other", icon: Code2 }
];

function CodeBlock({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // The value remains visible for manual copying when the clipboard is unavailable.
    }
  }

  return (
    <div className="relative mt-2 overflow-hidden rounded-md border border-default bg-canvas">
      <pre className="overflow-x-auto px-3 py-2.5 pr-24 font-mono text-[11px] leading-relaxed text-ink-secondary">
        {value}
      </pre>
      <button
        type="button"
        onClick={copy}
        className={cn(
          "absolute right-2 top-2 inline-flex items-center gap-1 rounded border border-default bg-surface px-2 py-1 text-[10px] text-ink-secondary hover:border-strong hover:text-ink-primary",
          copied && "border-credit/60 text-credit"
        )}
        aria-label={`${label} code`}
      >
        {copied ? <Check size={11} /> : <Copy size={11} />}
        {copied ? "Copied" : label}
      </button>
    </div>
  );
}

function InstructionBlock({
  title,
  children
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-default bg-canvas/40 p-4">
      <h3 className="text-sm font-medium text-ink-primary">{title}</h3>
      <div className="mt-2 text-xs leading-relaxed text-ink-secondary">{children}</div>
    </div>
  );
}

export function ConnectYourAI({ mcpUrl }: { mcpUrl: string }) {
  const [activeTab, setActiveTab] = useState<TabId>("claude-code");

  const oauthConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            "accounting-ops": {
              type: "http",
              url: mcpUrl
            }
          }
        },
        null,
        2
      ),
    [mcpUrl]
  );

  const tokenConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            "accounting-ops": {
              type: "http",
              url: mcpUrl,
              headers: {
                Authorization: "Bearer <token>"
              }
            }
          }
        },
        null,
        2
      ),
    [mcpUrl]
  );

  const claudeOAuthCommand = `claude mcp add --transport http accounting-ops ${mcpUrl}`;
  const claudeTokenCommand = `${claudeOAuthCommand} --header "Authorization: Bearer <token>"`;

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-semibold text-ink-primary">Connect your AI</h2>
        <p className="text-sm leading-relaxed text-ink-secondary">
          Use your own AI subscription to work with Accounting-Ops over MCP.
        </p>
      </div>

      <div className="mt-4 rounded-lg border border-brand-500/30 bg-brand-500/[0.04] p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
            MCP URL for this ring
          </span>
          <span className="text-[10px] text-ink-tertiary">Use exactly as shown</span>
        </div>
        <CodeBlock value={mcpUrl} label="Copy URL" />
      </div>

      <div className="mt-5 overflow-x-auto border-b border-subtle" role="tablist" aria-label="Connect your AI options">
        <div className="flex min-w-max gap-1">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            const selected = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                id={`connect-ai-tab-${tab.id}`}
                aria-selected={selected}
                aria-controls={`connect-ai-panel-${tab.id}`}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  "inline-flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-xs font-medium transition",
                  selected
                    ? "border-brand-500 text-brand-300"
                    : "border-transparent text-ink-tertiary hover:border-default hover:text-ink-secondary"
                )}
              >
                <Icon size={13} />
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>

      <div
        className="mt-4"
        role="tabpanel"
        id={`connect-ai-panel-${activeTab}`}
        aria-labelledby={`connect-ai-tab-${activeTab}`}
      >
        {activeTab === "claude-code" ? (
          <div className="space-y-3">
            <InstructionBlock title="Sign in with OAuth">
              <p>
                Run this command. A browser window asks you to sign in, then Claude Code connects as you.
              </p>
              <CodeBlock value={claudeOAuthCommand} label="Copy command" />
            </InstructionBlock>
            <InstructionBlock title="Or paste a personal access token">
              <p>
                Generate a token below, then replace <code className="font-mono text-[11px]">&lt;token&gt;</code> in this command.
              </p>
              <CodeBlock value={claudeTokenCommand} label="Copy command" />
            </InstructionBlock>
          </div>
        ) : null}

        {activeTab === "claude-desktop" ? (
          <InstructionBlock title="Claude Desktop / claude.ai">
            <ol className="list-decimal space-y-1 pl-4">
              <li>Open Settings → Connectors.</li>
              <li>Select Add custom connector.</li>
              <li>Paste the MCP URL above. Sign-in appears in the browser.</li>
            </ol>
          </InstructionBlock>
        ) : null}

        {activeTab === "chatgpt" ? (
          <InstructionBlock title="ChatGPT">
            <ol className="list-decimal space-y-1 pl-4">
              <li>Open Settings → Connectors → Developer mode.</li>
              <li>Select New connector.</li>
              <li>Paste the MCP URL above and complete sign-in.</li>
            </ol>
          </InstructionBlock>
        ) : null}

        {activeTab === "other" ? (
          <div className="space-y-3">
            <InstructionBlock title="OAuth / browser sign-in">
              <p>Use this in the client&apos;s mcp.json configuration. The client will open a browser when it connects.</p>
              <CodeBlock value={oauthConfig} label="Copy JSON" />
            </InstructionBlock>
            <InstructionBlock title="Personal access token">
              <p>
                For a token connection, replace <code className="font-mono text-[11px]">&lt;token&gt;</code> with the one-time value from the mint dialog.
              </p>
              <CodeBlock value={tokenConfig} label="Copy JSON" />
            </InstructionBlock>
          </div>
        ) : null}
      </div>

      <div className="mt-5 rounded-md border border-subtle bg-elevated/40 px-3 py-2.5 text-xs leading-relaxed text-ink-secondary">
        Your AI signs in as you and works in your workspaces. Revoke a token below.
      </div>
    </Card>
  );
}
