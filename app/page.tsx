import type { Metadata } from "next";
import { EngineeringRoom } from "./engineering-room";

export const metadata: Metadata = {
  title: "Engineering Room — local chat for coding agents",
  description:
    "An open-source, local-first chat room for humans, coding agents, and every worktree in a Git project.",
};

export default function Home() {
  return <EngineeringRoom />;
}
