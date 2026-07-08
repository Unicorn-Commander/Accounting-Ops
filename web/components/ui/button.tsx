import { forwardRef } from "react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
}

const variants: Record<Variant, string> = {
  primary:
    "bg-brand-500 hover:bg-brand-600 text-white shadow-brand-glow disabled:bg-elevated disabled:text-ink-disabled disabled:shadow-none",
  secondary:
    "border border-strong bg-surface hover:bg-elevated text-ink-primary",
  ghost: "hover:bg-white/5 text-ink-secondary hover:text-ink-primary",
  danger: "bg-debit/20 hover:bg-debit/30 text-debit"
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = "primary", size = "md", children, ...rest }, ref
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md font-medium transition disabled:cursor-not-allowed",
        size === "sm" ? "px-2.5 py-1 text-xs" : "px-4 py-2 text-sm",
        variants[variant],
        className
      )}
      {...rest}
    >
      {children}
    </button>
  );
});
