// A page for the dewarping journeys: the real DewarpStage over a fixture picture, with its state shown to the test.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { DewarpStage } from "@/features/dewarp/DewarpStage";
import type { DewarpState, Lens } from "@/features/dewarp/geometry";

declare global {
  interface Window { dewarpHarness: { mount: (options: { lens: Lens; state: DewarpState; width: number; height: number }) => void; state: () => DewarpState; unsupported: () => boolean } }
}

let latest: DewarpState = { mode: "raw", pan: 0, views: [] };
let unsupported = false;

function Harness({ lens, initial, width, height }: { lens: Lens; initial: DewarpState; width: number; height: number }) {
  const [state, setState] = useState(initial);
  latest = state;
  const source = document.getElementById("fixture") as HTMLImageElement;
  return (
    <div style={{ position: "relative", width, height }}>
      <DewarpStage source={source} lens={lens} state={state} label="Dome" playing={false} onChange={next => { latest = next; setState(next); }} onUnsupported={() => { unsupported = true; }} />
    </div>
  );
}

window.dewarpHarness = {
  mount: ({ lens, state, width, height }) => {
    latest = state;
    createRoot(document.getElementById("root")!).render(<Harness lens={lens} initial={state} width={width} height={height} />);
  },
  state: () => latest,
  unsupported: () => unsupported,
};
