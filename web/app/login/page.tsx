import { ArrowRight, Lock } from "lucide-react";
import { signIn } from "@/lib/auth";
import { APP_VERSION_LABEL } from "@/lib/version";

export const metadata = {
  title: "Sign in · Accounting-Ops",
  description: "Sign in to Accounting-Ops via your configured identity provider."
};

export default async function LoginPage({
  searchParams
}: {
  searchParams: Promise<{ callbackUrl?: string; error?: string; reason?: string }>;
}) {
  const params = await searchParams;
  // Default to the persona-aware landing router (/start) which forwards CPAs
  // to their cross-client practice view and everyone else to /dashboard. An
  // explicit callbackUrl (e.g. a deep link the user was bounced from) wins.
  const callbackUrl = params.callbackUrl ?? "/start";
  const error = params.error;
  const reason = params.reason;
  // Community self-host: local email+password login instead of uchub SSO.
  const isLocalAuth = (process.env.AO_AUTH_MODE ?? "keycloak").toLowerCase() === "local";

  return (
    <main className="grid min-h-screen place-items-center bg-canvas px-6">
      <div className="grid-bg absolute inset-0 pointer-events-none" />
      <div className="relative w-full max-w-md">
        <div className="absolute -inset-x-12 -inset-y-8 bg-gradient-to-tr from-brand-500/10 via-transparent to-agent-500/10 blur-2xl" />
        <div className="relative rounded-lg border border-default bg-surface p-8 shadow-card">
          <div className="flex items-center gap-3">
            <Logo />
            <div>
              <p className="text-base font-semibold">Accounting-Ops</p>
              <p className="text-[11px] uppercase tracking-wider text-ink-tertiary">
                {APP_VERSION_LABEL}
              </p>
            </div>
          </div>

          <h1 className="mt-8 text-2xl font-semibold tracking-tight">
            Sign in to continue
          </h1>
          <p className="mt-2 text-sm text-ink-secondary leading-relaxed">
            {isLocalAuth ? (
              <>Sign in with your self-host administrator credentials.</>
            ) : (
              <>
                Authentication runs through the configured identity provider
                (Keycloak <code className="font-mono text-xs">uchub</code> realm).
                Same login as Brigade, Crisis-Ops, Project-Ops.
              </>
            )}
          </p>

          {reason === "session_expired" && (
            <div className="mt-5 rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-2 text-sm text-pending">
              Your session expired. Sign in again to continue — your data is
              waiting.
            </div>
          )}
          {error && (
            <div className="mt-5 rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
              {error === "AccessDenied"
                ? "Sign-in was denied. Check your account permissions."
                : error === "Configuration"
                  ? "Identity provider is misconfigured. The team is paged."
                  : `Sign-in failed: ${error}`}
            </div>
          )}

          {isLocalAuth ? (
            <form
              action={async (formData: FormData) => {
                "use server";
                await signIn("credentials", {
                  email: String(formData.get("email") ?? ""),
                  password: String(formData.get("password") ?? ""),
                  redirectTo: callbackUrl
                });
              }}
              className="mt-6 space-y-3"
            >
              <div>
                <label htmlFor="email" className="mb-1 block text-xs font-medium text-ink-secondary">
                  Email
                </label>
                <input
                  id="email"
                  name="email"
                  type="email"
                  autoComplete="username"
                  required
                  className="w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm text-ink-primary outline-none focus:border-brand-500"
                />
              </div>
              <div>
                <label htmlFor="password" className="mb-1 block text-xs font-medium text-ink-secondary">
                  Password
                </label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  className="w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm text-ink-primary outline-none focus:border-brand-500"
                />
              </div>
              <button
                type="submit"
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-brand-500 px-4 py-2.5 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 transition"
              >
                <Lock size={14} />
                Sign in
                <ArrowRight size={14} />
              </button>
            </form>
          ) : (
            <form
              action={async () => {
                "use server";
                await signIn("keycloak", { redirectTo: callbackUrl });
              }}
              className="mt-6"
            >
              <button
                type="submit"
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-brand-500 px-4 py-2.5 text-sm font-medium text-white shadow-brand-glow hover:bg-brand-600 transition"
              >
                <Lock size={14} />
                Sign in with identity provider
                <ArrowRight size={14} />
              </button>
            </form>
          )}

          {!isLocalAuth && (
            <div className="mt-6 border-t border-subtle pt-5">
              <p className="text-xs text-ink-tertiary">
                No account?{" "}
                <a
                  href="mailto:support@example.com?subject=Accounting-Ops dev preview access"
                  className="text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline"
                >
                  Request dev-preview access.
                </a>
              </p>
            </div>
          )}
        </div>

        <p className="mt-6 text-center text-[11px] text-ink-tertiary">
          By signing in you accept that this is a {APP_VERSION_LABEL} and may
          change without notice.
        </p>
      </div>
    </main>
  );
}

function Logo() {
  return (
    <svg
      width="32"
      height="32"
      viewBox="0 0 22 22"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <linearGradient
          id="login-lg"
          x1="0"
          y1="0"
          x2="22"
          y2="22"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="hsl(265 78% 62%)" />
          <stop offset="1" stopColor="hsl(188 95% 56%)" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="20" height="20" rx="5" fill="url(#login-lg)" opacity="0.18" />
      <rect
        x="1.5"
        y="1.5"
        width="19"
        height="19"
        rx="4.5"
        stroke="url(#login-lg)"
        strokeWidth="1"
      />
      <path
        d="M6 14V8L11 11L16 8V14"
        stroke="url(#login-lg)"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}
