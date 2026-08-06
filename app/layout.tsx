import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://engineering-room.eitzko.chatgpt.site"),
  title: "Chat Room",
  description: "The local chat room for humans and coding agents.",
  applicationName: "Chat Room",
  authors: [{ name: "Chat Room" }],
  openGraph: {
    title: "Chat Room",
    description: "The local chat room for humans and coding agents.",
    type: "website",
    images: ["/social-preview.png"],
  },
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
