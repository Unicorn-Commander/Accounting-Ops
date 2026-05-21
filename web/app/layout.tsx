import type { Metadata } from "next";
import { Geist, Geist_Mono, Inter } from "next/font/google";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-geist-sans", display: "swap" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: "Accounting-Ops — Your books. Your machine. Your rules.",
  description:
    "Agent-first financial operations platform for operators who refuse to send their books to a frontier AI provider. Self-hostable. Multi-entity. Audit-defensible by construction.",
  openGraph: {
    title: "Accounting-Ops",
    description: "Your books. Your machine. Your rules.",
    siteName: "Accounting-Ops",
    type: "website"
  }
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${geist.variable} ${geistMono.variable} ${inter.variable} dark`}
    >
      <body className="min-h-screen bg-canvas text-ink-primary antialiased">
        {children}
      </body>
    </html>
  );
}
