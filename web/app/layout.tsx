import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Accounting-Ops",
  description: "Your books. Your machine. Your rules."
};

const nav = [
  ["Dashboard", "/dashboard"],
  ["Transactions", "/transactions"],
  ["Reconciliation", "/reconciliation-queue"],
  ["Rules", "/specialty-rules"],
  ["Chat", "/chat"],
  ["CPA Export", "/cpa-export"]
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="border-b border-line bg-paper">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
            <Link href="/dashboard" className="text-lg font-semibold">Accounting-Ops</Link>
            <nav className="flex flex-wrap gap-2 text-sm">
              {nav.map(([label, href]) => (
                <Link key={href} href={href} className="rounded-md px-2 py-1 text-ink/75 hover:bg-field hover:text-ink">
                  {label}
                </Link>
              ))}
            </nav>
          </div>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
