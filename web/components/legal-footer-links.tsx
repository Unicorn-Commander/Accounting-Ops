import Link from "next/link";
import { cn } from "@/lib/cn";

const LINKS = [
  { href: "/terms", label: "Terms" },
  { href: "/privacy", label: "Privacy" },
  { href: "/legal", label: "Legal" },
] as const;

/**
 * Compact Terms · Privacy · Legal link row for app shell / sidebar footers.
 */
export function LegalFooterLinks({
  className,
  separator = "·",
}: {
  className?: string;
  separator?: string;
}) {
  return (
    <nav
      aria-label="Legal"
      className={cn(
        "flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] text-ink-tertiary",
        className
      )}
    >
      {LINKS.map((link, i) => (
        <span key={link.href} className="inline-flex items-center gap-x-1.5">
          {i > 0 && (
            <span className="text-ink-disabled" aria-hidden>
              {separator}
            </span>
          )}
          <Link
            href={link.href}
            className="hover:text-ink-secondary hover:underline underline-offset-2 transition"
          >
            {link.label}
          </Link>
        </span>
      ))}
    </nav>
  );
}
