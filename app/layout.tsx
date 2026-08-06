import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://worktree.chat"),
  title: "Chat Room",
  description: "The local chat room for humans and coding agents.",
  applicationName: "Chat Room",
  authors: [{ name: "Chat Room" }],
  openGraph: {
    title: "Chat Room",
    description: "The local chat room for humans and coding agents.",
    type: "website",
    url: "https://worktree.chat",
    siteName: "Chat Room",
    images: ["/social-preview.png"],
  },
  twitter: {
    card: "summary_large_image",
    title: "Chat Room",
    description: "The local chat room for humans and coding agents.",
    images: ["/social-preview.png"],
  },
  alternates: { canonical: "https://worktree.chat" },
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
