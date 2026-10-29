import { always, eventually } from "@antithesishq/bombadil";
import { CharSet } from "@antithesishq/bombadil/actions";
import { actions, extract } from "@antithesishq/bombadil/terminal";

// This first specification is a deterministic native-terminal journey. Random
// exploration belongs in additional campaigns, not in the mandatory milestones.
const screen = extract((state) => {
  const lines: string[] = [];
  for (let row = 0; row < state.grid.size.rows; row++) lines.push(state.grid.rowText(row).trim());
  return { lines, text: lines.join("\n"), rows: state.grid.size.rows, columns: state.grid.size.columns,
    cursor: state.cursor.position, exit: state.exitStatus };
}).named("workspace_screen");

export const noUnexpectedExit = always(() => {
  const status = screen.current?.exit;
  return !status || (status.code === 0 && status.signal === null);
});
export const noTraceback = always(() => !screen.current?.text.includes("Traceback (most recent call last)") && !screen.current?.text.includes("Press ENTER to continue"));
export const cursorInBounds = always(() => !screen.current ||
  (screen.current.cursor.row < screen.current.rows && screen.current.cursor.column < screen.current.columns));
const shows = (prefix: string) => screen.current?.lines.some((line) => line.startsWith(prefix)) ?? false;
export const reachesSuccess = eventually(() => shows("• Edited sample.py")).within(15,"seconds");
export const reachesDenial = eventually(() => shows("✗ Denied") || shows("Interrupted ·")).within(15,"seconds");

let phase = 0;
const type = (text: string) => ({ TypeText: { CharSet: CharSet.fromLiterals(text) } });
export const journey = actions(() => {
  const view = screen.current;
  if (!view) return [type("")];
  const has = (prefix: string) => view.lines.some((line) => line.startsWith(prefix));
  const denied = () => has("✗ Denied") || has("Interrupted ·");
  // Resume and approve the pending edit, then ask the agent for a command and deny it.
  if (phase === 0 && (has("Awaiting approval") || has("Approved · awaiting execution"))) { phase = 1; return [type("y")]; }
  if (phase === 1 && has("• Edited sample.py")) { phase = 4; return [type("denial witness\r")]; }
  if (phase === 4 && has("Awaiting approval")) { phase = 5; return [type("n")]; }
  if (phase === 5 && denied()) { phase = 6; return [{ Resize: { rows:12, columns:40 } }]; }
  if (phase === 6 && view.rows === 12 && view.columns === 40) { phase = 7; return [{ Resize: { rows:24, columns:80 } }]; }
  if (phase === 7 && view.rows === 24 && view.columns === 80) { phase = 8; return [type("\x11")]; }
  return [type("")];
});
