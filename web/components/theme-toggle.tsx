"use client";

import { useCallback, useEffect, useState } from "react";

function setCookie(value: string) {
  document.cookie = `aops_theme=${value}; Path=/; SameSite=Lax; Max-Age=31536000`;
}

export function ThemeToggle() {
  const [theme, setThemeState] = useState<"dark" | "light">("dark");

  useEffect(() => {
    // Hydrate from what the server rendered
    const current = document.documentElement.getAttribute("data-theme") as "dark" | "light" || "dark";
    setThemeState(current);
  }, []);

  const setTheme = useCallback((t: "dark" | "light") => {
    setThemeState(t);
    document.documentElement.setAttribute("data-theme", t);
    if (t === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
    localStorage.setItem("aops_theme", t);
    setCookie(t);
  }, []);

  return (
    <div className="seg" id="themeseg">
      <button
        data-t="dark"
        className={theme === "dark" ? "on" : ""}
        onClick={() => setTheme("dark")}
        title="Dark"
        aria-label="Switch to dark theme"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="15" height="15">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
        </svg>
      </button>
      <button
        data-t="light"
        className={theme === "light" ? "on" : ""}
        onClick={() => setTheme("light")}
        title="Light"
        aria-label="Switch to light theme"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="15" height="15">
          <circle cx="12" cy="12" r="4.5" />
          <path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19" />
        </svg>
      </button>
    </div>
  );
}
