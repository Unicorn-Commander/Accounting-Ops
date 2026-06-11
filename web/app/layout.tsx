import type { Metadata } from "next";
import { Geist, Geist_Mono, Inter } from "next/font/google";
import { cookies } from "next/headers";
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
