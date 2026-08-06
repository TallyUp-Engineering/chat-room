import type { Metadata } from "next";
import { ChatRoom } from "./chat-room";

export const metadata: Metadata = {
  title: "Chat Room — local chat for coding agents",
  description:
    "An open-source, local-first chat room for humans, coding agents, and every worktree in a Git project.",
};

export default function Home() {
  return <ChatRoom />;
}
