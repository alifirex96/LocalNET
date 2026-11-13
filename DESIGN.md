# LocalNET — UI/UX Design Specification

## Product direction
A desktop-first local-network messenger and file-sharing client. The interface must feel modern, fast, trustworthy, and usable on a private LAN without depending on cloud services.

## Visual identity
- Theme: dark, polished, restrained glass surfaces.
- Primary gradient: electric blue to violet (`#2563EB → #7C3AED`).
- Accent blue: `#38BDF8`; accent violet: `#8B5CF6`.
- Background: `#080D1B`; elevated surface: `#111A2E`; secondary surface: `#17233D`.
- Main text: `#F4F7FF`; secondary text: `#A8B5D1`; muted text: `#7182A6`.
- Success: `#22C55E`; warning: `#F59E0B`; error: `#F43F5E`.
- Use gradients for primary actions, active navigation, progress and selected highlights—not for every panel.
- Rounded corners: 10–14 px for controls, 16–20 px for cards and panels.
- Subtle borders and shadows; avoid excessive glow and visual noise.

## Main desktop layout
Three-column workspace:
1. **Navigation sidebar** (220–250 px): brand, global search, Chats, Files, Devices, Transfers, Notifications, Settings; user profile and LAN connection status at bottom.
2. **Main workspace** (flexible): active chat, file browser, device list, or transfer queue depending on navigation.
3. **Context panel** (280–320 px, collapsible): chat details, participants, shared files, device details, or transfer metadata.

The layout must remain usable when the context panel is hidden and at compact desktop widths. Do not force a three-column layout on narrow windows.

## Core screens
### Chats
- Chat list with avatar/icon, name, last message, timestamp, unread count, and online indicator.
- Conversation header with peer/group name and connection state.
- Message bubbles with sender, time, delivery state, and attachment previews.
- Composer with attachment button, text input, send action, and drag-and-drop support.
- Empty state for first launch and for no selected conversation.

### Files
- Local/shared file browser with breadcrumb navigation, list/grid toggle, sorting, filters, and search.
- File rows show name, type icon, size, modified date, source/device, and available actions.
- Support drag-and-drop upload/share, copy link/path where applicable, and clear transfer feedback.
- Distinguish local files from files shared by another LAN peer.

### Devices
- LAN peers discovered on the local network.
- Device name, user, address/hostname where permitted, online state, and last-seen time.
- Actions: open chat, send file, view shared files.
- Show clear permission and trust states; do not expose sensitive network details unnecessarily.

### Transfers
- Unified upload/download queue with progress, speed, size, source/destination, pause/resume/cancel where supported, retry on failure, and completed history.
- Use accessible progress bars and explicit textual statuses; never rely on color alone.

### Notifications
- Group notifications by date and type.
- Read/unread states and clear-all action with confirmation when destructive.

### Settings
- Profile, appearance, LAN/network discovery, storage location, notifications, privacy/security, and diagnostics.
- Clearly explain which settings require restart or administrator privileges.

## Interaction and motion
- Hover and focus states for all interactive controls.
- Keyboard navigation and visible focus rings.
- Short transitions (140–220 ms), restrained fades/slides, no distracting perpetual animation.
- Confirm destructive actions; provide undo when practical.
- Toasts for completed actions and actionable error messages.
- Loading skeletons for data-backed panels; helpful empty and error states.

## Accessibility and usability
- Maintain readable contrast in dark mode.
- Minimum practical hit target 40×40 px.
- Support keyboard-only operation.
- Use text labels alongside status colors and icons.
- Respect reduced-motion preferences.
- Localize all user-facing strings; Russian is the initial interface language, with strings kept externalizable for future localization.

## Responsive behavior
- Desktop: full three-column workspace.
- Medium window: context panel collapses to a drawer.
- Narrow window: navigation becomes a compact rail/drawer; show one primary workspace at a time.

## Implementation guardrails
- This document defines visual direction, not a mandate to replace the current technology stack.
- Inspect the existing application structure before implementation and preserve working networking, storage, and transfer logic.
- Separate reusable UI components, theme tokens, and application logic.
- Do not introduce a cloud dependency; all core chat and file-sharing flows must remain LAN-capable.
- Include realistic sample data only in development/demo mode; never present demo transfers as real.
