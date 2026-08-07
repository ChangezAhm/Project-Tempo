import type { Metadata } from "next";
import { Geist_Mono, Inter, Source_Serif_4 } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const serif = Source_Serif_4({
  variable: "--font-serif",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Tempo",
  description: "Template intelligence for sponsor reporting.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${serif.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <header className="sticky top-0 z-40 border-b border-neutral-200/80 bg-paper/85 backdrop-blur-md">
          <div className="mx-auto flex h-15 max-w-6xl items-center gap-8 px-6">
            <Link href="/" className="flex items-baseline gap-2">
              <span
                className="text-[1.35rem] font-semibold tracking-tight text-ink"
                style={{ fontFamily: "var(--font-display)" }}
              >
                Tempo
              </span>
              <span className="hidden text-[11px] font-medium uppercase tracking-[0.18em] text-neutral-400 sm:inline">
                Template intelligence
              </span>
            </Link>
            <nav className="ml-auto flex items-center gap-1">
              <Link
                href="/"
                className="rounded-md px-3 py-1.5 text-sm text-neutral-600 transition hover:bg-neutral-100 hover:text-ink"
              >
                Library
              </Link>
              <Link
                href="/upload"
                className="ml-2 rounded-md bg-ink px-3.5 py-1.5 text-sm font-medium text-neutral-50 shadow-sm transition hover:bg-neutral-700"
              >
                New template
              </Link>
            </nav>
          </div>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-10">{children}</main>
        <footer className="border-t border-neutral-200/70">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5 text-xs text-neutral-400">
            <span>Tempo</span>
            <span>Sponsor reporting, understood.</span>
          </div>
        </footer>
      </body>
    </html>
  );
}
