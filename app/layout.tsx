import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://engineering-room.openai.site"),
  title: "Engineering Room",
  description: "The local chat room for humans and coding agents.",
  applicationName: "Engineering Room",
  authors: [{ name: "TallyUp Engineering" }],
  openGraph: {
    title: "Engineering Room",
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
