import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Flappy Bird - Next.js',
  description: 'A tiny Flappy Bird clone with terminal-controlled flaps.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
