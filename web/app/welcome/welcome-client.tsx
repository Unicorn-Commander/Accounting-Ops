"use client";

// WelcomeClient — the single-field first-run form. Submits the workspace name
// to the createFirstWorkspace server action via useActionState, which (on
// success) sets the active-workspace cookie and redirects to /setup. While the
// action is pending we show a spinner + reassuring copy and lock the inputs;
// on failure we surface the error in the standard debit-toned message.

import { useActionState } from "react";
import { ArrowRight, Loader2, Sparkles } from "lucide-react";
import { cn } from "@/lib/cn";
import { createFirstWorkspace } from "./actions";

export function WelcomeClient({ defaultName = "" }: { defaultName?: string }) {
  const [state, formAction, pending] = useActionState(createFirstWorkspace, {});

  return (
    <form action={formAction} className="space-y-5">
      <div>
        <label
          htmlFor="display_name"
          className="mb-1.5 block text-sm font-medium text-ink-primary"
        >
          What should we call your workspace?
        </label>
        <input
          id="display_name"
          name="display_name"
          type="text"
          defaultValue={defaultName}
          placeholder="Acme LLC or your name"
          maxLength={120}
          autoFocus
          disabled={pending}
          className="w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm text-ink-primary focus:border-brand-500 focus:outline-none disabled:opacity-60"
        />
        <p className="mt-2 text-xs text-ink-tertiary">
          You&apos;ll be the owner — you can rename it or invite people later.
        </p>
      </div>

      {state.error ? (
        <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
          {state.error}
        </div>
      ) : null}

      <button
        type="submit"
        disabled={pending}
        className={cn(
          "inline-flex w-full items-center justify-center gap-2 rounded-md bg-brand-500 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-brand-600",
          pending && "cursor-wait opacity-60"
        )}
      >
        {pending ? (
          <>
            <Loader2 size={15} className="animate-spin" />
            Setting up your workspace… (~10 seconds)
          </>
        ) : (
          <>
            <Sparkles size={15} />
            Create my workspace
            <ArrowRight size={15} />
          </>
        )}
      </button>
    </form>
  );
}
