/**
 * Inline SVG icon set. Inline rather than an icon package: ~20 icons, zero
 * dependency, and `currentColor` means each icon inherits its container's
 * semantic colour automatically.
 */
export type IconName =
  | 'chat' | 'shield' | 'list' | 'lock' | 'settings' | 'mic' | 'mic-off' | 'send'
  | 'stop' | 'cpu' | 'memory' | 'disk' | 'network' | 'battery' | 'check' | 'x'
  | 'alert' | 'clock' | 'play' | 'pause' | 'trash' | 'chevron' | 'app' | 'search'
  | 'camera' | 'broom' | 'info' | 'minus';

const P: Record<IconName, string> = {
  chat: 'M21 11.5a8.4 8.4 0 0 1-8.5 8.5 9 9 0 0 1-3.8-.8L3 21l1.9-5.2A8.4 8.4 0 0 1 4 11.5 8.4 8.4 0 0 1 12.5 3 8.4 8.4 0 0 1 21 11.5z',
  shield: 'M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10z',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  lock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  mic: 'M12 1a3 3 0 0 1 3 3v7a3 3 0 0 1-6 0V4a3 3 0 0 1 3-3zM19 10v1a7 7 0 0 1-14 0v-1M12 19v4M8 23h8',
  'mic-off': 'M1 1l22 22M9 9v2a3 3 0 0 0 5.1 2.1M15 9.3V4a3 3 0 0 0-5.9-.7M19 10v1a7 7 0 0 1-10.8 5.9M5 11a7 7 0 0 0 .4 2.3M12 19v4M8 23h8',
  send: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z',
  stop: 'M6 6h12v12H6z',
  cpu: 'M6 6h12v12H6zM9 9h6v6H9M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3',
  memory: 'M4 7h16v10H4zM8 17v3M16 17v3M8 11v2M12 11v2M16 11v2',
  disk: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  network: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z',
  battery: 'M2 7h16v10H2zM20 11v2',
  check: 'M20 6L9 17l-5-5',
  x: 'M18 6L6 18M6 6l12 12',
  alert: 'M12 3l9 16H3l9-16zM12 9v4M12 17h.01',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2',
  play: 'M6 3l14 9-14 9V3z',
  pause: 'M8 5h3v14H8zM13 5h3v14h-3z',
  trash: 'M3 6h18M8 6V4h8v2M5 6l1 15h12l1-15M10 11v6M14 11v6',
  chevron: 'M9 6l6 6-6 6',
  app: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3',
  camera: 'M3 7h4l2-3h6l2 3h4v13H3zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  broom: 'M14 3l7 7M11 6l7 7-6 6H5l-1-4 7-9zM4 20h8',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01',
  minus: 'M5 12h14',
};

export function Icon({ name, size = 16, strokeWidth = 1.7 }: { name: IconName; size?: number; strokeWidth?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={P[name]} />
    </svg>
  );
}
