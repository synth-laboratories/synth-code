import { always, eventually } from "@antithesishq/bombadil";
import { CharSet } from "@antithesishq/bombadil/actions";
import { actions, extract } from "@antithesishq/bombadil/terminal";

// Mandatory effect/denial witnesses precede bounded random terminal choices.
// The runner independently checks journal approvals and every workspace file.
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
export const reachesSuccess = eventually(() => screen.current?.lines.includes("succeeded") ?? false).within(15,"seconds");
export const reachesSearch = eventually(() => screen.current?.lines.includes("1 matches") ?? false).within(15,"seconds");
export const reachesDenial = eventually(() => screen.current?.lines.includes("denied") ?? false).within(15,"seconds");

let phase = 0;
let explorationSteps = 0;
const type = (text: string) => ({ TypeText: { CharSet: CharSet.fromLiterals(text) } });
export const journey = actions(() => {
  const view = screen.current;
  if (!view) return [type("")];
  const has = (line: string) => view.lines.includes(line);
  if (phase === 0 && (has("Awaiting approval") || has("Approved · awaiting execution"))) { phase = 1; return [type("\x1b[Z\r")]; }
  if (phase === 1 && has("succeeded")) { phase = 2; return [type("/read sample.py\r")]; }
  if (phase === 2 && has("Read")) { phase = 3; return [type("/search after\r")]; }
  if (phase === 3 && has("1 matches")) { phase = 4; return [type("denial witness\r")]; }
  if (phase === 4 && has("Awaiting approval")) { phase = 5; return [type("\r")]; }
  if (phase === 5 && has("denied")) phase = 6;
  if (phase === 6 && explorationSteps++ < 30) {
    const resize = [{Resize:{rows:12,columns:40}},{Resize:{rows:24,columns:80}}];
    return has("Awaiting approval")
      ? [type("\r"), type("\x1b[Z\r"), type("\t"), ...resize]
      : [type("fuzz-α\r"), type("fuzz-β\r"), ...resize];
  }
  if (phase === 6) {phase=7;return [{Resize:{rows:12,columns:40}}];}
  if (phase === 7 && view.rows===12) {phase=8;return [{Resize:{rows:24,columns:80}}];}
  if (phase === 8 && view.rows===24) {phase=9;return [type("\x11")];}
  return [type("")];
});
