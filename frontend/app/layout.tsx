import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Resolution & Growth Agent — Ledger",
  description: "Live audit ledger for the merchant-side agentic commerce resolution agent.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
