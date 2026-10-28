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

export const reachesCompletion = eventually(() => screen.current?.text.includes("Task complete:") ?? false).within(20,"seconds");
let phase=0;
const type=(text:string)=>({TypeText:{CharSet:CharSet.fromLiterals(text)}});
export const journey=actions(()=>{
 const view=screen.current;
 if(!view)return[type("")];
 if(phase===0 && view.lines.includes("Ready")){phase=1;return[type("Update sample.py and check it\r")];}
 if(phase===1 && view.lines.includes("Awaiting approval") && view.text.includes("Edit · sample.py")){phase=2;return[type("\x1b[Z\r")];}
 if(phase===2 && view.lines.includes("Awaiting approval") && view.text.includes("Command ·")){phase=3;return[type("\x1b[Z\r")];}
 if(phase===3 && view.text.includes("Task complete:")){phase=4;return[{Resize:{rows:12,columns:40}}];}
 if(phase===4 && view.rows===12 && view.text.includes("Task complete:")){phase=5;return[{Resize:{rows:24,columns:80}}];}
 if(phase===5 && view.rows===24){phase=6;return[type("\x11")];}
 return[type("")];
});
