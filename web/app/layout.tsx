import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  metadataBase: new URL("http://localhost:3000"),
  title: "COLLECTOR | 会积累数据资产的红人研究 Agent",
  description: "由 Agent 自动完成红人发现、采集、核验与交付，并把每次获得的优质创作者数据沉淀在用户自己的机器上。",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  openGraph: {
    title: "COLLECTOR | Local-first Influencer Research Agents",
    description: "Agent 自动完成红人研究全流程，让每一次执行都成为你的本地数据资产。",
    images: [{ url: "/og-home.png", width: 1536, height: 1024, alt: "COLLECTOR 会积累数据资产的红人研究 Agent" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "COLLECTOR | Local-first Influencer Research Agents",
    description: "Agent 自动完成红人研究全流程，让每一次执行都成为你的本地数据资产。",
    images: ["/og-home.png"],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#fffaf4",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>;
}
