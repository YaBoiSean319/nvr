// A page for the dewarping journeys: the real DewarpStage over a fixture picture, with its state shown to the test.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { DewarpStage } from "@/features/dewarp/DewarpStage";
import type { DewarpState, Lens } from "@/features/dewarp/geometry";

declare global {
  interface Window { dewarpHarness: { mount: (options: { lens: Lens; state: DewarpState; width: number; height: number; source?: "image" | "video" }) => Promise<void>; state: () => DewarpState; unsupported: () => boolean } }
}

let latest: DewarpState = { mode: "raw", pan: 0, views: [] };
let unsupported = false;

function Harness({ lens, initial, width, height, source, playing }: { lens: Lens; initial: DewarpState; width: number; height: number; source: HTMLImageElement | HTMLVideoElement; playing: boolean }) {
  const [state, setState] = useState(initial);
  latest = state;
  return (
    <div style={{ position: "relative", width, height }}>
      <DewarpStage getSource={() => source} lens={lens} state={state} label="Dome" playing={playing} onChange={next => { latest = next; setState(next); }} onUnsupported={() => { unsupported = true; }} />
    </div>
  );
}

window.dewarpHarness = {
  mount: async ({ lens, state, width, height, source = "image" }) => {
    latest = state;
    const image = document.getElementById("fixture") as HTMLImageElement;
    let picture: HTMLImageElement | HTMLVideoElement = image;
    if (source === "video") {
      // The fixture picture as a playing video, as a Live tile's is: a canvas redrawn ten times a second and captured as a stream.
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext("2d")!;
      const draw = () => context.drawImage(image, 0, 0);
      draw();
      window.setInterval(draw, 100);
      const video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = canvas.captureStream(10);
      await video.play();
      await new Promise<void>(resolve => (video.videoWidth ? resolve() : video.addEventListener("loadeddata", () => resolve(), { once: true })));
      picture = video;
    }
    createRoot(document.getElementById("root")!).render(<Harness lens={lens} initial={state} width={width} height={height} source={picture} playing={source === "video"} />);
  },
  state: () => latest,
  unsupported: () => unsupported,
};
