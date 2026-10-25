import { Modal } from "@/components/Modal";
import { SecurityPanel } from "./SecurityPanel";

/** Account security, opened from the account menu. */
export function SecurityDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Account security" description="How you sign in: passkeys, an authenticator app and recovery codes." wide
           footer={<button type="button" className="button" onClick={() => onOpenChange(false)}>Close</button>}>
      {open ? <SecurityPanel /> : null}
    </Modal>
  );
}
