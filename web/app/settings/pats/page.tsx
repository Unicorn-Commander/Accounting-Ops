import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import {
  getPATMeta,
  listMyPATs,
  type PATMeta,
  type PersonalAccessToken
} from "@/lib/personal-access-tokens";
import { PATsClient } from "./page-client";

export default async function PersonalAccessTokensPage() {
  const [tokens, meta] = await Promise.all([listMyPATs(), getPATMeta()]);

  const fallbackMeta: PATMeta = {
    all_scopes: ["accounting:read", "accounting:write", "accounting:admin"],
    owner_allowed_scopes: [],
    ttl_choices_days: [30, 60, 90, 365, 0],
    default_ttl_days: 90,
    max_ttl_days: 365,
    max_active_pats: 20,
    workspaces: []
  };

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Personal Access Tokens"
          subtitle="Long-lived bearer tokens for Claude Desktop, Cursor, and other MCP clients. Each token impersonates you and inherits your workspace permissions."
        />

        {meta === null ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Could not load your token metadata. Try refreshing the page.
            </p>
          </Card>
        ) : (
          <PATsClient
            initialTokens={tokens as PersonalAccessToken[]}
            meta={meta ?? fallbackMeta}
          />
        )}
      </PageShell>
    </AppShell>
  );
}
