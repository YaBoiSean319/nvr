import { useState } from "react";
import { Modal } from "@/components/Modal";

/** A secret the NVR shows exactly once; it is never stored by this page. */
export function SecretDialog({ secret, title, notice, onClose }: { secret: string | null; title: string; notice: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <Modal open={secret !== null} onOpenChange={open => { if (!open) { setCopied(false); onClose(); } }} title={title} description={notice}
           footer={<button type="button" className="button" data-variant="primary" onClick={() => { setCopied(false); onClose(); }}>I have stored it</button>}>
      <label className="field"><span>Secret</span><input className="mono" readOnly value={secret ?? ""} onFocus={event => event.target.select()} aria-label="Secret value" /></label>
      <div className="field-actions">
        <button type="button" className="button" onClick={() => { void navigator.clipboard?.writeText(secret ?? "").then(() => setCopied(true)).catch(() => setCopied(false)); }}>Copy</button>
        {copied ? <span role="status" className="test-ok">Copied.</span> : null}
      </div>
    </Modal>
  );
}
