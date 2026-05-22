import Link from "next/link";
import { auth } from "@/lib/auth";
import { SiteHeaderClient } from "@/components/site-header-client";
import { UserMenu } from "@/components/user-menu";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";

export async function SiteHeader({
  variant = "app"
}: {
  variant?: "app" | "marketing";
}) {
  const session = variant === "app" ? await auth() : null;
  const userBlock = session?.user ? (
    <UserMenu
      uc_uid={session.user.uc_uid}
      displayName={session.user.name ?? undefined}
      email={session.user.email ?? undefined}
    />
  ) : variant === "app" ? (
    <Link
      href="/login"
      className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 transition"
    >
      Sign in
    </Link>
  ) : null;

  return (
    <SiteHeaderClient variant={variant} userBlock={userBlock}>
      {variant === "app" && session?.user ? <WorkspaceSwitcher /> : null}
    </SiteHeaderClient>
  );
}
