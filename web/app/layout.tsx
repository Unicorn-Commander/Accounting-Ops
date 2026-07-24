import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Inter } from "next/font/google";
import { cookies } from "next/headers";
import "./globals.css";

export const viewport: Viewport = {
  // Required for env(safe-area-inset-*) to resolve on notched devices —
  // used by the mobile bottom nav.
  viewportFit: "cover",
};

const geist = Geist({ subsets: ["latin"], variable: "--font-geist-sans", display: "swap" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: "Accounting-Ops — Books that can answer for themselves",
  description:
    "Accounting-Ops is a verifiable financial operating system — append-only, tamper-evident books where every number shows its source. Agents do the work, you keep the authority, on your own infrastructure.",
  openGraph: {
    title: "Accounting-Ops — Books that can answer for themselves",
    description: "Every number gets a source, a decision trail, and someone accountable. Your books. Your infrastructure. Your agents.",
    siteName: "Accounting-Ops",
    type: "website"
  }
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const cookieStore = await cookies();
  const theme = cookieStore.get("aops_theme")?.value ?? "dark";

  return (
    <html
      lang="en"
      data-theme={theme}
      suppressHydrationWarning
      className={`${geist.variable} ${geistMono.variable} ${inter.variable}${theme === "dark" ? " dark" : ""}`}
    >
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem("aops_theme")||"dark";document.documentElement.setAttribute("data-theme",t);if(t==="dark")document.documentElement.classList.add("dark");else document.documentElement.classList.remove("dark")}catch(e){}})()`
          }}
        />
      </head>
      <body className="min-h-screen bg-canvas text-ink-primary antialiased">
        {children}
      </body>
    </html>
  );
}
