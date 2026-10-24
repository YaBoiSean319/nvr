import { Modal } from "@/components/Modal";
import type { Camera } from "@/api/types";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";

/** Playback already shows the maximum: the person decides which camera makes room, or cancels. */
export function FullChooser({ incoming, current, onReplace, onClose }: {
  incoming: Camera | undefined;
  current: Camera[];
  onReplace: (target: string) => void;
  onClose: () => void;
}) {
  return (
    <Modal open={Boolean(incoming)} onOpenChange={open => !open && onClose()} title="Playback is full"
      description={`Playback shows up to ${MAX_DISPLAYED_CAMERAS} cameras. Choose a camera to replace with ${incoming?.name ?? "the new camera"}, or cancel to keep the investigation as it is.`}
      footer={<button type="button" className="button" onClick={onClose}>Cancel</button>}>
      <ul className="choice-list" aria-label="Replace which camera">
        {current.map(camera => (
          <li key={camera.id}>
            <button type="button" className="browser-row" onClick={() => onReplace(camera.id)}>
              <span />
              <span />
              <span className="browser-name">Replace {camera.name}</span>
            </button>
          </li>
        ))}
      </ul>
    </Modal>
  );
}
